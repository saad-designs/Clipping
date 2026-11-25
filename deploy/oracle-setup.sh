#!/usr/bin/env bash
# Install or update the Clipping watcher on an Oracle Cloud Ubuntu VM (ARM or x86).
#   git clone <your repo> && cd Clipping && sudo bash deploy/oracle-setup.sh
# Safe to run again after `git pull`: code and packages update, your settings and data stay.
set -euo pipefail

APP=/opt/clipping
USER_NAME=clipper
SRC="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo."; exit 1; fi

echo "==> System packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg python3-venv python3-pip rsync >/dev/null

echo "==> Service user"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP" --shell /usr/sbin/nologin "$USER_NAME"

echo "==> Code"
mkdir -p "$APP"
rsync -a --delete --exclude data --exclude venv --exclude settings.yaml --exclude campaigns.yaml \
  --exclude .git --exclude tests --exclude studio "$SRC"/ "$APP"/
[ -f "$APP/settings.yaml" ] || cp "$APP/settings.example.yaml" "$APP/settings.yaml"
[ -f "$APP/campaigns.yaml" ] || cp "$APP/campaigns.example.yaml" "$APP/campaigns.yaml"

echo "==> Python packages"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements-watcher.txt"
chown -R "$USER_NAME:$USER_NAME" "$APP"

echo "==> systemd service"
install -m 644 "$SRC/deploy/clipping-watcher.service" /etc/systemd/system/clipping-watcher.service
systemctl daemon-reload
systemctl enable clipping-watcher >/dev/null
systemctl restart clipping-watcher
sleep 3
systemctl --no-pager --lines=5 status clipping-watcher || true

TOKEN_FILE="$APP/data/api_token.txt"
for _ in $(seq 1 10); do [ -f "$TOKEN_FILE" ] && break; sleep 1; done

cat <<MSG

Done. Next:
  1. Edit your campaigns:   sudo nano $APP/campaigns.yaml
     and Twitch keys:        sudo nano $APP/settings.yaml
     then:                   sudo systemctl restart clipping-watcher
  2. Private access with Tailscale (no ports opened to the internet):
       curl -fsSL https://tailscale.com/install.sh | sh
       sudo tailscale up
     Install Tailscale on your laptop/phone too and sign in to the same account.
  3. Dashboard:  http://<VM tailscale IP>:8787/?token=$( [ -f "$TOKEN_FILE" ] && cat "$TOKEN_FILE" || echo '<see '"$TOKEN_FILE"'>')
     (find the IP with:  tailscale ip -4)
  Logs:          journalctl -u clipping-watcher -f
MSG
