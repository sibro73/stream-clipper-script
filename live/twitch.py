"""Twitch Helix API (App-Access-Token, erneuert sich selbst)."""
from __future__ import annotations

import datetime as dt
import logging
import time

import requests

log = logging.getLogger("twitch")
API = "https://api.twitch.tv/helix/"


def parse_ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def rfc3339(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Helix:
    def __init__(self, client_id: str, client_secret: str):
        self.cid, self.secret = client_id, client_secret
        self.token, self.expires = "", 0.0
        self.s = requests.Session()

    def _auth(self):
        r = self.s.post("https://id.twitch.tv/oauth2/token", timeout=15, data={
            "client_id": self.cid, "client_secret": self.secret, "grant_type": "client_credentials"})
        r.raise_for_status()
        j = r.json()
        self.token, self.expires = j["access_token"], time.time() + j.get("expires_in", 3600) - 300

    def get(self, path: str, params: dict) -> dict:
        for attempt in range(4):
            if not self.token or time.time() > self.expires:
                self._auth()
            try:
                r = self.s.get(API + path, params=params, timeout=15,
                               headers={"Client-Id": self.cid, "Authorization": f"Bearer {self.token}"})
            except requests.RequestException as e:
                log.warning("Helix %s Netzwerkfehler: %s", path, e)
                time.sleep(2 + attempt * 3)
                continue
            if r.status_code == 401:
                self.token = ""
                continue
            if r.status_code == 429 or r.status_code >= 500:
                reset = float(r.headers.get("Ratelimit-Reset", time.time() + 5))
                time.sleep(min(30, max(1.0, reset - time.time())))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Helix {path} fehlgeschlagen")

    # ------------------------------------------------------------------ Endpunkte

    def users(self, logins: list[str]) -> dict[str, dict]:
        data = self.get("users", {"login": logins})["data"]
        return {u["login"].lower(): u for u in data}

    def streams(self, user_ids: list[str]) -> list[dict]:
        return self.get("streams", {"user_id": user_ids, "first": 100})["data"]

    def game_id(self, name: str) -> str | None:
        data = self.get("games", {"name": name})["data"]
        return data[0]["id"] if data else None

    def clips(self, broadcaster_id: str, since: float) -> list[dict]:
        return self.get("clips", {"broadcaster_id": broadcaster_id, "started_at": rfc3339(since),
                                  "ended_at": rfc3339(time.time() + 60), "first": 50})["data"]

    def current_vod(self, user_id: str, stream_id: str) -> dict | None:
        """Archiv-VOD des laufenden Streams (falls der Streamer VODs aktiviert hat)."""
        for v in self.get("videos", {"user_id": user_id, "type": "archive", "first": 3})["data"]:
            if str(v.get("stream_id")) == str(stream_id):
                return v
        return None
