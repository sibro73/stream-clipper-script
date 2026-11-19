#!/usr/bin/env python3
"""
Twitch-VOD -> TikTok-Clips Pipeline
Gebaut fuer die SkiiMaskTimmy-Kampagne (Whop / ClippedIn), per config.toml anpassbar.

Befehle:
  python clipper.py list                      Neueste VODs anzeigen
  python clipper.py frame  [--vod ID]         Standbild mit Raster (fuer Facecam-Koordinaten)
  python clipper.py layout [--image PFAD]     9:16-Vorschau mit aktueller config pruefen
  python clipper.py run    [--vod ID]         Neuestes (noch nicht verarbeitetes) VOD clippen
  python clipper.py run --file video.mp4      Lokale Datei clippen (Test / eigenes Gameplay)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
PY = sys.executable
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

CHAT_KEYWORDS = {
    "LUL", "LOL", "LMAO", "LMFAO", "KEKW", "OMEGALUL", "ICANT", "W", "WW", "CLIP", "CLIPIT",
    "WTF", "NAH", "HOLY", "POG", "POGGERS", "POGCHAMP", "PAUSE", "BRO", "GG", "INSANE",
    "💀", "😭", "😂",
}

CAPTION_HOOKS = [
    "the reaction says it all",
    "chat lost it at this one",
    "I can't stop rewatching this",
    "bro was NOT ready for that",
    "this is why you catch him live",
    "nobody saw that coming",
    "the timing on this",
    "you had to be there (now you are)",
]


# ----------------------------------------------------------------------------- helpers

def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def rel(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def run(cmd, cwd=None, check=True, capture=False):
    cmd = [str(c) for c in cmd]
    shown = " ".join(cmd)
    print("  $", shown if len(shown) < 220 else shown[:220] + " ...")
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=capture,
                          encoding="utf-8", errors="replace")


def require(tool: str):
    if shutil.which(tool) is None:
        sys.exit(f"FEHLER: '{tool}' wurde nicht gefunden. Siehe README -> Installation.")


def hms(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def even(x: float) -> int:
    return int(round(x / 2.0)) * 2


def vod_url(vod_id: str) -> str:
    return f"https://www.twitch.tv/videos/{vod_id}"


def find_download(folder: Path, stem: str) -> Path:
    files = [f for f in folder.glob(stem + ".*") if not f.name.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError(f"Download '{stem}' nicht gefunden in {folder}")
    return max(files, key=lambda f: f.stat().st_size)


# ----------------------------------------------------------------------------- state

STATE_FILE = ROOT / "state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"done": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------- twitch

def list_vods(channel: str, limit: int = 10) -> list[dict]:
    url = f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"
    r = run([PY, "-m", "yt_dlp", "--flat-playlist", "-J", "--playlist-end", limit, url], capture=True)
    data = json.loads(r.stdout)
    vods = []
    for e in data.get("entries") or []:
        vid = str(e.get("id", "")).lstrip("v")
        if vid.isdigit():
            vods.append({"id": vid, "title": e.get("title") or "", "duration": e.get("duration")})
    return vods


def download_audio(vod_id: str, workdir: Path) -> Path:
    try:
        return find_download(workdir, "audio")
    except RuntimeError:
        pass
    print("-> Lade Audio des VODs (fuer die Highlight-Erkennung) ...")
    run([PY, "-m", "yt_dlp", "-f", "bestaudio/worst", "-o", workdir / "audio.%(ext)s", vod_url(vod_id)])
    return find_download(workdir, "audio")


def download_chat(cfg: dict, vod_id: str, workdir: Path) -> Path | None:
    out = workdir / "chat.json"
    if out.exists():
        return out
    exe = cfg.get("tools", {}).get("twitch_downloader", "")
    exe_path = rel(exe) if exe else None
    if not exe_path or (not exe_path.exists() and shutil.which(exe) is None):
        print("   (TwitchDownloaderCLI nicht gefunden -> Erkennung nur ueber Audio. Siehe README.)")
        return None
    print("-> Lade Chat-Replay ...")
    r = run([exe_path if exe_path.exists() else exe, "chatdownload", "--id", vod_id, "-o", out], check=False)
    return out if r.returncode == 0 and out.exists() else None


def download_section(vod_id: str, start: float, end: float, workdir: Path, name: str) -> Path:
    for f in workdir.glob(name + ".*"):
        f.unlink()
    run([PY, "-m", "yt_dlp", "-f", "best[height<=1080]/best",
         "--download-sections", f"*{hms(start)}-{hms(end)}",
         "-o", workdir / f"{name}.%(ext)s", vod_url(vod_id)])
    return find_download(workdir, name)


# ----------------------------------------------------------------------------- analysis

def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def loudness_per_second(media: Path, sr: int = 8000) -> np.ndarray:
    print("-> Analysiere Lautstaerke ...")
    cmd = ["ffmpeg", "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    vals = []
    while True:
        b = _read_exact(p.stdout, sr * 2)
        if len(b) < 2:
            break
        a = np.frombuffer(b[: len(b) // 2 * 2], dtype=np.int16).astype(np.float32)
        rms = math.sqrt(float(np.mean(a * a))) + 1e-3
        vals.append(20 * math.log10(rms / 32768.0))
    p.wait()
    return np.array(vals, dtype=np.float32)


def chat_per_second(chat_path: Path, n: int) -> np.ndarray:
    data = json.loads(chat_path.read_text(encoding="utf-8"))
    counts = np.zeros(n, dtype=np.float32)
    for c in data.get("comments", []):
        t = int(c.get("content_offset_seconds", 0))
        if 0 <= t < n:
            body = ((c.get("message") or {}).get("body") or "").upper()
            tokens = set(re.findall(r"[A-Z0-9]+|[^\sA-Za-z0-9]", body))
            counts[t] += 1.0 + (1.5 if tokens & CHAT_KEYWORDS else 0.0)
    return counts


def smooth(x: np.ndarray, w: int) -> np.ndarray:
    w = max(1, min(w, len(x)))
    return np.convolve(x, np.ones(w) / w, mode="same")


def zscore(x: np.ndarray) -> np.ndarray:
    s = float(x.std())
    return (x - float(x.mean())) / (s if s > 1e-9 else 1.0)


def find_highlights(loud: np.ndarray, chat: np.ndarray | None, d: dict, limit: int) -> list[tuple[int, float]]:
    """Bis zu `limit` Kandidaten, bester Score zuerst."""
    n = len(loud)
    # Lautstaerke relativ zum lokalen Durchschnitt: Ausraster, Schreie, Explosionen
    audio = zscore(smooth(loud, 3) - smooth(loud, 120))
    score = d["weight_audio"] * audio
    if chat is not None and chat.sum() > 0:
        delay = int(d["chat_delay"])
        c = smooth(chat, 8)
        c = np.concatenate([c[delay:], np.zeros(delay)])  # Chat reagiert verzoegert
        score = score + d["weight_chat"] * zscore(c - smooth(c, 300))
    score[: int(d["skip_start"])] = -99
    score[max(0, n - 30):] = -99

    picks: list[tuple[int, float]] = []
    for t in np.argsort(-score):
        if score[t] < d["min_score"] or len(picks) >= limit:
            break
        if all(abs(int(t) - p) >= d["min_gap"] for p, _ in picks):
            picks.append((int(t), float(score[t])))
    return picks


# ----------------------------------------------------------------------------- video

def layout_filter(cfg: dict, inp: str, out: str, fc: dict | None = None) -> str:
    L = cfg["layout"]
    top_h = even(L["top_h"])
    bot_h = 1920 - top_h
    fc = fc or L["facecam"]
    gw = min(1920, even(1080 * 1080 / bot_h))
    gx = int(min(max(0, (1920 - gw) / 2 + L.get("game_offset_x", 0)), 1920 - gw))
    return (
        f"[{inp}]scale=1920:1080,setsar=1,split=2[lf_a][lf_b];"
        f"[lf_a]crop={fc['w']}:{fc['h']}:{fc['x']}:{fc['y']},"
        f"scale=1080:{top_h}:force_original_aspect_ratio=increase,crop=1080:{top_h}[lf_cam];"
        f"[lf_b]crop={gw}:1080:{gx}:0,scale=1080:{bot_h}[lf_game];"
        f"[lf_cam][lf_game]vstack=inputs=2,format=yuv420p[{out}]"
    )


def grab_frames_evenly(input_args: list, duration: float, n: int) -> list[np.ndarray]:
    """n Standbilder gleichmaessig ueber den Clip verteilt, in einem einzigen ffmpeg-Durchlauf."""
    step = max(duration / n, 0.1)
    r = subprocess.run(["ffmpeg", "-v", "error", *input_args, "-ss", f"{step / 2:.3f}",
                        "-vf", f"fps=1/{step:.3f},scale=1920:1080", "-frames:v", str(n),
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    size = 1920 * 1080 * 3
    return [np.frombuffer(r.stdout[i:i + size], dtype=np.uint8).reshape(1080, 1920, 3)
            for i in range(0, len(r.stdout) - size + 1, size)]


def _ring(img: np.ndarray, x: int, y: int, w: int, h: int, b: int = 14) -> np.ndarray:
    """Die Randstreifen eines Rechtecks (dort liegt der Facecam-Rahmen) als flacher Vektor."""
    parts = [img[y:y + b, x:x + w], img[y + h - b:y + h, x:x + w],
             img[y + b:y + h - b, x:x + b], img[y + b:y + h - b, x + w - b:x + w]]
    return np.concatenate([p.reshape(-1) for p in parts]).astype(np.float32)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a - a.mean(), b - b.mean()
    return float((a * b).sum() / (math.sqrt(float((a * a).sum()) * float((b * b).sum())) + 1e-9))


_TEMPLATES: dict = {}


def _template(cfg: dict, w: int, h: int):
    """Rahmen-Vorlage (Randstreifen) + Maske der leuchtenden Rahmenlinien, gecacht pro Groesse."""
    if (w, h) not in _TEMPLATES:
        from PIL import Image
        tpl = np.asarray(Image.open(rel(cfg["layout"]["frame_template"])).convert("RGB").resize((w, h)))
        tpl = tpl.astype(np.float32)
        b = 14
        lines = np.zeros((h, w), dtype=bool)
        lines[:b, :] = lines[-b:, :] = lines[:, :b] = lines[:, -b:] = True
        lines &= (tpl.max(axis=2) - tpl.min(axis=2) > 60) & (tpl[:, :, 2] > 120)
        _TEMPLATES[(w, h)] = (_ring(tpl, 0, 0, w, h), lines, tpl[lines])
    return _TEMPLATES[(w, h)]


def frame_match(cfg: dict, img: np.ndarray, fc: dict, shift: int = 3) -> bool:
    """Ist der Facecam-Rahmen an Position fc zu sehen? (+-shift px toleriert)
    Zwei Tests, einer muss klar anschlagen: Muster-Korrelation des ganzen Rands (robust bei bunten Spielen)
    oder Anteil passender Rahmenlinien (robust, wenn der halbdurchsichtige Rahmen vor wechselndem Hintergrund liegt)."""
    L = cfg["layout"]
    w, h = fc["w"], fc["h"]
    ref, lines, line_px = _template(cfg, w, h)
    best_ncc, best_lines = -1.0, 0.0
    for dx in range(-shift, shift + 1):
        for dy in range(-shift, shift + 1):
            x, y = fc["x"] + dx, fc["y"] + dy
            if x < 0 or y < 0 or x + w > 1920 or y + h > 1080:
                continue
            best_ncc = max(best_ncc, _ncc(_ring(img, x, y, w, h), ref))
            cut = img[y:y + h, x:x + w][lines].astype(np.float32)
            best_lines = max(best_lines, float((np.sqrt(((cut - line_px) ** 2).sum(axis=1)) < 70).mean()))
    return best_ncc >= L.get("frame_min_match", 0.55) or best_lines >= L.get("frame_min_lines", 0.25)


def find_facecam(cfg: dict, input_args: list, duration: float) -> dict | None:
    """Position der Facecam, die praktisch im ganzen Clip zu sehen ist - sonst None (Clip auslassen).
    Der Rahmen ist animiert und kann in einzelnen Bildern dunkel sein -> 1 Ausreisser pro 10 Stichproben erlaubt;
    ein echter Szenenwechsel faellt in mehrere Stichproben."""
    L = cfg["layout"]
    if not L.get("frame_template"):
        return L["facecam"]
    frames = grab_frames_evenly(input_args, duration, 10)
    if len(frames) < 5:
        return None
    for fc in [L["facecam"], *L.get("facecam_alt", [])]:
        if sum(frame_match(cfg, f, fc) for f in frames) >= len(frames) - 1:
            return fc
    return None


_NVENC: bool | None = None


def video_codec(cfg: dict, quality: int) -> list[str]:
    global _NVENC
    if _NVENC is None:
        _NVENC = False
        if cfg["encode"].get("prefer_nvenc", True):
            r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.3",
                                "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True)
            _NVENC = r.returncode == 0
        print(f"   Encoder: {'NVIDIA NVENC (GPU)' if _NVENC else 'libx264 (CPU)'}")
    if _NVENC:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality)]


AUDIO_OUT = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]


def compose(cfg: dict, input_args: list, duration: float, peak_local: float, out: Path, cam: dict | None = None):
    """Pass 1: 9:16-Layout + optionaler Hook (Hoehepunkt kurz zuerst zeigen)."""
    fc = layout_filter(cfg, "0:v", "stk", cam)
    H = cfg["hook"]
    if H.get("enabled", True):
        hs = max(0.0, peak_local - H["before"])
        he = min(duration, peak_local + H["after"])
        fc += (
            ";[stk]split=2[v1][v2];[0:a]asplit=2[a1][a2];"
            f"[v1]trim=start={hs:.3f}:end={he:.3f},setpts=PTS-STARTPTS[hv];"
            f"[a1]atrim=start={hs:.3f}:end={he:.3f},asetpts=PTS-STARTPTS[ha];"
            "[v2]setpts=PTS-STARTPTS[mv];[a2]asetpts=PTS-STARTPTS[ma];"
            "[hv][ha][mv][ma]concat=n=2:v=1:a=1[v][a]"
        )
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc = fc.replace("[stk]", "[v]")
        maps = ["-map", "[v]", "-map", "0:a"]
    run(["ffmpeg", "-y", "-v", "error", *input_args, "-filter_complex", fc, *maps,
         *video_codec(cfg, 18), *AUDIO_OUT, out])


# ----------------------------------------------------------------------------- subtitles

_WHISPER = None


def _add_cuda_dlls():
    """Windows: DLLs aus pip-Paketen nvidia-cublas-cu12 / nvidia-cudnn-cu12 auffindbar machen."""
    if os.name != "nt":
        return
    import site
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        nv = Path(sp) / "nvidia"
        if nv.is_dir():
            for b in nv.glob("*/bin"):
                try:
                    os.add_dll_directory(str(b))
                except Exception:
                    pass
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


def whisper_model(cfg: dict):
    global _WHISPER
    if _WHISPER is None:
        _add_cuda_dlls()
        from faster_whisper import WhisperModel
        name = cfg["subtitles"].get("model", "small")
        try:
            _WHISPER = WhisperModel(name, device="cuda", compute_type="int8_float16")
            print(f"   Whisper '{name}' laeuft auf der GPU")
        except Exception as e:
            print(f"   GPU fuer Whisper nicht verfuegbar ({str(e)[:80]}) -> CPU")
            _WHISPER = WhisperModel(name, device="cpu", compute_type="int8")
    return _WHISPER


def load_audio_16k(media: Path) -> np.ndarray:
    """Audio per ffmpeg dekodieren (16 kHz mono float32) - umgeht PyAV-Inkompatibilitaeten von faster-whisper."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(cfg: dict, media: Path) -> list:
    try:
        model = whisper_model(cfg)
        segments, _ = model.transcribe(load_audio_16k(media), language=cfg["subtitles"].get("language", "en"),
                                       word_timestamps=True, vad_filter=True)
        return [w for s in segments for w in (s.words or [])]
    except Exception as e:
        print(f"   Transkription fehlgeschlagen: {e}")
        return []


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


