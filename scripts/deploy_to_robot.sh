#!/usr/bin/env bash
# Usage: ./scripts/deploy_to_robot.sh user@host ['~/workspace/portal-hitl']
# Syncs .env; keeps the remote .env.local and data/training directories.
set -euo pipefail

REMOTE="${1:-}"
DEST="${2:-~/workspace/portal-hitl}"

if [[ -z "$REMOTE" ]]; then
    echo "usage: $(basename "$0") user@host [remote-path]" >&2
    echo "       remote-path defaults to ~/workspace/portal-hitl" >&2
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

echo "[deploy] syncing portal-hitl/ ..."
rsync -azP --delete --exclude-from="$IGNORE_FILE" "$HERE/" "$REMOTE:$DEST/"

cat <<EOF

[deploy] done. On the robot:

  cd $DEST
  uv sync

  # First-time calibration walks through ranges of motion when the robot
  # connects with no saved file. Saved files live under
  # ~/.cache/huggingface/lerobot/calibration/ keyed by B601_ID.

  uv run robot.py

EOF
