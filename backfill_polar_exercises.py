"""
Fred's Feats — backfill polar_exercises.csv from already-captured raw data
(ONE-OFF, run once).

The transaction containing these 12 exercises (334709649) was already
committed by the diagnostic script before the elapsed_time matching fix
existed, so it can't be re-fetched from Polar. But we don't need to —
the full raw response for every entry was already captured verbatim in
the diagnostic Action log (July 12 2026, ~10:32 UTC) before that
transaction was ever committed. This script replays that already-known
data through the SAME matching logic fetch_activities.py now uses
(elapsed_time, not moving_time) against whatever is in the CURRENT
runs.csv checked out in this workflow, and writes/merges the result into
polar_exercises.csv — no Polar API call needed at all.

NOT part of the daily pipeline. NOT scheduled. Run once via
workflow_dispatch, then delete this script and its workflow — this is a
one-time recovery for a specific known-lost batch, not a permanent tool.
"""
import csv
import re
import json

# ── Raw exercise data, copied verbatim from the July 12 2026 diagnostic ──────
# Action log (transaction 334709649, before it was committed). Real hyphenated
# field names as returned by Polar's actual per-exercise summary endpoint.
RAW_EXERCISES = [
    {'upload-time': '2026-07-03T07:14:16.000Z', 'id': 494952220, 'start-time': '2026-07-03T08:49:55', 'start-time-utc-offset': 120, 'duration': 'PT23M54S', 'heart-rate': {'average': 126, 'maximum': 161}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-04T19:18:48.000Z', 'id': 495242147, 'start-time': '2026-07-04T21:07:27', 'start-time-utc-offset': 120, 'duration': 'PT10M57S', 'heart-rate': {'average': 122, 'maximum': 150}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-04T21:24:28.000Z', 'id': 495254485, 'start-time': '2026-07-04T21:24:53', 'start-time-utc-offset': 120, 'duration': 'PT1H59M7S', 'heart-rate': {'average': 138, 'maximum': 153}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-05T20:34:54.000Z', 'id': 495436142, 'start-time': '2026-07-05T21:22:08', 'start-time-utc-offset': 120, 'duration': 'PT1H12M17S', 'heart-rate': {'average': 128, 'maximum': 153}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-07T18:27:50.000Z', 'id': 495766912, 'start-time': '2026-07-07T19:48:54', 'start-time-utc-offset': 120, 'duration': 'PT38M22S', 'heart-rate': {'average': 154, 'maximum': 179}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-08T13:20:57.000Z', 'id': 495897078, 'start-time': '2026-07-08T15:08:29', 'start-time-utc-offset': 120, 'duration': 'PT11M56S', 'heart-rate': {'average': 119, 'maximum': 147}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-08T15:31:07.000Z', 'id': 495917636, 'start-time': '2026-07-08T16:27:12', 'start-time-utc-offset': 120, 'duration': 'PT1H3M16S', 'heart-rate': {'average': 142, 'maximum': 166}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-09T19:43:57.000Z', 'id': 496161732, 'start-time': '2026-07-09T21:27:24', 'start-time-utc-offset': 120, 'duration': 'PT15M55S', 'heart-rate': {'average': 139, 'maximum': 148}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-09T20:22:44.000Z', 'id': 496168543, 'start-time': '2026-07-09T21:45:20', 'start-time-utc-offset': 120, 'duration': 'PT36M51S', 'heart-rate': {'average': 135, 'maximum': 160}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-11T21:37:08.000Z', 'id': 496523533, 'start-time': '2026-07-11T21:23:09', 'start-time-utc-offset': 120, 'duration': 'PT2H13M30S', 'heart-rate': {'average': 149, 'maximum': 177}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-12T07:45:21.000Z', 'id': 496558674, 'start-time': '2026-07-12T08:41:06', 'start-time-utc-offset': 120, 'duration': 'PT1H3M46S', 'heart-rate': {'average': 134, 'maximum': 160}, 'sport': 'OTHER'},
    {'upload-time': '2026-07-12T08:48:31.000Z', 'id': 496571832, 'start-time': '2026-07-12T09:46:22', 'start-time-utc-offset': 120, 'duration': 'PT1H1M39S', 'heart-rate': {'average': 140, 'maximum': 174}, 'sport': 'OTHER'},
]