PROFANITY = re.compile(
    r"\b(MOTHERFUCK\w*|FUCK\w*|SHIT\w*|BULLSHIT|BITCH\w*|CUNT\w*|ASSHOLE\w*|DICKS?|DICKHEAD\w*|PUSSY|COCKS?|"
    r"BASTARD\w*|SLUT\w*|WHORE\w*|WANK\w*|TWAT\w*|PRICK\w*)\b")
SLURS = re.compile(r"\b(NIGG\w*|FAGG?\w*|RETARD\w*)\b")


def censor(text: str) -> str:
    """Schimpfwoerter lesbar maskieren (FUCKING -> F*CKING), Beleidigungen ganz (N*****)."""
    def mask(m: re.Match) -> str:
        w = m.group(0)
        return w.replace("FUCK", "F*CK") if "FUCK" in w else re.sub(r"[AEIOU]", "*", w, count=1)
    text = PROFANITY.sub(mask, text)
    return SLURS.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)


def ass_escape(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(cfg: dict, words: list, duration: float, path: Path):
    S, L, O, C = cfg["subtitles"], cfg["layout"], cfg["overlay"], cfg["channel"]
    font, size = S.get("font", "Arial Black"), int(S.get("size", 80))
    top_h = even(L["top_h"])
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70,{int(S.get('margin_v', 520))},1",
        f"Style: Hi,{font},{size},&H0000E5FF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70,{int(S.get('margin_v', 520))},1",
        f"Style: Handle,{font},46,&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,14,0,8,40,40,{top_h + int(O.get('handle_offset', 70))},1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if O.get("show_handle_text", True):
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(duration)},Handle,,0,0,0,,{ass_escape(C['handle_text'])}")

    if S.get("enabled", True) and words:
        max_words = int(S.get("max_words", 3))
        chunks, cur = [], []
        for w in words:
            if cur and (len(cur) >= max_words or w.end - cur[0].start > 1.4 or w.start - cur[-1].end > 0.6):
                chunks.append(cur)
                cur = []
            cur.append(w)
        if cur:
            chunks.append(cur)
        for i, ch in enumerate(chunks):
            start = ch[0].start
            end = max(ch[-1].end, start + 0.35)
            if i + 1 < len(chunks):
                end = min(end, chunks[i + 1][0].start)
            text = " ".join(w.word.strip() for w in ch).upper()
            text = re.sub(r"[.,]+$", "", text)
            if S.get("censor", False):
                text = censor(text)
            style = "Hi" if i % 4 == 3 else "Sub"
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{ass_escape(text)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def finalize(cfg: dict, pass1: Path, ass: Path, out: Path):
    """Pass 2: Twitch-Overlay + Untertitel + Handle einbrennen."""
    O = cfg["overlay"]
    png = rel(O["png"]) if O.get("png") else None
    top_h = even(cfg["layout"]["top_h"])
    inputs = ["-i", pass1.name]
    if png and png.exists():
        inputs += ["-i", str(png)]
        y = O.get("y")
        y_expr = str(y) if y is not None else f"{top_h}-h/2"
        fc = (f"[1:v]scale={int(O.get('width', 760))}:-1[ov];"
              f"[0:v][ov]overlay=x=(W-w)/2:y={y_expr}[v1];[v1]subtitles={ass.name}[v]")
    else:
        fc = f"[0:v]subtitles={ass.name}[v]"
    # cwd = Clip-Ordner, damit der subtitles-Pfad unter Windows keine Probleme macht
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc, "-map", "[v]", "-map", "0:a",
         *video_codec(cfg, 21), "-c:a", "copy", "-movflags", "+faststart", out.name], cwd=pass1.parent)


