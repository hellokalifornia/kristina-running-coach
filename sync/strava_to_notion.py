#!/usr/bin/env python3
"""
Strava → Notion sync service for Kristina's running coach.

Runs on a schedule (every hour via Render cron job).
Fetches new runs from Strava and logs them to the Notion Runs database.
Skips runs that are already in Notion (deduplication by Strava ID).

Environment variables required:
    STRAVA_CLIENT_ID       — Strava app client ID
    STRAVA_CLIENT_SECRET   — Strava app client secret
    STRAVA_REFRESH_TOKEN   — Long-lived refresh token
    NOTION_TOKEN           — Notion integration token
    NOTION_DATABASE_ID     — Notion Runs database ID
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# ── Config from environment ──────────────────────────────────────────────────

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN")
NOTION_TOKEN         = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID   = os.environ.get("NOTION_DATABASE_ID", "33dd769ea65280d8aab5f7e361138e68")

STRAVA_TOKEN_URL      = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
NOTION_SEARCH_URL     = "https://api.notion.com/v1/databases/{}/query"
NOTION_PAGES_URL      = "https://api.notion.com/v1/pages"
NOTION_VERSION        = "2022-06-28"

# How many days back to check for new runs
SYNC_LOOKBACK_DAYS = 3


def check_env():
    missing = [v for v in ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
                            "STRAVA_REFRESH_TOKEN", "NOTION_TOKEN"] if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)


# ── Strava ───────────────────────────────────────────────────────────────────

def get_strava_token():
    """Exchange refresh token for a fresh access token."""
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
    """Fetch runs from the last N days."""
    after_ts = int(time.time()) - (days * 86400)
    url = f"{STRAVA_ACTIVITIES_URL}?per_page=50&after={after_ts}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            activities = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"ERROR: Strava activities fetch failed: {e.read().decode()}")
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
    """Fetch Strava IDs already logged in Notion (via Notes field)."""
    url = NOTION_SEARCH_URL.format(NOTION_DATABASE_ID)
    payload = json.dumps({"page_size": 100}).encode()
    req = urllib.request.Request(url, data=payload, headers=notion_headers(), method="POST")
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
    """Convert m/s to MM:SS/km string."""
    if not speed_ms or speed_ms <= 0:
        return ""
    secs = 1000 / speed_ms
    return f"{int(secs // 60)}:{int(secs % 60):02d}"


def infer_run_type(activity):
    """Guess run type from name and distance."""
    name = activity.get("name", "").lower()
    dist = activity.get("distance", 0) / 1000

    if any(w in name for w in ["brc", "interval", "track", "speed"]):
        return "Intervals"
    if dist >= 13 or "long" in name:
        return "Long Run"
    return "Zone Two"


def create_notion_entry(activity):
    """Create a new page in the Notion Runs database."""
    dist_km = round(activity["distance"] / 1000, 2)
    avg_pace = format_pace(activity.get("average_speed", 0))
    avg_hr = activity.get("average_heartrate")
    run_type = infer_run_type(activity)
    strava_id = str(activity["id"])
    date_str = activity["start_date_local"][:10]
    name = activity.get("name", "Run")

    properties = {
        "Week": {"title": [{"text": {"content": name}}]},
        "Run Date": {"date": {"start": date_str}},
        "Run Type": {"select": {"name": run_type}},
        "Distance, km": {"number": dist_km},
        "Avg Pace": {"rich_text": [{"text": {"content": avg_pace}}]},
        "Notes": {"rich_text": [{"text": {"content": f"strava_id:{strava_id}"}}]},
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
            result = json.loads(resp.read())
            return result.get("id")
    except urllib.error.HTTPError as e:
        print(f"ERROR: Notion page creation failed: {e.read().decode()}")
        return None


# ── Main sync ────────────────────────────────────────────────────────────────

def main():
    check_env()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting Strava → Notion sync...")

    # Get fresh Strava token
    access_token = get_strava_token()
    print("✓ Strava token refreshed")

    # Fetch recent runs
    runs = fetch_recent_runs(access_token)
    print(f"✓ Found {len(runs)} runs in last {SYNC_LOOKBACK_DAYS} days")

    if not runs:
        print("Nothing to sync. Done.")
        return

    # Get already-synced Strava IDs from Notion
    existing_ids = get_existing_strava_ids()
    print(f"✓ {len(existing_ids)} runs already in Notion")

    # Sync new runs
    new_count = 0
    for run in runs:
        strava_id = str(run["id"])
        if strava_id in existing_ids:
            print(f"  → Skipping '{run['name']}' (already synced)")
            continue

        page_id = create_notion_entry(run)
        if page_id:
            dist = round(run["distance"] / 1000, 2)
            pace = format_pace(run.get("average_speed", 0))
            print(f"  ✓ Synced '{run['name']}' — {dist}km @ {pace}/km")
            new_count += 1
        else:
            print(f"  ✗ Failed to sync '{run['name']}'")

    print(f"\nDone. {new_count} new run(s) added to Notion.")


if __name__ == "__main__":
    main()
