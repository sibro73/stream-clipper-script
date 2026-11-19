"""faster-whisper (GPU, Fallback CPU) + ASS-Untertitel im TikTok-Stil (3 Woerter, gross, jedes 4. gelb)."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("subs")
_MODEL = None
_LOCK = threading.Lock()


def _add_cuda_dlls():
    """Windows: DLLs aus nvidia-cublas-cu12 / nvidia-cudnn-cu12 auffindbar machen."""
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


def model(S: dict):
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            _add_cuda_dlls()
            from faster_whisper import WhisperModel
            name = S.get("model", "small")
            try:
                _MODEL = WhisperModel(name, device="cuda", compute_type="int8_float16")
                log.info("Whisper '%s' auf der GPU", name)
            except Exception as e:
                log.warning("Whisper GPU nicht verfuegbar (%s) -> CPU", str(e)[:100])
                _MODEL = WhisperModel(name, device="cpu", compute_type="int8")
    return _MODEL


def transcribe(S: dict, input_args: list[str]) -> list:
    """Woerter mit Zeitstempeln (relativ zum Clip-Anfang)."""
    r = subprocess.run(["ffmpeg", "-v", "error", *input_args, "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "-"], capture_output=True, check=True)
    audio = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if audio.size < 16000:
        return []
    segs, _ = model(S).transcribe(audio, language=S.get("language", "en"), word_timestamps=True, vad_filter=True)
    return [w for s in segs for w in (s.words or [])]


PROFANITY = re.compile(
    r"\b(MOTHERFUCK\w*|FUCK\w*|SHIT\w*|BULLSHIT|BITCH\w*|CUNT\w*|ASSHOLE\w*|DICKS?|DICKHEAD\w*|PUSSY|COCKS?|"
    r"BASTARD\w*|SLUT\w*|WHORE\w*|WANK\w*|TWAT\w*|PRICK\w*)\b")
SLURS = re.compile(r"\b(NIGG\w*|FAGG?\w*|RETARD\w*)\b")


def censor(text: str) -> str:
    def mask(m: re.Match) -> str:
        w = m.group(0)
        return w.replace("FUCK", "F*CK") if "FUCK" in w else re.sub(r"[AEIOU]", "*", w, count=1)
    text = PROFANITY.sub(mask, text)
    return SLURS.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)


def _t(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def write_ass(S: dict, words: list, path: Path, margin_v: int):
    font, size = S.get("font", "Arial Black"), int(S.get("size", 80))
    style = "&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,{font},{size},&H00FFFFFF,{style},{margin_v},1",
        f"Style: Hi,{font},{size},&H0000E5FF,{style},{margin_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
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
        start, end = ch[0].start, max(ch[-1].end, ch[0].start + 0.35)
        if i + 1 < len(chunks):
            end = min(end, chunks[i + 1][0].start)
        text = re.sub(r"[.,]+$", "", " ".join(w.word.strip() for w in ch).upper())
        if S.get("censor", False):
            text = censor(text)
        lines.append(f"Dialogue: 0,{_t(start)},{_t(end)},{'Hi' if i % 4 == 3 else 'Sub'},,0,0,0,,{_esc(text)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
