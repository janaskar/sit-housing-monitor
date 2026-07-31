#!/usr/bin/env python3
"""
Monitor Sit student housing (bolig.sit.no) for newly available units in a given
city (default: Trondheim) and send a push notification to your phone via ntfy.sh
whenever a new unit appears.

How it works
------------
The bolig.sit.no site is a static Gatsby front-end that loads live availability
from a GraphQL API. This script calls that same API directly (no browser needed),
filters to the target city, diffs the result against the last-seen set stored in
a small JSON state file, and pushes a notification for any newly-appeared unit.

Environment variables
----------------------
NTFY_TOPIC   (required) Your secret ntfy topic name, e.g. "sit-trondheim-a8f3k2".
NTFY_SERVER  (optional) ntfy server base URL. Default: https://ntfy.sh
CITY         (optional) City to watch (the "parent" location). Default: Trondheim
STATE_FILE   (optional) Path to the JSON state file. Default: state/seen.json

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
      availableTo
      isHighlighted
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


def fetch_available_units(city, retries=3):
    """Return list of available rental-object dicts for the given city."""
    variables = {
        "f": {
            "pageSize": 500,
            "offset": 0,
            "showUnavailable": False,
            "includeFilterCounts": False,
            # parent = city, empty children = every building in that city.
            "location": [{"parent": city, "children": []}],
        }
    }
    payload = {"query": QUERY, "variables": variables}

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            result = http_post_json(GRAPHQL_ENDPOINT, payload)
            if result.get("errors"):
                raise RuntimeError(f"GraphQL errors: {result['errors']}")
            housings = (result.get("data") or {}).get("housings") or {}
            units = housings.get("housingRentalObjects") or []
            # Keep only genuinely available ones (belt-and-suspenders).
            return [u for u in units if u.get("isAvailable")]
        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as e:
            last_err = e
            log(f"Fetch attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(5 * attempt)
    raise SystemExit(f"Could not fetch housing data after {retries} attempts: {last_err}")


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


def save_state(path, seen_ids):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "city": CITY,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seen": sorted(seen_ids),
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
        # e.g. "2026-08-03T00:00:00.000+02:00"
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d %b %Y")
    except ValueError:
        return iso_str


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
        body["priority"] = priority
    try:
        # ntfy supports JSON publishing by POSTing to the server root.
        http_post_json(NTFY_SERVER, body)
        log(f"Sent notification: {title}")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        log(f"Failed to send ntfy notification: {e}")


def main():
    if not NTFY_TOPIC:
        log("WARNING: NTFY_TOPIC is not set. The script will run but cannot push "
            "notifications. Set the NTFY_TOPIC environment variable / secret.")

    units = fetch_available_units(CITY)
    by_id = {u["rentalObjectId"]: u for u in units}
    current_ids = set(by_id.keys())
    log(f"{CITY}: {len(current_ids)} available unit(s): {sorted(current_ids) or '-'}")

    previous = load_state(STATE_FILE)

    if previous is None:
        # First run: establish a baseline and send one startup summary so you
        # know the monitor is alive, without spamming for every existing unit.
        if current_ids:
            lines = [f"- {i} (from {format_available_from(by_id[i].get('availableFrom'))})"
                     for i in sorted(current_ids)]
            send_ntfy(
                title=f"Sit monitor started - watching {CITY}",
                message=f"{len(current_ids)} unit(s) available right now:\n" + "\n".join(lines),
                click="https://bolig.sit.no/en/",
                tags=["house"],
            )
        else:
            send_ntfy(
                title=f"Sit monitor started - watching {CITY}",
                message=f"No {CITY} units available right now. "
                        "You'll get a ping when one appears.",
                tags=["house"],
            )
        save_state(STATE_FILE, current_ids)
        log("Baseline saved. Future runs will notify only on new units.")
        return

    new_ids = current_ids - previous
    if not new_ids:
        log("No new units since last check.")
    else:
        log(f"NEW unit(s): {sorted(new_ids)}")
        for uid in sorted(new_ids):
            u = by_id[uid]
            send_ntfy(
                title=f"New {CITY} housing available",
                message=f"{uid}\nAvailable from {format_available_from(u.get('availableFrom'))}",
                click=unit_link(uid),
                tags=["house"],
                priority=4,  # ntfy priority: 1=min .. 3=default .. 5=max. 4=high.
            )

    # Persist the full current set so units that come back later re-trigger.
    save_state(STATE_FILE, current_ids)


if __name__ == "__main__":
    main()
