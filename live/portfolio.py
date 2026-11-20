"""Meldet fertige Clips an das Portfolio (silvanbrors.ch), fuer die Zahl «Clips heute».

Optional: Ohne PORTFOLIO_API_URL und PORTFOLIO_API_KEY in .env passiert nichts.
Es wird nur die Clip-ID und der Zeitpunkt gesendet, kein Video und kein Streamer-Inhalt.
Fehler stoppen den Clipper nie: der Aufruf laeuft im Hintergrund und wird nur geloggt.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading

import requests

from .config import env

log = logging.getLogger("clipper.portfolio")

# Das Backend schlaeft (Fly.io) und braucht beim Aufwachen ein paar Sekunden.
TIMEOUT_S = 30


def clip_id(login: str, video_stem: str) -> str:
    """Eindeutige ID im Format, das das Backend erlaubt: [A-Za-z0-9._:-], hoechstens 100 Zeichen."""
    return re.sub(r"[^A-Za-z0-9._:-]", "-", f"{login}:{video_stem}")[:100]


def payload(cid: str, created: float) -> dict:
    when = dt.datetime.fromtimestamp(created, dt.timezone.utc).replace(microsecond=0)
    return {"clipId": cid, "createdAt": when.isoformat().replace("+00:00", "Z")}


def report_clip(login: str, video_stem: str, created: float) -> None:
    url = env("PORTFOLIO_API_URL", required=False)
    key = env("PORTFOLIO_API_KEY", required=False)
    if not url or not key:
        return
    body = payload(clip_id(login, video_stem), created)

    def post():
        try:
            r = requests.post(f"{url.rstrip('/')}/api/clipper/events", json=body,
                              headers={"X-Api-Key": key}, timeout=TIMEOUT_S)
            if r.status_code in (200, 201):
                log.info("[%s] Portfolio: Clip gemeldet (%s)", login, r.status_code)
            else:
                log.warning("[%s] Portfolio: HTTP %s %s", login, r.status_code, r.text[:200])
        except Exception as e:  # nie den Clipper stoppen
            log.warning("[%s] Portfolio nicht erreichbar: %s", login, e)

    threading.Thread(target=post, name="portfolio", daemon=True).start()