# ----------------------------------------------------------------------------- captions

def make_caption(cfg: dict, idx: int) -> str:
    C = cfg["channel"]
    tags = list(C.get("hashtags", []))
    if C.get("game_hashtag"):
        tags.append(C["game_hashtag"])
    hook = CAPTION_HOOKS[idx % len(CAPTION_HOOKS)]
    return f"{hook}\n\nLive on {C['handle_text']}\n{' '.join(tags)}"


# ----------------------------------------------------------------------------- commands

def cmd_list(cfg, args):
    state = load_state()
    for v in list_vods(cfg["channel"]["name"], args.limit):
        mark = "[erledigt]" if v["id"] in state["done"] else "          "
        dur = f"{int(v['duration'] or 0) // 60} min"
        print(f"{mark} {v['id']}  {dur:>8}  {v['title'][:70]}")


def cmd_frame(cfg, args):
    from PIL import Image, ImageDraw
    work = ROOT / "work" / "frame"
    work.mkdir(parents=True, exist_ok=True)
    if args.image:
        src = Path(args.image)
    else:
        vod = args.vod or list_vods(cfg["channel"]["name"], 1)[0]["id"]
        seg = download_section(vod, args.at, args.at + 3, work, "frame_seg")
        src = work / "frame.png"
        run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", seg, "-frames:v", "1",
             "-vf", "scale=1920:1080", src])
    img = Image.open(src).convert("RGB").resize((1920, 1080))
    img.save(work / "frame.png")  # Basis fuer "python clipper.py layout"
    d = ImageDraw.Draw(img)
    for x in range(0, 1920, 100):
        d.line([(x, 0), (x, 1080)], fill=(255, 0, 0), width=1)
        d.text((x + 3, 3), str(x), fill=(255, 255, 0))
    for y in range(0, 1080, 100):
        d.line([(0, y), (1920, y)], fill=(255, 0, 0), width=1)
        d.text((3, y + 3), str(y), fill=(255, 255, 0))
    out = ROOT / "frame_grid.png"
    img.save(out)
    print(f"\nGespeichert: {out}\nLies die Facecam-Ecken ab und trag sie in config.toml unter [layout].facecam ein.")


