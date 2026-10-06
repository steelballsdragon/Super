#!/usr/bin/env bash
# Pulls the latest market bot code and restarts it if anything changed.
# Run every 5 minutes by marketbot-update.timer, or right away by /update.
set -euo pipefail

APP_DIR=/opt/marketbot
git_() { git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"; }

# Root runs the scripts in here, so only root may change them.
if [ -n "$(find "$APP_DIR" \( ! -user root -o ! -type l -perm /022 \) -print -quit)" ]; then
  chown -R root:root "$APP_DIR"
  chmod -R go-w "$APP_DIR"
fi

# Keep the timer and /update permission current, even when the code hasn't changed.
bash "$APP_DIR/deploy/marketbot-system-setup.sh" || echo "marketbot-system-setup.sh failed; continuing" >&2

branch="$(git_ rev-parse --abbrev-ref HEAD)"
git_ fetch -q origin "$branch"
if [ "$(git_ rev-parse HEAD)" = "$(git_ rev-parse "origin/$branch")" ]; then
  exit 0
fi

echo "Updating the market bot to $(git_ rev-parse --short "origin/$branch")"
git_ reset -q --hard "origin/$branch"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
systemctl restart marketbot
