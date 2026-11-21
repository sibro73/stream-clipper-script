"""Konfiguration: .env (Secrets), streamers.yaml (Streamer), settings.yaml (Schwellen & Co.)."""
from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEFAULTS: dict = {
    "game": {"name": "Grand Theft Auto VI"},
    "poll": {"streams_s": 60, "clips_s": 30},
    "buffer": {"minutes": 8, "segment_s": 4, "quality": "720p30,720p60,720p,best", "max_streams": 2},
    "trigger": {
        "cooldown_s": 900, "max_per_hour": 0,
        "confirm_s": 120,
        "clips": {"min_clips": 2, "strong_min": 4, "window_s": 120, "lookback_s": 180},
        "chat": {
            "window_s": 10, "history_windows": 30, "warmup_windows": 6,
            "spike_ratio": 3.0, "strong_ratio": 5.0, "min_msgs": 15, "laugh_strong_min": 25,
            "laugh_emotes": ["LUL", "KEKW", "LMAO", "OMEGALUL", "ICANT"],
            "laugh_ratio": 0.3, "laugh_min": 10, "laugh_vs_baseline": 2.0,
            "delay_s": 4,
        },
    },
    "clip": {"pre_s": 14, "post_s": 6, "min_s": 10, "peak_search_back_s": 12, "peak_search_fwd_s": 4, "video_bitrate_k": 2000, "fps": 30, "width": 720, "max_mb": 49,
             "top_h_min": 500, "top_h_max": 860, "prefer_nvenc": True},
    "subtitles": {"enabled": True, "model": "small", "language": "en", "font": "Arial Black",
                  "size": 80, "margin_v": 520, "max_words": 3, "censor": True},
    "activity": {"enabled": True, "check_s": 60, "window_s": 120, "idle_checks": 2,
                 "speech_min": 0.03, "speech_low": 0.08, "quiet_db": -40,
                 "recheck_min": 10, "probe_s": 60,
                 "sleep_words": ["sleep stream", "sleepstream", "sleeping", "zzz+", "schlafstream", "schlafen"]},
    "banner": {"enabled": True, "burn_in": False, "model": "gemma3:4b", "max_words": 9, "max_chars": 60, "timeout_s": 60,
               "size": 62, "use_as_hook": True, "keep_alive": "24h"},
    "caption": {"provider": "transcript", "max_hook_chars": 80, "extra_hashtags": ["#gta6"],
                "model": "claude-haiku-5-5",
                "ollama": {"url": "http://localhost:11434", "model": "llama3.2", "timeout_s": 30}},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_settings() -> dict:
    p = ROOT / "settings.yaml"
    over = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    merged = _merge(DEFAULTS, over or {})
    local = ROOT / "settings.local.yaml"      # optional, nicht im Repo: Abweichungen pro Rechner (z. B. Server)
    if local.exists():
        merged = _merge(merged, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    return merged


@dataclass
class Streamer:
    login: str
    caption_name: str
    hashtags: list[str] = field(default_factory=list)
    campaign_tags: list[str] = field(default_factory=list)
    facecam: dict | None = None          # {x, y, w, h} im 1920x1080-Raster, manuell gesetzt
    game_offset_x: int = 0

    @property
    def campaign(self) -> bool:
        return bool(self.campaign_tags)


def load_streamers() -> list[Streamer]:
    data = yaml.safe_load((ROOT / "streamers.yaml").read_text(encoding="utf-8")) or {}
    out = []
    for s in data.get("streamers", []):
        out.append(Streamer(
            login=str(s["twitch_login"]).lower(),
            caption_name=s.get("caption_name") or s["twitch_login"],
            hashtags=list(s.get("hashtags") or []),
            campaign_tags=list(s.get("campaign_tags") or []),
            facecam=s.get("facecam"),
            game_offset_x=int(s.get("game_offset_x", 0)),
        ))
    return out


def env(name: str, required: bool = True) -> str:
    v = os.environ.get(name, "").strip()
    if required and not v:
        raise SystemExit(f"FEHLER: {name} fehlt in .env")
    return v


# ----------------------------------------------------------------------------- Facecam-Cache

class FacecamCache:
    """Bestaetigte Facecam-Boxen pro Streamer (erst nach /ok) + abgelehnte Kandidaten (/redo)."""

    def __init__(self, path: Path = ROOT / "facecam_cache.json"):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.data.setdefault("confirmed", {})
        self.data.setdefault("rejected", {})

    def _save(self):
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def get(self, login: str) -> dict | None:
        return self.data["confirmed"].get(login)

    def rejected(self, login: str) -> list[dict]:
        return list(self.data["rejected"].get(login, []))

    def confirm(self, login: str, box: dict):
        with self.lock:
            self.data["confirmed"][login] = box
            self._save()

    def reject(self, login: str, box: dict):
        with self.lock:
            self.data["rejected"].setdefault(login, []).append(box)
            self._save()
