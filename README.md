# Clipper Live – Twitch-Momente in 1–2 Min aufs Handy

Beobachtet grosse Twitch-Streamer, die **GTA VI** spielen, nimmt die letzten 8 Minuten jedes Streams in einem
Ringpuffer auf und baut bei einem Highlight sofort einen fertigen 9:16-Clip (Facecam oben, Gameplay unten,
englische Untertitel). Den schickt es per Telegram mit Caption-Vorschlag und Link zum Moment.

Du machst nur noch: **Clip anschauen → posten → einreichen.**

```
Helix "Get Streams" (60 s) ──► live + GTA VI? ──► streamlink 720p ─► ffmpeg ─► buffer/<login>/*.ts (8 Min)
                                                 └► Chat (IRC, anonym)
Signale:  Twitch-Clips von verschiedenen Leuten (Get Clips, 30 s)  │  Chat-Spike / Lach-Emotes (IRC)
Trigger:  STARK allein (Chat ≥5x, ≥25 Lacher/10 s, Clips von ≥4 Leuten)
          oder SCHWACH + SCHWACH: Chat (≥3x) UND Clips (≥2 Leute) zum gleichen Moment (±2 Min)
          └── Cooldown 5 Min pro Streamer ──► Moment = lautester Punkt im Ton ──► 14 s davor + 6 s danach (20 s)
          ──► Whisper-Untertitel ─► 9:16 ─► < 50 MB ─► Telegram (Video, Caption, VOD-Link, Thumbnail)
```

## 1. Einrichtung (einmalig)

1. Pakete: `pip install -r requirements.txt` (ffmpeg + CUDA-Pakete sind schon da)
2. **Twitch-App** anlegen: <https://dev.twitch.tv/console> → *Register Your Application*
   (Name beliebig, OAuth Redirect `http://localhost`, Kategorie *Application Integration*, Client-Typ *Confidential*).
   Client-ID und *New Secret* in die `.env` eintragen.
3. `.env` prüfen – Telegram ist schon eingetragen. Optional `ANTHROPIC_API_KEY` für Claude-Haiku-Captions
   (ohne Key: Caption aus Vorlage).
4. Test: `python -m live check` → zeigt, ob alles gesetzt ist und wer gerade live ist.

## 2. Starten

```
run_live.bat                         # Dauerbetrieb, startet nach Absturz selbst neu
python -m live run                   # direkt
python -m live run --any-game --only lacy --test-trigger 90    # Test: egal welches Spiel, 1 Test-Clip nach 90 s
python -m live check --top           # wer ist live + aktuelle Top-Streams
```

Die Windows-Aufgabe **„Clipper Live“** startet `run_live.bat` bei der Anmeldung. Solange es läuft, geht der PC
nicht in den Standby. Protokoll: `logs/live.log` (Aufnahme-Fehler: `logs/rec_<login>.log`).

## 3. Telegram

Jeder Clip kommt als Video mit: Streamer, Trigger-Grund, Uhrzeit, Latenz, **VOD-Link mit Zeitstempel**
(falls der Streamer VODs hat) bzw. Twitch-Clip-Link, Transkript-Anfang und **Caption zum Kopieren**.

Die Caption besteht aus einer Hook-Zeile. Danach werden **immer** automatisch angehängt:
`<caption_name> live on twitch.tv/<login>` + `campaign_tags` + `hashtags` + `extra_hashtags`.

Den Hook liefert ein austauschbarer Generator (`caption.provider` in `settings.yaml`, Code in `live/caption.py`):

| provider | Hook |
|---|---|
| `transcript` (Standard) | erster prägnanter Satz aus dem Whisper-Transkript, max. ~80 Zeichen, Schimpfwörter zensiert |
| `ollama` | lokales Modell (`ollama pull llama3.2`, Einstellungen unter `caption.ollama`) |
| `anthropic` | Claude, braucht `ANTHROPIC_API_KEY` |

