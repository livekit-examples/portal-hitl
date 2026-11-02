#!/usr/bin/env bash
# Rsync portal-rebot to the robot host, then print follow-up steps.
#
# Usage:
#   ./scripts/deploy_to_robot.sh binh@the-mini-anton
#   ./scripts/deploy_to_robot.sh binh@the-mini-anton '~/code/portal-rebot'
#
# The destination defaults to ~/workspace/portal-rebot and is the full
# path, so a second copy under a different name is just a second argument.
# Quote a leading ~ when you pass one: unquoted, your own shell expands it
# to YOUR home before the script sees it, and that path is then used on
# the robot. Quoted, it reaches the robot and expands there.
#
# Both arguments are given here, never read from `.env` -- which machine
# you are pushing to is a per-invocation choice, not deployment config,
# and one stale line in `.env` silently sending a build to the wrong arm
# is not a failure mode worth keeping. `.env` itself IS synced, so the
# robot picks up LIVEKIT_* / B601_* without re-entry; per-machine
# overrides go in `.env.local`, which is not synced.
set -euo pipefail

REMOTE="${1:-}"
DEST="${2:-~/workspace/portal-rebot}"

if [[ -z "$REMOTE" ]]; then
    echo "usage: $(basename "$0") user@host [remote-path]" >&2
    echo "       remote-path defaults to ~/workspace/portal-rebot" >&2
    exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "rsync not found on this machine" >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")/.." && pwd)"
IGNORE_FILE="$HERE/scripts/deploy.rsyncignore"

echo "[deploy] remote: $REMOTE:$DEST"
echo "[deploy] ensuring remote directory exists ..."
# Unquoted on the remote side so a leading ~ expands there. rsync below
# expands it the same way; quoting only here would create a literal "~"
# directory in the remote home.
ssh "$REMOTE" "mkdir -p $DEST"

echo "[deploy] syncing portal-rebot/ ..."
rsync -azP --delete --exclude-from="$IGNORE_FILE" "$HERE/" "$REMOTE:$DEST/"

cat <<EOF

[deploy] done. On the robot:

  cd $DEST
  uv sync

  # First-time calibration walks through ranges of motion when the robot
  # connects with no saved file. Saved files live under
  # ~/.cache/huggingface/lerobot/calibration/ keyed by B601_ID.

  uv run robot

EOF
