#!/usr/bin/env bash
# FBI-BOT Ubuntu installer: system deps + venv + systemd service.
# Usage:
#   sudo bash install.sh                 # /opt/fbi-mumble-bot, user mumblebot
#   sudo bash install.sh /srv/fbi-bot    # custom dir
set -euo pipefail

APP_DIR="${1:-/opt/fbi-mumble-bot}"
APP_USER="${APP_USER:-mumblebot}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# Fail fast with a clear message if run without the rest of the project.
for f in bot.py requirements.txt bot.conf.example fbi-bot.service; do
  if [ ! -f "$SRC_DIR/$f" ]; then
    echo "ERROR: $f not found in $SRC_DIR" >&2
    echo "Run this script from inside the full project folder, e.g.:" >&2
    echo "  git clone https://github.com/DefconUnicorn/fbi-mumble-bot" >&2
    echo "  cd fbi-mumble-bot && sudo bash install.sh" >&2
    exit 1
  fi
done

echo "==> Installing OS packages (libopus, ffmpeg)..."
apt-get update
# build-essential + python3-dev: protobuf may need a source build on newer
# Pythons (no 3.20.x wheel); libopus-dev for good measure alongside libopus0.
apt-get install -y python3 python3-venv python3-pip python3-dev build-essential libopus0 libopus-dev ffmpeg

echo "==> Creating user: $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Copying bot to $APP_DIR"
mkdir -p "$APP_DIR"
cp "$SRC_DIR/bot.py" "$SRC_DIR/requirements.txt" "$SRC_DIR/bot.conf.example" "$SRC_DIR/fbi-bot.service" "$APP_DIR/"
[ -f "$SRC_DIR/README.md" ] && cp "$SRC_DIR/README.md" "$APP_DIR/"
touch "$APP_DIR/__keep__" 2>/dev/null || true
rm -f "$APP_DIR/__keep__"

echo "==> Creating venv + pip install"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
# pymumble 1.6.1's sdist carries a stale protobuf==3.12.2 pin that breaks pip
# resolution (upstream itself uses protobuf 3.20.3), so install it ignoring
# deps, then install our pinned deps on top.
"$APP_DIR/venv/bin/pip" install --no-deps pymumble==1.6.1
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"
echo "==> Verifying Python deps"
"$APP_DIR/venv/bin/python" -c "import pymumble_py3, google.protobuf; print('deps OK, protobuf', google.protobuf.__version__)"

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
