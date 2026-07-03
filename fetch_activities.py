"""
Fred's Feats — daily data sync.

Pulls runs/strength from Garmin Connect, sleep/steps from Polar AccessLink,
computes aggregate stats and lactate threshold history, then calls Claude to
generate a structured daily coaching briefing. Writes everything the
dashboard (index.html) reads as static CSV/JSON files.

Despite the filename, this is no longer Garmin-only — Polar AccessLink is a
first-class data source here too (see the "Polar Accesslink" section below).
"""
import os
import csv
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from garminconnect import Garmin

email = os.environ["GARMIN_EMAIL"]
password = os.environ["GARMIN_PASSWORD"]

client = Garmin(email, password)
client.login()

# ── Mode detection ────────────────────────────────────────────────────────────
# Full refetch on first Sunday of each month or if FULL_REFRESH env var is set
today = datetime.today()
is_full_refresh = (today.weekday() == 6 and today.day <= 7) or os.environ.get("FULL_REFRESH") == "true"
print(f"Mode: {'FULL REFRESH' if is_full_refresh else 'INCREMENTAL'}")

# ── Load existing data ────────────────────────────────────────────────────────
existing_runs = []
existing_strength = []

if os.path.exists("runs.csv") and not is_full_refresh:
    with open("runs.csv", "r", encoding="utf-8") as f:
        existing_runs = list(csv.DictReader(f))

if os.path.exists("strength.csv") and not is_full_refresh:
    with open("strength.csv", "r", encoding="utf-8") as f:
        existing_strength = list(csv.DictReader(f))

# Find cutoff date for incremental fetch
# Only consider sources that actually have existing data — a missing/empty
# strength.csv (or runs.csv) shouldn't drag the cutoff all the way back to
# 2000 and force a full-history refetch every run until that file exists.
_available_last_dates = []
if existing_runs:
    _available_last_dates.append(existing_runs[0]["date"])
if existing_strength:
    _available_last_dates.append(existing_strength[0]["date"])
cutoff = min(_available_last_dates) if _available_last_dates else "2000-01-01"
print(f"Fetching activities since: {cutoff}")

# ── Fetch activities ──────────────────────────────────────────────────────────
new_activities = []
batch_size = 100
start = 0

while True:
    batch = client.get_activities(start, batch_size)
    if not batch:
        break
    # In incremental mode, stop when we reach activities older than cutoff
    if not is_full_refresh:
        batch = [a for a in batch if a.get("startTimeLocal", "")[:10] >= cutoff]
        new_activities.extend(batch)
        if len(batch) < batch_size:
            break
    else:
        new_activities.extend(batch)
        if len(batch) < batch_size:
            break
    start += batch_size

print(f"Fetched {len(new_activities)} activities from Garmin")

# ── Filter functions ──────────────────────────────────────────────────────────
def is_running(a):
    type_key = a.get("activityType", {}).get("typeKey", "").lower()
    return type_key in ["running", "treadmill_running", "trail_running"]

def is_treadmill(a):
    return a.get("activityType", {}).get("typeKey", "").lower() == "treadmill_running"

def is_strength(a):
    type_key = a.get("activityType", {}).get("typeKey", "").lower()
    return type_key in ["strength_training", "fitness_equipment"]

new_running = [a for a in new_activities if is_running(a)]
new_strength = [a for a in new_activities if is_strength(a)]

# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_time(seconds):
    if not seconds:
        return ""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}"

def calc_pace(duration_s, distance_m):
    if not distance_m or not duration_s:
        return ""
    pace_sec = (duration_s / 60) / (distance_m / 1000)
    pace_min = int(pace_sec)
    pace_s = int((pace_sec - pace_min) * 60)
    return f"{pace_min}:{pace_s:02d}"

def sec_to_pace(seconds):
    if not seconds or seconds <= 0:
        return ""
    total_seconds = int(round(seconds))
    pace_min = total_seconds // 60
    pace_s = total_seconds % 60
    return f"{pace_min}:{pace_s:02d}"

def speed_to_pace(speed_mps):
    if not speed_mps or speed_mps <= 0:
        return ""
    return sec_to_pace(1000 / speed_mps)

def parse_pace_sec(pace_str):
    if not pace_str:
        return None
    try:
        m, s = str(pace_str).split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return None

def lt_speed_to_pace(speed_value):
    """Convert Garmin LT speed to min/km, accepting m/s and km/h-like values."""
    if not speed_value or speed_value <= 0:
        return ""

    # Garmin activity speeds are normally m/s. Keep a km/h fallback because
    # the LT endpoint is less transparent than activity payloads.
    candidates = [1000 / speed_value, 3600 / speed_value]
    for pace_seconds in candidates:
        if 120 < pace_seconds < 900:
            return sec_to_pace(pace_seconds)
    return ""

def is_valid_lt_record(record):
    pace_sec = parse_pace_sec(record.get("lt_pace"))
    return pace_sec is not None and 120 < pace_sec < 900

def get_week(date):
    return date.isocalendar()[:2]

# ── Build run records ─────────────────────────────────────────────────────────
run_fieldnames = [
    "date", "name", "type", "distance_km", "moving_time", "elapsed_time",
    "avg_hr", "max_hr", "elevation_gain_m", "elevation_loss_m",
    "min_elevation_m", "max_elevation_m", "avg_pace_min_km", "max_pace_min_km",
    "avg_cadence", "calories", "training_load",
    "aerobic_training_effect", "anaerobic_training_effect", "vo2max_estimate",
    "activity_id"
]

def build_run_row(a):
    dist = a.get("distance", 0)
    duration = a.get("movingDuration", 0)
    elev_gain = a.get("elevationGain", "")
    elev_loss = a.get("elevationLoss", "")
    min_elev = a.get("minElevation", "")
    max_elev = a.get("maxElevation", "")
    max_speed = a.get("maxSpeed", None)
    cadence = a.get("averageRunningCadenceInStepsPerMinute", "")

    return {
        "date": a.get("startTimeLocal", "")[:10],
        "name": a.get("activityName", ""),
        "type": "treadmill" if is_treadmill(a) else "outdoor",
        "distance_km": round(dist / 1000, 2) if dist else "",
        "moving_time": fmt_time(duration),
        "elapsed_time": fmt_time(a.get("duration", 0)),
        "avg_hr": a.get("averageHR", ""),
        "max_hr": a.get("maxHR", ""),
        "elevation_gain_m": round(elev_gain, 1) if elev_gain else "",
        "elevation_loss_m": round(elev_loss, 1) if elev_loss else "",
        "min_elevation_m": round(min_elev, 1) if min_elev else "",
        "max_elevation_m": round(max_elev, 1) if max_elev else "",
        "avg_pace_min_km": calc_pace(duration, dist),
        "max_pace_min_km": speed_to_pace(max_speed),
        "avg_cadence": round(cadence) if cadence else "",
        "calories": a.get("calories", ""),
        "training_load": a.get("activityTrainingLoad", ""),
        "aerobic_training_effect": a.get("aerobicTrainingEffect", ""),
        "anaerobic_training_effect": a.get("anaerobicTrainingEffect", ""),
        "vo2max_estimate": a.get("vO2MaxValue", ""),
        "activity_id": a.get("activityId", "")
    }

