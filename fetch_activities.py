"""
Fred's Feats — raw data fetch (Garmin + Polar only).

Pulls runs/strength from Garmin Connect and sleep/steps from Polar
AccessLink, deduplicates/merges against existing CSVs, and writes:
  runs.csv, strength.csv, lactate.json, sleep.csv, polar_steps.csv

Does NOT compute any aggregates, streaks, deltas, or call the AI coach —
that all lives in build_dashboard.py, which reads the files this script
writes. Splitting it this way means a bug in aggregation/coach logic can
never prevent today's raw activity data from being fetched and committed.
"""
import os
import csv
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from garminconnect import Garmin

email = os.environ["GARMIN_EMAIL"]
password = os.environ["GARMIN_PASSWORD"]

client = Garmin(email, password)
client.login()

# ── Mode detection ────────────────────────────────────────────────────────────
# Full refetch on first Sunday of each month or if FULL_REFRESH env var is set.
# This flag only governs the RAW FETCH here — build_dashboard.py always
# recomputes aggregates fresh from whatever is on disk regardless of mode.
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

# Find cutoff date for incremental fetch.
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
    candidates = [1000 / speed_value, 3600 / speed_value]
    for pace_seconds in candidates:
        if 120 < pace_seconds < 900:
            return sec_to_pace(pace_seconds)
    return ""

def is_valid_lt_record(record):
    pace_sec = parse_pace_sec(record.get("lt_pace"))
    return pace_sec is not None and 120 < pace_sec < 900

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
strength_fieldnames = ["date", "name", "elapsed_time", "duration_min", "activity_id", "start_time"]

def build_strength_row(a):
    duration_s = a.get("duration", 0)
    full_start = a.get("startTimeLocal", "")
    return {
        "date": full_start[:10],
        "name": a.get("activityName", ""),
        "elapsed_time": fmt_time(duration_s),
        "duration_min": round(duration_s / 60, 1) if duration_s else "",
        "activity_id": a.get("activityId", ""),
        "start_time": full_start
    }

# ── Merge new + existing, deduplicate ─────────────────────────────────────────
# Prefer Garmin's own activityId as the true unique key — date+name alone can
# collapse two genuinely different same-day activities that share a name.
# Falls back to date+name only for rows written before this field existed
# (activity_id will be blank on those).
def _row_key(row):
    aid = row.get("activity_id", "")
    if aid:
        return ("id", aid)
    return ("legacy", row.get("date", ""), row.get("name", ""))

def _dedup_new_rows(rows):
    # Garmin's paginated get_activities() can return the same activity twice
    # across overlapping batches. Without this, merge() (and the full-refresh
    # path below, which skips merge() entirely) would carry both copies
    # straight through, silently doubling that activity's distance/duration —
    # this was the cause of the July 2026 same-week-only doubling bug.
    seen = {}
    for r in rows:
        seen.setdefault(_row_key(r), r)
    return list(seen.values())

def merge(new_rows, existing_rows):
    new_rows = _dedup_new_rows(new_rows)
    new_keys = {_row_key(n) for n in new_rows}
    # Permanent transition guard: rows written before activity_id existed key
    # as ("legacy", date, name) — that can never equal a freshly-fetched
    # row's ("id", activity_id) key even when both represent the exact same
    # real activity. Without this check, any activity inside the incremental
    # fetch window that already existed as a legacy row survives merge()
    # TWICE, silently doubling that activity's distance/duration everywhere.
    new_date_names = {(n.get("date", ""), n.get("name", "")) for n in new_rows}
    merged = list(new_rows)
    for r in existing_rows:
        key = _row_key(r)
        if key in new_keys:
            continue
        if key[0] == "legacy" and (r.get("date", ""), r.get("name", "")) in new_date_names:
            continue  # same real activity as a newly-fetched id-keyed row — drop the legacy duplicate
        merged.append(r)
    return sorted(merged, key=lambda x: x.get("date", ""), reverse=True)

new_run_rows = _dedup_new_rows([build_run_row(a) for a in new_running])
new_strength_rows = _dedup_new_rows([build_strength_row(a) for a in new_strength])

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

print(f"runs.csv: {len(all_run_rows)} rows | strength.csv: {len(all_strength_rows)} rows")

# ── Lactate threshold ─────────────────────────────────────────────────────────
lt_records = []
lt_file = "lactate.json"

if os.path.exists(lt_file):
    with open(lt_file, "r", encoding="utf-8") as f:
        lt_records = json.load(f)

