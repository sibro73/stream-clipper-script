"""Englische Caption: Hook-Zeile + IMMER Pflicht-Name und -Hashtags aus streamers.yaml.

Der Hook kommt von einem austauschbaren Generator (settings.yaml -> caption.provider):
  transcript  erster praegnanter Satz aus dem Whisper-Transkript (Standard, ohne KI, ohne Kosten)
  ollama      lokales Modell ueber die Ollama-API (caption.ollama.url / .model)
  anthropic   Claude (braucht ANTHROPIC_API_KEY in der .env)
Faellt der gewaehlte Generator aus, geht es weiter mit "transcript", danach mit einer festen Vorlage.
Neuer Generator: Funktion (S, streamer, transcript, reason) -> str | None schreiben und in PROVIDERS eintragen.
"""
from __future__ import annotations

import logging
import random
import re
from typing import Callable

from .config import Streamer, env
from .subtitles import PROFANITY, SLURS

log = logging.getLogger("caption")

HookFn = Callable[[dict, Streamer, str, str], "str | None"]

TEMPLATE_HOOKS = [
    "the reaction says it all 💀",
    "chat lost it at this one",
    "I can't stop rewatching this",
    "bro was NOT ready for that",
    "this is why you catch him live",
    "nobody saw that coming",
    "the timing on this 😭",
    "GTA 6 is already giving us moments like this",
]

# --------------------------------------------------------------------------- transcript (ohne KI)

FILLER = {"yeah", "yes", "yo", "no", "ok", "okay", "oh", "uh", "um", "hmm", "huh", "what", "wait", "bro", "dude",
          "like", "so", "and", "the", "a", "i", "it", "is", "that", "this", "right", "chat", "guys", "hey", "wow",
          "damn", "bruh", "man", "lol", "haha", "ha", "ah", "gg", "go", "come", "on", "thank", "you", "thanks"}
_PROF_I = re.compile(PROFANITY.pattern, re.I)
_SLUR_I = re.compile(SLURS.pattern, re.I)


def _censor(text: str) -> str:
    return _PROF_I.sub(lambda m: re.sub(r"[aeiouAEIOU]", "*", m.group(0), count=1), text)


def _sentences(transcript: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", re.sub(r"\s+", " ", transcript).strip())
    return [p.strip(" -–\"'") for p in parts if p.strip()]


def _is_punchy(s: str) -> bool:
    words = re.findall(r"[A-Za-z']+", s.lower())
    if len(words) < 3 or len(s) < 12 or _SLUR_I.search(s):
        return False
    meaningful = [w for w in words if w not in FILLER]
    return len(meaningful) >= 2 and len(set(words)) / len(words) >= 0.5     # nicht "no no no no no"


def _shorten(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(",;:- ") + "…"


def transcript_hook(S: dict, streamer: Streamer, transcript: str, reason: str) -> str | None:
    limit = int(S["caption"].get("max_hook_chars", 80))
    for s in _sentences(transcript):
        if _is_punchy(s):
            s = _shorten(_censor(s), limit)
            return s[0].upper() + s[1:]
    return None


# --------------------------------------------------------------------------- LLM-Generatoren

SYSTEM = (
    "You write one short caption line for a TikTok/Shorts clip of a Twitch streamer playing GTA 6. "
    "Write in English, max 80 characters, casual and punchy, like a real clipper would post it. "
    "Base it on what actually happens in the transcript; do not invent events. "
    "No hashtags, no @mentions, no quotation marks, no links. Output only the caption line."
)


def _prompt(streamer: Streamer, transcript: str, reason: str) -> str:
    return (f"Streamer: {streamer.caption_name}\nWhy this was clipped: {reason}\n"
            f"Transcript of the clip (auto-generated, may be messy):\n{transcript[:1500] or '(no speech)'}")


def _clean(S: dict, text: str) -> str | None:
    text = re.sub(r"#\w+", "", text.strip().splitlines()[0] if text.strip() else "").strip().strip('"').strip()
    return _shorten(text, int(S["caption"].get("max_hook_chars", 80)) + 30) or None


def ollama_hook(S: dict, streamer: Streamer, transcript: str, reason: str) -> str | None:
    import requests
    O = S["caption"].get("ollama", {})
    r = requests.post(f"{O.get('url', 'http://localhost:11434').rstrip('/')}/api/chat", timeout=O.get("timeout_s", 30),
                      json={"model": O.get("model", "llama3.2"), "stream": False,
                            "options": {"temperature": 0.8, "num_predict": 60},
                            "messages": [{"role": "system", "content": SYSTEM},
                                         {"role": "user", "content": _prompt(streamer, transcript, reason)}]})
    r.raise_for_status()
    return _clean(S, r.json().get("message", {}).get("content", ""))


_anthropic = None


def anthropic_hook(S: dict, streamer: Streamer, transcript: str, reason: str) -> str | None:
    global _anthropic
    if not env("ANTHROPIC_API_KEY", required=False):
        return None
    import anthropic
    if _anthropic is None:
        _anthropic = anthropic.Anthropic(timeout=20.0, max_retries=1)
    msg = _anthropic.messages.create(
        model=S["caption"].get("model", "claude-haiku-5-5"), max_tokens=300, system=SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": _prompt(streamer, transcript, reason)}])
    if msg.stop_reason == "refusal":
        return None
    return _clean(S, " ".join(b.text for b in msg.content if b.type == "text"))


PROVIDERS: dict[str, HookFn] = {
    "transcript": transcript_hook,
    "ollama": ollama_hook,
    "anthropic": anthropic_hook,
}


# --------------------------------------------------------------------------- Zusammenbau

def make_hook(S: dict, streamer: Streamer, transcript: str, reason: str) -> tuple[str, str]:
    """(hook, quelle). Reihenfolge: gewaehlter Generator -> transcript -> Vorlage."""
    chosen = S["caption"].get("provider", "transcript")
    if chosen not in PROVIDERS:
        log.warning("Unbekannter caption.provider '%s' -> transcript", chosen)
        chosen = "transcript"
    for name in dict.fromkeys([chosen, "transcript"]):
        try:
            hook = PROVIDERS[name](S, streamer, transcript, reason)
            if hook:
                return hook, name
        except Exception as e:
            log.warning("Caption-Generator '%s' fehlgeschlagen (%s)", name, str(e)[:120])
    return random.choice(TEMPLATE_HOOKS), "vorlage"


def build_caption(S: dict, streamer: Streamer, hook: str) -> str:
    """Name + Pflicht-Hashtags werden hier deterministisch angehaengt - nie dem Generator ueberlassen."""
    tags, seen = [], set()
    for t in [*streamer.campaign_tags, *streamer.hashtags, *S["caption"].get("extra_hashtags", [])]:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    name_line = f"{streamer.caption_name} live on twitch.tv/{streamer.login}"
    return f"{hook}\n\n{name_line}\n{' '.join(tags)}".strip()