# ── Build strength records ────────────────────────────────────────────────────
strength_fieldnames = ["date", "name", "elapsed_time", "duration_min", "activity_id"]

def build_strength_row(a):
    duration_s = a.get("duration", 0)
    return {
        "date": a.get("startTimeLocal", "")[:10],
        "name": a.get("activityName", ""),
        "elapsed_time": fmt_time(duration_s),
        "duration_min": round(duration_s / 60, 1) if duration_s else "",
        "activity_id": a.get("activityId", "")
    }

# ── Merge new + existing, deduplicate ─────────────────────────────────────────
# Prefer Garmin's own activityId as the true unique key — date+name alone can
# collapse two genuinely different same-day activities that share a name (e.g.
# two runs both auto-named "Running"). Falls back to date+name only for rows
# written before this field existed (activity_id will be blank on those).
def _row_key(row):
    aid = row.get("activity_id", "")
    if aid:
        return ("id", aid)
    return ("legacy", row.get("date", ""), row.get("name", ""))

def merge(new_rows, existing_rows, key_fields=None):
    new_keys = {_row_key(n) for n in new_rows}
    merged = list(new_rows)
    for r in existing_rows:
        if _row_key(r) not in new_keys:
            merged.append(r)
    return sorted(merged, key=lambda x: x.get("date", ""), reverse=True)

new_run_rows = [build_run_row(a) for a in new_running]
new_strength_rows = [build_strength_row(a) for a in new_strength]

if is_full_refresh:
    all_run_rows = sorted(new_run_rows, key=lambda x: x.get("date", ""), reverse=True)
    all_strength_rows = sorted(new_strength_rows, key=lambda x: x.get("date", ""), reverse=True)
else:
    all_run_rows = merge(new_run_rows, existing_runs)
    all_strength_rows = merge(new_strength_rows, existing_strength)

