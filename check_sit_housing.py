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
from datetime import datetime, timezone

GRAPHQL_ENDPOINT = os.environ.get(
    "SIT_GRAPHQL_ENDPOINT",
    "https://as-portal-a-prod884f86a.azurewebsites.net/graphql",
)
CITY = os.environ.get("CITY", "Trondheim")
HIGHLIGHT_AREA = os.environ.get("HIGHLIGHT_AREA", "Karinelund").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", os.path.join("state", "seen.json"))
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


def load_state(path):
    if not os.path.exists(path):
        return None  # None => first run / no baseline yet
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen", []))
    except (json.JSONDecodeError, OSError) as e:
        log(f"Warning: could not read state file ({e}); treating as first run.")
        return None


def save_state(path, units):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "city": CITY,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seen": sorted(units.keys()),
        # Human-friendly snapshot of what's currently available (id -> area).
        "current": {uid: (info.get("area") or "unknown") for uid, info in sorted(units.items())},
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


def main():
    if not NTFY_TOPIC:
        log("WARNING: NTFY_TOPIC is not set. The script will run but cannot push "
            "notifications. Set the NTFY_TOPIC environment variable / secret.")

    units = fetch_available_units()
    current_ids = set(units.keys())
    summary = ", ".join(f"{uid} ({units[uid].get('area') or '?'})" for uid in sorted(current_ids))
    log(f"{CITY}: {len(current_ids)} available unit(s): {summary or '-'}")

    previous = load_state(STATE_FILE)

    if previous is None:
        # First run: establish a baseline and send one startup summary so you
        # know the monitor is alive, without spamming for every existing unit.
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
        save_state(STATE_FILE, units)
        log("Baseline saved. Future runs will notify only on new units.")
        return

    new_ids = current_ids - previous
    if not new_ids:
        log("No new units since last check.")
    else:
        log(f"NEW unit(s): {sorted(new_ids)}")
        for uid in sorted(new_ids):
            notify_new_unit(uid, units[uid])

    # Persist the full current set so units that come back later re-trigger.
    save_state(STATE_FILE, units)


if __name__ == "__main__":
    main()
