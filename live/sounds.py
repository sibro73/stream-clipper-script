"""TikTok-Sound-Vorschlag pro Clip (aus sounds.yaml, nach Stimmung). Hinzufuegen musst du ihn in TikTok selbst."""
from __future__ import annotations

import logging
import random
import re
from collections import deque

import yaml

from .config import ROOT

log = logging.getLogger("sounds")
_recent: deque = deque(maxlen=6)        # zuletzt vorgeschlagen -> abwechseln


def _load() -> dict:
    p = ROOT / "sounds.yaml"
    try:
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("moods", {})
    except Exception as e:
        log.warning("sounds.yaml: %s", e)
        return {}


def mood_of(moods: dict, banner: str | None, transcript: str, reason: str) -> str:
    """Banner zaehlt am meisten (beschreibt die Szene), dann Trigger-Grund, dann Transkript."""
    scores = {m: 0.0 for m in moods}
    for text, weight in ((banner or "", 3.0), (transcript, 1.0)):
        low = text.lower()
        for m, cfg in moods.items():
            for kw in cfg.get("keywords") or []:
                scores[m] += weight * len(re.findall(r"\b" + re.escape(kw.lower()), low))
    if "lach" in reason.lower() and "funny" in scores:
        scores["funny"] += 2.5
    best = max(scores, key=scores.get) if scores else "hype"
    return best if scores.get(best, 0) > 0 else ("hype" if "hype" in moods else best)


def suggest(banner: str | None, transcript: str, reason: str) -> tuple[str, list[str]] | None:
    """(stimmung, [sound, alternative]) oder None, wenn sounds.yaml fehlt."""
    moods = _load()
    if not moods:
        return None
    mood = mood_of(moods, banner, transcript, reason)
    pool = list(moods.get(mood, {}).get("sounds") or [])
    if not pool:
        return None
    fresh = [s for s in pool if s not in _recent] or pool
    # leicht zu den oberen Eintraegen gewichtet
    first = random.choices(fresh, weights=[len(pool) - pool.index(s) for s in fresh])[0]
    rest = [s for s in pool if s != first]
    picks = [first] + ([random.choice(rest)] if rest else [])
    _recent.append(first)
    return mood, picks
