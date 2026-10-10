#!/usr/bin/env bash
# FBI-BOT Ubuntu installer: system deps + venv + systemd service.
# Usage:
#   sudo bash install.sh                 # /opt/fbi-mumble-bot, user mumblebot
#   sudo bash install.sh /srv/fbi-bot    # custom dir
set -euo pipefail

APP_DIR="${1:-/opt/fbi-mumble-bot}"
APP_USER="${APP_USER:-mumblebot}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing OS packages (libopus, ffmpeg)..."
apt-get update
apt-get install -y python3 python3-venv python3-pip libopus0 libopus-dev ffmpeg

echo "==> Creating user: $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Copying bot to $APP_DIR"
mkdir -p "$APP_DIR"
cp "$SRC_DIR/bot.py" "$SRC_DIR/requirements.txt" "$SRC_DIR/bot.conf.example" "$APP_DIR/"
touch "$APP_DIR/__keep__" 2>/dev/null || true
rm -f "$APP_DIR/__keep__"

echo "==> Creating venv + pip install"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/bot.conf" ]; then
  cp "$APP_DIR/bot.conf.example" "$APP_DIR/bot.conf"
  echo "==> Created $APP_DIR/bot.conf - EDIT IT (server, channels) before starting"
else
  echo "==> Keeping existing $APP_DIR/bot.conf"
fi

mkdir -p "$APP_DIR/recordings"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Installing systemd unit"
sed -e "s|^User=.*|User=$APP_USER|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$APP_DIR|" \
    -e "s|^ExecStart=.*|ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py -c $APP_DIR/bot.conf|" \
    "$SRC_DIR/fbi-bot.service" > /etc/systemd/system/fbi-bot.service

systemctl daemon-reload
systemctl enable fbi-bot.service

echo ""
echo "NEXT STEPS:"
echo "  1. nano $APP_DIR/bot.conf            # set server / port / channels"
echo "  2. sudo -u $APP_USER $APP_DIR/venv/bin/python $APP_DIR/bot.py -c $APP_DIR/bot.conf --list-channels"
echo "     # verifies connection + prints exact channel names"
echo "  3. systemctl start fbi-bot && journalctl -u fbi-bot -f"
