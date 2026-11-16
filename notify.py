#!/usr/bin/env python3
"""
Telegram-Begleiter fuer clipper.py: schickt jeden Morgen die Top-Clips mit Caption aufs Handy
und erinnert zu den Post-Zeiten ans Einreichen bei Whop.

Befehle:
  python notify.py setup          Chat-ID ermitteln (vorher dem Bot in Telegram /start schicken)
  python notify.py test           Testnachricht senden
  python notify.py morning        Top-Clips + Captions senden (09:00, einmal pro Tag)
  python notify.py remind         Erinnerung fuer den zuletzt faelligen Post-Slot senden
  python notify.py posted PFAD    Clip von Hand als gepostet markieren
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECRETS = ROOT / "secrets.toml"
POSTED = ROOT / "posted.json"
PLANS = ROOT / "plans"
PHONE = ROOT / "work" / "phone"
TITLES = ROOT / "vod_titles.json"

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

GIVEAWAY_WORDS = ("giveaway", "count from", "10 to 1", "10 to one", "winner", "exclamation mark",
                  "spin the wheel", "roll the wheel")
CAPTION_MODEL = "claude-opus-5-5"


# ----------------------------------------------------------------------------- helpers

def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def secrets() -> dict:
    if not SECRETS.exists():
        sys.exit(f"FEHLER: {SECRETS.name} fehlt. Siehe README -> Handy-Benachrichtigungen.")
    return load_toml(SECRETS)


def today() -> str:
    return f"{dt.date.today():%Y-%m-%d}"


# ----------------------------------------------------------------------------- telegram

def tg(method: str, fields: dict | None = None, file_field: str | None = None, file_path: Path | None = None):
    """Telegram-Bot-API aufrufen (multipart, nur Standardbibliothek)."""
    token = secrets()["telegram"]["token"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    if not file_path:  # ohne Datei: normales Formular
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields or {}).encode())
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data}")
        return data["result"]
    boundary = uuid.uuid4().hex
    body = bytearray()
    for k, v in (fields or {}).items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    if file_field and file_path:
        ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                 f"filename=\"{file_path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        body += file_path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def send_text(text: str):
    tg("sendMessage", {"chat_id": secrets()["telegram"]["chat_id"], "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": "true"})


def html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------------------------- clip pool

def vod_titles(vods: set[str]) -> dict:
    """VOD-Titel (fuer Spiel-Kontext der Captions), einmal per yt-dlp geholt und gecacht."""
    cache = load_json(TITLES, {})
    if vods - set(cache):
        try:
            sys.path.insert(0, str(ROOT))
            import clipper
            cfg = clipper.load_config(ROOT / "config.toml")
            for v in clipper.list_vods(cfg["channel"]["name"], 30):
                cache[v["id"]] = v["title"]
            save_json(TITLES, cache)
        except Exception as e:
            print(f"   (VOD-Titel nicht abrufbar: {e})")
    return cache


def clip_pool() -> list[dict]:
    """Alle noch nicht geposteten, nicht-Giveaway-Clips, neueste Lauf-Ordner zuerst, darin nach Score."""
    used = set(load_json(POSTED, []))
    pool = []
    for cj in sorted((ROOT / "output").glob("*_*/clips.json")):
        run_date, vod = cj.parent.name.split("_", 1)
        for r in load_json(cj, []):
            f = cj.parent / r["clip"]
            key = f"{cj.parent.name}/{r['clip']}"
            text = (r.get("transcript") or "").lower()
            if not f.exists() or key in used or any(w in text for w in GIVEAWAY_WORDS):
                continue
            pool.append({**r, "key": key, "path": f, "vod": vod, "run_date": run_date})
    # Clips aus dem heutigen Lauf zuerst, dann der Vorrat - jeweils bester Score zuerst
    pool.sort(key=lambda c: (c["run_date"] != today(), -c["score"]))
    return pool


# ----------------------------------------------------------------------------- captions

def _ask_api(prompt: str, key: str) -> str:
    """Claude API (eigener API-Key in secrets.toml)."""
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    msg = client.beta.messages.create(
        model=CAPTION_MODEL,
        max_tokens=2000,
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default", "output_config": {"effort": "low"}},
        messages=[{"role": "user", "content": prompt}],
    )
    if msg.stop_reason == "refusal":
        return ""
    return "".join(b.text for b in msg.content if b.type == "text")


def _ask_claude_code(prompt: str) -> str:
    """Claude Code im Hintergrund (laeuft ueber das Claude-Abo, kein API-Key noetig)."""
    exe = shutil.which("claude") or str(Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd")
    neutral = ROOT / "work" / "captions"  # neutraler Ordner: kein Projektkontext
    neutral.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([exe, "-p", "Follow the instructions in the input exactly.", "--output-format", "text"],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=neutral, timeout=180)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])
    return r.stdout


def write_caption(cfg: dict, clip: dict, title: str) -> str:
    """Hook + Hashtags von Claude; Kanal-Link und Pflicht-Hashtags setzt der Code selbst."""
    C = cfg["channel"]
    base_tags = list(C.get("hashtags", []))
    hook, extra = "", []
    prompt = (
        "Write a TikTok caption hook for a short Twitch clip of the streamer SkiiMaskTimmy (UK, FPS/zombie games).\n"
        f"Stream title: {title or 'unknown'}\n"
        f"What is said in the clip (auto-transcribed, may contain errors):\n{clip.get('transcript') or '-'}\n\n"
        "Rules: English, max 70 characters, casual TikTok tone (lowercase ok, 1-2 emojis ok), "
        "make people curious about the moment, no hashtags in the hook, no links, don't invent events the "
        "transcript doesn't support.\n"
        "Answer with exactly two lines and nothing else:\n"
        "HOOK: <the hook>\n"
        "TAGS: <2-3 hashtags for the game/topic, e.g. #deadanddamned #zombies>"
    )
    try:
        key = secrets().get("anthropic", {}).get("api_key")
        text = _ask_api(prompt, key) if key else _ask_claude_code(prompt)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        m = re.search(r"HOOK:\s*(.+)", text)
        hook = m.group(1) if m else next((l for l in lines if not l.startswith(("#", "TAGS"))), "")
        hook = re.sub(r"#\w+", "", hook).strip().strip('"').strip()
        t = re.search(r"TAGS:\s*(.+)", text)
        extra = re.findall(r"#\w+", t.group(1) if t else text)
    except Exception as e:
        print(f"   Caption per Claude fehlgeschlagen ({str(e)[:120]}) -> Vorlage")
    if not hook:
        hook = (clip.get("caption") or "").split("\n")[0] or "the reaction says it all"
    tags = list(dict.fromkeys(base_tags + [t.lower() for t in extra]))[:7]
    return f"{hook}\n\nLive on {C['handle_text']}\n{' '.join(tags)}"


# ----------------------------------------------------------------------------- phone copy

def phone_copy(src: Path, name: str) -> Path:
    """Kleinere Kopie (~8 Mbit/s, < 50 MB) fuer Telegram; Bild 1080x1920 und Ton bleiben."""
    PHONE.mkdir(parents=True, exist_ok=True)
    out = PHONE / f"{name}.mp4"
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-c:v", "h264_nvenc", "-b:v", "8M",
                        "-maxrate", "10M", "-bufsize", "16M", "-c:a", "copy", "-movflags", "+faststart", str(out)],
                       check=True)
        if out.stat().st_size > 48_000_000:
            out.unlink()
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-c:v", "h264_nvenc", "-b:v", "5M",
                            "-maxrate", "6M", "-bufsize", "10M", "-c:a", "copy", "-movflags", "+faststart",
                            str(out)], check=True)
    return out


# ----------------------------------------------------------------------------- commands

def cmd_setup(args):
    updates = tg("getUpdates")
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in updates if "message" in u}
    if not chats:
        sys.exit("Keine Nachricht gefunden. Schick deinem Bot in Telegram zuerst /start und versuch es nochmal.")
    chat_id, name = list(chats.items())[-1]
    text = SECRETS.read_text(encoding="utf-8")
    text = re.sub(r'chat_id\s*=\s*".*?"', f'chat_id = "{chat_id}"', text)
    SECRETS.write_text(text, encoding="utf-8")
    print(f"Chat-ID {chat_id} ({name}) in {SECRETS.name} gespeichert.")
    send_text("✅ Clipper ist verbunden. Ab morgen kommen hier um 9 Uhr deine Clips.")


def cmd_test(args):
    send_text("👋 Test vom Clipper - Telegram funktioniert.")
    print("Gesendet.")


def cmd_morning(args):
    cfg = load_toml(ROOT / "config.toml")
    N = cfg.get("notify", {})
    slots = N.get("slots", ["16:00", "18:00", "21:00"])
    count = int(N.get("clips", 5))
    plan_file = PLANS / f"{today()}.json"
    if plan_file.exists() and not args.force:
        print("Heute schon gesendet (--force zum Wiederholen).")
        return

    pool = clip_pool()[:count]
    if not pool:
        send_text("📭 Heute keine neuen Clips (kein neuer Stream und kein Vorrat mehr).")
        return
    titles = vod_titles({c["vod"] for c in pool})
    new = sum(c["run_date"] == today() for c in pool)

    send_text(f"☀️ <b>Clips für heute</b> ({new} neu aus dem letzten Stream, {len(pool) - new} aus dem Vorrat)\n\n"
              f"Bitte jetzt in TikTok <b>planen</b>: #1–#{min(len(slots), len(pool))} zu den angegebenen Zeiten, "
              f"der Rest ist Reserve. Jeden Clip vorher anschauen!\n"
              f"Tipp: Caption antippen = kopiert.\n\n"
              f"📊 Kurz checken: Hat ein älterer Clip <b>2'000+ Views</b>? → Analytics-Screenshot bei Whop hochladen.")
    plan = {"date": today(), "slots": []}
    used = load_json(POSTED, [])
    for i, c in enumerate(pool, 1):
        slot = slots[i - 1] if i <= len(slots) else None
        print(f"[{i}/{len(pool)}] {c['key']} -> {slot or 'Reserve'}")
        caption = write_caption(cfg, c, titles.get(c["vod"], ""))
        video = phone_copy(c["path"], f"{c['vod']}_{c['path'].stem}")
        head = f"<b>#{i} – {'⏰ ' + slot + ' Uhr' if slot else 'Reserve'}</b>  ({c['path'].parent.name}/{c['path'].name})"
        text = f"{head}\n\n<code>{html(caption)}</code>"
        tg("sendVideo", {"chat_id": secrets()["telegram"]["chat_id"], "caption": text[:1024], "parse_mode": "HTML",
                         "supports_streaming": "true", "width": "1080", "height": "1920"}, "video", video)
        if slot:
            plan["slots"].append({"time": slot, "nr": i, "clip": c["key"], "caption": caption, "reminded": False})
        used.append(c["key"])  # auch Reserve-Clips nicht nochmal vorschlagen
    save_json(POSTED, used)
    save_json(plan_file, plan)
    for f in PHONE.glob("*.mp4"):  # Handy-Kopien sind gesendet -> Platz sparen
        f.unlink(missing_ok=True)
    print("Fertig.")


def cmd_remind(args):
    plan_file = PLANS / f"{today()}.json"
    if not plan_file.exists():
        return
    plan = load_json(plan_file, {})
    now = dt.datetime.now().strftime("%H:%M")
    due = [s for s in plan.get("slots", []) if s["time"] <= now and not s["reminded"]]
    for s in due:
        send_text(f"🔔 <b>#{s['nr']} ist jetzt live</b> ({s['time']} Uhr)\n\n"
                  f"Innerhalb von 30 Min bei <b>Whop einreichen</b>:\n"
                  f"TikTok → Video → Teilen → Link kopieren → in der Kampagne einreichen.\n\n"
                  f"<i>{html(s['clip'])}</i>")
        s["reminded"] = True
    save_json(plan_file, plan)


def cmd_posted(args):
    p = Path(args.path).resolve()
    key = f"{p.parent.name}/{p.name}"
    used = load_json(POSTED, [])
    if key not in used:
        used.append(key)
        save_json(POSTED, used)
    print(f"Als gepostet markiert: {key}")


def main():
    ap = argparse.ArgumentParser(description="Telegram-Benachrichtigungen fuer den Clipper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("test")
    p = sub.add_parser("morning")
    p.add_argument("--force", action="store_true")
    sub.add_parser("remind")
    p = sub.add_parser("posted")
    p.add_argument("path")
    args = ap.parse_args()
    {"setup": cmd_setup, "test": cmd_test, "morning": cmd_morning, "remind": cmd_remind,
     "posted": cmd_posted}[args.cmd](args)


if __name__ == "__main__":
    main()
