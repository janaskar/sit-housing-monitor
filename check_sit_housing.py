#!/usr/bin/env python3
"""
Monitor Sit student housing (bolig.sit.no) for newly available units in a given
city (default: Trondheim) and send a push notification to your phone via ntfy.sh
whenever a new unit appears. Each notification includes the building/area, and a
chosen favourite area (default: Karinelund) is flagged with top priority.

How it works
------------
The bolig.sit.no site is a static Gatsby front-end that loads live availability
from a GraphQL API. This script calls that same API directly (no browser needed):
it lists the available units in the target city, then resolves each unit's
building/area, diffs the result against the last-seen set stored in a small JSON
state file, and pushes a notification for any newly-appeared unit.

Environment variables
----------------------
NTFY_TOPIC      (required) Your secret ntfy topic, e.g. "sit-trondheim-a8f3k2".
NTFY_SERVER     (optional) ntfy server base URL. Default: https://ntfy.sh
CITY            (optional) City to watch (the "parent" location). Default: Trondheim
HIGHLIGHT_AREA  (optional) Area to flag with top priority. Default: Karinelund
STATE_FILE      (optional) Path to the JSON state file. Default: state/seen.json

Exit code is 0 for normal runs (including "no new housing") so a scheduler never
treats a quiet check as a failure.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

GRAPHQL_ENDPOINT = os.environ.get(
    "SIT_GRAPHQL_ENDPOINT",
    "https://as-portal-a-prod884f86a.azurewebsites.net/graphql",
)
CITY = os.environ.get("CITY", "Trondheim")
HIGHLIGHT_AREA = os.environ.get("HIGHLIGHT_AREA", "Karinelund").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", os.path.join("state", "seen.json"))
# Don't re-notify about the same unit within this window, even if it disappears
# from the listing and comes back (listings flicker / get briefly reserved).
RENOTIFY_HOURS = int(os.environ.get("RENOTIFY_HOURS", "72"))
# Forget units that have been gone this long, so a genuine re-list after a long
# absence notifies again (and the state file doesn't grow forever).
PRUNE_DAYS = int(os.environ.get("PRUNE_DAYS", "30"))
UNIT_URL_TEMPLATE = "https://bolig.sit.no/en/unit/{slug}"

# The query the site uses, reduced to the fields we care about.
QUERY = """
query ($f: GetHousingsInput!) {
  housings(filter: $f) {
    totalCount
    housingRentalObjects {
      rentalObjectId
      isAvailable
      availableFrom
    }
    filterCounts {
      locations { key value }
    }
  }
}
""".strip()


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def http_post_json(url, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "sit-housing-monitor/1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def graphql_housings(location_children, include_filter_counts=False, retries=3):
    """Call the housings query for CITY, optionally scoped to specific buildings."""
    variables = {
        "f": {
            "pageSize": 500,
            "offset": 0,
            "showUnavailable": False,
            "includeFilterCounts": include_filter_counts,
            # parent = city; empty children = every building in that city.
            "location": [{"parent": CITY, "children": location_children}],
        }
    }
    payload = {"query": QUERY, "variables": variables}
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            result = http_post_json(GRAPHQL_ENDPOINT, payload)
            if result.get("errors"):
                raise RuntimeError(f"GraphQL errors: {result['errors']}")
            return (result.get("data") or {}).get("housings") or {}
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as e:
            last_err = e
            log(f"Fetch attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(5 * attempt)
    raise SystemExit(f"Could not fetch housing data after {retries} attempts: {last_err}")


def fetch_available_units():
    """
    Return a dict: rentalObjectId -> {availableFrom, area}.

    The availability API returns only unit IDs, so we resolve each unit's
    building/area by querying the buildings that currently have availability.
    """
    top = graphql_housings([], include_filter_counts=True)
    units = {
        u["rentalObjectId"]: {"availableFrom": u.get("availableFrom"), "area": None}
        for u in (top.get("housingRentalObjects") or [])
        if u.get("isAvailable")
    }
    if not units:
        return units

    # Buildings (children of CITY) that currently have >0 available units.
    prefix = CITY
    locations = (top.get("filterCounts") or {}).get("locations") or []
    buildings = [
        loc["key"][len(prefix):]
        for loc in locations
        if loc["key"].startswith(prefix) and loc["key"] != prefix and loc["value"] > 0
    ]

    # Map each available unit to its building.
    for building in buildings:
        res = graphql_housings([building])
        for u in (res.get("housingRentalObjects") or []):
            uid = u["rentalObjectId"]
            if uid in units:
                units[uid]["area"] = building
    return units


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def load_state(path):
    """
    Return (ledger, first_run).

    ledger maps rentalObjectId -> {area, available, last_seen, notified_at,
    available_from}. It is a *persistent* record: a unit stays in the ledger
    (marked available=False) after it disappears, so brief flickers don't cause
    duplicate notifications. `first_run` is True only when there's no usable
    state at all (fresh install), which triggers the one-off startup summary.
    """
    if not os.path.exists(path):
        return {}, True
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f"Warning: could not read state file ({e}); treating as first run.")
        return {}, True

    known = data.get("known")
    if isinstance(known, dict):
        return known, False

    # Migrate the old format ({seen: [...], current: {...}}): treat everything
    # already seen as known + available + already-notified, so upgrading does
    # not re-spam you for units you were already told about.
    seen = data.get("seen")
    if isinstance(seen, list):
        current = data.get("current") or {}
        stamp = now_iso()
        migrated = {
            uid: {
                "area": current.get(uid),
                "available": True,
                "last_seen": stamp,
                "notified_at": stamp,
                "available_from": None,
            }
            for uid in seen
        }
        log(f"Migrated {len(migrated)} unit(s) from the old state format.")
        return migrated, False

    return {}, True


def save_state(path, ledger):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    current = {
        uid: (e.get("area") or "unknown")
        for uid, e in sorted(ledger.items())
        if e.get("available")
    }
    payload = {
        "city": CITY,
        "updated": now_iso(),
        # Human-friendly snapshot of what's available right now (id -> area).
        "current": current,
        # Full persistent ledger used for de-duplication.
        "known": {uid: ledger[uid] for uid in sorted(ledger)},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def unit_link(rental_object_id):
    return UNIT_URL_TEMPLATE.format(slug=rental_object_id.lower())


def format_available_from(iso_str):
    if not iso_str:
        return "unknown"
    try:
        return datetime.fromisoformat(iso_str).strftime("%d %b %Y")  # e.g. 03 Sep 2026
    except ValueError:
        return iso_str


def is_highlight(area):
    return bool(area) and HIGHLIGHT_AREA and area.lower() == HIGHLIGHT_AREA.lower()


def send_ntfy(title, message, click=None, tags=None, priority=None):
    if not NTFY_TOPIC:
        log("NTFY_TOPIC not set; skipping push. Message was:")
        log(f"  {title} | {message} | {click}")
        return
    body = {"topic": NTFY_TOPIC, "title": title, "message": message}
    if click:
        body["click"] = click
    if tags:
        body["tags"] = tags
    if priority:
        body["priority"] = priority  # ntfy priority is an integer 1..5
    try:
        http_post_json(NTFY_SERVER, body)  # ntfy JSON publishing (POST to root)
        log(f"Sent notification: {title}")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        log(f"Failed to send ntfy notification: {e}")


def notify_new_unit(uid, info):
    area = info.get("area") or "unknown area"
    when = format_available_from(info.get("availableFrom"))
    if is_highlight(area):
        send_ntfy(
            title=f"{HIGHLIGHT_AREA} spot available in {CITY}!",
            message=f"{area}\n{uid}\nAvailable from {when}",
            click=unit_link(uid),
            tags=["star", "house"],
            priority=5,  # max/urgent
        )
    else:
        send_ntfy(
            title=f"New {CITY} housing: {area}",
            message=f"{area}\n{uid}\nAvailable from {when}",
            click=unit_link(uid),
            tags=["house"],
            priority=4,  # high
        )


def within_cooldown(notified_at, now):
    """True if we already notified about this unit recently (suppress re-ping)."""
    ts = parse_iso(notified_at)
    return ts is not None and (now - ts) < timedelta(hours=RENOTIFY_HOURS)


def prune_ledger(ledger, now):
    """Drop units that have been unavailable longer than PRUNE_DAYS."""
    cutoff = now - timedelta(days=PRUNE_DAYS)
    stale = [
        uid for uid, e in ledger.items()
        if not e.get("available")
        and (parse_iso(e.get("last_seen")) or now) < cutoff
    ]
    for uid in stale:
        del ledger[uid]
    if stale:
        log(f"Pruned {len(stale)} stale unit(s) absent > {PRUNE_DAYS} days.")


def main():
    if not NTFY_TOPIC:
        log("WARNING: NTFY_TOPIC is not set. The script will run but cannot push "
            "notifications. Set the NTFY_TOPIC environment variable / secret.")

    units = fetch_available_units()
    current_ids = set(units.keys())
    summary = ", ".join(f"{uid} ({units[uid].get('area') or '?'})" for uid in sorted(current_ids))
    log(f"{CITY}: {len(current_ids)} available unit(s): {summary or '-'}")

    ledger, first_run = load_state(STATE_FILE)
    now = datetime.now(timezone.utc)
    stamp = now_iso()

    # Decide which currently-available units are genuinely worth a notification.
    to_notify = []
    for uid in sorted(current_ids):
        info = units[uid]
        entry = ledger.get(uid)
        was_available = bool(entry and entry.get("available"))
        just_appeared = entry is None or not was_available

        if just_appeared and not (entry and within_cooldown(entry.get("notified_at"), now)):
            to_notify.append(uid)

        if entry is None:
            entry = {}
            ledger[uid] = entry
        entry["area"] = info.get("area")
        entry["available"] = True
        entry["last_seen"] = stamp
        entry["available_from"] = info.get("availableFrom")
        if uid in to_notify:
            entry["notified_at"] = stamp

    # Anything in the ledger not available this run is marked gone (but kept, so
    # a brief disappearance/reappearance doesn't count as "new").
    for uid, entry in ledger.items():
        if uid not in current_ids:
            entry["available"] = False

    prune_ledger(ledger, now)

    if first_run:
        # Fresh install: one startup summary so you know it's alive; do not
        # send a per-unit ping for everything already listed.
        if current_ids:
            lines = [f"- {units[i].get('area') or 'unknown'}: {i} "
                     f"(from {format_available_from(units[i].get('availableFrom'))})"
                     for i in sorted(current_ids)]
            send_ntfy(
                title=f"Sit monitor started - watching {CITY}",
                message=f"{len(current_ids)} unit(s) available now (favourite: {HIGHLIGHT_AREA}):\n"
                        + "\n".join(lines),
                click="https://bolig.sit.no/en/",
                tags=["house"],
            )
        else:
            send_ntfy(
                title=f"Sit monitor started - watching {CITY}",
                message=f"No {CITY} units available right now. "
                        f"You'll get a ping when one appears (favourite: {HIGHLIGHT_AREA}).",
                tags=["house"],
            )
        save_state(STATE_FILE, ledger)
        log("Baseline saved. Future runs will notify only on new units.")
        return

    if not to_notify:
        log("No new units since last check.")
    else:
        log(f"NEW unit(s): {to_notify}")
        for uid in to_notify:
            notify_new_unit(uid, units[uid])

    save_state(STATE_FILE, ledger)


if __name__ == "__main__":
    main()
