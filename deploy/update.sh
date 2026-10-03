#!/usr/bin/env bash
# Pulls the latest ScoreBot code and restarts the bot if anything changed.
# Run every 5 minutes by scorebot-update.timer, or right away by /update.
set -euo pipefail

APP_DIR=/opt/scorebot
git_() { git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"; }

# Root runs the scripts in here, so only root may change them. Older installs
# gave the bot's user ownership; take it back before running anything else.
if [ -n "$(find "$APP_DIR" \( ! -user root -o ! -type l -perm /022 \) -print -quit)" ]; then
  chown -R root:root "$APP_DIR"
  chmod -R go-w "$APP_DIR"
fi

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
systemctl restart scorebot
