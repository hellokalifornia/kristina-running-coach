#!/usr/bin/env python3
"""
Strava → Notion sync for Kristina's running coach.

Logic:
- Fetches recent runs from Strava
- For each run, identifies type (Intervals / Long Run / Zone Two)
- Calculates which training week it belongs to by run date
- Finds the matching EMPTY row in that week in Notion (no Run Date yet)
- Updates that row with actual data — never creates new rows
- Skips if Strava ID already exists anywhere in the table (dedup)
- Skips if no matching empty row found (unplanned run)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, datetime, timezone, timedelta

# ── Config ───────────────────────────────────────────────────────────────────

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN")
NOTION_TOKEN         = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID   = os.environ.get("NOTION_DATABASE_ID", "33dd769ea65280d8aab5f7e361138e68")

STRAVA_TOKEN_URL      = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
NOTION_DB_QUERY_URL   = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
NOTION_PAGE_URL       = "https://api.notion.com/v1/pages/{}"
NOTION_VERSION        = "2022-06-28"

SYNC_LOOKBACK_DAYS = 3
PLAN_START = date(2026, 3, 16)  # Week 1 = March 16, 2026


# ── Training plan helpers ────────────────────────────────────────────────────

def get_training_week(run_date: date) -> int:
    delta = (run_date - PLAN_START).days
    if delta < 0:
        return 0
    return (delta // 7) + 1


def infer_run_type(activity: dict) -> str:
    name = activity.get("name", "").lower()
    dist = activity.get("distance", 0) / 1000
    if "brc" in name:
        return "Intervals"
    if dist >= 13:
        return "Long Run"
    if dist <= 8:
        return "Zone Two"  # recovery
    return "Zone Two"


# ── Strava ───────────────────────────────────────────────────────────────────

def check_env():
    missing = [v for v in ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
                            "STRAVA_REFRESH_TOKEN", "NOTION_TOKEN"] if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}")
        sys.exit(1)


def get_strava_token() -> str:
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
        print(f"ERROR: Strava token refresh: {e.read().decode()}")
        sys.exit(1)


def fetch_recent_runs(token: str) -> list:
    after_ts = int(time.time()) - (SYNC_LOOKBACK_DAYS * 86400)
    url = f"{STRAVA_ACTIVITIES_URL}?per_page=50&after={after_ts}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as resp:
            activities = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"ERROR: Strava fetch: {e.read().decode()}")
        sys.exit(1)
    return [a for a in activities if a.get("sport_type") == "Run" or a.get("type") == "Run"]


# ── Notion ───────────────────────────────────────────────────────────────────

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_all_notion_rows() -> list:
    """Fetch all rows from the Runs database."""
    all_results = []
    payload = {"page_size": 100}

    while True:
        req = urllib.request.Request(
            NOTION_DB_QUERY_URL,
            data=json.dumps(payload).encode(),
            headers=notion_headers(),
            method="POST"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"ERROR: Notion query: {e.read().decode()}")
            return []

        all_results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return all_results


def get_row_props(row: dict) -> dict:
    """Extract key properties from a Notion row."""
    props = row.get("properties", {})

    def text(key):
        blocks = props.get(key, {}).get("rich_text", [])
        return blocks[0].get("text", {}).get("content", "") if blocks else ""

    def title(key):
        blocks = props.get(key, {}).get("title", [])
        return blocks[0].get("text", {}).get("content", "") if blocks else ""

    def select(key):
        s = props.get(key, {}).get("select")
        return s.get("name", "") if s else ""

    def run_date(key):
        d = props.get(key, {}).get("date")
        return d.get("start", "") if d else ""

    # Read Strava ID column, stripping any accidental "strava_id:" prefix
    strava_id = text("Strava ID").strip()
    if strava_id.startswith("strava_id:"):
        strava_id = strava_id.replace("strava_id:", "").strip()

    return {
        "id": row["id"],
        "week": title("Week"),
        "run_type": select("Run Type"),
        "run_date": run_date("Run Date"),
        "strava_id": strava_id,
        "phase": select("Phase"),
    }


def format_pace(speed_ms: float) -> str:
    if not speed_ms or speed_ms <= 0:
        return ""
    secs = 1000 / speed_ms
    return f"{int(secs // 60)}:{int(secs % 60):02d}"


def update_notion_row(page_id: str, activity: dict, run_date_str: str):
    """Update an existing Notion row with actual run data."""
    dist_km  = round(activity["distance"] / 1000, 2)
    avg_pace = format_pace(activity.get("average_speed", 0))
    avg_hr   = activity.get("average_heartrate")
    strava_id = str(activity["id"])

    properties = {
        "Distance, km": {"number": dist_km},
        "Avg Pace":     {"rich_text": [{"text": {"content": avg_pace}}]},
        "Strava ID":    {"rich_text": [{"text": {"content": strava_id}}]},
        "Run Date":     {"date": {"start": run_date_str}},
    }
    if avg_hr:
        properties["Average HR"] = {"number": round(avg_hr)}

    url = NOTION_PAGE_URL.format(page_id)
    req = urllib.request.Request(
        url,
        data=json.dumps({"properties": properties}).encode(),
        headers=notion_headers(),
        method="PATCH"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            json.loads(resp.read())
            return True
    except urllib.error.HTTPError as e:
        print(f"ERROR: Notion update failed: {e.read().decode()}")
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    check_env()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting Strava → Notion sync...")

    # Fetch Strava runs
    strava_token = get_strava_token()
    print("✓ Strava token refreshed")
    runs = fetch_recent_runs(strava_token)
    print(f"✓ Found {len(runs)} run(s) in last {SYNC_LOOKBACK_DAYS} days from Strava")

    if not runs:
        print("Nothing to sync. Done.")
        return

    # Fetch all Notion rows
    rows = fetch_all_notion_rows()
    print(f"✓ Fetched {len(rows)} rows from Notion")
    parsed_rows = [get_row_props(r) for r in rows]

    # Build set of already-synced Strava IDs
    synced_ids = {r["strava_id"] for r in parsed_rows if r["strava_id"]}
    print(f"✓ {len(synced_ids)} run(s) already synced")

    synced_count = 0
    for run in runs:
        strava_id  = str(run["id"])
        name       = run.get("name", "")
        dist       = round(run["distance"] / 1000, 2)
        date_str   = run["start_date_local"][:10]
        run_date   = date.fromisoformat(date_str)
        week_num   = get_training_week(run_date)
        run_type   = infer_run_type(run)
        week_label = f"Week {week_num}"

        # Skip if already synced
        if strava_id in synced_ids:
            print(f"  → Skipping '{name}' (already in Notion)")
            continue

        if week_num == 0:
            print(f"  → Skipping '{name}' (before plan start)")
            continue

        # Find matching empty row: same week + same run type + no run date yet
        # Match week_label as prefix since title may have been overwritten with activity name
        target = None
        for row in parsed_rows:
            week_matches = (row["week"] == week_label or
                           row["week"].startswith(week_label))
            if (week_matches
                    and row["run_type"] == run_type
                    and not row["run_date"]
                    and not row["strava_id"]):
                target = row
                break

        if not target:
            print(f"  → No empty {run_type} slot in {week_label} for '{name}' — skipping")
            continue

        # Update the row
        success = update_notion_row(target["id"], run, date_str)
        if success:
            pace = format_pace(run.get("average_speed", 0))
            print(f"  ✓ Filled '{name}' → {week_label} {run_type} slot ({dist}km @ {pace}/km)")
            # Mark row as used so we don't double-fill in same run
            target["run_date"] = date_str
            target["strava_id"] = strava_id
            synced_count += 1
        else:
            print(f"  ✗ Failed to update row for '{name}'")

    print(f"\nDone. {synced_count} run(s) synced to Notion.")


if __name__ == "__main__":
    main()