def cmd_layout(cfg, args):
    src = Path(args.image) if args.image else ROOT / "work" / "frame" / "frame.png"
    if not src.exists():
        sys.exit("Kein Standbild gefunden. Zuerst: python clipper.py frame")
    out = ROOT / "layout_preview.png"
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-filter_complex", layout_filter(cfg, "0:v", "v"),
         "-map", "[v]", "-frames:v", "1", out])
    print(f"\nVorschau: {out}")


def cmd_run(cfg, args):
    D = cfg["detect"]
    if args.clips:
        D["clips_per_vod"] = args.clips
    if args.no_hook:
        cfg["hook"]["enabled"] = False
    state = load_state()

    if args.file:
        source = Path(args.file).resolve()
        if not source.exists():
            sys.exit(f"Datei nicht gefunden: {source}")
        label = source.stem
        work = ROOT / "work" / re.sub(r"\W+", "_", label)
        work.mkdir(parents=True, exist_ok=True)
        loud = loudness_per_second(source)
        chat = None
    else:
        if args.vod:
            vod, title = args.vod, ""
        else:
            todo = [v for v in list_vods(cfg["channel"]["name"], 10) if v["id"] not in state["done"]]
            if not todo:
                print("Keine neuen VODs. Fertig.")
                return
            vod, title = todo[0]["id"], todo[0]["title"]
        print(f"\n=== VOD {vod}  {title[:60]}")
        label = vod
        work = ROOT / "work" / vod
        work.mkdir(parents=True, exist_ok=True)
        audio = download_audio(vod, work)
        loud = loudness_per_second(audio)
        chat_path = download_chat(cfg, vod, work)
        chat = chat_per_second(chat_path, len(loud)) if chat_path else None

    want = int(D["clips_per_vod"])
    # Reserve-Kandidaten: Szenen ohne sichtbare Facecam werden uebersprungen (Kampagnen-Regel)
    picks = find_highlights(loud, chat, D, want * 3)
    if not picks:
        print("Keine Highlights ueber dem Schwellenwert gefunden (detect.min_score senken?).")
        return
    print(f"-> {len(picks)} Kandidaten gefunden, Ziel: {want} Clips mit sichtbarer Facecam")

    out_dir = ROOT / "output" / f"{dt.date.today():%Y-%m-%d}_{label}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = []
    for k, (peak, score) in enumerate(picks, 1):
        if len(report) >= want:
            break
        i = len(report) + 1
        name = f"clip_{i:02d}"
        print(f"\n[Kandidat {k}/{len(picks)} -> {name}] Hoehepunkt bei {hms(peak)[:8]} (Score {score:.1f})")
        start = max(0.0, peak - D["pre"])
        end = min(len(loud), peak + D["post"])
        dur = end - start
        try:
            if args.file:
                input_args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(source)]
            else:
                seg = download_section(label, start, end, work, f"seg_{k:02d}")
                input_args = ["-i", str(seg)]
            cam = find_facecam(cfg, input_args, dur)
            if cam is None:
                print("   Facecam nicht durchgehend sichtbar -> uebersprungen")
                if not args.file and not args.keep:
                    seg.unlink(missing_ok=True)
                continue
            clip_dir = work / name
            clip_dir.mkdir(exist_ok=True)
            pass1 = clip_dir / "pass1.mp4"
            compose(cfg, input_args, dur, peak - start, pass1, cam)
            total = dur + (cfg["hook"]["before"] + cfg["hook"]["after"] if cfg["hook"].get("enabled") else 0)
            words = transcribe(cfg, pass1) if cfg["subtitles"].get("enabled", True) else []
            ass = clip_dir / "subs.ass"
            build_ass(cfg, words, total + 1, ass)
            finalize(cfg, pass1, ass, clip_dir / "final.mp4")
            final = out_dir / f"{name}.mp4"
            shutil.move(str(clip_dir / "final.mp4"), final)
            spoken = " ".join(w.word.strip() for w in words)
            report.append({"clip": final.name, "vod_time": hms(peak)[:8], "score": round(score, 2),
                           "transcript": spoken[:300], "caption": make_caption(cfg, i + hash(label) % 8)})
            print(f"   OK -> {final}")
        except Exception as e:
            print(f"   FEHLER bei {name}: {e}")

    with open(out_dir / "captions.txt", "w", encoding="utf-8") as f:
        f.write(f"Clips aus {label} - vor dem Posten jeden Clip anschauen und die Caption anpassen!\n")
        f.write("Checkliste: Facecam sichtbar | twitch.tv/skiimasktimmy im Bild + Caption | Englisch | Likes/Kommentare AN\n\n")
        for r in report:
            f.write(f"===== {r['clip']}  (VOD {r['vod_time']}, Score {r['score']})\n")
            f.write(f"Gesagt: {r['transcript'] or '-'}\n\n--- Caption ---\n{r['caption']}\n\n")
    (out_dir / "clips.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.file and report and label not in state["done"]:
        state["done"].append(label)
        save_state(state)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\nFertig: {len(report)} Clips in {out_dir}")


def cmd_run_all(cfg, args):
    """Alle noch nicht verarbeiteten VODs nacheinander clippen (aelteste zuerst)."""
    if os.name == "nt":  # Windows waehrend des Laufs nicht einschlafen lassen
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    print(f"===== {dt.datetime.now():%Y-%m-%d %H:%M}")
    done = load_state()["done"]
    todo = [v["id"] for v in list_vods(cfg["channel"]["name"], args.limit) if v["id"] not in done]
    print(f"{len(todo)} offene VODs: {', '.join(todo) or '-'}")
    for vod in reversed(todo):
        args.vod = vod
        try:
            cmd_run(load_config(Path(args.config)), args)
        except Exception as e:
            print(f"FEHLER bei VOD {vod}: {e}")


def keep_awake():
    """Windows nicht schlafen lassen, solange das Skript laeuft (sonst schlaeft der PC nach dem
    Aufwecken durch die Aufgabenplanung nach ~2 Min wieder ein)."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED


def main():
    keep_awake()
    ap = argparse.ArgumentParser(description="Twitch-VOD -> TikTok-Clips")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="Neueste VODs anzeigen")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("frame", help="Standbild mit Raster speichern")
    p.add_argument("--vod")
    p.add_argument("--at", type=float, default=900, help="Sekunde im VOD (Standard 900 = 15 min)")
    p.add_argument("--image", help="Eigenes Bild statt VOD verwenden")

    p = sub.add_parser("layout", help="9:16-Vorschau rendern")
    p.add_argument("--image")

    p = sub.add_parser("run", help="Clips erstellen")
    p.add_argument("--vod", help="Bestimmte VOD-ID")
    p.add_argument("--file", help="Lokale Videodatei statt Twitch")
    p.add_argument("--clips", type=int, help="Anzahl Clips (ueberschreibt config)")
    p.add_argument("--no-hook", action="store_true")
    p.add_argument("--keep", action="store_true", help="Arbeitsdateien behalten")
    p.add_argument("--all", action="store_true", help="Alle offenen VODs nacheinander clippen")
    p.add_argument("--limit", type=int, default=20, help="mit --all: so viele neueste VODs pruefen")

    args = ap.parse_args()
    require("ffmpeg")
    cfg = load_config(Path(args.config))
    if args.cmd == "run" and args.all:
        return cmd_run_all(cfg, args)
    {"list": cmd_list, "frame": cmd_frame, "layout": cmd_layout, "run": cmd_run}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
