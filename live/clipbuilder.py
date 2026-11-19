"""Aus dem Ringpuffer einen fertigen 9:16-Clip bauen (Facecam oben / Gameplay unten, Untertitel, < 50 MB)."""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import subtitles
from .recorder import Recorder

log = logging.getLogger("clip")


def even(v: float) -> int:
    return int(round(v / 2)) * 2


@dataclass
class ClipResult:
    video: Path
    thumb: Path
    cover: Path
    duration: float
    clip_start: float            # Epoch des ersten Clip-Frames
    transcript: str
    layout: str                  # "facecam" oder "vollbild"
    frames: list = field(default_factory=list)   # Stichproben (RGB 1920x1080) fuer die Facecam-Suche


_NVENC: bool | None = None


def _vcodec(prefer: bool, kbps: int) -> list[str]:
    global _NVENC
    if _NVENC is None:
        _NVENC = False
        if prefer:
            r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.3",
                                "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True)
            _NVENC = r.returncode == 0
        log.info("Encoder: %s", "NVENC (GPU)" if _NVENC else "libx264 (CPU)")
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k", "-bufsize", f"{kbps * 2}k"]
    if _NVENC:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", *rate]
    return ["-c:v", "libx264", "-preset", "veryfast", *rate]


def wait_for_buffer(rec: Recorder, until: float, timeout: float) -> bool:
    """Warten, bis ein fertiges Segment bis `until` reicht (segments() liefert nur abgeschlossene)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        segs = rec.segments()
        if segs and segs[-1].end >= until:
            return True
        time.sleep(1)
    return False


def grab_frames(input_args: list[str], duration: float, n: int) -> list[np.ndarray]:
    step = max(duration / n, 0.2)
    r = subprocess.run(["ffmpeg", "-v", "error", *input_args, "-vf", f"fps=1/{step:.3f},scale=1920:1080",
                        "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    size = 1920 * 1080 * 3
    return [np.frombuffer(r.stdout[i:i + size], dtype=np.uint8).reshape(1080, 1920, 3)
            for i in range(0, len(r.stdout) - size + 1, size)]


def layout_filter(C: dict, cam: dict | None, offset_x: int, subs: str) -> tuple[str, int]:
    """filter_complex + MarginV fuer die Untertitel.

    Direkt in Ausgabegroesse (z.B. 720x1280) gebaut, ohne Umweg ueber 1080p. Facecam-Koordinaten sind im
    1920x1080-Raster und werden per iw/ih auf die echte Quellgroesse umgerechnet."""
    ow = even(C.get("width", 720))
    oh = even(ow * 16 / 9)
    k = ow / 1080                                   # Config-Werte (top_h_*) gelten fuer 1080 breit
    out = f"format=yuv420p,subtitles={subs}[v]"
    sx = lambda v: f"{v}*iw/1920"
    sy = lambda v: f"{v}*ih/1080"
    if cam:
        top = even(min(max(1080 * cam["h"] / cam["w"], C["top_h_min"]), C["top_h_max"]) * k)
        bot = oh - top
        gw = min(1920, even(1080 * ow / bot))      # Gameplay-Ausschnitt im 1920er-Raster
        gx = int(min(max(0, (1920 - gw) / 2 + offset_x), 1920 - gw))
        fc = (f"[0:v]setsar=1,split=2[a][b];"
              f"[a]crop={sx(cam['w'])}:{sy(cam['h'])}:{sx(cam['x'])}:{sy(cam['y'])},"
              f"scale={ow}:{top}:force_original_aspect_ratio=increase,crop={ow}:{top}[cam];"
              f"[b]crop={sx(gw)}:ih:{sx(gx)}:0,scale={ow}:{bot}[game];"
              f"[cam][game]vstack=inputs=2,{out}")
        return fc, None
    # Vollbild-Variante (keine Facecam gefunden): Stream mittig, unscharfer Hintergrund
    fh = even(ow * 9 / 16)
    fc = ("[0:v]setsar=1,split=2[a][b];"
          "[a]scale=180:320:force_original_aspect_ratio=increase,crop=180:320,boxblur=6:2,"
          f"eq=brightness=-0.12,scale={ow}:{oh}[bg];"
          f"[b]scale={ow}:{fh}[fg];"
          f"[bg][fg]overlay=0:(H-h)/2,{out}")
    return fc, (1920 - 1080 * 9 // 16) // 2 - 110   # MarginV (ASS-Raster 1080x1920): knapp unter dem Bild


def build(job, rec: Recorder, settings: dict, streamer, choose_facecam, out_root: Path) -> ClipResult:
    C, S = settings["clip"], settings["subtitles"]
    A, B = job.moment - C["pre_s"], job.moment + C["post_s"]
    if not wait_for_buffer(rec, B, timeout=max(15.0, B - time.time()) + rec.seg_s * 3 + 20):
        log.warning("[%s] Segment nach Moment nicht rechtzeitig fertig - nehme was da ist", streamer.login)
    segs = [s for s in rec.segments() if s.end > A and s.start < B]
    # nur ein zusammenhaengender Block (nach einem Reconnect gibt es Luecken): der mit dem Moment drin
    groups: list[list] = []
    for s in segs:
        if groups and s.start - groups[-1][-1].end <= 2:
            groups[-1].append(s)
        else:
            groups.append([s])
    if not groups:
        raise RuntimeError("Kein Puffer fuer diesen Moment vorhanden")
    block = next((g for g in groups if g[0].start <= job.moment <= g[-1].end + 1),
                 max(groups, key=lambda g: g[-1].end - g[0].start))
    A = max(A, block[0].start)
    B = min(B, block[-1].end)
    dur = B - A
    if dur < C["min_s"]:
        raise RuntimeError(f"Nur {dur:.0f}s im Puffer - zu kurz")

    work = out_root / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(job.moment))}_{streamer.login}"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "src.ts"
    with open(src, "wb") as out:        # TS-Segmente mit durchgehenden Zeitstempeln -> einfach aneinanderhaengen
        for s in block:
            with open(s.path, "rb") as f:
                shutil.copyfileobj(f, out)
    off = A - block[0].start
    inp = ["-ss", f"{off:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]

    frames = grab_frames(inp, dur, 8)
    cam = choose_facecam(streamer, frames)
    words = subtitles.transcribe(S, inp) if S.get("enabled", True) else []
    fc, mv = layout_filter(C, cam, streamer.game_offset_x, "subs.ass")
    subtitles.write_ass(S, words, work / "subs.ass", mv if mv is not None else int(S.get("margin_v", 520)))

    video = work / "clip.mp4"
    kbps = int(C["video_bitrate_k"])
    limit = float(C["max_mb"]) * 1024 * 1024
    for attempt in range(3):
        subprocess.run(["ffmpeg", "-y", "-v", "error", *inp, "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0?",
                        *_vcodec(C.get("prefer_nvenc", True), kbps), "-r", str(C.get("fps", 30)),
                        "-g", str(2 * int(C.get("fps", 30))),
                        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart", video.name],
                       cwd=work, check=True, capture_output=True)
        size = video.stat().st_size
        if size <= limit:
            break
        kbps = int(kbps * limit / size * 0.9)
        log.info("[%s] %.1f MB > Limit -> neu mit %d kbit/s", streamer.login, size / 1e6, kbps)

    # Thumbnail = Frame am Trigger-Moment
    t_moment = min(max(0.0, job.moment - A), dur - 0.5)
    cover, thumb = work / "cover.jpg", work / "thumb.jpg"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t_moment:.2f}", "-i", str(video), "-frames:v", "1",
                    "-q:v", "3", str(cover)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(cover), "-vf", "scale=180:320", "-q:v", "5",
                    str(thumb)], check=True, capture_output=True)
    src.unlink(missing_ok=True)
    return ClipResult(video, thumb, cover, dur, A, " ".join(w.word.strip() for w in words),
                      "facecam" if cam else "vollbild", frames)
