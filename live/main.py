"""Echtzeit-Clip-Pipeline: Live-Erkennung -> Ringpuffer -> Trigger (Clips-API + Chat) -> Clip -> Telegram."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import logging.handlers
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import caption, clipbuilder, facecam
from .chat import ChatWatcher
from .config import ROOT, FacecamCache, Streamer, env, load_settings, load_streamers
from .recorder import Recorder
from .telegram import CommandListener, Telegram, esc
from .twitch import Helix, parse_ts

log = logging.getLogger("main")
CLIPS_DIR = ROOT / "clips"


@dataclass
class Job:
    login: str
    moment: float          # Epoch-Zeit des Moments (Clip endet ~post_s danach)
    reason: str
    kind: str              # "chat" | "clips" | "test"
    detected: float
    clip_url: str | None = None
    tracked: "Tracked | None" = None   # Puffer bleibt nutzbar, auch wenn der Streamer danach wegwechselt


@dataclass
class Tracked:
    streamer: Streamer
    user_id: str
    stream: dict
    recorder: Recorder
    since: float
    seen_clips: set = field(default_factory=set)
    seeded: bool = False
    recent_clips: list = field(default_factory=list)   # (created_at, url)
    last_moment: float = 0.0
    vod: dict | None = None
    vod_checked: float = 0.0
    test_fired: bool = False


def setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-9s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(ROOT / "logs" / "live.log", maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    for noisy in ("urllib3", "httpx", "httpx2", "anthropic", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def keep_awake():
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED


def fmt_offset(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m{sec % 60:02d}s"


class App:
    def __init__(self, args):
        self.args = args
        self.S = load_settings()
        streamers = load_streamers()
        if args.only:
            only = {x.strip().lower() for x in args.only.split(",")}
            streamers = [s for s in streamers if s.login in only] or [Streamer(l, l) for l in only]
        self.streamers = {s.login: s for s in streamers}
        self.helix = Helix(env("TWITCH_CLIENT_ID"), env("TWITCH_CLIENT_SECRET"))
        self.tg = Telegram(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"))
        self.cache = FacecamCache()
        self.tracked: dict[str, Tracked] = {}
        self.lock = threading.RLock()
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.sendq: queue.Queue = queue.Queue()
        self.pending_cam: dict[str, dict] = {}      # login -> {box, msg_id, frames}
        self.chat = ChatWatcher(self.S, lambda l, m, r: self.trigger(l, m, r, "chat"))
        self.users: dict[str, dict] = {}
        self.game_id: str | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ Start / Loop

    def run(self):
        keep_awake()
        self.users = self.helix.users(list(self.streamers))
        for login in self.streamers:
            if login not in self.users:
                log.warning("Twitch-Login '%s' nicht gefunden - pruefe streamers.yaml", login)
        gname = self.S["game"]["name"]
        self.game_id = self.helix.game_id(gname)
        log.info("Spiel '%s' -> game_id %s%s", gname, self.game_id,
                 " (ignoriert: --any-game)" if self.args.any_game else "")
        cleanup_old_clips()
        self.chat.start()
        CommandListener(self.tg, self.on_command).start()
        threading.Thread(target=self.worker, name="worker", daemon=True).start()
        threading.Thread(target=self.sender, name="sender", daemon=True).start()
        threading.Thread(target=self._warm_whisper, name="warmup", daemon=True).start()
        try:
            self.tg.send_text(f"🟢 <b>Clipper live</b> gestartet – beobachte {len(self.users)} Streamer"
                              f"{' (alle Spiele, Testmodus)' if self.args.any_game else ''}.")
        except Exception as e:
            log.warning("Telegram-Startnachricht: %s", e)

        P = self.S["poll"]
        next_streams = next_clips = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now >= next_streams:
                    next_streams = now + P["streams_s"]
                    self._safe(self.poll_streams)
                if now >= next_clips:
                    next_clips = now + P["clips_s"]
                    self._safe(self.poll_clips)
                if self.args.test_trigger:
                    self._test_triggers()
                self._stop.wait(1)
        except KeyboardInterrupt:
            log.info("Beende ...")
        finally:
            with self.lock:
                for t in self.tracked.values():
                    t.recorder.stop()
            self.chat.stop()

    def _safe(self, fn):
        try:
            fn()
        except Exception as e:
            log.exception("%s: %s", fn.__name__, e)

    def _warm_whisper(self):
        try:
            from . import subtitles
            subtitles.model(self.S["subtitles"])
        except Exception as e:
            log.warning("Whisper vorladen: %s", e)

    # ------------------------------------------------------------------ Live-Erkennung

    def game_ok(self, s: dict) -> bool:
        if self.args.any_game:
            return True
        if self.game_id and str(s.get("game_id")) == str(self.game_id):
            return True
        return self.S["game"]["name"].lower() in (s.get("game_name") or "").lower()

    def poll_streams(self):
        ids = {u["id"]: login for login, u in self.users.items()}
        live = {s["user_id"]: s for s in self.helix.streams(list(ids))} if ids else {}
        # Bandbreite: hoechstens max_streams gleichzeitig; Kampagnen-Streamer zuerst, dann Reihenfolge in streamers.yaml
        order = sorted(ids.items(), key=lambda kv: (not self.streamers[kv[1]].campaign,
                                                     list(self.streamers).index(kv[1])))
        eligible = [login for uid, login in order if live.get(uid) and self.game_ok(live[uid])]
        limit = int(self.S["buffer"].get("max_streams") or 0)
        allowed = set(eligible[:limit] if limit else eligible)
        with self.lock:
            for uid, login in order:
                s = live.get(uid)
                ok = login in allowed
                tr = self.tracked.get(login)
                if ok and not tr:
                    self.start_tracking(login, uid, s)
                elif ok and tr:
                    if tr.stream.get("id") != s.get("id"):
                        tr.vod, tr.vod_checked = None, 0.0
                    tr.stream = s
                elif tr and not ok:
                    why = ("offline" if not s else f"spielt jetzt '{s.get('game_name')}'" if not self.game_ok(s)
                           else "Limit buffer.max_streams - Streamer mit hoeherer Prioritaet ist live")
                    self.stop_tracking(login, why)
                if login in self.tracked:
                    self._update_vod(self.tracked[login])
            self.chat.set_channels(set(self.tracked))

    def start_tracking(self, login: str, uid: str, s: dict):
        rec = Recorder(login, self.S)
        rec.start()
        self.tracked[login] = Tracked(self.streamers[login], uid, s, rec, time.time())
        log.info("[%s] LIVE mit '%s' (%s Zuschauer) -> tracke", login, s.get("game_name"), s.get("viewer_count"))

    def stop_tracking(self, login: str, why: str):
        tr = self.tracked.pop(login)
        tr.recorder.stop()
        log.info("[%s] nicht mehr getrackt (%s)", login, why)

    def _update_vod(self, tr: Tracked):
        if tr.vod or time.time() - tr.vod_checked < 300:
            return
        tr.vod_checked = time.time()
        try:
            tr.vod = self.helix.current_vod(tr.user_id, tr.stream["id"])
        except Exception as e:
            log.debug("VOD: %s", e)

    # ------------------------------------------------------------------ Trigger: Clips-API

    def poll_clips(self):
        T = self.S["trigger"]["clips"]
        with self.lock:
            items = list(self.tracked.items())
        for login, tr in items:
            try:
                clips = self.helix.clips(tr.user_id, time.time() - T["lookback_s"])
            except Exception as e:
                log.warning("[%s] Get Clips: %s", login, e)
                continue
            new = [c for c in clips if c["id"] not in tr.seen_clips]
            tr.seen_clips.update(c["id"] for c in clips)
            if not tr.seeded:          # beim Start vorhandene Clips nur merken
                tr.seeded = True
                continue
            now = time.time()
            tr.recent_clips += [(parse_ts(c["created_at"]), c["url"]) for c in new]
            tr.recent_clips = [x for x in tr.recent_clips if x[0] >= now - T["window_s"]]
            if len(tr.recent_clips) >= T["min_clips"]:
                first = min(tr.recent_clips)
                n = len(tr.recent_clips)
                tr.recent_clips = []
                self.trigger(login, first[0], f"{n} neue Twitch-Clips in {T['window_s'] // 60} Min", "clips",
                             clip_url=first[1])

    def _test_triggers(self):
        with self.lock:
            for login, tr in self.tracked.items():
                if not tr.test_fired and time.time() - tr.since >= self.args.test_trigger:
                    tr.test_fired = True
                    threading.Thread(target=self.trigger, args=(login, time.time(), "Test-Trigger", "test"),
                                     daemon=True).start()

    # ------------------------------------------------------------------ Trigger -> Job

    def trigger(self, login: str, moment: float, reason: str, kind: str, clip_url: str | None = None):
        cd = self.S["trigger"]["cooldown_s"]
        with self.lock:
            tr = self.tracked.get(login)
            if not tr:
                return
            segs = tr.recorder.segments()
            if not segs or moment < segs[0].start + 5:
                log.info("[%s] Trigger '%s' ignoriert: Moment %s liegt vor dem Puffer", login, reason,
                         time.strftime("%H:%M:%S", time.localtime(moment)))
                return
            if tr.last_moment and abs(moment - tr.last_moment) < cd:
                log.info("[%s] Trigger '%s' ignoriert (Cooldown, gleicher Moment wie vor %.0fs)",
                         login, reason, moment - tr.last_moment)
                return
            tr.last_moment = moment
        log.info("[%s] TRIGGER %s: %s (Moment %s)", login, kind, reason, time.strftime("%H:%M:%S",
                                                                                     time.localtime(moment)))
        self.jobs.put(Job(login, moment, reason, kind, time.time(), clip_url, tr))

    # ------------------------------------------------------------------ Clip bauen + senden

    def worker(self):
        while True:
            job = self.jobs.get()
            try:
                self.process(job)
            except Exception as e:
                log.exception("[%s] Clip fehlgeschlagen: %s", job.login, e)
                try:
                    self.tg.send_text(f"⚠️ Clip für <b>{esc(job.login)}</b> fehlgeschlagen: {esc(str(e)[:300])}")
                except Exception:
                    pass

    def process(self, job: Job):
        tr = job.tracked
        st = tr.streamer
        res = clipbuilder.build(job, tr.recorder, self.S, st, self.choose_facecam, CLIPS_DIR)
        hook = caption.ai_hook(self.S, st, res.transcript, job.reason)
        cap, from_ai = caption.build_caption(self.S, st, hook)
        (res.video.parent / "caption.txt").write_text(cap, encoding="utf-8")

        links = []
        if tr.vod:
            off = res.clip_start - parse_ts(tr.vod["created_at"])
            links.append(f'<a href="{tr.vod["url"]}?t={fmt_offset(off)}">VOD bei {fmt_offset(off)}</a>')
        if job.clip_url:
            links.append(f'<a href="{job.clip_url}">Twitch-Clip</a>')
        if not links:
            links.append(f'<a href="https://www.twitch.tv/{st.login}">twitch.tv/{st.login}</a> (kein VOD)')
        if res.layout != "facecam":
            layout = "Vollbild (keine Facecam gefunden – mit /box setzen)"
        elif st.login in self.pending_cam and not st.facecam:
            layout = "Facecam oben / Gameplay unten (Box noch nicht bestätigt: /ok oder /redo)"
        else:
            layout = "Facecam oben / Gameplay unten"
        said = esc(res.transcript[:160] + ("…" if len(res.transcript) > 160 else "")) or "–"
        size_mb = res.video.stat().st_size / 1e6
        text = (
            f"🎬 <b>{esc(st.caption_name)}</b> · {esc(tr.stream.get('game_name') or '')}"
            f"{' · Kampagne' if st.campaign else ''}\n"
            f"⚡ {esc(job.reason)}\n"
            f"🕒 {time.strftime('%H:%M:%S', time.localtime(job.moment))} · fertig nach "
            f"{time.time() - job.detected:.0f}s · {res.duration:.0f}s · {size_mb:.1f} MB\n"
            f"▶️ {' · '.join(links)}\n"
            f"🖼 {layout}\n"
            f"🗣 {said}\n\n"
            f"📋 Caption{'' if from_ai else ' (Vorlage)'}:\n<pre>{esc(cap)}</pre>"
        )
        log.info("[%s] Clip fertig nach %.0fs: %s (%.1f MB) -> Upload", st.login, time.time() - job.detected,
                 res.video, size_mb)
        self.sendq.put((job, res, text, size_mb))

    def sender(self):
        """Uploads laufen getrennt vom Clip-Bau, damit der naechste Clip nicht auf eine langsame Leitung wartet."""
        while True:
            job, res, text, size_mb = self.sendq.get()
            t0 = time.time()
            try:
                w = int(self.S["clip"].get("width", 720)) // 2 * 2
                self.tg.send_video(res.video, text, res.thumb, res.cover, w, w * 16 // 9 // 2 * 2, res.duration)
                log.info("[%s] Clip gesendet: %.1f MB, Upload %.0fs, %.0fs nach Trigger", job.login, size_mb,
                         time.time() - t0, time.time() - job.detected)
            except Exception as e:
                log.exception("[%s] Upload fehlgeschlagen: %s", job.login, e)
                try:
                    self.tg.send_text(f"⚠️ Upload für <b>{esc(job.login)}</b> fehlgeschlagen ({esc(str(e)[:200])}). "
                                      f"Datei liegt unter {esc(str(res.video))}")
                except Exception:
                    pass

    # ------------------------------------------------------------------ Facecam

    def choose_facecam(self, st: Streamer, frames) -> dict | None:
        if st.facecam:
            return st.facecam
        box = self.cache.get(st.login)
        if box:
            return box
        # Vorschlag sofort nutzen (Hochformat); /ok speichert ihn dauerhaft, /redo sucht neu
        if st.login not in self.pending_cam and frames:
            self._propose_facecam(st.login, frames)
        p = self.pending_cam.get(st.login)
        return p["box"] if p else None

    def _propose_facecam(self, login: str, frames) -> bool:
        box = facecam.detect(frames, self.cache.rejected(login))
        if not box:
            self.pending_cam.pop(login, None)
            self.tg.send_text(f"🙈 Keine Facecam bei <b>{esc(login)}</b> gefunden. Clips kommen im Vollbild-Layout.\n"
                              f"Manuell setzen: in streamers.yaml <code>facecam: {{x: .., y: .., w: .., h: ..}}</code> "
                              f"oder hier <code>/box {esc(login)} x y w h</code> (1920×1080-Raster).")
            return False
        prev = ROOT / "clips" / f"facecam_{login}.jpg"
        prev.parent.mkdir(exist_ok=True)
        facecam.draw_preview(frames[len(frames) // 2], box, prev, f"Facecam? {login}")
        entry = {"box": box, "msg_id": None, "frames": frames}
        self.pending_cam[login] = entry
        caption_text = (
            f"🎥 Ist das die Facecam von <b>{esc(login)}</b>?\n"
            f"Box: x={box['x']} y={box['y']} w={box['w']} h={box['h']}\n"
            f"Die Clips nutzen diese Box schon. <b>/ok</b> = dauerhaft speichern, <b>/redo</b> = neu suchen "
            f"(am besten als Reply auf dieses Bild).\n"
            f"Oder selbst: <code>/box {esc(login)} x y w h</code>")

        def send():  # im Hintergrund, damit der Clip-Bau nicht auf den Upload wartet
            try:
                entry["msg_id"] = self.tg.send_photo(prev, caption_text).get("message_id")
            except Exception as e:
                log.warning("[%s] Facecam-Bild senden: %s", login, e)
        threading.Thread(target=send, name=f"fc-{login}", daemon=True).start()
        return True

    def _fresh_frames(self, login: str):
        with self.lock:
            tr = self.tracked.get(login)
        if not tr:
            return None
        segs = tr.recorder.segments()[-7:-1]
        if not segs:
            return None
        tmp = CLIPS_DIR / f"_frames_{login}.ts"
        with open(tmp, "wb") as out:
            for s in segs:
                out.write(s.path.read_bytes())
        try:
            return clipbuilder.grab_frames(["-i", str(tmp)], segs[-1].end - segs[0].start, 8)
        finally:
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------ Telegram-Befehle

    def on_command(self, text: str, msg: dict):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if cmd == "/status":
            return self._status()
        if cmd not in ("/ok", "/redo", "/box"):
            return
        login = None
        reply_id = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_id:
            login = next((l for l, p in self.pending_cam.items() if p["msg_id"] == reply_id), None)
        if not login and args and args[0].lower() in self.streamers:
            login = args.pop(0).lower()
        if not login and len(self.pending_cam) == 1:
            login = next(iter(self.pending_cam))
        if not login:
            self.tg.send_text("Welcher Streamer? Als Reply auf das Facecam-Bild antworten oder "
                              "<code>/ok lacy</code> schreiben.")
            return
        if cmd == "/box":
            try:
                x, y, w, h = (int(v) for v in args[:4])
            except ValueError:
                self.tg.send_text("Format: <code>/box login x y w h</code>")
                return
            self.cache.confirm(login, {"x": x, "y": y, "w": w, "h": h})
            self.pending_cam.pop(login, None)
            self.tg.send_text(f"✅ Facecam für <b>{esc(login)}</b> gespeichert: x={x} y={y} w={w} h={h}")
            return
        p = self.pending_cam.get(login)
        if not p:
            self.tg.send_text(f"Für {esc(login)} wartet gerade keine Facecam auf Bestätigung.")
            return
        if cmd == "/ok":
            self.cache.confirm(login, p["box"])
            self.pending_cam.pop(login, None)
            self.tg.send_text(f"✅ Facecam für <b>{esc(login)}</b> dauerhaft gespeichert.")
            log.info("[%s] Facecam bestaetigt: %s", login, p["box"])
        else:  # /redo
            self.cache.reject(login, p["box"])
            frames = self._fresh_frames(login) or p["frames"]
            self.tg.send_text(f"🔄 Suche neu für <b>{esc(login)}</b> …")
            self._propose_facecam(login, frames)

    def _status(self):
        chat = self.chat.snapshot()
        with self.lock:
            rows = [f"• <b>{esc(l)}</b>: {esc(t.stream.get('game_name') or '')}, Puffer "
                    f"{t.recorder.buffered_seconds():.0f}s · {esc(chat.get(l, 'Chat verbindet …'))}"
                    for l, t in self.tracked.items()]
        self.tg.send_text("📊 <b>Status</b>\n" + ("\n".join(rows) or "Niemand live (mit passendem Spiel).")
                          + f"\nWarteschlange: {self.jobs.qsize()}")


def cleanup_old_clips(days: float = 2):
    if not CLIPS_DIR.exists():
        return
    cutoff = time.time() - days * 86400
    for d in CLIPS_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------------- CLI

def cmd_check(args):
    """Konfiguration pruefen und zeigen, wer gerade live ist (ohne aufzunehmen)."""
    S = load_settings()
    streamers = load_streamers()
    for tool in ("ffmpeg",):
        print(f"{tool}: {'OK' if shutil.which(tool) else 'FEHLT'}")
    r = subprocess.run([sys.executable, "-m", "streamlink", "--version"], capture_output=True, text=True)
    print("streamlink:", (r.stdout or r.stderr).strip())
    for k in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "ANTHROPIC_API_KEY"):
        print(f"{k}: {'gesetzt' if env(k, required=False) else 'FEHLT'}")
    if not (env("TWITCH_CLIENT_ID", False) and env("TWITCH_CLIENT_SECRET", False)):
        return
    h = Helix(env("TWITCH_CLIENT_ID"), env("TWITCH_CLIENT_SECRET"))
    gid = h.game_id(S["game"]["name"])
    print(f"Spiel '{S['game']['name']}' -> game_id {gid}")
    users = h.users([s.login for s in streamers])
    live = {s["user_id"]: s for s in h.streams([u["id"] for u in users.values()])} if users else {}
    for st in streamers:
        u = users.get(st.login)
        if not u:
            print(f"  {st.login:16s} NICHT GEFUNDEN")
            continue
        s = live.get(u["id"])
        if s:
            match = (gid and s["game_id"] == gid) or S["game"]["name"].lower() in s["game_name"].lower()
            print(f"  {st.login:16s} LIVE  {s['game_name']!r} ({s['viewer_count']} Zuschauer)"
                  f"{'  <- wird getrackt' if match else ''}")
        else:
            print(f"  {st.login:16s} offline")
    if args.top:
        print("\nTop-Streams gerade (zum Testen mit --only <login> --any-game):")
        for s in h.get("streams", {"first": 10, "language": "en"})["data"]:
            print(f"  {s['user_login']:20s} {s['viewer_count']:>7}  {s['game_name']}")


def main():
    ap = argparse.ArgumentParser(prog="python -m live")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "check"])
    ap.add_argument("--any-game", action="store_true", help="Spiel nicht pruefen (Test)")
    ap.add_argument("--only", help="nur diese Logins (Komma-getrennt), auch welche ausserhalb streamers.yaml")
    ap.add_argument("--test-trigger", type=int, default=0, metavar="SEK",
                    help="Test: pro Streamer einmal einen Trigger SEK Sekunden nach Aufnahmestart ausloesen")
    ap.add_argument("--top", action="store_true", help="check: aktuelle Top-Streams anzeigen")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if args.cmd == "check":
        return cmd_check(args)
    setup_logging()
    log.info("Start %s", " ".join(sys.argv[1:]))
    App(args).run()


if __name__ == "__main__":
    main()