**Banner:** Zwischen Facecam und Gameplay steht ein kurzer Satz, was im Clip passiert ("Rage tries to kill him but
fails"). Den schreibt ein lokales Bild-Sprachmodell (Ollama, `gemma3:4b`) aus 3 Standbildern + Transkript.
Einmalig einrichten: `winget install Ollama.Ollama` und `ollama pull gemma3:4b`. Ohne Ollama kommt der Clip ohne Banner.

**TikTok-Sound:** Jede Telegram-Nachricht schlägt einen passenden Sound + Alternative vor (Stimmung aus Banner,
Transkript und Trigger). Die Liste steht in `sounds.yaml` – eigene/aktuelle Trend-Sounds dort eintragen.
Den Sound fügst du in TikTok selbst hinzu (leise, ~15 %).

Fällt der Generator aus oder liefert nichts, geht es weiter mit `transcript`, danach mit einer festen Vorlage.
In Telegram steht hinter „Caption“, woher der Hook kam.

**Facecam:** Beim ersten Clip eines Streamers ohne `facecam` in `streamers.yaml` kommt ein Standbild mit der
erkannten Box. Bis du bestätigst, kommen die Clips im Vollbild-Layout.

| Befehl | Wirkung |
|---|---|
| `/ok` (als Reply auf das Bild) | Box speichern (`facecam_cache.json`), ab jetzt Facecam-Layout |
| `/redo` | Box verwerfen, neu suchen (anderer Kandidat) |
| `/box lacy 1440 20 460 258` | Box selbst setzen (x y w h im 1920×1080-Raster) |
| `/status` | Wer wird getrackt, Puffer, Chat-Aktivität |

Eine Box in `streamers.yaml` (`facecam: {x, y, w, h}`) hat immer Vorrang.

### Portfolio (optional)

Jeder gesendete Clip wird an `silvanbrors.ch` gemeldet, nur ID und Zeitpunkt, für die Zahl «Clips heute».
Dafür in `.env`:

```
PORTFOLIO_API_URL=https://api.silvanbrors.ch
PORTFOLIO_API_KEY=<CLIPPER_API_KEY aus dem Portfolio-Backend>
```

Ohne diese zwei Werte passiert nichts. Ist das Portfolio nicht erreichbar, steht nur eine Warnung im Log.

## 4. Einstellungen

- **Streamer:** `streamers.yaml` (neuen Block kopieren)
- **Schwellen, Puffer, Clip-Länge, Untertitel, Bitrate:** `settings.yaml` (kommentiert)
- Zu viele Chat-Trigger → `trigger.chat.spike_ratio` / `min_msgs` hoch; zu wenige → runter.
- **Bandbreite:** Jede Aufnahme braucht ca. 3–5 Mbit/s Download. `buffer.max_streams` (eingestellt: 4) begrenzt die
  gleichzeitigen Aufnahmen. Kampagnen-Streamer haben Vorrang, danach gilt die Reihenfolge in `streamers.yaml`.
  **Schlaf-/AFK-Streams** werden übersprungen: Jede Minute wird der Ton geprüft (Anteil Sprache + Pegel).
  Ist es zweimal hintereinander still, rückt der nächste aktive Streamer nach. Inaktive werden alle 10 Min
  kurz über den Audio-Stream geprüft und kommen zurück, sobald sie wieder reden (`activity` in `settings.yaml`).
  Eine volle Leitung bremst den Upload der Clips stark und führt zu Lücken im Puffer. Mit LAN-Kabel statt WLAN
  läuft es deutlich besser.

Fertige Clips liegen in `clips/<zeit>_<login>/` (`clip.mp4`, `caption.txt`) und werden nach 2 Tagen gelöscht.

## Alte Pipeline (SkiiMaskTimmy, VOD-basiert)

Liegt komplett in `archive/` und im Git-Branch `old-timmy`. Die Windows-Aufgaben dazu sind gelöscht. Die Jobs
bei cron-job.org musst du dort selbst löschen.
