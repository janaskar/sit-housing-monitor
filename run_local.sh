#!/usr/bin/env bash
# Live local runner for the Sit housing monitor (run in WSL via Windows Task
# Scheduler, which fires this every minute). Each invocation does a ~55-second
# "burst" of fast polls (one lightweight request every ~15s), so a unit that
# appears and is snapped up quickly is far less likely to slip through a gap.
#
# It shares state with the cloud GitHub Actions workflow through git so the two
# never send duplicate notifications: pull the latest committed state first, then
# the Python loop commits/pushes only when a new unit is actually found.
#
# The ntfy topic is read from .local/ntfy_topic (gitignored, stays on this PC).
set -uo pipefail
cd "$(dirname "$0")" || exit 1

# Only one burst at a time: Task Scheduler may fire again before the previous
# burst finishes. A second invocation grabs no lock and exits immediately.
exec 9>".local/live.lock"
if ! flock -n 9; then
  exit 0
fi

TOPIC_FILE=".local/ntfy_topic"
if [ ! -f "$TOPIC_FILE" ]; then
  echo "Missing $TOPIC_FILE (should contain your ntfy topic)."; exit 1
fi
export NTFY_TOPIC
NTFY_TOPIC="$(tr -d ' \t\r\n' < "$TOPIC_FILE")"
export CITY="${CITY:-Trondheim}"
export LIVE_INTERVAL="${LIVE_INTERVAL:-15}"   # seconds between polls
export BURST_SECONDS="${BURST_SECONDS:-50}"   # stay under the 1-min relaunch (leaves margin for git)
export GIT_SYNC=1                             # push state on change (keeps cloud in sync)

# Pick up any state the cloud committed while the PC was off, then poll live.
git pull --rebase --autostash >/dev/null 2>&1 || git rebase --abort >/dev/null 2>&1
python3 check_sit_housing.py --loop
