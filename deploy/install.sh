#!/usr/bin/env bash
# Installs (or updates) ScoreBot as an always-on systemd service on Ubuntu.
#
#   curl -fsSL https://raw.githubusercontent.com/steelballsdragon/Super/main/deploy/install.sh | sudo bash
#
# Run it again at any time to update the bot to the latest code; the saved
# token and followed leagues are kept.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/steelballsdragon/Super.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/scorebot
ENV_FILE=/etc/scorebot.env
SERVICE=scorebot

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo (… | sudo bash)"
command -v apt-get >/dev/null || die "this script needs Ubuntu (choose an Ubuntu image when creating the server)"

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv >/dev/null

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || die "Python 3.10+ is required; use Ubuntu 22.04 or newer"

id -u scorebot >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin scorebot

say "Downloading ScoreBot"
if [ -d "$APP_DIR/.git" ]; then
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

say "Installing Python packages"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R scorebot:scorebot "$APP_DIR"

if [ ! -s "$ENV_FILE" ] || ! grep -q '^DISCORD_TOKEN=.\+' "$ENV_FILE"; then
  say "Discord bot token"
  echo "Paste your bot token (from the Discord Developer Portal → Bot page)."
  echo "It won't be shown as you paste. Press Enter when done."
  token=""
  while [ -z "$token" ]; do
    read -rs -p "Token: " token </dev/tty
    echo
  done
  umask 077
  cat > "$ENV_FILE" <<ENV
DISCORD_TOKEN=$token
DATA_FILE=/var/lib/scorebot/subscriptions.json
POLL_INTERVAL=30
ENV
  chmod 600 "$ENV_FILE"
fi

say "Setting up the always-on service"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=ScoreBot Discord live scores
Wants=network-online.target
After=network-online.target

[Service]
User=scorebot
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python main.py
StateDirectory=scorebot
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"

sleep 5
if systemctl is-active -q "$SERVICE"; then
  say "ScoreBot is running and will start automatically after reboots."
else
  journalctl -u "$SERVICE" -n 20 --no-pager || true
  die "ScoreBot didn't start — see the log above"
fi
cat <<'HELP'

Useful commands:
  sudo journalctl -u scorebot -f          # live log (Ctrl+C to exit)
  sudo systemctl restart scorebot         # restart
  sudo nano /etc/scorebot.env             # change the token, then restart
  (re-run the install command to update to the latest code)
HELP
