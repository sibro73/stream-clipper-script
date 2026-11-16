# Clipper – Twitch-VODs → TikTok-Clips (SkiiMaskTimmy-Kampagne)

Lädt automatisch die neuesten Twitch-VODs, findet die besten Momente (Chat-Spikes + Lautstärke),
schneidet sie in 9:16 (Facecam oben, Gameplay unten), setzt den Höhepunkt als Hook an den Anfang,
brennt Untertitel, Twitch-Overlay und `twitch.tv/skiimasktimmy` ein und schreibt passende Captions.

Du machst nur noch: **Clips anschauen → posten → bei Whop einreichen.**

---

## 1. Installation (einmalig, ca. 15 Minuten)

Python 3.11 oder neuer wird benötigt (`python --version`).

1. **ffmpeg** installieren (in PowerShell oder cmd):
   ```
   winget install Gyan.FFmpeg
   ```
   Danach das Terminal **schliessen und neu öffnen**. Test: `ffmpeg -version`

2. Im Ordner `clipper` die Python-Pakete installieren:
   ```
   pip install -r requirements.txt
   ```

3. **GPU für Untertitel** (deine RTX A2000, macht es viel schneller):
   ```
   pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
   ```
   Klappt das nicht, läuft Whisper automatisch auf der CPU (langsamer, aber funktioniert).

4. **TwitchDownloaderCLI** (für Chat-Spikes, stark empfohlen):
   - https://github.com/lay295/TwitchDownloader/releases → `TwitchDownloaderCLI-…-Windows-x64.zip`
   - Entpacken und `TwitchDownloaderCLI.exe` in den Ordner `clipper/tools/` legen.
   - Ohne das Tool erkennt der Clipper Highlights nur über die Lautstärke.

5. **Twitch-Overlay** aus der Whop-Kampagne herunterladen und als
   `clipper/assets/twitch_overlay.png` speichern (die Datei im Ordner ist nur ein Platzhalter).

## 2. Facecam einstellen (einmalig)

Die Position von Timmys Facecam muss in `config.toml` stehen.

```
python clipper.py frame
```
→ speichert `frame_grid.png` (Standbild aus dem neuesten VOD mit 100-px-Raster).
Lies ab, wo die Facecam liegt (x, y = obere linke Ecke, w = Breite, h = Höhe)
und trag es in `config.toml` unter `[layout] facecam` ein. (Oder schick mir das Bild.)

```
python clipper.py layout
```
→ `layout_preview.png` zeigt, wie der 9:16-Clip aussieht. So lange anpassen, bis es passt.

## 3. Clips machen (täglich)

```
python clipper.py list          # neueste VODs anzeigen
python clipper.py run           # neuestes, noch nicht verarbeitetes VOD clippen
python clipper.py run --vod 2612345678 --clips 10    # bestimmtes VOD, 10 Clips
```
Oder einfach `run.bat` doppelklicken.

```
python clipper.py run --all     # alle noch offenen VODs nacheinander clippen
```

**Automatisch:** Die Windows-Aufgabe „Clipper SkiiMaskTimmy taeglich“ startet jeden Tag um 6:00 `run_daily.bat`
(= `run --all`). Protokoll: `logs\daily.log`.
**Nachts den Laptop in den Standby („Energie sparen“) statt herunterfahren und ans Netzteil hängen** – dann weckt
Windows ihn um 6:00 bzw. 9:00 selbst auf, und die Skripte halten ihn wach, bis sie fertig sind.
Heruntergefahren oder auf Akku wird er nicht geweckt.
Ändern/abschalten: Aufgabenplanung öffnen → Aufgabe suchen → Eigenschaften bzw. Deaktivieren.

**Aufs Handy (Telegram, `notify.py`):** Direkt nach dem Clip-Lauf (ca. 6:30) kommen die Top-5-Clips mit fertiger Caption (von Claude Code
über dein Claude-Abo, oder per API-Key falls in `secrets.toml` eingetragen)
und Post-Zeit (#1–#3 um 16/18/21 Uhr, Rest Reserve). Kurz nach jeder Post-Zeit kommt die Erinnerung,
innerhalb von 30 Min bei Whop einzureichen. Zugangsdaten in `secrets.toml` (nicht weitergeben).
Bereits gepostete Clips stehen in `posted.json` (von Hand: `python notify.py posted <pfad>`).
Die Aufgabe „Clipper Telegram Morgen“ um 9:00 ist nur Reserve: Sie schickt nichts, wenn die Clips heute schon kamen.

Die Whop-Erinnerungen (16:02 / 18:02 / 21:02) kommen von **cron-job.org**, damit sie auch bei
ausgeschaltetem PC ankommen. Die fertigen Aufrufe stehen in `cronjob_urls.txt` (lokal, enthält den Bot-Token,
nicht auf GitHub). Die lokale Aufgabe „Clipper Telegram Whop-Erinnerung“ ist deshalb deaktiviert.
Wenn du in `config.toml` `notify.slots` änderst, die Zeiten bei cron-job.org mit anpassen.

Clips ohne durchgehend sichtbare Facecam (Starting Soon, Just Chatting mit Vollbild-Kamera …) werden
automatisch übersprungen und durch den nächstbesten Moment ersetzt.

Ergebnis: `output/<datum>_<vod>/`
- `clip_01.mp4`, `clip_02.mp4`, … (1080×1920, fertig zum Posten)
- `captions.txt` – Caption-Vorschlag + was im Clip gesagt wird

Testen ohne Twitch: `python clipper.py run --file irgendein_video.mp4`

## 4. Vor dem Posten (Kampagnen-Regeln)

- [ ] Clip angeschaut: ist es wirklich ein guter Moment? Schlechte löschen.
- [ ] Facecam sichtbar, Twitch-Overlay + `twitch.tv/skiimasktimmy` im Bild
- [ ] Caption enthält `twitch.tv/skiimasktimmy` + passende Hashtags, auf Englisch
- [ ] Likes und Kommentare **an**, keine Collabs
- [ ] Bei Whop einreichen; ab 2'000 Views Bildschirmaufnahme der **Video-Analytics** schicken
- [ ] Clips **von Hand** posten (keine Bots / kein Auto-Upload)

## Einstellungen (config.toml)

| Was | Wo |
|---|---|
| Anzahl Clips pro VOD | `detect.clips_per_vod` |
| Mehr / weniger Clips finden | `detect.min_score` (tiefer = mehr) |
| Clip-Länge | `detect.pre` + `detect.post` (+ ca. 2.5 s Hook) |
| Hook aus | `hook.enabled = false` |
| Grösse der Facecam oben | `layout.top_h` |
| Untertitel-Position / -Grösse | `subtitles.margin_v` / `subtitles.size` |
| Genauere Untertitel | `subtitles.model = "medium"` |
| Schimpfwörter in Untertiteln maskieren | `subtitles.censor` |
| Facecam-Positionen / Szenen-Prüfung | `layout.facecam`, `layout.facecam_alt`, `layout.frame_template` |
| Game-Hashtag | `channel.game_hashtag` |

Für einen anderen Streamer: `channel.name`, `channel.handle_text`, `hashtags`, Facecam-Position und Overlay anpassen.
