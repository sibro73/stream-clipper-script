"""Facecam per Gesichtserkennung finden (Haar-Cascade) - nur als Vorschlag, Bestaetigung per Telegram.

GTA-Figuren haben auch Gesichter. Deshalb zaehlt nur ein Gesicht, das in den meisten Stichproben an
(fast) derselben Stelle ist - eine Webcam steht still, Spielfiguren bewegen sich bzw. die Kamera dreht.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

W, H = 1920, 1080
_CASCADE = None


def _cascade():
    global _CASCADE
    if _CASCADE is None:
        import cv2
        _CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _CASCADE


def _inside(cx: float, cy: float, box: dict) -> bool:
    return box["x"] <= cx <= box["x"] + box["w"] and box["y"] <= cy <= box["y"] + box["h"]


def box_from_face(cx: float, cy: float, size: float) -> dict:
    """Typische Facecam: Gesicht ~ 1/4 der Breite, 16:9, Gesicht etwas ueber der Mitte."""
    w = min(W * 0.5, size * 4.0)
    h = w * 9 / 16
    x = min(max(0.0, cx - w / 2), W - w)
    y = min(max(0.0, cy - h * 0.45), H - h)
    ev = lambda v: int(round(v / 2)) * 2
    return {"x": ev(x), "y": ev(y), "w": ev(w), "h": ev(h)}


def detect(frames: list[np.ndarray], rejected: list[dict] | None = None) -> dict | None:
    import cv2
    rejected = rejected or []
    clusters: list[dict] = []
    for i, f in enumerate(frames):
        gray = cv2.cvtColor(cv2.resize(f, (960, 540)), cv2.COLOR_RGB2GRAY)
        faces = _cascade().detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(24, 24))
        for (x, y, w, h) in faces:
            cx, cy, s = (x + w / 2) * 2, (y + h / 2) * 2, w * 2.0
            for c in clusters:
                if math.dist((cx, cy), (c["cx"], c["cy"])) < 0.6 * c["s"] and 0.6 < s / c["s"] < 1.6:
                    c["pts"].append((cx, cy, s))
                    c["frames"].add(i)
                    n = len(c["pts"])
                    c["cx"] += (cx - c["cx"]) / n
                    c["cy"] += (cy - c["cy"]) / n
                    c["s"] += (s - c["s"]) / n
                    break
            else:
                clusters.append({"cx": cx, "cy": cy, "s": s, "pts": [(cx, cy, s)], "frames": {i}})
    need = max(2, math.ceil(len(frames) * 0.6))
    best, best_score = None, -1e9
    for c in clusters:
        if len(c["frames"]) < need or any(_inside(c["cx"], c["cy"], r) for r in rejected):
            continue
        pts = np.array(c["pts"])
        jitter = float(pts[:, :2].std(axis=0).mean()) / c["s"]
        edge = min(c["cx"], W - c["cx"], c["cy"], H - c["cy"]) / (H / 2)   # 0 = am Rand, 1 = Mitte
        score = len(c["frames"]) * 10 - jitter * 20 - edge * 4
        if score > best_score:
            best, best_score = c, score
    if not best:
        return None
    # Ausschnitt ums Gesicht (16:9). Bewusst eher eng: faengt nie Spiel/Chat ein; exakter Rahmen per /box.
    return box_from_face(best["cx"], best["cy"], best["s"])


def draw_preview(frame: np.ndarray, box: dict, out: Path, label: str = ""):
    from PIL import Image, ImageDraw
    img = Image.fromarray(frame)
    d = ImageDraw.Draw(img)
    for k in range(6):
        d.rectangle([box["x"] - k, box["y"] - k, box["x"] + box["w"] + k, box["y"] + box["h"] + k],
                    outline=(0, 255, 90))
    if label:
        d.rectangle([box["x"], max(0, box["y"] - 40), box["x"] + 12 * len(label) + 20, box["y"]], fill=(0, 0, 0))
        d.text((box["x"] + 8, max(0, box["y"] - 32)), label, fill=(0, 255, 90))
    img.resize((1280, 720)).save(out, quality=88)
