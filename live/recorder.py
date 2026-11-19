"""Ringpuffer pro Streamer: streamlink (720p) -> ffmpeg -> 10-s-TS-Segmente.

Zeitzuordnung: Nicht die Dateizeit, sondern die echten Stream-Zeitstempel (PTS, mit -copyts unveraendert)
bestimmen, welcher Moment in welchem Segment liegt. Verankert an der Uhr ueber
  offset = min(Schreibzeit - PTS-Ende)  der letzten ~10 Min  (= Lieferung ohne Verzoegerung).
So stimmen die Zeiten auch nach Netz-Aussetzern, uebersprungenen Segmenten und Aufhol-Schueben.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT

log = logging.getLogger("recorder")


@dataclass
class Segment:
    start: float      # Uhrzeit (Epoch) des ersten Frames
    end: float
    path: Path


class Recorder(threading.Thread):
    def __init__(self, login: str, settings: dict):
        super().__init__(name=f"rec-{login}", daemon=True)
        self.login = login
        B = settings["buffer"]
        self.keep_s = float(B["minutes"]) * 60
        self.seg_s = int(B["segment_s"])
        self.quality = B["quality"]
        self.dir = ROOT / "buffer" / login
        self.dir.mkdir(parents=True, exist_ok=True)
        for f in self.dir.glob("*.ts"):           # alter Puffer ist nach Neustart wertlos
            f.unlink(missing_ok=True)
        self._halt = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()
        self._meta: dict[Path, tuple[str, float, float]] = {}   # path -> (session, pts_start, dauer)
        self._offs: dict[str, deque] = {}                       # session -> Stichproben (mtime - pts_end)
        self._session = ""
        self.started_at = time.time()

    # ------------------------------------------------------------------ Ablauf

    def run(self):
        backoff = 5
        while not self._halt.is_set():
            t0 = time.time()
            try:
                self._record_once()
            except Exception as e:
                log.exception("[%s] Aufnahme-Fehler: %s", self.login, e)
            if self._halt.is_set():
                break
            backoff = 5 if time.time() - t0 > 120 else min(backoff * 2, 60)
            log.warning("[%s] Aufnahme beendet -> Neustart in %ss", self.login, backoff)
            self._halt.wait(backoff)

    def _record_once(self):
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        err = open(logs / f"rec_{self.login}.log", "ab")
        self._session = str(int(time.time()))
        sl = subprocess.Popen(
            [sys.executable, "-m", "streamlink", "--stdout", "--retry-open", "3", "--hls-live-edge", "3",
             "--loglevel", "warning", f"https://www.twitch.tv/{self.login}", self.quality],
            stdout=subprocess.PIPE, stderr=err)
        ff = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-copyts",
             "-f", "segment", "-segment_time", str(self.seg_s), "-segment_format", "mpegts",
             str(self.dir / f"{self._session}_%06d.ts")],
            stdin=sl.stdout, stderr=err)
        sl.stdout.close()
        self._procs = [sl, ff]
        log.info("[%s] Aufnahme gestartet (%s)", self.login, self.quality)
        last_new = time.time()
        while not self._halt.is_set():
            if ff.poll() is not None or sl.poll() is not None:
                break
            self._index(final=False)
            self._cleanup()
            last_new = max(last_new, self._newest_mtime())
            if time.time() - last_new > max(60, self.seg_s * 6):
                log.warning("[%s] seit %ds keine neuen Daten -> Neustart", self.login, int(time.time() - last_new))
                break
            self._halt.wait(2)
        self._kill()
        self._index(final=True)      # letztes Segment der Sitzung ist jetzt auch fertig
        err.close()

    # ------------------------------------------------------------------ Index (PTS -> Uhrzeit)

    def _probe(self, f: Path) -> tuple[float, float] | None:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time,duration",
                            "-of", "csv=p=0", str(f)], capture_output=True, text=True)
        try:
            start, dur = (float(x) for x in r.stdout.strip().split(",")[:2])
            return start, dur
        except ValueError:
            return None

    def _index(self, final: bool):
        files = sorted(self.dir.glob(f"{self._session}_*.ts"))
        done = files if final else files[:-1]          # das neueste Segment wird noch geschrieben
        for f in done:
            if f in self._meta:
                continue
            p = self._probe(f)
            if not p or p[1] <= 0:
                continue
            start, dur = p
            sample = f.stat().st_mtime - (start + dur)
            with self._lock:
                q = self._offs.setdefault(self._session, deque(maxlen=60))
                if q and sample < min(q) - 600:      # PTS-Ueberlauf / neuer Stream -> neu verankern
                    q.clear()
                q.append(sample)
                self._meta[f] = (self._session, start, dur)

    def _newest_mtime(self) -> float:
        return max((f.stat().st_mtime for f in self.dir.glob("*.ts")), default=0.0)

    def _cleanup(self):
        cutoff = time.time() - self.keep_s
        for f in list(self.dir.glob("*.ts")):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    with self._lock:
                        self._meta.pop(f, None)
            except OSError:
                pass

    def _kill(self):
        for p in self._procs:
            if p.poll() is None:
                p.kill()
        for p in self._procs:
            try:
                p.wait(timeout=10)
            except Exception:
                pass
        self._procs = []

    def stop(self):
        self._halt.set()
        self._kill()

    # ------------------------------------------------------------------ Puffer lesen

    def segments(self) -> list[Segment]:
        """Fertige Segmente mit Uhrzeit, chronologisch."""
        with self._lock:
            out = []
            for f, (sess, pts, dur) in self._meta.items():
                q = self._offs.get(sess)
                if not q or not f.exists():
                    continue
                start = pts + min(q)
                out.append(Segment(start, start + dur, f))
        out.sort(key=lambda s: s.start)
        return out

    def buffered_seconds(self) -> float:
        return sum(s.end - s.start for s in self.segments())