# Drop old bad LT records before appending — keeps lactate.json clean, not
# merely hidden by a downstream filter.
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
# the sole source of truth for runs.csv/strength.csv — the athlete's Polar Loop
# mis-tags H10-strap runs as "indoor" activities, so Polar exercise data is
# NOT pulled or merged here. Polar is used only for sleep.csv and
# polar_steps.csv, via simple non-transactional GET requests (token-scoped,
# no user-id in path). The older activity-transactions create/list/commit
# flow is deprecated per Polar's docs and was the source of repeated 405
# errors — replaced July 2026.
#
# NOTE: Polar has been known to adjust response shapes over time — if fields
# come back empty, check https://www.polar.com/accesslink-api/ for the
# current schema before assuming the fetch is broken.

polar_token = os.environ.get("POLAR_ACCESS_TOKEN", "")

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
    # ── Continuous heart rate (via /v3/users/continuous-heart-rate) ─────────
    # NOTE: per Polar's own docs, this resource does NOT include heart rate
    # from training sessions (those come from a separate training-data
    # resource, not implemented here). This is 5-minute-interval all-day HR —
    # useful as general context, but NOT the source for the strength-session
    # HR overlay (see Other Pending) once that's built; that will need the
    # training-session-specific resource, a separate future addition.
    #
    # Rolling 8-day window rather than full history: keeps each run's fetch
    # small, and Polar's 24/7 data endpoints only guarantee ~90 days lookback
    # anyway. Merged into existing polar_hr.csv by (date, time) so re-running
    # the same window twice doesn't duplicate rows.
    try:
        existing_hr = {}
        if os.path.exists("polar_hr.csv"):
            with open("polar_hr.csv", "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("date") and row.get("time"):
                        existing_hr[(row["date"], row["time"])] = row.get("heart_rate", "")

        hr_window_start = (today.date() - timedelta(days=8)).isoformat()
        hr_window_end = today.date().isoformat()
        hr_resp = polar_get(
            f"https://www.polaraccesslink.com/v3/users/continuous-heart-rate?from={hr_window_start}&to={hr_window_end}"
        )
        # Response shape CONFIRMED (July 2026, via live debug output): a dict
        # with key "heart_rates", a list of per-day objects each with "date"
        # and "heart_rate_samples" (list of {"heart_rate": int, "sample_time":
        # "HH:MM:SS"}). Sample spacing is NOT a clean 5-minute grid in
        # practice — can cluster tightly (several samples one minute apart)
        # then gap widely; treat as event-driven, not fixed-interval.
        hr_days = (hr_resp or {}).get("heart_rates", [])
        new_hr_count = 0
        for day in hr_days:
            hr_date = day.get("date", "")
            for sample in day.get("heart_rate_samples", []):
                sample_time = sample.get("sample_time", "")
                hr_val = sample.get("heart_rate")
                if hr_date and sample_time and hr_val is not None:
                    existing_hr[(hr_date, sample_time)] = hr_val
                    new_hr_count += 1

        hr_fieldnames = ["date", "time", "heart_rate"]
        with open("polar_hr.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=hr_fieldnames)
            writer.writeheader()
            for (d, t) in sorted(existing_hr.keys(), reverse=True):
                writer.writerow({"date": d, "time": t, "heart_rate": existing_hr[(d, t)]})

        print(f"Polar continuous HR: {new_hr_count} sample(s) fetched this window, {len(existing_hr)} total in polar_hr.csv")
    except Exception as e:
        print(f"Polar continuous HR fetch skipped: {e}")
else:
    print("POLAR_ACCESS_TOKEN not set — skipping Polar fetch")

# ── Fetch status (for build_dashboard.py's confidence score) ─────────────────
# Written last, only on successful completion of this whole script. If Garmin
# login or activity fetch throws above, this file never gets written/updated —
# build_dashboard.py can then see the timestamp is stale (or missing entirely)
# and score pipeline freshness honestly instead of assuming success just
# because it happened to run in the same job.
with open("fetch_status.json", "w", encoding="utf-8") as f:
    json.dump({
        "last_success_utc": today.strftime("%Y-%m-%d %H:%M UTC"),
        "last_success_date": str(today.date()),
        "mode": "full_refresh" if is_full_refresh else "incremental"
    }, f, indent=2)

print("fetch_activities.py complete — raw data written.")

# ── Per-run HR detail (via Garmin's get_activity_details) ────────────────────
# Pulls raw per-activity HR time series for recent runs, so build_dashboard.py
# can eventually bin actual time-in-zone using our own LT zone boundaries,
# rather than relying only on a single avg_hr per run. Uses Garmin (H10-
# sourced) as the authoritative HR source for runs — kept DELIBERATELY
# separate from polar_hr.csv (Loop-sourced), so the two can be compared
# against each other for the same run window later, the same way steps.csv
# (iPhone) and polar_steps.csv (Polar) are reconciled/compared today. Neither
# source is being discarded in favor of the other.
#
# Confirmed via direct inspection of the installed garminconnect library
# source (July 2026): get_activity_details(activity_id, maxchart, maxpoly)
# calls GET {activity}/{activity_id}/details. The exact response JSON shape
# was NOT independently verified against a real payload before this commit —
# same situation polar_hr.csv was in before its shape bug was found and
# fixed. Parsing below is defensive (searches metricDescriptors for anything
# heart-rate-like and a matching timestamp/elapsed-duration field, rather
# than assuming exact key names) and includes a one-time debug print of the
# first real response's metricDescriptors so the actual shape can be
# confirmed or corrected after the first live run, same as was done for
# polar_hr.csv.
RUN_HR_DETAIL_WINDOW_DAYS = 8

def _find_metric_index(descriptors, keywords):
    """Find the index of a metric whose key/name contains any of the given
    keywords (case-insensitive). Returns None if not found. Defensive against
    not knowing Garmin's exact field naming without a confirmed real payload."""
    for d in descriptors or []:
        key = str(d.get("key", "") or d.get("metricName", "") or "").lower()
        idx = d.get("metricsIndex", d.get("index"))
        if idx is not None and any(kw in key for kw in keywords):
            return idx
    return None

existing_run_hr_activity_ids = set()
if os.path.exists("run_hr_samples.csv"):
    with open("run_hr_samples.csv", "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("activity_id"):
                existing_run_hr_activity_ids.add(row["activity_id"])

hr_detail_cutoff = (today.date() - timedelta(days=RUN_HR_DETAIL_WINDOW_DAYS)).isoformat()
runs_needing_hr_detail = [
    r for r in new_run_rows
    if r.get("activity_id")
    and r.get("date", "") >= hr_detail_cutoff
    and r["activity_id"] not in existing_run_hr_activity_ids
]

new_hr_sample_rows = []
_debug_printed_once = False
for r in runs_needing_hr_detail:
    aid = r["activity_id"]
    try:
        details = client.get_activity_details(aid)
        descriptors = details.get("metricDescriptors", [])

        if not _debug_printed_once:
            print(f"DEBUG run_hr_samples metricDescriptors for activity {aid}: {descriptors}")
            _debug_printed_once = True

        hr_idx = _find_metric_index(descriptors, ["heartrate", "heart_rate"])
        time_idx = _find_metric_index(descriptors, ["timestamp", "elapsedduration", "elapsed_duration", "sumduration"])

        if hr_idx is None or time_idx is None:
            print(f"Run HR detail: could not identify HR/time columns for activity {aid} — skipping")
            continue

        metrics_list = (details.get("activityDetailMetrics") or [])
        for entry in metrics_list:
            values = entry.get("metrics", [])
            if len(values) > max(hr_idx, time_idx):
                hr_val = values[hr_idx]
                t_val = values[time_idx]
                if hr_val is not None and t_val is not None:
                    new_hr_sample_rows.append({
                        "activity_id": aid,
                        "date": r.get("date", ""),
                        "elapsed_seconds": t_val,
                        "heart_rate": hr_val
                    })
        print(f"Run HR detail: {len(metrics_list)} sample(s) processed for activity {aid} ({r.get('date','')})")
    except Exception as e:
        print(f"Run HR detail fetch skipped for activity {aid}: {e}")

if new_hr_sample_rows:
    file_exists = os.path.exists("run_hr_samples.csv")
    with open("run_hr_samples.csv", "a" if file_exists else "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["activity_id", "date", "elapsed_seconds", "heart_rate"])
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_hr_sample_rows)
    print(f"run_hr_samples.csv: appended {len(new_hr_sample_rows)} sample(s) across {len(runs_needing_hr_detail)} run(s)")
else:
    print(f"run_hr_samples.csv: no new samples this run ({len(runs_needing_hr_detail)} run(s) attempted)")
