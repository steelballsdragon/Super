#!/usr/bin/env bash
# Pulls the latest ScoreBot code and restarts the bot if anything changed.
# Run every 5 minutes by scorebot-update.timer, or right away by /update.
set -euo pipefail

APP_DIR=/opt/scorebot
git_() { git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"; }

# Keep the timer and /update permission current, even when the code hasn't changed.
bash "$APP_DIR/deploy/system-setup.sh" || echo "system-setup.sh failed; continuing" >&2

branch="$(git_ rev-parse --abbrev-ref HEAD)"
git_ fetch -q origin "$branch"
if [ "$(git_ rev-parse HEAD)" = "$(git_ rev-parse "origin/$branch")" ]; then
  exit 0
fi

echo "Updating ScoreBot to $(git_ rev-parse --short "origin/$branch")"
git_ reset -q --hard "origin/$branch"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R scorebot:scorebot "$APP_DIR"
systemctl restart scorebot