def _iso_time_to_seconds_of_day(iso_str):
    """CONFIRMED BUG, FIXED: Garmin's runs.csv start_time is space-separated
    ("2026-07-12 08:39:38"), not "T"-separated like Polar's ISO format —
    the original .split("T")[1] silently returned None for every Garmin
    run. See fetch_activities.py for the full writeup."""
    try:
        sep = "T" if "T" in iso_str else " "
        time_part = iso_str.split(sep)[1]
        time_part = time_part.split("+")[0].split("Z")[0]
        h, m, s = time_part.split(":")
        s = s.split(".")[0]
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return None


def _duration_str_to_seconds(dur_str):
    try:
        h, m, s = (int(p) for p in dur_str.split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return None


def _parse_iso8601_duration_to_seconds(dur_str):
    if not dur_str:
        return None
    m = re.match(r'^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$', str(dur_str))
    if not m or not any(m.groups()):
        return None
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s = float(m.group(3) or 0)
    return h * 3600 + mi * 60 + s


# ── Load current run history from the checked-out repo ───────────────────────
all_run_rows = []
with open("runs.csv", "r", encoding="utf-8") as f:
    all_run_rows = list(csv.DictReader(f))
print(f"Loaded {len(all_run_rows)} rows from runs.csv for matching")

runs_by_date = {}
for r in all_run_rows:
    d = r.get("date")
    if d:
        runs_by_date.setdefault(d, []).append(r)

# ── Load existing polar_exercises.csv (if any) to merge into ─────────────────
existing_exercises = {}
try:
    with open("polar_exercises.csv", "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("polar_exercise_id"):
                existing_exercises[row["polar_exercise_id"]] = row
except FileNotFoundError:
    pass

matched_count = 0
for ex in RAW_EXERCISES:
    ex_id = str(ex.get("id", ""))
    ex_start = ex.get("start-time", "")  # already local — same as production code
    ex_duration_raw = ex.get("duration", "")
    if not ex_id or not ex_start:
        continue

    ex_date = ex_start[:10]
    ex_start_sec = _iso_time_to_seconds_of_day(ex_start)
    ex_duration_sec = _parse_iso8601_duration_to_seconds(ex_duration_raw)
    if ex_start_sec is None or ex_duration_sec is None:
        print(f"Exercise {ex_id}: could not parse start-time/duration — skipping")
        continue
    ex_end_sec = ex_start_sec + ex_duration_sec

    hr_block = ex.get("heart-rate") or {}
    avg_hr = hr_block.get("average")
    max_hr = hr_block.get("maximum")
    sport = ex.get("sport", "")

    matched_run = None
    same_date_runs = runs_by_date.get(ex_date, [])
    for r in same_date_runs:
        run_start = r.get("start_time", "")
        if not run_start:
            continue
        run_start_sec = _iso_time_to_seconds_of_day(run_start)
        run_dur_sec = _duration_str_to_seconds(r.get("elapsed_time", ""))  # elapsed_time, per the fix
        if run_start_sec is None or run_dur_sec is None:
            continue
        run_end_sec = run_start_sec + run_dur_sec
        if ex_start_sec <= run_end_sec and run_start_sec <= ex_end_sec:
            matched_run = r
            break

    if not matched_run:
        print(f"Exercise {ex_id} ({ex_date}, {ex_duration_raw}): no matching Garmin run on {ex_date} — not written")
        continue

    existing_exercises[ex_id] = {
        "date": ex_date,
        "garmin_activity_id": matched_run.get("activity_id", ""),
        "polar_exercise_id": ex_id,
        "start_time": ex_start,
        "duration_min": round(ex_duration_sec / 60, 1),
        "sport": sport,
        "avg_hr": avg_hr if avg_hr is not None else "",
        "max_hr": max_hr if max_hr is not None else ""
    }
    matched_count += 1
    print(f"Exercise {ex_id} ({ex_date}, {ex_duration_raw}) MATCHED Garmin activity {matched_run.get('activity_id','')}")

ex_fieldnames = ["date", "garmin_activity_id", "polar_exercise_id", "start_time", "duration_min", "sport", "avg_hr", "max_hr"]
with open("polar_exercises.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=ex_fieldnames, extrasaction="ignore")
    writer.writeheader()
    for eid in sorted(existing_exercises.keys(), key=lambda k: existing_exercises[k].get("date", ""), reverse=True):
        writer.writerow(existing_exercises[eid])

print(f"\npolar_exercises.csv written: {matched_count} matched from this backfill, {len(existing_exercises)} total rows")
