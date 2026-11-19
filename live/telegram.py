"""Telegram Bot API: Videos/Fotos/Text senden + Befehle (/ok, /redo, /box, /status) empfangen."""
from __future__ import annotations

import html
import re
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import requests

log = logging.getLogger("telegram")
esc = html.escape
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact(e) -> str:
    """Bot-Token aus Fehlermeldungen entfernen (requests schreibt die URL mit Token hinein)."""
    return _TOKEN_RE.sub("bot<TOKEN>", str(e))


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = str(chat_id)
        self.s = requests.Session()

    def _call(self, method: str, data: dict, files: dict | None = None, timeout: int = 120) -> dict:
        last = None
        for attempt in range(3):
            try:
                opened = {k: (Path(p).name, open(p, "rb")) for k, p in (files or {}).items()}
                try:
                    r = self.s.post(self.base + method, data=data, files=opened or None, timeout=timeout)
                finally:
                    for _, f in opened.values():
                        f.close()
                j = r.json()
                if j.get("ok"):
                    return j["result"]
                if r.status_code == 429:
                    time.sleep(int(j.get("parameters", {}).get("retry_after", 5)))
                    continue
                raise RuntimeError(f"Telegram {method}: {j.get('description')}")
            except requests.RequestException as e:
                last = e
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"Telegram {method} fehlgeschlagen: {redact(last)}")

    def send_text(self, text: str) -> dict:
        return self._call("sendMessage", {"chat_id": self.chat_id, "text": text[:4096], "parse_mode": "HTML",
                                          "disable_web_page_preview": "true"}, timeout=30)

    def send_photo(self, path: Path, caption: str) -> dict:
        return self._call("sendPhoto", {"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
                          {"photo": path}, timeout=60)

    def send_video(self, path: Path, caption: str, thumb: Path, cover: Path, w: int, h: int, dur: float) -> dict:
        data = {"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML",
                "supports_streaming": "true", "width": w, "height": h, "duration": int(round(dur)),
                "thumbnail": "attach://thumb", "cover": "attach://cover"}
        return self._call("sendVideo", data, {"video": path, "thumb": thumb, "cover": cover}, timeout=300)


class CommandListener(threading.Thread):
    """Long-Polling auf getUpdates; nur Nachrichten aus der eigenen Chat-ID werden beachtet."""

    def __init__(self, tg: Telegram, handler: Callable[[str, dict], None]):
        super().__init__(name="tg-cmd", daemon=True)
        self.tg, self.handler = tg, handler
        self._halt = threading.Event()

    def stop(self):
        self._halt.set()

    def run(self):
        offset = None
        try:  # alte, schon gelesene Befehle ueberspringen
            old = self.tg._call("getUpdates", {"offset": -1, "timeout": 0}, timeout=20)
            if old:
                offset = old[-1]["update_id"] + 1
        except Exception as e:
            log.warning("getUpdates (Start): %s", redact(e))
        while not self._halt.is_set():
            try:
                data = {"timeout": 30, "allowed_updates": '["message"]'}
                if offset is not None:
                    data["offset"] = offset
                for u in self.tg._call("getUpdates", data, timeout=45):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != self.tg.chat_id:
                        continue
                    text = (msg.get("text") or "").strip()
                    if text.startswith("/"):
                        try:
                            self.handler(text, msg)
                        except Exception:
                            log.exception("Befehl %r", text)
            except Exception as e:
                log.warning("getUpdates: %s", redact(e))
                self._halt.wait(10)
