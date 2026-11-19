"""Englische Caption: Hook-Zeile von Claude Haiku (Fallback: Vorlage) + IMMER Pflicht-Name und -Hashtags."""
from __future__ import annotations

import logging
import random
import re

from .config import Streamer, env

log = logging.getLogger("caption")

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

SYSTEM = (
    "You write one short caption line for a TikTok/Shorts clip of a Twitch streamer playing GTA 6. "
    "Write in English, max 110 characters, casual and punchy, like a real clipper would post it. "
    "Base it on what actually happens in the transcript; do not invent events. "
    "No hashtags, no @mentions, no quotation marks, no links. Output only the caption line."
)

_client = None


def ai_hook(S: dict, streamer: Streamer, transcript: str, reason: str) -> str | None:
    global _client
    if not env("ANTHROPIC_API_KEY", required=False):
        return None
    try:
        import anthropic
        if _client is None:
            _client = anthropic.Anthropic(timeout=20.0, max_retries=1)
        msg = _client.messages.create(
            model=S["caption"]["model"],
            max_tokens=300,
            system=SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content":
                       f"Streamer: {streamer.caption_name}\nWhy this was clipped: {reason}\n"
                       f"Transcript of the clip (auto-generated, may be messy):\n{transcript[:1500] or '(no speech)'}"}],
        )
        if msg.stop_reason == "refusal":
            return None
        text = " ".join(b.text for b in msg.content if b.type == "text").strip()
        text = re.sub(r"#\w+", "", text).strip().strip('"').strip()
        return text[:150] or None
    except Exception as e:
        log.warning("Claude-Caption fehlgeschlagen (%s) -> Vorlage", str(e)[:120])
        return None


def build_caption(S: dict, streamer: Streamer, hook: str | None) -> tuple[str, bool]:
    """Gibt (caption, von_ki) zurueck. Name + Pflicht-Hashtags werden hier deterministisch angehaengt."""
    from_ai = bool(hook)
    hook = hook or random.choice(TEMPLATE_HOOKS)
    tags, seen = [], set()
    for t in [*streamer.campaign_tags, *streamer.hashtags, *S["caption"].get("extra_hashtags", [])]:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    name_line = f"{streamer.caption_name} live on twitch.tv/{streamer.login}"
    return f"{hook}\n\n{name_line}\n{' '.join(tags)}".strip(), from_ai
