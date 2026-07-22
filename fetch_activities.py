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

# NEW (July 2026) — hikes previously fell through every filter and were
# never fetched into ANY file at all (not runs.csv, not a separate file,
# nothing) despite an earlier assumption that daily step totals in
# steps.csv/polar_steps.csv "already captured" hike activity — that only
# captures raw step counts, not the fact that a discrete hike happened, so
# it was never actually true that hikes were represented anywhere.
# Deliberately kept OUT of runs.csv (a hike's pace is categorically slower
# than running pace and would skew PB/pace-based evidence and LT-gap
# calculations) — written to its own hikes.csv instead, calendar-visible
# only for now. typeKey "hiking" is Garmin's documented value but NOT yet
# confirmed against a real payload from this account — see the one-time
# DEBUG print below, which will confirm or correct this on the first real
# hike fetched.
def is_hike(a):
    type_key = a.get("activityType", {}).get("typeKey", "").lower()
    return type_key in ["hiking"]

new_running = [a for a in new_activities if is_running(a)]
new_strength = [a for a in new_activities if is_strength(a)]
new_hikes = [a for a in new_activities if is_hike(a)]

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

def _iso_time_to_seconds_of_day(iso_str):
    """Extracts seconds-since-midnight from a timestamp string, tolerant of
    a trailing 'Z', a timezone offset, or fractional seconds. Used to check
    whether a Polar exercise window overlaps a Garmin run window on the
    same date — only the time-of-day matters, not the date itself (that's
    matched separately via the date string).

    CONFIRMED BUG, FIXED (July 2026, via real backfill data): this
    originally assumed a 'T' separator (ISO 8601, e.g. Polar's
    start-time: "2026-07-12T08:41:06"), but Garmin's runs.csv start_time
    is written SPACE-separated ("2026-07-12 08:39:38", from startTimeLocal)
    — a plain .split("T")[1] on a space-separated string raises IndexError,
    silently caught, always returning None. This meant every Garmin run's
    computed start time was None from the very first version of the
    exercise-matching feature, so NO exercise could ever match ANY run,
    for a more fundamental reason than the later moving_time-vs-
    elapsed_time fix (which was correct but never got a chance to matter).
    Confirmed by testing this function directly against a real runs.csv
    row before shipping this fix — do not remove the space-vs-T handling
    without re-confirming both timestamp formats live again."""
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
    """Parses this codebase's own HH:MM:SS duration format (used in
    runs.csv's moving_time) — NOT the same format as Polar's exercise
    duration field, see _parse_iso8601_duration_to_seconds below."""
    try:
        h, m, s = (int(p) for p in dur_str.split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return None

def _parse_iso8601_duration_to_seconds(dur_str):
    """Polar's exercise 'duration' field is typically ISO 8601 duration
    format (e.g. 'PT48M48S'), NOT HH:MM:SS — a different shape from every
    other duration field in this codebase, and unconfirmed against a real
    payload until the first live fetch. Parsed defensively: returns None
    (never raises, never guesses) if the string doesn't match, so a caller
    can skip that entry and log why rather than silently miscomputing."""
    if not dur_str:
        return None
    m = re.match(r'^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$', str(dur_str))
    if not m or not any(m.groups()):
        return None
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    s = float(m.group(3) or 0)
    return h * 3600 + mi * 60 + s

# NOTE (July 2026): a _polar_exercise_local_datetime() UTC-offset-applying
# helper previously lived here, built against the WRONG endpoint (a bare
# GET /v3/exercises that turned out not to be Polar's real Exercises
# resource at all). It was removed after a live diagnostic against the
# REAL transaction-based endpoint (see POLAR EXERCISE TRANSACTION FLOW
# below) proved start-time on that endpoint is ALREADY LOCAL — applying a
# UTC offset to it would have silently shifted every timestamp by +2h.
# Do not re-add offset-shifting logic to exercise start-time without
# re-confirming live first; two differently-named Polar fields
# (start_time on the old endpoint vs start-time on the real one) turned
# out to have opposite timezone conventions despite looking similar.

# ── Build run records ─────────────────────────────────────────────────────────
run_fieldnames = [
    "date", "name", "type", "distance_km", "moving_time", "elapsed_time",
    "avg_hr", "max_hr", "elevation_gain_m", "elevation_loss_m",
    "min_elevation_m", "max_elevation_m", "avg_pace_min_km", "max_pace_min_km",
    "avg_cadence", "calories", "training_load",
    "aerobic_training_effect", "anaerobic_training_effect", "vo2max_estimate",
    "activity_id", "start_time"
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
        "activity_id": a.get("activityId", ""),
        "start_time": a.get("startTimeLocal", "")
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

# ── Build hike records (minimal — calendar visibility only) ──────────────────
# Deliberately NOT the same rich shape as run_fieldnames: hikes aren't fed
# into any pace/PB/LT-zone logic (see is_hike() note above), so only enough
# fields to show a hike on the calendar and, if useful later, a rough
# distance/duration. Extend this if hikes ever need real stats treatment —
# not needed for the current calendar-icon-only ask.
hike_fieldnames = ["date", "name", "distance_km", "elapsed_time", "activity_id", "start_time"]

def build_hike_row(a):
    dist = a.get("distance", 0)
    full_start = a.get("startTimeLocal", "")
    return {
        "date": full_start[:10],
        "name": a.get("activityName", ""),
        "distance_km": round(dist / 1000, 2) if dist else "",
        "elapsed_time": fmt_time(a.get("duration", 0)),
        "activity_id": a.get("activityId", ""),
        "start_time": full_start
    }

# ── Merge new + existing, deduplicate ─────────────────────────────────────────
# Prefer Garmin's own activityId as the true unique key — date+name alone can
# collapse two genuinely different same-day activities that share a name.
# Falls back to date+name only for rows written before this field existed
# (activity_id will be blank on those).
def _row_key(row):
    # NORMALIZED (July 2026, defensive hardening after an unresolved real
    # incident): duplicate rows persisted across several incremental runs
    # despite merge()'s final whole-list dedup pass appearing correct on
    # direct code inspection — root cause never confirmed, only worked
    # around by a FULL_REFRESH (which bypasses merge()/existing_rows
    # entirely and so can't actually confirm or rule out a dedup bug).
    # One real possibility: two rows that LOOK identical could carry
    # activity_id in subtly different string forms after a CSV round-trip
    # (stray whitespace, differing numeric-string formatting), producing
    # two DIFFERENT keys here and silently defeating the dedup with no
    # error. str() + strip() costs nothing and closes that possibility
    # even though it was never proven to be the actual cause — if
    # duplicates recur despite this, the mystery is still open and needs
    # a live debug print of _row_key() output for the specific colliding
    # rows, not another guess.
    aid = str(row.get("activity_id", "") or "").strip()
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
    # Final whole-list dedup pass (added July 2026, after a real duplicate
    # was found in committed data — exact-duplicate activity_id rows for
    # both a run and a strength session, most likely from two overlapping/
    # near-simultaneous workflow runs each independently seeing the same
    # activity as "new" before the other's commit landed). Prior to this,
    # merge() only checked new-vs-existing and within-new — it never checked
    # whether EXISTING rows were already duplicated against EACH OTHER. Any
    # duplicate that reached disk by ANY means would then persist forever,
    # since neither existing check compares two existing rows to each other.
    # This pass makes the pipeline self-healing regardless of how a
    # duplicate got introduced, not just the two previously-patched causes.
    _before_dedup_count = len(merged)
    merged = _dedup_new_rows(merged)
    _collapsed = _before_dedup_count - len(merged)
    if _collapsed > 0:
        print(f"DEBUG merge(): final dedup pass collapsed {_collapsed} duplicate row(s) (before={_before_dedup_count}, after={len(merged)})")
    return sorted(merged, key=lambda x: x.get("date", ""), reverse=True)

new_run_rows = _dedup_new_rows([build_run_row(a) for a in new_running])
new_strength_rows = _dedup_new_rows([build_strength_row(a) for a in new_strength])
new_hike_rows = _dedup_new_rows([build_hike_row(a) for a in new_hikes])

if new_hike_rows:
    print(f"DEBUG hikes: {len(new_hike_rows)} activity(ies) matched typeKey 'hiking' — sample name/date: "
          f"{new_hike_rows[0].get('name')!r} / {new_hike_rows[0].get('date')!r}. "
          f"If this looks wrong (e.g. 0 found when a hike was definitely logged), the real typeKey "
          f"needs confirming from a live Garmin payload — see is_hike().")

existing_hikes = []
if os.path.exists("hikes.csv") and not is_full_refresh:
    with open("hikes.csv", "r", encoding="utf-8") as f:
        existing_hikes = list(csv.DictReader(f))

if is_full_refresh:
    all_run_rows = sorted(new_run_rows, key=lambda x: x.get("date", ""), reverse=True)
    all_strength_rows = sorted(new_strength_rows, key=lambda x: x.get("date", ""), reverse=True)
    all_hike_rows = sorted(new_hike_rows, key=lambda x: x.get("date", ""), reverse=True)
else:
    all_run_rows = merge(new_run_rows, existing_runs)
    all_strength_rows = merge(new_strength_rows, existing_strength)
    all_hike_rows = merge(new_hike_rows, existing_hikes)

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

# ── Write hikes.csv ───────────────────────────────────────────────────────────
with open("hikes.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=hike_fieldnames, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(all_hike_rows)

print(f"hikes.csv: {len(all_hike_rows)} rows")

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
# POLAR_USER_ID: previously stored but unused (every other Polar endpoint
# is scoped by the bearer token alone). Now genuinely needed — the real
# exercise-transactions endpoints are path-scoped by user id, unlike
# sleep/steps/continuous-HR/cardio-load.
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
    # ── Cardio load (via /v3/users/cardio-load) ──────────────────────────────
    # NEW (July 2026). Daily Training Load Pro data: cardio_load, strain
    # (7-day rolling avg load), tolerance (28-day rolling avg load),
    # cardio_load_ratio (strain/tolerance), and cardio_load_status (Polar's
    # own verbal bucket, e.g. "Productive"/"Overreaching"/"Maintaining"/
    # "Detraining"/"Recovering"). Confirmed via Polar's official docs
    # (polar.com/accesslink-api) that this is a distinct daily resource from
    # continuous-heart-rate, returning one entry per day in range.
    #
    # DELIBERATELY NOT pulling Muscle Load: per Polar's own Training Load Pro
    # documentation, Muscle Load is only computed "if you're using a separate
    # running or cycling power sensor with your watch" — this athlete's setup
    # (Garmin watch + H10 strap + Polar Loop) has no power meter, so Muscle
    # Load would not populate regardless of whether it's requested. Cardio
    # Load/Strain/Tolerance require no such sensor and are confirmed to work
    # from HR + duration alone (TRIMP-based), which this setup already
    # provides via the Loop's continuous HR.
    #
    # RESPONSE SHAPE NOT YET CONFIRMED LIVE: Polar's docs show a per-day
    # object shape (date, cardio_load_status, cardio_load, strain, tolerance,
    # cardio_load_ratio, cardio_load_level), but whether the endpoint returns
    # a bare list or wraps it under a key (e.g. "cardio_load_days", by analogy
    # with continuous-heart-rate's "heart_rates" wrapper) is unverified against
    # a real payload — same "don't trust docs, confirm live" discipline as
    # every other Polar/Garmin endpoint in this file. Parsing below handles
    # BOTH a bare list and a dict-wrapped list defensively, and prints the raw
    # top-level shape once so the first real run can confirm/correct this.
    try:
        existing_cardio_load = {}
        if os.path.exists("polar_cardio_load.csv"):
            with open("polar_cardio_load.csv", "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("date"):
                        existing_cardio_load[row["date"]] = row

        cl_resp = polar_get("https://www.polaraccesslink.com/v3/users/cardio-load")

        if isinstance(cl_resp, list):
            cl_days = cl_resp
        elif isinstance(cl_resp, dict):
            # Try the most likely wrapper keys; fall back to "no list found"
            # rather than guessing further.
            cl_days = None
            for k in ("cardio_load_days", "cardio_loads", "days"):
                if isinstance(cl_resp.get(k), list):
                    cl_days = cl_resp[k]
                    break
            if cl_days is None:
                print(f"DEBUG polar cardio-load: dict response, no recognized list key. Top-level keys: {list(cl_resp.keys())}")
                cl_days = []
        else:
            cl_days = []

        if cl_days:
            print(f"DEBUG polar cardio-load: {len(cl_days)} day(s) returned, sample keys: {list(cl_days[0].keys())}")

        new_cl_count = 0
        for day in cl_days:
            cl_date = day.get("date", "")
            if not cl_date:
                continue
            existing_cardio_load[cl_date] = {
                "date": cl_date,
                "cardio_load_status": day.get("cardio_load_status", ""),
                "cardio_load": day.get("cardio_load", ""),
                "strain": day.get("strain", ""),
                "tolerance": day.get("tolerance", ""),
                "cardio_load_ratio": day.get("cardio_load_ratio", "")
            }
            new_cl_count += 1

        cl_fieldnames = ["date", "cardio_load_status", "cardio_load", "strain", "tolerance", "cardio_load_ratio"]
        with open("polar_cardio_load.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=cl_fieldnames, extrasaction="ignore")
            writer.writeheader()
            for date in sorted(existing_cardio_load.keys(), reverse=True):
                writer.writerow(existing_cardio_load[date])

        print(f"Polar cardio load: {new_cl_count} day(s) fetched, {len(existing_cardio_load)} total in polar_cardio_load.csv")
    except Exception as e:
        print(f"Polar cardio load fetch skipped: {e}")

    # ── Polar exercise list (via the REAL exercise-transactions flow) ────────
    # REWRITTEN (July 2026). The original implementation used a bare
    # GET /v3/exercises, which turned out to be the WRONG resource
    # entirely — confirmed via a read-only diagnostic script that never
    # returned matching data, then confirmed via Polar's own official
    # example repo that Exercises is a TRANSACTION-based resource: you
    # must POST to open a transaction, GET the list of exercises within
    # it, GET each exercise's own summary, and PUT to commit — only then
    # does new data become available on a future fetch. This was
    # independently proven against real data: a diagnostic transaction
    # matched 8 of 12 returned entries exactly (duration AND start time,
    # to the minute) against the athlete's own Polar Flow app screenshots,
    # including a real run split into two sessions by a mid-run stop.
    #
    # FIELD NAMES ARE HYPHENATED on this endpoint (start-time, heart-rate,
    # detailed-sport-info, training-load-pro) — CONFIRMED live, NOT the
    # underscored names (start_time) the old wrong endpoint used. Two
    # differently-shaped Polar resources, not a typo.
    #
    # start-time IS ALREADY LOCAL TIME, NOT UTC — CONFIRMED by exact-minute
    # matches against the Polar Flow app's own displayed local times
    # (e.g. "08.41" in the app == "2026-07-12T08:41:06" here). This is the
    # OPPOSITE convention from what the old (wrong) endpoint would have
    # needed. The start-time-utc-offset field IS present in the payload
    # but must NOT be applied to start-time — doing so would incorrectly
    # shift an already-local time by the local UTC offset (+2h in CEST).
    # A previous _polar_exercise_local_datetime() helper that applied this
    # offset has been removed entirely — see the note near the other
    # helper functions above. Do not re-add offset-shifting logic here
    # without re-confirming live first.
    #
    # MUSCLE LOAD CONFIRMED NOT AVAILABLE (independent confirmation of an
    # already-documented finding): every real entry's training-load-pro
    # block showed muscle-load: -1.0 / NOT_AVAILABLE — consistent with
    # Polar's requirement of a separate running/cycling power sensor,
    # which this setup doesn't have. Not pulled into polar_exercises.csv
    # for this reason, same as polar_cardio_load.csv.
    #
    # COMMIT DISCIPLINE (per Polar's own docs: "if you commit without
    # successfully processing and storing the data, you will not be able
    # to retrieve it again through the standard transaction flow"): the
    # transaction is committed ONLY after polar_exercises.csv has been
    # successfully written below. Any exception during processing skips
    # the commit, leaving the same batch available on the next scheduled
    # run — Polar's documented at-least-once delivery guarantee, used
    # exactly as intended rather than fought against.
    #
    # A fresh POST can return 204 (no new data) if a prior transaction was
    # opened and never committed/rolled back — observed live during
    # diagnosis. Handled as a clean no-op: log it and move on, rather than
    # erroring. Two transactions opened during diagnosis were deliberately
    # left uncommitted for safety; this first real run may encounter that
    # backlog, or Polar may have already cleared it — either way this code
    # only ever acts on whatever transaction it's actually given.
    if polar_user_id:
        try:
            existing_exercises = {}
            if os.path.exists("polar_exercises.csv"):
                with open("polar_exercises.csv", "r", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if row.get("polar_exercise_id"):
                            existing_exercises[row["polar_exercise_id"]] = row

            # PENDING_TRANSACTION_FILE: if a prior run detected a near-miss
            # (see COMMIT DISCIPLINE below) and deliberately skipped
            # committing, it wrote this file so a LATER run can resume the
            # exact same transaction directly rather than hitting 204 forever
            # (a fresh POST while a transaction is open just returns 204,
            # with no way back to it unless we already know its resource-uri
            # — confirmed live during diagnosis). Cleared only after a
            # successful commit.
            PENDING_TRANSACTION_FILE = "polar_pending_transaction.json"
            transaction_id = None
            resource_url = None

            if os.path.exists(PENDING_TRANSACTION_FILE):
                with open(PENDING_TRANSACTION_FILE, "r", encoding="utf-8") as f:
                    pending = json.load(f)
                transaction_id = pending.get("transaction_id")
                resource_url = pending.get("resource_url")
                print(f"Polar exercises: resuming pending transaction {transaction_id} from a prior run's near-miss")
            else:
                create_url = f"https://www.polaraccesslink.com/v3/users/{polar_user_id}/exercise-transactions"
                create_req = urllib.request.Request(
                    create_url,
                    headers={"Authorization": f"Bearer {polar_token}", "Accept": "application/json"},
                    method="POST"
                )
                try:
                    with urllib.request.urlopen(create_req, timeout=30) as resp:
                        if resp.status == 204:
                            print("Polar exercises: 204 No Content — no new exercise data this run")
                        else:
                            tx_body = json.loads(resp.read().decode("utf-8"))
                            transaction_id = tx_body.get("transaction-id")
                            resource_url = tx_body.get("resource-uri")
                            print(f"Polar exercises: opened transaction {transaction_id}")
                            with open(PENDING_TRANSACTION_FILE, "w", encoding="utf-8") as f:
                                json.dump({"transaction_id": transaction_id, "resource_url": resource_url}, f)
                except urllib.error.HTTPError as e:
                    _log_polar_http_error("exercise-transaction create", e)

            if resource_url:
                list_resp = polar_get(resource_url)
                exercise_urls = (list_resp or {}).get("exercises", [])
                print(f"Polar exercises: {len(exercise_urls)} entr(ies) in transaction {transaction_id}")

                runs_by_date = {}
                for r in all_run_rows:
                    d = r.get("date")
                    if d:
                        runs_by_date.setdefault(d, []).append(r)

                new_exercise_count = 0
                had_near_miss = False
                for ex_url in exercise_urls:
                    ex = polar_get(ex_url)
                    if not ex:
                        continue
                    ex_id = str(ex.get("id", ""))
                    ex_start = ex.get("start-time", "")  # already local — do NOT apply start-time-utc-offset
                    ex_duration_raw = ex.get("duration", "")
                    if not ex_id or not ex_start:
                        continue

                    ex_date = ex_start[:10]
                    ex_start_sec = _iso_time_to_seconds_of_day(ex_start)
                    ex_duration_sec = _parse_iso8601_duration_to_seconds(ex_duration_raw)
                    if ex_start_sec is None or ex_duration_sec is None:
                        print(f"Polar exercise {ex_id}: could not parse start-time/duration ({ex_start!r}, {ex_duration_raw!r}) — skipping")
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
                        # Using elapsed_time here deliberately, NOT moving_time —
                        # moving_time excludes paused/stopped time, but Polar's
                        # Loop tracks real wall-clock exercise windows. A run
                        # with a real mid-run stop (auto-pause or manual pause
                        # on the watch) would have moving_time understate the
                        # true window end, potentially missing genuine overlap
                        # with a Loop-detected session that started after the
                        # stop. elapsed_time (wall-clock, pause-inclusive) is
                        # the correct basis for matching against Polar's
                        # windows specifically — this is intentionally
                        # DIFFERENT from moving_time's use elsewhere in this
                        # codebase (pace/effort calculations), where excluding
                        # pauses is the correct choice instead.
                        run_dur_sec = _duration_str_to_seconds(r.get("elapsed_time", ""))
                        if run_start_sec is None or run_dur_sec is None:
                            continue
                        run_end_sec = run_start_sec + run_dur_sec
                        if ex_start_sec <= run_end_sec and run_start_sec <= ex_end_sec:
                            matched_run = r
                            break

                    if not matched_run:
                        # Diagnostic for the case where a Garmin run exists on
                        # the SAME DATE but the windows still didn't overlap —
                        # makes the actual gap visible in the log instead of
                        # just "0 matched" with no way to tell why. Also
                        # tracked as had_near_miss below — see COMMIT
                        # DISCIPLINE note near the commit call for why this
                        # gates whether we commit this run at all.
                        for r in same_date_runs:
                            rs = _iso_time_to_seconds_of_day(r.get("start_time", ""))
                            rd = _duration_str_to_seconds(r.get("elapsed_time", ""))
                            if rs is not None and rd is not None:
                                had_near_miss = True
                                print(f"Polar exercise {ex_id} NEAR MISS: exercise window {ex_start_sec}-{ex_end_sec}s vs Garmin run {r.get('activity_id')} window {rs}-{rs+rd}s on {ex_date} (gap: {min(abs(ex_start_sec - (rs+rd)), abs(rs - ex_end_sec))}s)")
                        continue  # no genuine Garmin run overlaps — likely a non-run Loop-detected session, or see NEAR MISS above

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
                    new_exercise_count += 1

                ex_fieldnames = ["date", "garmin_activity_id", "polar_exercise_id", "start_time", "duration_min", "sport", "avg_hr", "max_hr"]
                with open("polar_exercises.csv", "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=ex_fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    for eid in sorted(existing_exercises.keys(), key=lambda k: existing_exercises[k].get("date", ""), reverse=True):
                        writer.writerow(existing_exercises[eid])

                print(f"Polar exercises: {new_exercise_count} matched to a real Garmin run this fetch, {len(existing_exercises)} total in polar_exercises.csv")

                # COMMIT DISCIPLINE, extended (July 2026, after a real miss):
                # committing only on "no exception" wasn't enough — a run
                # was written successfully with 0 matches due to a matching
                # LOGIC bug (moving_time vs elapsed_time), and that "success"
                # still triggered a commit, permanently losing the ability
                # to re-fetch that exact batch once the bug was found and
                # fixed. Now: if ANY near-miss was detected this run (a
                # same-date Garmin run existed but didn't overlap), treat
                # that as a signal matching may still be incomplete/wrong,
                # and skip the commit entirely — the same batch stays
                # available for the next run once matching is trusted again.
                # A transaction with genuinely no near-misses (e.g. every
                # unmatched entry is a real non-run Loop session, like a
                # misdetected bike commute) commits normally.
                if had_near_miss:
                    print("Polar exercises: NEAR MISS(es) detected — commit skipped so this batch can be retried; see NEAR MISS lines above for the gap")
                    print(f"Polar exercises: transaction {transaction_id} left pending in {PENDING_TRANSACTION_FILE} for the next run to resume")
                else:
                    # Commit ONLY now that the CSV write above succeeded AND
                    # no near-misses were flagged.
                    commit_req = urllib.request.Request(
                        resource_url,
                        headers={"Authorization": f"Bearer {polar_token}", "Accept": "application/json"},
                        method="PUT"
                    )
                    try:
                        with urllib.request.urlopen(commit_req, timeout=30) as resp:
                            print(f"Polar exercises: transaction {transaction_id} committed (status {resp.status})")
                            if os.path.exists(PENDING_TRANSACTION_FILE):
                                os.remove(PENDING_TRANSACTION_FILE)
                    except urllib.error.HTTPError as e:
                        _log_polar_http_error(f"exercise-transaction {transaction_id} commit", e)
                        print(f"Polar exercises: commit failed — same data will be offered again next run")
        except Exception as e:
            print(f"Polar exercises fetch skipped: {e}")
    else:
        print("POLAR_USER_ID not set — skipping Polar exercises (transaction endpoints require it)")

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
RUN_HR_DETAIL_WINDOW_DAYS = 8

def _find_metric(descriptors, keywords):
    for d in descriptors or []:
        key = str(d.get("key", "") or "").lower()
        idx = d.get("metricsIndex")
        factor = (d.get("unit") or {}).get("factor", 1.0)
        if idx is not None and any(kw in key for kw in keywords):
            return idx, (factor if factor else 1.0)
    return None, None

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

        hr_idx, hr_factor = _find_metric(descriptors, ["heartrate", "heart_rate"])
        time_idx, _ = _find_metric(descriptors, ["timestamp"])

        if not _debug_printed_once:
            hr_key = descriptors[hr_idx]["key"] if hr_idx is not None else None
            time_key = descriptors[time_idx]["key"] if time_idx is not None else None
            print(f"DEBUG resolved columns for activity {aid}: hr_idx={hr_idx} key={hr_key} factor={hr_factor} | time_idx={time_idx} key={time_key} (self-normalized, factor ignored)")
            _debug_printed_once = True

        if hr_idx is None or time_idx is None:
            print(f"Run HR detail: could not identify HR/time columns for activity {aid} — skipping")
            continue

        metrics_list = (details.get("activityDetailMetrics") or [])
        raw_pairs = []
        for entry in metrics_list:
            values = entry.get("metrics", [])
            if len(values) > max(hr_idx, time_idx):
                hr_raw = values[hr_idx]
                t_raw = values[time_idx]
                if hr_raw is not None and t_raw is not None:
                    raw_pairs.append((t_raw, hr_raw))

        if raw_pairs:
            min_t = min(t for t, _ in raw_pairs)
            for t_raw, hr_raw in raw_pairs:
                new_hr_sample_rows.append({
                    "activity_id": aid,
                    "date": r.get("date", ""),
                    "elapsed_seconds": round((t_raw - min_t) / 1000, 1),
                    "heart_rate": round(hr_raw / hr_factor)
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

# ── Garmin fitness metrics (VO2max) ────────────────────────────────────────────
try:
    today_str = today.strftime("%Y-%m-%d")
    max_metrics_resp = client.get_max_metrics(today_str)
    if max_metrics_resp:
        generic = max_metrics_resp[0].get("generic", {}) or {}
        vo2max_value = generic.get("vo2MaxValue")
        vo2max_precise = generic.get("vo2MaxPreciseValue")

        existing_fitness_rows = []
        if os.path.exists("garmin_fitness_metrics.csv"):
            with open("garmin_fitness_metrics.csv", "r", encoding="utf-8") as f:
                existing_fitness_rows = list(csv.DictReader(f))
        existing_fitness_rows = [r for r in existing_fitness_rows if r.get("date") != today_str]
        existing_fitness_rows.insert(0, {
            "date": today_str,
            "vo2max_value": vo2max_value if vo2max_value is not None else "",
            "vo2max_precise": vo2max_precise if vo2max_precise is not None else ""
        })

        with open("garmin_fitness_metrics.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["date", "vo2max_value", "vo2max_precise"])
            writer.writeheader()
            writer.writerows(existing_fitness_rows)
        print(f"garmin_fitness_metrics.csv: recorded {today_str} (VO2max {vo2max_value}), {len(existing_fitness_rows)} total rows")
    else:
        print(f"Garmin fitness metrics: no new estimate for {today_str} (empty response — normal, not every day has one)")
except Exception as e:
    print(f"Garmin fitness metrics fetch skipped: {e}")
