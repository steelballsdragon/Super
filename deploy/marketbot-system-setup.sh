#!/usr/bin/env bash
# Installs the market bot's update timer and the permission behind /update.
# Run as root by install-marketbot.sh, and on every check by marketbot-update.sh so existing servers pick
# up changes here. Only touches files that differ.
set -euo pipefail

APP_DIR=/opt/marketbot
SERVICE=marketbot
changed=0

install_file() {  # install_file <path> <mode>, content on stdin
  local tmp
  tmp="$(mktemp)"
  cat > "$tmp"
  if ! cmp -s "$tmp" "$1"; then
    install -m "$2" "$tmp" "$1"
    changed=1
  fi
  rm -f "$tmp"
}

install_file "/etc/systemd/system/$SERVICE-update.service" 0644 <<UNIT
[Unit]
Description=Update the market bot to the latest code
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash $APP_DIR/deploy/marketbot-update.sh
UNIT

install_file "/etc/systemd/system/$SERVICE-update.timer" 0644 <<UNIT
[Unit]
Description=Check for market bot updates every 5 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
RandomizedDelaySec=30s

[Install]
WantedBy=timers.target
UNIT

# Lets the bot's /update command start an update check (marketbot-update.service), and nothing else.
rule="$SERVICE ALL=(root) NOPASSWD: $(command -v systemctl) start --no-block $SERVICE-update.service"
tmp_rule="$(mktemp)"
echo "$rule" > "$tmp_rule"
if visudo -cqf "$tmp_rule"; then
  install_file "/etc/sudoers.d/$SERVICE-update" 0440 < "$tmp_rule"
else
  echo "Skipping the /update permission: sudoers rule failed validation" >&2
fi
rm -f "$tmp_rule"

if [ "$changed" = 1 ]; then
  systemctl daemon-reload
  systemctl enable -q "$SERVICE-update.timer"
  systemctl restart "$SERVICE-update.timer"
  echo "Updated the market bot's update timer and permissions"
fi
