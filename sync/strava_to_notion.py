#!/usr/bin/env python3
"""
Strava → Notion sync service for Kristina's running coach.
Runs every hour via GitHub Actions (free).
Fetches new runs from Strava and logs them to the Notion Runs database.
Automatically assigns Week number, Phase, and Run Type.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, date, timezone, timedelta

# ── Config ───────────────────────────────────────────────────────────────────

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN")
NOTION_TOKEN         = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID   = os.environ.get("NOTION_DATABASE_ID", "33dd769ea65280d8aab5f7e361138e68")

STRAVA_TOKEN_URL      = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
NOTION_DB_QUERY_URL   = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
NOTION_PAGES_URL      = "https://api.notion.com/v1/pages"
NOTION_VERSION        = "2022-06-28"

SYNC_LOOKBACK_DAYS = 3

# ── Training plan config ─────────────────────────────────────────────────────

PLAN_START = date(2026, 3, 16)  # Week 1 started March 16, 2026

# Rest weeks (long run drops back) — every 4th week
REST_WEEKS = {4, 8, 11, 14, 17, 20, 23}

# Phase boundaries (inclusive week numbers)
PHASES = [
    (1,  11, "Phase 1 - Base"),
    (12, 18, "Phase 2 - Tempo"),
    (19, 25, "Phase 3 - Race Specific"),
    (26, 27, "Phase 4 - Taper"),
]


def get_training_week(run_date: date) -> int:
    """Calculate which training week a run falls in (1-indexed)."""
    delta = (run_date - PLAN_START).days
    if delta < 0:
        return 0  # Before plan started
    return (delta // 7) + 1


def get_phase(week: int) -> str:
    for start, end, name in PHASES:
        if start <= week <= end:
            return name
    return "Phase 1 - Base"


# ── Strava ───────────────────────────────────────────────────────────────────

def check_env():
    missing = [v for v in ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
                            "STRAVA_REFRESH_TOKEN", "NOTION_TOKEN"] if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)


def get_strava_token():
    data = urllib.parse.urlencode({
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(STRAVA_TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as e:
        print(f"ERROR: Strava token refresh failed: {e.read().decode()}")
        sys.exit(1)


def fetch_recent_runs(access_token, days=SYNC_LOOKBACK_DAYS):
    after_ts = int(time.time()) - (days * 86400)
    url = f"{STRAVA_ACTIVITIES_URL}?per_page=50&after={after_ts}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            activities = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"ERROR: Strava fetch failed: {e.read().decode()}")
        sys.exit(1)
    return [a for a in activities if a.get("sport_type") == "Run" or a.get("type") == "Run"]


# ── Notion ───────────────────────────────────────────────────────────────────

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def get_existing_strava_ids():
    payload = json.dumps({"page_size": 100}).encode()
    req = urllib.request.Request(NOTION_DB_QUERY_URL, data=payload,
                                 headers=notion_headers(), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            results = json.loads(resp.read()).get("results", [])
    except urllib.error.HTTPError as e:
        print(f"ERROR: Notion query failed: {e.read().decode()}")
        return set()

    ids = set()
    for page in results:
        notes = page.get("properties", {}).get("Notes", {}).get("rich_text", [])
        for block in notes:
            text = block.get("text", {}).get("content", "")
            if text.startswith("strava_id:"):
                ids.add(text.replace("strava_id:", "").strip())
    return ids


def format_pace(speed_ms):
    if not speed_ms or speed_ms <= 0:
        return ""
    secs = 1000 / speed_ms
    return f"{int(secs // 60)}:{int(secs % 60):02d}"


def infer_run_type(activity, week):
    """Infer run type from Strava name, distance, and training phase."""
    name = activity.get("name", "").lower()
    dist = activity.get("distance", 0) / 1000

    # BRC / interval keywords
    if any(w in name for w in ["brc", "interval", "track", "speed", "tempo"]):
        return "Intervals"
    # Long run by distance (13km+ in base, lower threshold later)
    if dist >= 13:
        return "Long Run"
    # Short recovery run
    if dist <= 8:
        return "Zone Two"
    # Default 9-12km = Zone Two
    return "Zone Two"


def create_notion_entry(activity):
    dist_km   = round(activity["distance"] / 1000, 2)
    avg_pace  = format_pace(activity.get("average_speed", 0))
    avg_hr    = activity.get("average_heartrate")
    strava_id = str(activity["id"])
    date_str  = activity["start_date_local"][:10]
    run_date  = date.fromisoformat(date_str)
    name      = activity.get("name", "Run")

    week  = get_training_week(run_date)
    phase = get_phase(week)
    run_type = infer_run_type(activity, week)
    week_label = f"Week {week}" if week > 0 else "Pre-plan"

    properties = {
        "Week":         {"title": [{"text": {"content": name}}]},
        "Run Date":     {"date": {"start": date_str}},
        "Run Type":     {"select": {"name": run_type}},
        "Distance, km": {"number": dist_km},
        "Avg Pace":     {"rich_text": [{"text": {"content": avg_pace}}]},
        "Notes":        {"rich_text": [{"text": {"content": f"strava_id:{strava_id}"}}]},
        "Phase":        {"select": {"name": phase}},
    }

    if avg_hr:
        properties["Average HR"] = {"number": round(avg_hr)}

    payload = json.dumps({
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
    }).encode()

    req = urllib.request.Request(NOTION_PAGES_URL, data=payload,
                                 headers=notion_headers(), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()).get("id"), week_label, phase
    except urllib.error.HTTPError as e:
        print(f"ERROR: Notion page creation failed: {e.read().decode()}")
        return None, week_label, phase


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    check_env()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting Strava → Notion sync...")

    access_token = get_strava_token()
    print("✓ Strava token refreshed")

    runs = fetch_recent_runs(access_token)
    print(f"✓ Found {len(runs)} run(s) in last {SYNC_LOOKBACK_DAYS} days")

    if not runs:
        print("Nothing to sync. Done.")
        return

    existing_ids = get_existing_strava_ids()
    print(f"✓ {len(existing_ids)} run(s) already in Notion")

    new_count = 0
    for run in runs:
        strava_id = str(run["id"])
        if strava_id in existing_ids:
            print(f"  → Skipping '{run['name']}' (already synced)")
            continue

        page_id, week_label, phase = create_notion_entry(run)
        if page_id:
            dist = round(run["distance"] / 1000, 2)
            pace = format_pace(run.get("average_speed", 0))
            print(f"  ✓ Synced '{run['name']}' — {dist}km @ {pace}/km [{week_label}, {phase}]")
            new_count += 1
        else:
            print(f"  ✗ Failed to sync '{run['name']}'")

    print(f"\nDone. {new_count} new run(s) added to Notion.")


if __name__ == "__main__":
    main()
