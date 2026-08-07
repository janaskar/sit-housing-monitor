#!/usr/bin/env python3
"""
Watch an Abakus event's registration timing and push a phone notification (via
ntfy.sh) whenever it changes -- e.g. when the organisers finally set/adjust the
"Pameelding apner" (registration opens) time.

Why this exists: for BedEx 2026 (event 4072) the registration-opening time and
the prikk / payment deadlines are still placeholders (the deadlines fall *before*
registration even opens, which can't be right). We want a ping the moment they
lock in the real times, so we can register the instant it opens.

It reuses the same conventions as check_sit_housing.py: no third-party deps
(urllib only), the ntfy topic from NTFY_TOPIC, and -- when GIT_SYNC=1 -- it
commits/pushes its small state file so the cloud fallback and the local runner
never double-notify.

Environment:
  NTFY_TOPIC        (required for pushes) same secret topic as the housing monitor
  NTFY_SERVER       (optional) default https://ntfy.sh
  ABAKUS_EVENT_ID   (optional) default 4072 (BedEx 2026)
  ABAKUS_STATE_FILE (optional) default state/abakus.json
  GIT_SYNC=1        (optional) commit+push the state file on change
"""

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    OSLO = ZoneInfo("Europe/Oslo")
except Exception:  # zoneinfo missing or no tz data -- fall back to fixed CEST
    OSLO = timezone(timedelta(hours=2))

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
EVENT_ID = os.environ.get("ABAKUS_EVENT_ID", "4072").strip()
API_URL = f"https://lego.abakus.no/api/v1/events/{EVENT_ID}/"
EVENT_URL = f"https://abakus.no/events/{EVENT_ID}-bedex-2026"
STATE_FILE = os.environ.get("ABAKUS_STATE_FILE", os.path.join("state", "abakus.json"))
GIT_SYNC = os.environ.get("GIT_SYNC") == "1"

# Fields whose changes we care about, in the order we want them shown, with the
# label used in the notification. Pool activation dates are added dynamically.
TRACKED = [
    ("activationTime", "Registration opens (event)"),
    ("registrationCloseTime", "Registration closes"),
    ("unregistrationDeadline", "Prikk deadline (late unreg.)"),
    ("paymentDueDate", "Payment deadline"),
    ("mergeTime", "Pools merge"),
    ("startTime", "Trip starts"),
    ("endTime", "Trip ends"),
]
# A change in any of these means "registration timing was (re)configured" -> urgent.
OPENING_KEYS = {"activationTime", "registrationCloseTime"}


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "abakus-event-watcher/1.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "abakus-event-watcher/1.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def fmt(iso_utc):
    """Render an API UTC timestamp as local Oslo time, e.g. 'Sep 1, 12:00'."""
    if not iso_utc:
        return "not set"
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        return dt.astimezone(OSLO).strftime("%b %d, %H:%M")
    except ValueError:
        return str(iso_utc)


def snapshot(event):
    """Extract just the timing fields we track into a flat, comparable dict."""
    snap = {key: event.get(key) for key, _ in TRACKED}
    for pool in event.get("pools", []) or []:
        snap[f"pool:{pool.get('name')}"] = pool.get("activationDate")
    return snap


def label_for(key):
    for k, lbl in TRACKED:
        if k == key:
            return lbl
    if key.startswith("pool:"):
        return f"Pameelding opens ({key[5:]})"
    return key


def send_ntfy(title, message, priority=4):
    if not NTFY_TOPIC:
        log("NTFY_TOPIC not set; skipping push. Message was:")
        log(f"  {title} | {message}")
        return
    body = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "click": EVENT_URL,
        "tags": ["calendar", "briefcase"],
        "priority": priority,
    }
    try:
        http_post_json(NTFY_SERVER, body)
        log(f"Sent notification: {title}")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        log(f"Failed to send ntfy notification: {e}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def save_state(snap):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    payload = {"event_id": EVENT_ID,
               "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "snapshot": snap}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def git_sync():
    if not GIT_SYNC:
        return

    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True)

    git("add", STATE_FILE)
    if git("commit", "-m", "Update Abakus event state [skip ci]").returncode != 0:
        return  # nothing to commit
    git("pull", "--rebase", "--autostash")
    if git("push").returncode != 0:
        log("git push failed (will retry on next change).")


def main():
    if not NTFY_TOPIC:
        log("WARNING: NTFY_TOPIC not set; running without push notifications.")

    try:
        event = http_get_json(API_URL)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        log(f"Failed to fetch Abakus event {EVENT_ID}: {e}")
        return

    snap = snapshot(event)
    prev = load_state()

    if prev is None:
        # First run: arm the watcher and send a one-time summary so you know the
        # current (placeholder) times and that monitoring is live.
        lines = [f"{label_for(k)}: {fmt(snap[k])}" for k, _ in TRACKED if k in snap]
        for k in sorted(snap):
            if k.startswith("pool:"):
                lines.append(f"{label_for(k)}: {fmt(snap[k])}")
        send_ntfy(f"Watching {event.get('title', 'Abakus event')} registration",
                  "Now monitoring for opening-time changes.\n\n" + "\n".join(lines),
                  priority=3)
        save_state(snap)
        git_sync()
        log("First run: stored snapshot and sent startup summary.")
        return

    old = prev.get("snapshot", {})
    changes = []
    urgent = False
    for key in list(snap) + [k for k in old if k not in snap]:
        before, after = old.get(key), snap.get(key)
        if before != after:
            changes.append(f"{label_for(key)}:\n  {fmt(before)}  ->  {fmt(after)}")
            if key in OPENING_KEYS or key.startswith("pool:"):
                urgent = True

    if not changes:
        log("No change in Abakus registration timing.")
        return

    title = f"{event.get('title', 'Abakus event')} times changed!"
    if urgent:
        title = f"BedEx registration time SET/changed!"
    send_ntfy(title, "\n\n".join(changes), priority=5 if urgent else 4)
    save_state(snap)
    git_sync()
    log(f"Detected {len(changes)} change(s); notified.")


if __name__ == "__main__":
    main()