# ── Write runs.csv ────────────────────────────────────────────────────────────
with open("runs.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=run_fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(all_run_rows)

# ── Write strength.csv ────────────────────────────────────────────────────────
with open("strength.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=strength_fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(all_strength_rows)

# ── Aggregate stats ───────────────────────────────────────────────────────────
# Historical dashboard stats use yesterday as cutoff because today's data can be partial.
today_date = today.date()
last_complete_date = today_date - timedelta(days=1)
last_complete_str = str(last_complete_date)

def row_date(row):
    if not row.get("date"):
        return None
    try:
        return datetime.strptime(row["date"], "%Y-%m-%d").date()
    except Exception:
        return None

complete_run_rows = [r for r in all_run_rows if row_date(r) and r["date"] <= last_complete_str]
complete_strength_rows = [s for s in all_strength_rows if row_date(s) and s["date"] <= last_complete_str]

prev_year_start = datetime(today.year - 1, 1, 1).date()
prev_year_end = datetime(today.year - 1, 12, 31).date()

summary_runs_this_year = [a for a in complete_run_rows if a.get("date", "")[:4] == str(today.year)]
summary_runs_prev_year = [a for a in complete_run_rows
    if str(prev_year_start) <= a.get("date", "") <= str(prev_year_end)]

summary_strength_this_year = [a for a in complete_strength_rows if a.get("date", "")[:4] == str(today.year)]

total_distance_this_year = sum(float(a.get("distance_km") or 0) for a in summary_runs_this_year)
total_distance_prev_year = sum(float(a.get("distance_km") or 0) for a in summary_runs_prev_year)
total_strength_min_this_year = sum(float(a.get("duration_min") or 0) for a in summary_strength_this_year)

run_dates = sorted(set(
    datetime.strptime(a["date"], "%Y-%m-%d").date()
    for a in complete_run_rows if a.get("date")
))

weeks_with_runs = set(get_week(d) for d in run_dates)
strength_dates = sorted(set(
    datetime.strptime(a["date"], "%Y-%m-%d").date()
    for a in complete_strength_rows if a.get("date")
))
weeks_with_strength = set(get_week(d) for d in strength_dates)

def calc_current_streak(weeks_set):
    streak = 0
    week = last_complete_date - timedelta(days=last_complete_date.weekday())
    while get_week(week) in weeks_set:
        streak += 1
        week -= timedelta(weeks=1)
    return streak

def calc_longest_streak(weeks_set):
    if not weeks_set:
        return 0
    sorted_weeks = sorted(weeks_set)
    longest = current = 1
    for i in range(1, len(sorted_weeks)):
        y1, w1 = sorted_weeks[i-1]
        y2, w2 = sorted_weeks[i]
        diff = (datetime.strptime(f"{y2} {w2} 1", "%G %V %u").date() -
                datetime.strptime(f"{y1} {w1} 1", "%G %V %u").date()).days
        if diff == 7:
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest

weeks_in_year = len(set(get_week(
    datetime.strptime(a["date"], "%Y-%m-%d").date()
) for a in summary_runs_this_year if a.get("date")))

summary = {
    "last_updated": today.strftime("%Y-%m-%d %H:%M UTC"),
    "last_complete_date": last_complete_str,
    "total_runs_this_year": len(summary_runs_this_year),
    "total_distance_this_year_km": round(total_distance_this_year, 1),
    "total_distance_prev_year_km": round(total_distance_prev_year, 1),
    "avg_runs_per_week_this_year": round(len(summary_runs_this_year) / max(weeks_in_year, 1), 1),
    "current_weekly_streak": calc_current_streak(weeks_with_runs),
    "longest_weekly_streak": calc_longest_streak(weeks_with_runs),
    "total_strength_this_year": len(summary_strength_this_year),
    "total_strength_min_this_year": round(total_strength_min_this_year, 0),
    "current_strength_weekly_streak": calc_current_streak(weeks_with_strength),
    "longest_strength_weekly_streak": calc_longest_streak(weeks_with_strength)
}

with open("summary.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2)

# ── Lactate threshold ─────────────────────────────────────────────────────────
lt_records = []
lt_file = "lactate.json"

if os.path.exists(lt_file):
    with open(lt_file, "r", encoding="utf-8") as f:
        lt_records = json.load(f)

# Drop old bad LT records before appending. This keeps lactate.json clean, not
# merely hidden by the dashboard filter.
lt_records = [r for r in lt_records if is_valid_lt_record(r)]

try:
    lt = client.get_lactate_threshold()
    lt_data = lt.get("speed_and_heart_rate", {})
    lt_hr = lt_data.get("heartRate")
    lt_speed = lt_data.get("speed")
    lt_date = lt_data.get("calendarDate", "")[:10]
    lt_pace = lt_speed_to_pace(lt_speed)

    if lt_hr and lt_pace:
        today_str = str(today.date())
        if not any(r["date"] == today_str for r in lt_records):
            lt_records.append({
                "date": today_str,
                "lt_hr": round(lt_hr),
                "lt_pace": lt_pace,
                "lt_source_date": lt_date
            })
            print(f"LT recorded: {lt_pace} /km @ {round(lt_hr)} bpm")
        else:
            print("LT already recorded today")
    else:
        print("LT data not available or outside sane pace range")
except Exception as e:
    print(f"LT fetch skipped: {e}")

lt_records = sorted([r for r in lt_records if is_valid_lt_record(r)], key=lambda x: x["date"])

with open(lt_file, "w", encoding="utf-8") as f:
    json.dump(lt_records, f, indent=2)

# ── Polar Accesslink: sleep + steps ───────────────────────────────────────────
# Deliberately separate from Garmin data. Garmin (via H10 chest strap) remains
# the sole source of truth for runs.csv/strength.csv — Frederik's Polar Loop
# mis-tags H10-strap runs as "indoor" activities, so Polar exercise data is
# NOT pulled or merged here. Polar is used only for:
#   - sleep.csv   (Garmin doesn't provide this at all)
#   - polar_steps.csv (kept separate from iPhone-derived steps.csv so wrist
#     vs. phone step counts can be compared on the dashboard, not silently merged)
#
# Both use simple non-transactional GET requests (token-scoped, no user-id in
# path). The older activity-transactions create/list/commit flow is listed as
# deprecated in Polar's current docs (polar.com/accesslink-api) and was the
# source of repeated, unexplained HTTP 405 errors — switched away from it
# July 2026 in favor of GET /v3/users/activities.
#
# NOTE: Polar has been known to adjust response shapes over time — if fields
# come back empty, check https://www.polar.com/accesslink-api/ for the
# current schema before assuming the fetch is broken.

polar_token = os.environ.get("POLAR_ACCESS_TOKEN", "")
polar_user_id = os.environ.get("POLAR_USER_ID", "")

def _sleep_duration_min(start_iso, end_iso):
    """Derive sleep duration in minutes from start/end ISO timestamps.
    Polar's 'total_sleep' field name is unverified against real payloads and
    came back empty in practice — start/end timestamps are confirmed present
    and reliable, so duration is computed directly from those instead."""
    if not start_iso or not end_iso:
        return ""
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        return round((end - start).total_seconds() / 60)
    except Exception:
        return ""

def _log_polar_http_error(context, e):
    error_body = ""
    try:
        error_body = e.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    print(f"Polar {context} failed — HTTP {e.code}: {error_body[:300]}")

def polar_get(url):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {polar_token}",
            "Accept": "application/json"
        },
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _log_polar_http_error(f"GET {url}", e)
        raise

if polar_token:
    # ── Sleep ──────────────────────────────────────────────────────────────
    try:
        existing_sleep = {}
        if os.path.exists("sleep.csv"):
            with open("sleep.csv", "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("date"):
                        existing_sleep[row["date"]] = row

        sleep_resp = polar_get("https://www.polaraccesslink.com/v3/users/sleep")
        nights = (sleep_resp or {}).get("nights", [])

        for night in nights:
            date = night.get("date", "")
            if not date:
                continue
            existing_sleep[date] = {
                "date": date,
                "sleep_score": night.get("sleep_score", ""),
                "total_sleep_min": _sleep_duration_min(night.get("sleep_start_time"), night.get("sleep_end_time")),
                "sleep_start": night.get("sleep_start_time", ""),
                "sleep_end": night.get("sleep_end_time", ""),
                "continuity": night.get("continuity", "")
            }

        sleep_fieldnames = ["date", "sleep_score", "total_sleep_min", "sleep_start", "sleep_end", "continuity"]
        with open("sleep.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=sleep_fieldnames, extrasaction="ignore")
            writer.writeheader()
            for date in sorted(existing_sleep.keys(), reverse=True):
                writer.writerow(existing_sleep[date])

        print(f"Polar sleep: {len(nights)} nights fetched, {len(existing_sleep)} total in sleep.csv")
    except Exception as e:
        print(f"Polar sleep fetch skipped: {e}")

    # ── Steps (via /v3/users/activities — non-transactional) ────────────────
    # NOTE: the old create/list/commit "activity-transactions" flow is listed
    # as deprecated in Polar's current API docs (polar.com/accesslink-api) and
    # was the source of repeated, unexplained 405 errors. This endpoint mirrors
    # the sleep fetch above — simple GET, token-scoped, no transaction needed.
    try:
        existing_polar_steps = {}
        if os.path.exists("polar_steps.csv"):
            with open("polar_steps.csv", "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("date"):
                        existing_polar_steps[row["date"]] = int(row.get("steps") or 0)

        activities = polar_get("https://www.polaraccesslink.com/v3/users/activities") or []

        new_step_count = 0
        for act in activities:
            start_time = act.get("start_time", "")
            date = start_time[:10] if start_time else ""
            steps = act.get("steps")
            if date and steps is not None:
                existing_polar_steps[date] = int(steps)
                new_step_count += 1

        with open("polar_steps.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["date", "steps"])
            writer.writeheader()
            for date in sorted(existing_polar_steps.keys(), reverse=True):
                writer.writerow({"date": date, "steps": existing_polar_steps[date]})

        print(f"Polar steps: {new_step_count} day(s) fetched, {len(existing_polar_steps)} total in polar_steps.csv")
    except Exception as e:
        print(f"Polar steps fetch skipped: {e}")
else:
    print("POLAR_ACCESS_TOKEN not set — skipping Polar fetch")

# ── AI Coach block ────────────────────────────────────────────────────────────
# Calls Anthropic API (claude-sonnet-4-6) to generate a daily coaching summary.
# Data-anchored, tonally neutral, coaching-oriented with conversational
# reflection on outliers. Falls back to a static message if API call fails.

all_runs_sorted = sorted(all_run_rows, key=lambda x: x.get("date", ""))
all_strength_sorted = sorted(all_strength_rows, key=lambda x: x.get("date", ""))
today_date = today.date()

runs_this_year = [a for a in all_run_rows if a.get("date", "")[:4] == str(today.year)]
runs_prev_year = [a for a in all_run_rows
    if str(prev_year_start) <= a.get("date", "") <= str(prev_year_end)]
strength_this_year = [a for a in all_strength_rows if a.get("date", "")[:4] == str(today.year)]

# ── Build data context for the prompt ────────────────────────────────────────

# Last 4 weeks of runs
cutoff_4wk = today_date - timedelta(days=28)
cutoff_8wk = today_date - timedelta(days=56)

recent_runs = [r for r in all_runs_sorted
    if r.get("date") and datetime.strptime(r["date"], "%Y-%m-%d").date() >= cutoff_4wk]
prior_runs = [r for r in all_runs_sorted
    if r.get("date") and cutoff_8wk <= datetime.strptime(r["date"], "%Y-%m-%d").date() < cutoff_4wk]

recent_dist = sum(float(r.get("distance_km") or 0) for r in recent_runs)
prior_dist = sum(float(r.get("distance_km") or 0) for r in prior_runs)

# Last 4 weeks of strength
recent_strength = [s for s in all_strength_sorted
    if s.get("date") and datetime.strptime(s["date"], "%Y-%m-%d").date() >= cutoff_4wk]
prior_strength = [s for s in all_strength_sorted
    if s.get("date") and cutoff_8wk <= datetime.strptime(s["date"], "%Y-%m-%d").date() < cutoff_4wk]

# Steps data — reconciled from iPhone + Polar, same logic as the dashboard's
# reconciledStepsData in index.html (average when both sources report a day,
# fallback to whichever single source exists). Kept in sync deliberately: the
# coach's own step commentary was previously built from steps.csv (iPhone)
# alone, which could disagree with what the dashboard actually displays.
iphone_steps_data = {}
if os.path.exists("steps.csv"):
    with open("steps.csv", "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date"):
                iphone_steps_data[row["date"]] = int(row.get("steps") or 0)

polar_steps_data_for_coach = {}
if os.path.exists("polar_steps.csv"):
    with open("polar_steps.csv", "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date"):
                polar_steps_data_for_coach[row["date"]] = int(row.get("steps") or 0)

steps_data = {}
for d in set(list(iphone_steps_data.keys()) + list(polar_steps_data_for_coach.keys())):
    iphone_val = iphone_steps_data.get(d)
    polar_val = polar_steps_data_for_coach.get(d)
    if iphone_val and polar_val:
        steps_data[d] = round((iphone_val + polar_val) / 2)
    elif iphone_val:
        steps_data[d] = iphone_val
    elif polar_val:
        steps_data[d] = polar_val

recent_steps = {d: s for d, s in steps_data.items()
    if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_4wk}
avg_daily_steps = round(sum(recent_steps.values()) / max(len(recent_steps), 1))

rest_day_steps = [s for d, s in recent_steps.items()
    if d not in {r["date"] for r in recent_runs}
    and d not in {s["date"] for s in recent_strength}]
avg_rest_steps = round(sum(rest_day_steps) / max(len(rest_day_steps), 1)) if rest_day_steps else 0

# Sleep data (supporting context only — no behavior change based on this yet)
sleep_data = {}
if os.path.exists("sleep.csv"):
    with open("sleep.csv", "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date"):
                sleep_data[row["date"]] = row

recent_sleep = {d: s for d, s in sleep_data.items()
    if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_4wk}
sleep_lines = []
for d in sorted(recent_sleep.keys(), reverse=True)[:14]:
    s = recent_sleep[d]
    mins = s.get("total_sleep_min", "")
    hrs_str = f"{int(float(mins)) // 60}h{int(float(mins)) % 60:02d}m" if mins else "?"
    sleep_lines.append(f"  {d} | {hrs_str} | score {s.get('sleep_score', '?')}")

sleep_durations = [float(s["total_sleep_min"]) for s in recent_sleep.values() if s.get("total_sleep_min")]
sleep_scores = [float(s["sleep_score"]) for s in recent_sleep.values() if s.get("sleep_score")]
avg_sleep_min = round(sum(sleep_durations) / len(sleep_durations)) if sleep_durations else None
avg_sleep_score = round(sum(sleep_scores) / len(sleep_scores), 1) if sleep_scores else None

# LT trend
lt_history = []
if os.path.exists(lt_file):
    with open(lt_file, "r", encoding="utf-8") as f:
        raw_lt = json.load(f)
    lt_history = [r for r in raw_lt
        if r.get("lt_pace") and 120 < (parse_pace_sec(r["lt_pace"]) or 0) < 900]
    lt_history = sorted(lt_history, key=lambda x: x["date"])

latest_lt = lt_history[-1] if lt_history else None
baseline_lt = next((r for r in reversed(lt_history)
    if datetime.strptime(r["date"], "%Y-%m-%d").date() <= today_date - timedelta(days=30)), None)

# ── Data-confidence score ──────────────────────────────────────────────────────
# Deliberately NOT generated by the LLM — a model has no real calibrated access
# to "how confident am I," so asking it to invent a percentage would just be
# plausible-sounding fabrication. This measures DATA QUALITY/FRESHNESS only —
# never training volume or behavior (a legitimate taper/deload/recovery week
# must not make the dashboard look like something is broken). All historical
# components use last_complete_date per the lastCompleteDate convention;
# "recent context presence" is the one exception, matching the same
# today-inclusive treatment already given to the last-run label and coach
# summary elsewhere in this file.

def compute_confidence():
    reasons = []
    score = 0.0

    # LT freshness — 20 pts. Full at <=14 days old, zero at >=60 days or none.
    if latest_lt:
        lt_age_days = (last_complete_date - datetime.strptime(latest_lt["date"], "%Y-%m-%d").date()).days
        lt_pts = max(0, min(20, 20 * (1 - max(0, lt_age_days - 14) / 46)))
        score += lt_pts
        reasons.append(f"LT reading {lt_age_days}d old")
    else:
        reasons.append("No LT reading available")

    # HR data validity (last 8 complete runs) — 15 pts.
    recent_complete_runs = [r for r in recent_runs if r.get("date") and r["date"] <= last_complete_str][-8:]
    hr_valid = [r for r in recent_complete_runs if r.get("avg_hr") not in (None, "", "0")]
    if recent_complete_runs:
        hr_pts = 15 * (len(hr_valid) / len(recent_complete_runs))
        score += hr_pts
        reasons.append(f"HR data on {len(hr_valid)}/{len(recent_complete_runs)} recent runs")
    else:
        reasons.append("No recent runs to check HR data")

    # Sleep completeness (last 14 complete nights) — 15 pts.
    complete_sleep_nights = [d for d in recent_sleep.keys() if d <= last_complete_str]
    sleep_pts = 15 * (min(len(complete_sleep_nights), 14) / 14)
    score += sleep_pts
    reasons.append(f"Sleep tracked {min(len(complete_sleep_nights), 14)}/14 nights")

    # Steps completeness (last 7 complete days) — 10 pts.
    complete_step_days = [d for d in recent_steps.keys() if d <= last_complete_str]
    cutoff_7d = today_date - timedelta(days=7)
    complete_step_days_7d = [d for d in complete_step_days if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_7d]
    steps_pts = 10 * (min(len(complete_step_days_7d), 7) / 7)
    score += steps_pts
    reasons.append(f"Steps tracked {min(len(complete_step_days_7d), 7)}/7 days")

    # Pipeline freshness — 15 pts. Was the last fetch recent (today or yesterday)?
    pipeline_age_days = (today_date - last_complete_date).days  # last_complete_date = yesterday by definition
    # last_updated reflects THIS run, so pipeline freshness is really "did this run complete" —
    # scored as full since we're computing this mid-run. Kept as an explicit component so a
    # future stale/failed-run scenario has a clear place to reduce this score.
    pipeline_pts = 15
    score += pipeline_pts
    reasons.append("Pipeline fetch completed on schedule")

    # Recent context presence — 25 pts. Any run, strength, steps, or sleep entry
    # in the last 3 days (today-inclusive, unlike the components above).
    cutoff_3d = today_date - timedelta(days=3)
    has_recent_run = any(r.get("date", "") >= str(cutoff_3d) for r in recent_runs)
    has_recent_strength = any(s.get("date", "") >= str(cutoff_3d) for s in recent_strength)
    has_recent_steps = any(d >= str(cutoff_3d) for d in recent_steps.keys())
    has_recent_sleep = any(d >= str(cutoff_3d) for d in recent_sleep.keys())
    recent_signals = sum([has_recent_run, has_recent_strength, has_recent_steps, has_recent_sleep])
    context_pts = 25 * (recent_signals / 4)
    score += context_pts
    if recent_signals >= 3:
        reasons.append("Multiple data sources active in last 3 days")
    elif recent_signals > 0:
        reasons.append("Limited data sources active in last 3 days")
    else:
        reasons.append("No recent activity data in last 3 days")

    pct = round(score)
    if pct >= 85:
        label = "High data confidence"
    elif pct >= 65:
        label = "Moderate data confidence"
    else:
        label = "Low data confidence"

    return pct, label, reasons

confidence_pct, confidence_label, confidence_reasons = compute_confidence()

# ── Evidence catalog ────────────────────────────────────────────────────────────
# Every item here is a fully pre-written, factually-locked sentence computed by
# Python from real data. The model is instructed to SELECT from this list and
# copy items verbatim — it never generates its own evidence text. This is the
# only way to guarantee every ▲▼▬ claim in the coach output is actually true;
# letting the model freely write "evidence" risks fabricated claims (e.g. citing
# an "easy-run HR trend" using data that doesn't actually exist for that comparison).
#
# Value-neutral by design: arrows indicate DIRECTION only (▲ increased / ▼
# decreased / ▬ stable), never a good/bad judgment — matches the same principle
# used for the dashboard's delta indicators. Items are only included when the
# underlying comparison is actually possible (e.g. skip sleep evidence if there's
# no prior-period data yet) — no fixed count, whatever's genuinely available.

def _trend_arrow(delta, threshold=0.0001):
    if delta > threshold:
        return "▲"
    elif delta < -threshold:
        return "▼"
    return "▬"

def _priority_tier(abs_delta, high, medium):
    """Magnitude-based tier, computed purely from the delta size — not the LLM.
    Same reasoning as the confidence score: no calibrated self-rating available,
    so 'importance' is inferred deterministically from how large the change
    actually is on that metric's own scale."""
    if abs_delta >= high:
        return "High"
    elif abs_delta >= medium:
        return "Medium"
    return "Low"

def build_evidence_catalog():
    # Each item: (text, priority). Priority thresholds are per-metric because
    # "meaningful" looks different on each scale (5s/km on LT vs 5 steps/day
    # are not comparable magnitudes) — same principle as the dashboard's
    # qualitative-label thresholds.
    items = []

    # LT pace vs 30 days ago
    if latest_lt and baseline_lt:
        latest_sec = parse_pace_sec(latest_lt["lt_pace"])
        baseline_sec = parse_pace_sec(baseline_lt["lt_pace"])
        if latest_sec is not None and baseline_sec is not None:
            delta_sec = baseline_sec - latest_sec  # positive = faster (fewer seconds)
            arrow = _trend_arrow(delta_sec)
            text = (f"{arrow} LT pace {'improved' if delta_sec > 0 else 'slowed' if delta_sec < 0 else 'unchanged'} "
                    f"{abs(delta_sec)}s/km over 30 days ({baseline_lt['lt_pace']} → {latest_lt['lt_pace']}/km)")
            items.append((text, _priority_tier(abs(delta_sec), high=5, medium=2)))

    # Weekly volume vs prior 4-week block
    if prior_dist > 0:
        vol_delta_pct = ((recent_dist - prior_dist) / prior_dist) * 100
        arrow = _trend_arrow(recent_dist - prior_dist, threshold=1)
        text = (f"{arrow} 4-week volume {recent_dist:.0f} km vs {prior_dist:.0f} km prior block "
                f"({vol_delta_pct:+.0f}%)")
        items.append((text, _priority_tier(abs(vol_delta_pct), high=15, medium=5)))

    # Current weekly streak length — informational, not a magnitude-of-change
    # metric in the same sense, so always Medium rather than High/Low.
    current_streak = summary.get("current_weekly_streak")
    if current_streak:
        text = f"▬ {current_streak}-week running streak (best: {summary.get('longest_weekly_streak', '?')} wks)"
        items.append((text, "Medium"))

    # Sleep avg vs prior period
    prior_sleep = {d: s for d, s in sleep_data.items()
        if cutoff_8wk <= datetime.strptime(d, "%Y-%m-%d").date() < cutoff_4wk}
    prior_sleep_durations = [float(s["total_sleep_min"]) for s in prior_sleep.values() if s.get("total_sleep_min")]
    if sleep_durations and prior_sleep_durations:
        avg_prior_sleep = sum(prior_sleep_durations) / len(prior_sleep_durations)
        avg_recent_sleep = sum(sleep_durations) / len(sleep_durations)
        delta_sleep = avg_recent_sleep - avg_prior_sleep
        arrow = _trend_arrow(delta_sleep, threshold=5)
        def _fmt_hm(mins):
            return f"{int(mins) // 60}h{int(mins) % 60:02d}m"
        text = f"{arrow} Sleep averaging {_fmt_hm(avg_recent_sleep)} vs {_fmt_hm(avg_prior_sleep)} prior period"
        items.append((text, _priority_tier(abs(delta_sleep), high=30, medium=10)))

    # Strength session consistency vs prior period
    if len(recent_strength) or len(prior_strength):
        delta_strength = len(recent_strength) - len(prior_strength)
        arrow = _trend_arrow(delta_strength)
        text = f"{arrow} Strength consistency: {len(recent_strength)} sessions vs {len(prior_strength)} prior 4wk"
        items.append((text, _priority_tier(abs(delta_strength), high=2, medium=1)))

    # HR trend on comparable-effort runs (easy/aerobic pace band only, so it's
    # a genuine like-for-like comparison rather than mixing easy and hard efforts)
    if latest_lt:
        lt_sec = parse_pace_sec(latest_lt["lt_pace"])
        if lt_sec:
            def _easy_runs_hr(runs):
                easy = [r for r in runs if r.get("avg_hr") not in (None, "", "0")
                    and (parse_pace_sec(r.get("avg_pace_min_km")) or 0) > lt_sec + 45]
                hrs = [float(r["avg_hr"]) for r in easy]
                return hrs
            recent_easy_hr = _easy_runs_hr(recent_runs)
            prior_easy_hr = _easy_runs_hr(prior_runs)
            if len(recent_easy_hr) >= 2 and len(prior_easy_hr) >= 2:
                avg_recent_hr = sum(recent_easy_hr) / len(recent_easy_hr)
                avg_prior_hr = sum(prior_easy_hr) / len(prior_easy_hr)
                delta_hr = avg_recent_hr - avg_prior_hr
                arrow = _trend_arrow(delta_hr, threshold=1)
                text = (f"{arrow} Easy-run HR averaging {avg_recent_hr:.0f} bpm vs {avg_prior_hr:.0f} bpm prior period "
                        f"(comparable-effort runs, {len(recent_easy_hr)} vs {len(prior_easy_hr)} runs)")
                items.append((text, _priority_tier(abs(delta_hr), high=5, medium=2)))

    return items

evidence_catalog_tiered = build_evidence_catalog()
evidence_catalog = [text for text, _priority in evidence_catalog_tiered]
evidence_priority_map = {text: priority for text, priority in evidence_catalog_tiered}

# YoY
try:
    today_last_year = today_date.replace(year=today_date.year - 1)
except ValueError:
    today_last_year = today_date.replace(year=today_date.year - 1, day=28)

dist_ytd = sum(float(r.get("distance_km") or 0) for r in runs_this_year)
dist_last_year_ytd = sum(
    float(r.get("distance_km") or 0) for r in runs_prev_year
    if datetime.strptime(r["date"], "%Y-%m-%d").date() <= today_last_year
)

# Personal bests
pb_cats = [("5K", 4), ("10K", 8), ("Half", 18), ("Marathon", 38), ("50K", 45)]
pb_lines = []
for label, min_dist in pb_cats:
    eligible = [r for r in all_run_rows
        if float(r.get("distance_km") or 0) >= min_dist and r.get("avg_pace_min_km")]
    if eligible:
        best = min(eligible, key=lambda r: parse_pace_sec(r["avg_pace_min_km"]) or 9999)
        pb_lines.append(f"{label}: {best['avg_pace_min_km']} /km on {best['date']} ({best.get('distance_km')} km)")

# Recent run details (last 8) — elevation included for trail context
run_details = []
for r in reversed(recent_runs[-8:]):
    elev = f" | ↑{r.get('elevation_gain_m','?')}m" if r.get('elevation_gain_m') else ""
    run_details.append(
        f"  {r['date']} | {r.get('distance_km','?')} km | {r.get('avg_pace_min_km','?')} /km | "
        f"HR {r.get('avg_hr','?')} | load {r.get('training_load','?')} | "
        f"ATE {r.get('aerobic_training_effect','?')}{elev} | {r.get('type','?')}"
    )


# ── Build 12-week context ─────────────────────────────────────────────────────
cutoff_12wk = today_date - timedelta(days=84)
runs_12wk = [r for r in all_runs_sorted
    if r.get("date") and datetime.strptime(r["date"], "%Y-%m-%d").date() >= cutoff_12wk]
dist_12wk = sum(float(r.get("distance_km") or 0) for r in runs_12wk)
avg_weekly_dist_12wk = dist_12wk / 12
avg_weekly_runs_12wk = len(runs_12wk) / 12

# ATE baseline (YTD average — supporting context only)
ate_values = [float(r.get("aerobic_training_effect") or 0)
    for r in all_run_rows if r.get("aerobic_training_effect")
    and r.get("date", "").startswith(str(today_date.year))]
avg_ate_ytd = round(sum(ate_values) / max(len(ate_values), 1), 2)

# Strength sessions per week YTD
strength_per_week_ytd = round(
    summary.get('total_strength_this_year', 0) / max(today_date.timetuple().tm_yday / 7, 1), 1)

# ── Build prompt ──────────────────────────────────────────────────────────────
system_prompt = """You are an experienced hybrid performance coach with a strong sports science background working with Frederik, a Danish athlete with serious ultra-endurance and multi-sport capacity.

Your role is to generate a short daily training status summary based on the data provided.

ATHLETE IDENTITY:
Frederik is not a one-dimensional runner. He trains for dual capacity: ultra-endurance readiness (long efforts, back-to-back durability, time on feet, trail) AND speed/explosive capacity (fast paces, sprint ability). These are complementary qualities within a broader athletic philosophy. He also trains strength seriously, and has broader athletic interests including bouldering, martial arts, swimming, and longevity/mobility work. His daily commute includes 8 km by bike on weekdays. Do not treat him as a runner who also lifts — treat him as a complete athlete.

STANDING PHILOSOPHY:
Frederik's training orientation is long-term progression across endurance, speed, strength, mobility and athleticism. Recommendations should favour sustainable progression over short-term optimisation, while recognising his capacity and willingness to push hard when appropriate.

Do not assume a consolidation or lower-volume week indicates regression. Interpret it in the context of long-term trends, consistency, performance, strength work, background activity and recovery.

COACHING PRINCIPLES:
- Distinguish current training load from demonstrated fitness. A reduction in recent load is not automatically a loss of fitness.
- Do not equate lower recent mileage with low preparedness. Consider long-term history, threshold pace, strength consistency, recent long runs and accumulated base before drawing conclusions.
- For ultra and trail contexts, prioritise consistency, time on feet and back-to-back durability alongside pace development — not instead of it.
- Use recent and medium-term trends to identify whether training load is building, consolidating, recovering or tapering, while recognising the athlete generally maintains year-round readiness rather than strict event periodisation.
- Prefer within-athlete comparisons over population norms unless discussing general physiological principles.
- Interpret trends before sessions. Daily sessions should be viewed primarily as evidence contributing to longer-term trends rather than isolated performances.
- Compare today's session against similar sessions from the athlete's own history whenever possible.
- Only flag potential detraining if multiple metrics suggest it simultaneously.
- Avoid confirmation bias. If the data contradicts prior assumptions about the athlete, favour the data.
- When uncertainty exists, acknowledge it rather than inventing certainty.
- Sleep data (from Polar Loop) is supporting context only. Note it when relevant, but do not change training recommendations or caution level based on sleep — this data stream is new and not yet validated enough to drive advice.

DECISION FRAMEWORK — ask these questions before writing:
1. What changed since the prior period? Is the change meaningful or within normal variance?
2. Does it matter — and if so, why?
3. What should the athlete notice or consider as a result?

Every paragraph must implicitly answer at least one of these. Never merely restate statistics.

Tone and style:
- Data-anchored: root every observation in specific numbers from the data.
- Tonally neutral: neither cheerleader nor alarm bell.
- Conversational when flagging outliers or standout efforts — name them directly and briefly reflect on what they might signal.
- Assume Frederik understands training concepts — no need to explain basics.
- No generic encouragement phrases.

Output format — respond using exactly this structure, with these literal delimiter lines:

HEADLINE:
One sentence, the single most important insight from today's data. This is the answer to "what is today's story?" — not a generic state label like "Building" or "Consolidating". Be specific and data-anchored. Examples of the right level of specificity: "Aerobic fitness remains stable despite reduced peak mileage." / "Threshold fitness continues to strengthen." Do not restate the athlete's name or date.

SUMMARY:
1–3 short paragraphs — as many as the data genuinely warrants, no more. This section is your COACHING JUDGEMENT, not a restatement of numbers: the EVIDENCE section below will carry the specific figures, so do not repeat exact stats here (no "127 km vs 125 km", no "HR 161", no "Load 239" — that level of detail belongs in EVIDENCE only). Instead, lead with the conclusion and only add a sentence of plain-language reasoning if it materially changes how the reader should interpret the conclusion. Someone should be able to read this section alone, in under 30 seconds, and understand the coaching takeaway. No bullet points, no headers, no greeting, no sign-off. Write in second person ("your threshold...", "you've...").

EVIDENCE:
The AVAILABLE EVIDENCE list below (in the data section) is numbered. Write ONLY the numbers of items that genuinely support the headline and summary you wrote — one number per line, nothing else on that line (no text, no restating the item). Select as many or as few as are genuinely relevant, in any order — there is no fixed count. If nothing in the list meaningfully supports today's story, write a single line: "0"

WATCH:
2–4 short bullet points (one per line, starting with "- ") naming specific things worth paying attention to over the coming week — not prescribed workouts or mileage targets, since the training programme is already structured elsewhere. Frame these as things to observe or monitor, e.g. "Watch whether easy-run HR continues to decline." / "Sleep quality may become more important after the long run." If there is nothing meaningfully worth flagging this week, write a single line: "- Nothing notable to flag this week — steady state."

Do not add any text outside these four sections, and use the exact delimiter labels (HEADLINE:, SUMMARY:, EVIDENCE:, WATCH:) on their own lines."""

user_prompt = f"""Today: {today_date} (week {today_date.isocalendar()[1]} of {today_date.year})

ATHLETE PROFILE:
- Experienced ultra and trail runner, Danish, mid-30s
- Longest effort: 137 km (Møn/Vordingborg 100-mile attempt)
- Running PBs: see PERSONAL BESTS below
- Current demonstrated strength profile:
    Squat 95 kg | Bench 110 kg | OH Press 55 kg | BB RDL 130 kg | Pull-ups 16 reps BW
- Daily commute: 8 km by bike on weekdays (untracked background load)
- Active modalities: running, strength, occasional bouldering, martial arts, swimming
- Training frequency: ~4x/week running + regular strength
- Running streak: {summary.get('current_weekly_streak', '?')} weeks current (best: {summary.get('longest_weekly_streak', '?')} weeks)
- Strength streak: {summary.get('current_strength_weekly_streak', '?')} weeks current

STANDING TRAINING FOCUS:
Long-term progression across ultra-endurance, speed capacity, strength, mobility and broad athletic readiness. Not strictly periodising toward a single event — building and maintaining durable hybrid capacity year-round.

THIS YEAR VS LAST:
- Distance to date {today_date.year}: {dist_ytd:.0f} km
- Distance to same date {today_last_year.year}: {dist_last_year_ytd:.0f} km
{"(Note: last year's baseline is low — interpret YoY carefully.)" if dist_last_year_ytd < 100 else ""}

VOLUME CONTEXT:
- 12-week total: {dist_12wk:.0f} km across {len(runs_12wk)} runs ({avg_weekly_dist_12wk:.1f} km/week, {avg_weekly_runs_12wk:.1f} runs/week)
- Recent 4 weeks: {recent_dist:.0f} km across {len(recent_runs)} runs
- Prior 4 weeks: {prior_dist:.0f} km across {len(prior_runs)} runs
- 4-week change: {((recent_dist - prior_dist) / max(prior_dist, 1) * 100):+.0f}%

RECENT RUNS (last 8, oldest first — compare against the athlete's own baseline, not generic norms):
{chr(10).join(run_details) if run_details else "  No runs in the last 4 weeks"}

STRENGTH:
- Last 4 weeks: {len(recent_strength)} sessions
- Year to date: {summary.get('total_strength_this_year', '?')} sessions ({strength_per_week_ytd}/week average)

LACTATE THRESHOLD:
- Current: {latest_lt['lt_pace'] + ' /km @ ' + str(latest_lt['lt_hr']) + ' bpm (' + latest_lt['date'] + ')' if latest_lt else 'No data yet'}
- 30-day baseline: {baseline_lt['lt_pace'] + ' /km (' + baseline_lt['date'] + ')' if baseline_lt else 'Insufficient history — accumulating daily'}

PERSONAL BESTS (outdoor, average pace by minimum distance from full run history):
{chr(10).join(pb_lines) if pb_lines else "No PB data"}

SUPPORTING METRICS:
- Year-to-date average aerobic training effect: {avg_ate_ytd} (supporting context only — prefer pace, heart rate and duration for commentary)
- Average daily steps (last 4 weeks): {avg_daily_steps:,}
- Average steps on rest days: {avg_rest_steps:,}
- Step days tracked: {len(recent_steps)}

SLEEP (last 4 weeks, from Polar Loop — supporting context only, do not let this drive training recommendations):
- Average duration: {f"{avg_sleep_min // 60}h{avg_sleep_min % 60:02d}m" if avg_sleep_min else "No data yet"}
- Average score: {avg_sleep_score if avg_sleep_score is not None else "No data yet"}
- Nights tracked: {len(recent_sleep)}
{chr(10).join(sleep_lines) if sleep_lines else "  No sleep data in the last 4 weeks"}

AVAILABLE EVIDENCE (numbered — in the EVIDENCE: section, write only the numbers of items you select, not the text):
{chr(10).join(f"  {i+1}. {item}" for i, item in enumerate(evidence_catalog)) if evidence_catalog else '  No evidence items available today — write "0" in the EVIDENCE section.'}

Generate the coaching summary now."""

# ── Call Anthropic API ────────────────────────────────────────────────────────
api_key = os.environ.get("ANTHROPIC_API_KEY", "")
coach_text = None
token_usage = None


# ── API pricing ───────────────────────────────────────────────────────────────
# WARNING: These rates are hardcoded and must be updated manually if Anthropic
# changes their pricing. Last verified: 2026-07-01
# Current model: claude-sonnet-4-6
# Pricing for claude-sonnet-4-6 (USD per million tokens)
# Source: https://www.anthropic.com/pricing
# Input:  $3.00 per million tokens
# Output: $15.00 per million tokens
PRICE_INPUT_PER_M  = 3.00
PRICE_OUTPUT_PER_M = 15.00

USD_TO_DKK = 6.90  # fixed rate — approximate, update manually if needed

if api_key:
    try:
        payload = json.dumps({
            "model": "claude-sonnet-4-6",
            "max_tokens": 700,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}]
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            coach_text = result["content"][0]["text"].strip()
            usage = result.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cost_usd = (input_tokens * PRICE_INPUT_PER_M / 1_000_000) + \
                       (output_tokens * PRICE_OUTPUT_PER_M / 1_000_000)
            cost_dkk = cost_usd * USD_TO_DKK
            token_usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost_usd, 6),
                "cost_dkk": round(cost_dkk, 5),
                "date": str(today_date)
            }
            print(f"AI coach summary generated ({len(coach_text)} chars)")
            print(f"  Tokens: {input_tokens} it + {output_tokens} ot = ${cost_usd:.5f} / {cost_dkk:.4f} kr")

    except Exception as e:
        print(f"AI coach API call failed: {e}")
        coach_text = None
else:
    print("ANTHROPIC_API_KEY not set — skipping AI coach")

# ── Fallback ──────────────────────────────────────────────────────────────────
if not coach_text:
    coach_text = "Training data updated — coach summary unavailable today."

# ── Parse HEADLINE / SUMMARY / EVIDENCE / WATCH sections ──────────────────────
# Defensive parsing: if the model doesn't follow the delimited format exactly,
# fall back to treating the whole response as the summary (same spirit as the
# existing "coach unavailable" fallback above) rather than failing the run.
#
# Evidence items are additionally validated against evidence_catalog — only
# lines that EXACTLY match a real pre-computed catalog entry survive. This is
# the actual fabrication guard: even if the model ignores the "copy verbatim"
# instruction and writes something plausible-but-invented, it gets silently
# dropped here rather than displayed as if it were verified fact.
def parse_coach_sections(text):
    headline = ""
    summary = text
    evidence_items = []
    watch_items = []
    try:
        headline_match = re.search(r"HEADLINE:\s*(.+?)(?=\n\s*SUMMARY:)", text, re.DOTALL)
        summary_match = re.search(r"SUMMARY:\s*(.+?)(?=\n\s*EVIDENCE:)", text, re.DOTALL)
        evidence_match = re.search(r"EVIDENCE:\s*(.+?)(?=\n\s*WATCH:)", text, re.DOTALL)
        watch_match = re.search(r"WATCH:\s*(.+)", text, re.DOTALL)

        if headline_match and summary_match:
            headline = headline_match.group(1).strip()
            summary = summary_match.group(1).strip()

            if evidence_match:
                evidence_raw = evidence_match.group(1).strip()
                # Extract every integer the model wrote (defensive: handles "3",
                # "3.", "- 3", "Item 3", etc. — whatever format it picks, as
                # long as a number is somewhere on the line). "0" or no valid
                # in-range numbers simply yields an empty selection.
                selected_indices = sorted(set(
                    int(n) for n in re.findall(r"\d+", evidence_raw)
                    if 1 <= int(n) <= len(evidence_catalog)
                ))
                priority_order = {"High": 0, "Medium": 1, "Low": 2}
                evidence_items = sorted(
                    [{"text": evidence_catalog[i - 1], "priority": evidence_priority_map.get(evidence_catalog[i - 1], "Medium")}
                     for i in selected_indices],
                    key=lambda x: priority_order.get(x["priority"], 1)
                )

            if watch_match:
                watch_raw = watch_match.group(1).strip()
                watch_items = [
                    line.strip().lstrip("- ").strip()
                    for line in watch_raw.split("\n")
                    if line.strip().startswith("-")
                ]
    except Exception as parse_err:
        print(f"Coach section parsing failed, using raw text as summary: {parse_err}")
    return headline, summary, evidence_items, watch_items

coach_headline, coach_summary_text, coach_evidence_items, coach_watch_items = parse_coach_sections(coach_text)

# ── Accumulate usage history ──────────────────────────────────────────────────
usage_file = "api_usage.json"
usage_history = []
if os.path.exists(usage_file):
    with open(usage_file, "r", encoding="utf-8") as f:
        usage_history = json.load(f)

if token_usage:
    # Replace today's entry if already exists
    usage_history = [u for u in usage_history if u.get("date") != str(today_date)]
    usage_history.append(token_usage)
    usage_history = sorted(usage_history, key=lambda x: x["date"], reverse=True)

with open(usage_file, "w", encoding="utf-8") as f:
    json.dump(usage_history, f, indent=2)

# ── Calculate MTD and YTD totals ─────────────────────────────────────────────
this_month = today_date.strftime("%Y-%m")
this_year = str(today_date.year)

mtd_entries = [u for u in usage_history if u.get("date", "").startswith(this_month)]
ytd_entries = [u for u in usage_history if u.get("date", "").startswith(this_year)]

mtd_cost_usd = sum(u.get("cost_usd", 0) for u in mtd_entries)
ytd_cost_usd = sum(u.get("cost_usd", 0) for u in ytd_entries)
mtd_cost_dkk = mtd_cost_usd * USD_TO_DKK
ytd_cost_dkk = ytd_cost_usd * USD_TO_DKK

# ── Write coach_summary.json ──────────────────────────────────────────────────
coach_summary = {
    "last_updated": today.strftime("%Y-%m-%d %H:%M UTC"),
    "headline": coach_headline,
    "confidence_pct": confidence_pct,
    "confidence_label": confidence_label,
    "confidence_reasons": confidence_reasons,
    "summary": coach_summary_text,
    "watch_items": coach_watch_items,
    "evidence_items": coach_evidence_items,
    "insights": [coach_summary_text],
    "quiet": [],
    "usage": token_usage,
    "usage_mtd_usd": round(mtd_cost_usd, 5),
    "usage_mtd_dkk": round(mtd_cost_dkk, 4),
    "usage_ytd_usd": round(ytd_cost_usd, 5),
    "usage_ytd_dkk": round(ytd_cost_dkk, 4)
}

with open("coach_summary.json", "w", encoding="utf-8") as f:
    json.dump(coach_summary, f, indent=2)

print(f"Coach summary written.")
print(f"  MTD: ${mtd_cost_usd:.5f} / {mtd_cost_dkk:.4f} kr")
print(f"  YTD: ${ytd_cost_usd:.5f} / {ytd_cost_dkk:.4f} kr")
print(f"  Headline: {coach_headline[:120]}")
print(f"  Watch items: {len(coach_watch_items)}")
print(f"  Evidence items: {len(coach_evidence_items)} (catalog had {len(evidence_catalog)} available)")
print(f"  Confidence: {confidence_pct}% ({confidence_label})")

