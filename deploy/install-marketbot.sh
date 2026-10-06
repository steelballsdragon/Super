#!/usr/bin/env bash
# Installs (or updates) the stocks & crypto market bot as an always-on systemd service on Ubuntu.
#
#   curl -fsSL https://raw.githubusercontent.com/steelballsdragon/Super/main/deploy/install-marketbot.sh | sudo bash
#
# A timer checks GitHub every 5 minutes and restarts the bot when anything changed (or use /update in
# Discord). Running this script again updates right away; the saved token and channels are kept.
#
# To install without prompts (e.g. from a cloud server's startup script), set the token first:
#
#   export MARKET_DISCORD_TOKEN=your-token
#   export ANTHROPIC_API_KEY=your-key   # optional: Claude reads the news too
#   curl -fsSL https://raw.githubusercontent.com/steelballsdragon/Super/main/deploy/install-marketbot.sh | bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/steelballsdragon/Super.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/marketbot
ENV_FILE=/etc/marketbot.env
SERVICE=marketbot

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo (… | sudo bash)"
command -v apt-get >/dev/null || die "this script needs Ubuntu (choose an Ubuntu image when creating the server)"

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=600 update -qq
apt-get -o DPkg::Lock::Timeout=600 install -y -qq git python3 python3-venv tzdata >/dev/null

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || die "Python 3.10+ is required; use Ubuntu 22.04 or newer"

id -u "$SERVICE" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE"

say "Downloading the market bot"
if [ -d "$APP_DIR/.git" ]; then
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -c safe.directory="$APP_DIR" -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

say "Installing Python packages (numpy and matplotlib take a minute)"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
# The bot only reads its code (its data lives in /var/lib/marketbot), and root runs the update scripts in
# here, so the bot's user must not be able to change it.
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"

token="${MARKET_DISCORD_TOKEN:-}"
ai_key="${ANTHROPIC_API_KEY:-}"
if [ -z "$token" ] && { [ ! -s "$ENV_FILE" ] || ! grep -q '^MARKET_DISCORD_TOKEN=.\+' "$ENV_FILE"; }; then
  say "Discord bot token"
  echo "Paste the market bot's token (Discord Developer Portal → your application → Bot → Reset Token)."
  echo "It won't be shown as you paste. Press Enter when done."
  while [ -z "$token" ]; do
    read -rs -p "Token: " token </dev/tty
    echo
  done
  echo
  echo "Optional: an Anthropic API key lets Claude read the important headlines (better news calls)."
  echo "Press Enter to skip; you can add ANTHROPIC_API_KEY to $ENV_FILE later."
  read -rs -p "Anthropic API key: " ai_key </dev/tty || true
  echo
fi
if [ -n "$token" ]; then
  if [ -z "$ai_key" ] && [ -s "$ENV_FILE" ]; then
    ai_key="$(sed -n 's/^ANTHROPIC_API_KEY=//p' "$ENV_FILE" | head -n1)"
  fi
  umask 077
  {
    echo "MARKET_DISCORD_TOKEN=$token"
    echo "MARKET_DATA_DIR=/var/lib/marketbot"
    if [ -n "$ai_key" ]; then echo "ANTHROPIC_API_KEY=$ai_key"; fi
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

say "Setting up the always-on service"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Market bot (stocks and crypto) for Discord
Wants=network-online.target
After=network-online.target

[Service]
User=$SERVICE
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=MPLCONFIGDIR=/var/lib/marketbot/matplotlib
ExecStart=$APP_DIR/.venv/bin/python -m marketbot
StateDirectory=marketbot
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

bash "$APP_DIR/deploy/marketbot-system-setup.sh"

systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"

sleep 5
if systemctl is-active -q "$SERVICE"; then
  say "The market bot is running and will start automatically after reboots."
else
  journalctl -u "$SERVICE" -n 20 --no-pager || true
  die "The market bot didn't start — see the log above"
fi
cat <<'HELP'

Next: in Discord, run /setup in your server to create the stocks, crypto, news and research channels.

Useful commands:
  sudo journalctl -u marketbot -f          # live log (Ctrl+C to exit)
  sudo systemctl restart marketbot         # restart
  sudo nano /etc/marketbot.env             # change the token or add ANTHROPIC_API_KEY, then restart
  (updates install automatically within 5 minutes, or right away with /update in Discord)
HELP
