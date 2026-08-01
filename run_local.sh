#!/usr/bin/env bash
# Local 10-minute runner for the Sit housing monitor (run in WSL via Windows
# Task Scheduler). It shares state with the cloud GitHub Actions workflow through
# git, so the two instances never send duplicate notifications:
#
#   1. pull the latest committed state (the cloud may have updated the ledger)
#   2. run the check (which may push a notification and rewrite the state file)
#   3. push the new state ONLY if the set of available units actually changed
#      (avoids commit spam / push races; timestamp-only churn is discarded)
#
# The ntfy topic is read from .local/ntfy_topic (gitignored, stays on this PC).
set -uo pipefail
cd "$(dirname "$0")" || exit 1

STATE="state/seen.json"
TOPIC_FILE=".local/ntfy_topic"

if [ ! -f "$TOPIC_FILE" ]; then
  echo "Missing $TOPIC_FILE (should contain your ntfy topic)."; exit 1
fi
export NTFY_TOPIC
NTFY_TOPIC="$(tr -d ' \t\r\n' < "$TOPIC_FILE")"
export CITY="${CITY:-Trondheim}"

# Print the sorted set of currently-available unit ids from the state file.
avail() {
  python3 - "$STATE" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(" ".join(sorted(d.get("current", {}).keys())))
except Exception:
    print("")
PY
}

# Get in sync with whatever the cloud (or a previous local run) committed.
git pull --rebase --autostash >/dev/null 2>&1 || git rebase --abort >/dev/null 2>&1

before="$(avail)"
python3 check_sit_housing.py
rc=$?
after="$(avail)"

if [ "$before" != "$after" ]; then
  # Availability changed (a new/dropped unit) -> persist so the cloud stays in sync.
  git add "$STATE"
  git commit -m "Update housing state (local) [skip ci]" >/dev/null 2>&1
  git pull --rebase --autostash >/dev/null 2>&1
  git push >/dev/null 2>&1 || echo "push failed (will retry next run)"
else
  # Only timestamps changed -> discard so the working tree stays clean.
  git checkout -- "$STATE" >/dev/null 2>&1
fi

exit $rc
