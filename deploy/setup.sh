#!/usr/bin/env bash
# Einrichtung auf einem frischen Ubuntu-Server (Oracle Cloud Free, ARM). Mehrfach ausfuehrbar.
#   ssh ubuntu@<IP>
#   git clone https://github.com/sibro73/stream-clipper-script.git clipper && cd clipper && bash deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"

echo "== Pakete"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg fonts-dejavu-core git curl

echo "== Python-Umgebung"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "== Ollama (fuer Hook/Sound-Stimmung; ohne laeuft der Clipper trotzdem)"
if ! command -v ollama >/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama
ollama pull gemma3:4b

echo "== Server-Einstellungen"
[ -f settings.local.yaml ] || cp deploy/settings.server.yaml settings.local.yaml
mkdir -p logs buffer clips

if [ ! -f .env ]; then
  echo
  echo "!! .env fehlt. Vom PC hochladen:  scp .env facecam_cache.json ubuntu@<IP>:$DIR/"
  echo "   Danach dieses Skript nochmal starten."
  exit 1
fi

echo "== Check"
.venv/bin/python -m live check || true

echo "== Dienst"
sed "s#/home/ubuntu/clipper#$DIR#g; s#User=ubuntu#User=$(whoami)#" deploy/clipper.service \
  | sudo tee /etc/systemd/system/clipper.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now clipper
sudo systemctl restart clipper
echo
echo "Fertig. Log:  journalctl -u clipper -f    (oder tail -f $DIR/logs/live.log)"
