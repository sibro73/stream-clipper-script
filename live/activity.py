"""Ist der Streamer gerade aktiv - oder laeuft ein Schlaf-/AFK-Stream?

Gemessen wird am Ton: Anteil Sprache (Silero-VAD aus faster-whisper) und Pegel. Messung 2026-11-19:
aktive GTA-Streams 19-62 % Sprache bei -13 bis -31 dBFS, lacys Schlaf-Stream 0 % bei -50 dBFS.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("activity")
SR = 16000


@dataclass
class Activity:
    speech: float        # Anteil Sprache 0..1
    db: float            # Median-Pegel in dBFS
    seconds: float

    def __str__(self):
        return f"{self.speech * 100:.0f}% Sprache, {self.db:.0f} dBFS"


def title_says_sleep(title: str, A: dict) -> bool:
    words = A.get("sleep_words", [])
    if not words:
        return False
    alts = "|".join(w if w.endswith("+") else re.escape(w) for w in words)      # "zzz+" = Regex
    return re.search(r"(?<!no )(?<!not )(?<!don't )\b(" + alts + r")\b", title or "", re.I) is not None


def _measure_pcm(pcm: bytes) -> Activity | None:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    if len(a) < SR * 10:
        return None
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    sec = a[: len(a) // SR * SR].reshape(-1, SR)
    db = float(np.median(20 * np.log10(np.sqrt((sec ** 2).mean(axis=1)) + 1e-9)))
    ts = get_speech_timestamps(a, VadOptions(min_silence_duration_ms=500))
    speech = sum(x["end"] - x["start"] for x in ts) / len(a)
    return Activity(speech, db, len(a) / SR)


def measure_buffer(rec, seconds: float) -> Activity | None:
    """Die letzten `seconds` aus dem Ringpuffer messen (None = noch zu wenig Puffer)."""
    segs = rec.segments()
    if not segs or segs[-1].end - segs[0].start < seconds * 0.9:
        return None
    use = [s for s in segs if s.start >= segs[-1].end - seconds]
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as f:
        tmp = Path(f.name)
        for s in use:
            with open(s.path, "rb") as src:
                shutil.copyfileobj(src, f)
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(tmp), "-map", "0:a:0", "-ac", "1", "-ar", str(SR),
                            "-f", "s16le", "-"], capture_output=True, timeout=60)
        return _measure_pcm(r.stdout)
    finally:
        tmp.unlink(missing_ok=True)


def probe(login: str, seconds: float) -> Activity | None:
    """Kurz in den Audio-Stream reinhoeren (audio_only, ~160 kbit/s) - braucht keinen Aufnahme-Platz."""
    sl = subprocess.Popen([sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "error",
                           f"https://www.twitch.tv/{login}", "audio_only,worst"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-t", str(seconds), "-map", "0:a:0", "-ac", "1",
                            "-ar", str(SR), "-f", "s16le", "-"], stdin=sl.stdout, capture_output=True,
                           timeout=seconds + 60)
        return _measure_pcm(r.stdout)
    except Exception as e:
        log.warning("[%s] Aktivitaets-Probe fehlgeschlagen: %s", login, e)
        return None
    finally:
        sl.kill()


def is_idle(a: Activity, A: dict) -> bool:
    if a.speech < A["speech_min"]:
        return True
    return a.speech < A["speech_low"] and a.db < A["quiet_db"]
