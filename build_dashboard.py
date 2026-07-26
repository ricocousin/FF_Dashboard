"""
Fred's Feats — aggregation + AI coach.

Reads runs.csv, strength.csv, lactate.json, sleep.csv, polar_steps.csv,
steps.csv, strength_tests.csv (all written by fetch_activities.py or, for
steps.csv/strength_tests.csv, by other sources) and computes EVERY derived
dashboard number (streaks, deltas, chart data, PBs, LT zones/trend, calendar
date-sets) into dashboard_metrics.json. Then calls
Claude to generate the daily coaching briefing into coach_summary.json.

index.html remains a pure renderer of these two JSON files.

Deliberately independent of fetch_activities.py: this script always
recomputes fresh from whatever is currently on disk, regardless of whether
today's Garmin/Polar fetch succeeded, partially succeeded, or was skipped.
It never calls Garmin or Polar itself and can be re-run any number of times
against the same on-disk data (e.g. while iterating on the coach prompt)
without burning a Garmin login or an extra API fetch.
"""
import os
import csv
import json
import re
import random
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

today = datetime.today()

# Human-facing "last updated" stamp in Danish local time (the athlete is in
# Denmark; the GitHub runner is UTC, which is why this previously showed UTC).
# %Z renders CEST/CET automatically from the zone. Falls back to a UTC stamp
# only if the tz database is somehow unavailable.
try:
    last_updated_str = datetime.now(ZoneInfo("Europe/Copenhagen")).strftime("%Y-%m-%d %H:%M %Z")
except Exception:
    last_updated_str = today.strftime("%Y-%m-%d %H:%M UTC")

# ── Load raw data from disk ───────────────────────────────────────────────────
def _load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

all_run_rows = sorted(_load_csv("runs.csv"), key=lambda x: x.get("date", ""), reverse=True)
all_strength_rows = sorted(_load_csv("strength.csv"), key=lambda x: x.get("date", ""), reverse=True)

STRENGTH_DURATION_SANITY_CAP_MIN = 240  # 4 hours
for _row in all_strength_rows:
    try:
        _dur = float(_row.get("duration_min") or 0)
        if _dur > STRENGTH_DURATION_SANITY_CAP_MIN:
            _row["duration_min"] = str(STRENGTH_DURATION_SANITY_CAP_MIN)
    except (ValueError, TypeError):
        pass

_polar_hr_by_date = {}
for row in _load_csv("polar_hr.csv"):
    d = row.get("date")
    t = row.get("time")
    hr = row.get("heart_rate")
    if d and t and hr not in (None, ""):
        _polar_hr_by_date.setdefault(d, []).append((t, hr))

_run_hr_by_activity = {}
for row in _load_csv("run_hr_samples.csv"):
    aid = row.get("activity_id")
    es = row.get("elapsed_seconds")
    hr = row.get("heart_rate")
    if aid and es not in (None, "") and hr not in (None, ""):
        try:
            _run_hr_by_activity.setdefault(aid, []).append((float(es), float(hr)))
        except (ValueError, TypeError):
            continue
for _aid in _run_hr_by_activity:
    _run_hr_by_activity[_aid].sort(key=lambda x: x[0])

# polar_exercises.csv — written by fetch_activities.py via GET /v3/exercises,
# already plausibility-filtered to only entries overlapping a real Garmin
# run window (see that file for the full matching logic). Indexed by
# garmin_activity_id for the three-way HR comparison below. Diagnostic
# only at this stage — not used for any fallback/backfill logic yet.
_polar_exercise_by_garmin_id = {}
for row in _load_csv("polar_exercises.csv"):
    gid = row.get("garmin_activity_id")
    if gid:
        _polar_exercise_by_garmin_id[gid] = row

# ── Helpers ────────────────────────────────────────────────────────────────
def sec_to_pace(seconds):
    if not seconds or seconds <= 0:
        return ""
    total_seconds = int(round(seconds))
    pace_min = total_seconds // 60
    pace_s = total_seconds % 60
    return f"{pace_min}:{pace_s:02d}"

def parse_pace_sec(pace_str):
    if not pace_str:
        return None
    try:
        m, s = str(pace_str).split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return None

def is_valid_lt_record(record):
    pace_sec = parse_pace_sec(record.get("lt_pace"))
    return pace_sec is not None and 120 < pace_sec < 900

def get_week(date):
    return date.isocalendar()[:2]

# ── Aggregate stats ───────────────────────────────────────────────────────────
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

# Hikes (NEW — feeds the 16-week/annual chart stacking + combined running
# totals below; kept as a distinct list from complete_run_rows so PBs/pace/
# LT-gap/zone-time logic elsewhere in this file remains untouched — hikes
# are deliberately still excluded from all of that, see hikes.csv notes).
all_hike_rows = sorted(_load_csv("hikes.csv"), key=lambda x: x.get("date", ""), reverse=True)
complete_hike_rows = [h for h in all_hike_rows if row_date(h) and h["date"] <= last_complete_str]

prev_year_start = datetime(today.year - 1, 1, 1).date()
prev_year_end = datetime(today.year - 1, 12, 31).date()

summary_runs_this_year = [a for a in complete_run_rows if a.get("date", "")[:4] == str(today.year)]
summary_runs_prev_year = [a for a in complete_run_rows
    if str(prev_year_start) <= a.get("date", "") <= str(prev_year_end)]

summary_strength_this_year = [a for a in complete_strength_rows if a.get("date", "")[:4] == str(today.year)]

total_distance_this_year = sum(float(a.get("distance_km") or 0) for a in summary_runs_this_year)
total_distance_prev_year = sum(float(a.get("distance_km") or 0) for a in summary_runs_prev_year)
total_strength_min_this_year = sum(float(a.get("duration_min") or 0) for a in summary_strength_this_year)

# Hike distance sums (this year / prev year), same "complete days only" cutoff
# as everything above. Folds hikes into the RUNNING tile's combined "Avg /
# week" + yearly-progress totals further down this file, per explicit
# athlete direction — hike km stacks on top of run km in both volume charts
# AND these two overview figures, with a colored note showing how much of
# the total came from hiking specifically.
summary_hikes_this_year = [h for h in complete_hike_rows if h.get("date", "")[:4] == str(today.year)]
summary_hikes_prev_year = [h for h in complete_hike_rows
    if str(prev_year_start) <= h.get("date", "") <= str(prev_year_end)]
total_hike_distance_this_year = sum(float(h.get("distance_km") or 0) for h in summary_hikes_this_year)
total_hike_distance_prev_year = sum(float(h.get("distance_km") or 0) for h in summary_hikes_prev_year)

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
    "last_updated": last_updated_str,
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

# ── Load LT history ────────────────────────────────────────────────────────────
lt_file = "lactate.json"
lt_history = []
if os.path.exists(lt_file):
    with open(lt_file, "r", encoding="utf-8") as f:
        raw_lt = json.load(f)
    lt_history = [r for r in raw_lt if is_valid_lt_record(r)]
    lt_history = sorted(lt_history, key=lambda x: x["date"])

latest_lt = lt_history[-1] if lt_history else None
baseline_lt = next((r for r in reversed(lt_history)
    if datetime.strptime(r["date"], "%Y-%m-%d").date() <= today_date - timedelta(days=30)), None)

# ── Steps: reconciled from iPhone + Polar ─────────────────────────────────────
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
STEPS_DISAGREEMENT_THRESHOLD = 0.5
for d in set(list(iphone_steps_data.keys()) + list(polar_steps_data_for_coach.keys())):
    iphone_val = iphone_steps_data.get(d)
    polar_val = polar_steps_data_for_coach.get(d)
    if iphone_val and polar_val:
        larger = max(iphone_val, polar_val)
        disagreement = abs(iphone_val - polar_val) / larger if larger else 0
        if disagreement > STEPS_DISAGREEMENT_THRESHOLD:
            steps_data[d] = larger
        else:
            steps_data[d] = round((iphone_val + polar_val) / 2)
    elif iphone_val:
        steps_data[d] = iphone_val
    elif polar_val:
        steps_data[d] = polar_val

# ── Sleep data ─────────────────────────────────────────────────────────────────
sleep_data = {}
if os.path.exists("sleep.csv"):
    with open("sleep.csv", "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date"):
                sleep_data[row["date"]] = row

# ── Polar cardio load (Training Load Pro: strain / tolerance / ratio) ────────
# NEW (July 2026). polar_cardio_load.csv written by fetch_activities.py via
# GET /v3/users/cardio-load. Strain (7d rolling avg load) and Tolerance (28d
# rolling avg load) are already Polar-computed rolling averages, so no
# further windowing is needed here — just read the latest row and build a
# trend array for the chart. Deliberately kept as its OWN section (own units:
# an arbitrary TRIMP-derived load score, not minutes or a 0-100 score) rather
# than merged into "recovery" — see project notes on this: strain/tolerance
# and sleep-based recovery are cross-referenced side by side on the
# dashboard, not combined into a single number, since they measure different
# things (load applied vs. resourcing available) on different scales.
cardio_load_rows = sorted(_load_csv("polar_cardio_load.csv"), key=lambda x: x.get("date", ""))
_latest_cl = cardio_load_rows[-1] if cardio_load_rows else None

def _cl_float(row, key):
    v = row.get(key) if row else None
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None

_latest_strain = _cl_float(_latest_cl, "strain")
_latest_tolerance = _cl_float(_latest_cl, "tolerance")
_latest_ratio = _cl_float(_latest_cl, "cardio_load_ratio")
# Fall back to computing the ratio ourselves if Polar's own field is missing
# but we have both inputs — keeps the card usable even if that one field
# comes back blank on a given day.
if _latest_ratio is None and _latest_strain is not None and _latest_tolerance not in (None, 0):
    _latest_ratio = _latest_strain / _latest_tolerance

CARDIO_LOAD_TREND_DAYS = 28
_cl_trend_rows = [r for r in cardio_load_rows if r.get("date", "") >= str(today_date - timedelta(days=CARDIO_LOAD_TREND_DAYS))]
cardio_load = {
    "latest_date": _latest_cl["date"] if _latest_cl else None,
    "status": (_latest_cl.get("cardio_load_status") or None) if _latest_cl else None,
    "strain": _latest_strain,
    "tolerance": _latest_tolerance,
    "ratio": round(_latest_ratio, 2) if _latest_ratio is not None else None,
    "trend": {
        "labels": [r["date"][5:] for r in _cl_trend_rows],
        "strain": [_cl_float(r, "strain") for r in _cl_trend_rows],
        "tolerance": [_cl_float(r, "tolerance") for r in _cl_trend_rows]
    } if len(_cl_trend_rows) > 1 else None
}

# ── Strength/power/durability test history (manually maintained) ─────────────
strength_test_history = {}

# ── Garmin fitness metrics (VO2max + fitness age) ─────────────────────────────
garmin_fitness_rows = sorted(_load_csv("garmin_fitness_metrics.csv"), key=lambda x: x.get("date", ""))
_latest_fitness = garmin_fitness_rows[-1] if garmin_fitness_rows else None
fitness_metrics = {
    "latest_date": _latest_fitness["date"] if _latest_fitness else None,
    "latest_vo2max": float(_latest_fitness["vo2max_value"]) if _latest_fitness and _latest_fitness.get("vo2max_value") else None,
    "latest_vo2max_precise": float(_latest_fitness["vo2max_precise"]) if _latest_fitness and _latest_fitness.get("vo2max_precise") else None,
    "trend": {
        "labels": [r["date"][5:] for r in garmin_fitness_rows if r.get("vo2max_value")],
        "vo2max": [float(r["vo2max_value"]) for r in garmin_fitness_rows if r.get("vo2max_value")]
    }
}
if os.path.exists("strength_tests.csv"):
    with open("strength_tests.csv", "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date") and row.get("exercise") and row.get("value"):
                strength_test_history.setdefault(row["exercise"], []).append({
                    "date": row["date"],
                    "value": row["value"],
                    "unit": row.get("unit", ""),
                    "category": row.get("category", "")
                })
    for ex in strength_test_history:
        strength_test_history[ex].sort(key=lambda r: r["date"])

# ── Upcoming events (manually maintained) ─────────────────────────────────────
UPCOMING_EVENTS_MAX = 3
upcoming_events = []
if os.path.exists("upcoming_events.csv"):
    with open("upcoming_events.csv", "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("date") and row.get("event_name"):
                try:
                    event_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if event_date >= today_date:
                    upcoming_events.append({
                        "date": row["date"],
                        "event_name": row["event_name"],
                        "distance_km": row.get("distance_km", ""),
                        "notes": row.get("notes", "")
                    })
    upcoming_events.sort(key=lambda e: e["date"])
    upcoming_events = upcoming_events[:UPCOMING_EVENTS_MAX]

# ── AI Coach data context ─────────────────────────────────────────────────────
all_runs_sorted = sorted(all_run_rows, key=lambda x: x.get("date", ""))
all_strength_sorted = sorted(all_strength_rows, key=lambda x: x.get("date", ""))

runs_this_year = [a for a in all_run_rows if a.get("date", "")[:4] == str(today.year)]
runs_prev_year = [a for a in all_run_rows
    if str(prev_year_start) <= a.get("date", "") <= str(prev_year_end)]
strength_this_year = [a for a in all_strength_rows if a.get("date", "")[:4] == str(today.year)]

cutoff_4wk = today_date - timedelta(days=28)
cutoff_8wk = today_date - timedelta(days=56)

recent_runs = [r for r in all_runs_sorted
    if r.get("date") and datetime.strptime(r["date"], "%Y-%m-%d").date() >= cutoff_4wk]
prior_runs = [r for r in all_runs_sorted
    if r.get("date") and cutoff_8wk <= datetime.strptime(r["date"], "%Y-%m-%d").date() < cutoff_4wk]

recent_dist = sum(float(r.get("distance_km") or 0) for r in recent_runs)
prior_dist = sum(float(r.get("distance_km") or 0) for r in prior_runs)

recent_strength = [s for s in all_strength_sorted
    if s.get("date") and datetime.strptime(s["date"], "%Y-%m-%d").date() >= cutoff_4wk]
prior_strength = [s for s in all_strength_sorted
    if s.get("date") and cutoff_8wk <= datetime.strptime(s["date"], "%Y-%m-%d").date() < cutoff_4wk]

recent_steps = {d: s for d, s in steps_data.items()
    if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_4wk}
avg_daily_steps = round(sum(recent_steps.values()) / max(len(recent_steps), 1))

rest_day_steps = [s for d, s in recent_steps.items()
    if d not in {r["date"] for r in recent_runs}
    and d not in {s["date"] for s in recent_strength}]
avg_rest_steps = round(sum(rest_day_steps) / max(len(rest_day_steps), 1)) if rest_day_steps else 0

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

# ── Dashboard metrics (single source of truth for index.html) ────────────────
def _iso_week_key(d):
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"

def _week_start_date(iso_key):
    iso_year, iso_week = iso_key.split("-W")
    return datetime.fromisocalendar(int(iso_year), int(iso_week), 1).date()

def _shift_week_key(iso_key, weeks):
    return _iso_week_key(_week_start_date(iso_key) + timedelta(weeks=weeks))

def _week_range(start_key, end_key):
    weeks, wk, guard = [], start_key, 0
    while wk <= end_key and guard < 800:
        weeks.append(wk)
        wk = _shift_week_key(wk, 1)
        guard += 1
    return weeks

def _run_mins(row):
    parts = (row.get("moving_time") or "").split(":")
    if len(parts) != 3:
        return 0
    h, m, s = (int(p) for p in parts)
    return h * 60 + m + s / 60

def _qual(delta, threshold):
    if delta is None:
        return None
    if abs(delta) <= threshold:
        return "Stable"
    return "Increased" if delta > 0 else "Decreased"

this_year_str = str(today_date.year)
prev_year_str = str(today_date.year - 1)
this_month_short = today_date.strftime("%b")

year_runs = [r for r in complete_run_rows if r.get("date", "").startswith(this_year_str)]
year_sessions = [s for s in complete_strength_rows if s.get("date", "").startswith(this_year_str)]
year_hikes = [h for h in complete_hike_rows if h.get("date", "").startswith(this_year_str)]
year_dist = sum(float(r.get("distance_km") or 0) for r in year_runs)
year_hike_dist = sum(float(h.get("distance_km") or 0) for h in year_hikes)
total_strength_min_year = sum(float(s.get("duration_min") or 0) for s in year_sessions)

run_weeks_year = {_iso_week_key(datetime.strptime(r["date"], "%Y-%m-%d").date()) for r in year_runs if r.get("date")}
s_weeks_year = {_iso_week_key(datetime.strptime(s["date"], "%Y-%m-%d").date()) for s in year_sessions if s.get("date")}
hike_weeks_year = {_iso_week_key(datetime.strptime(h["date"], "%Y-%m-%d").date()) for h in year_hikes if h.get("date")}
activity_weeks_year = run_weeks_year | s_weeks_year
# Combined run-or-hike week set — denominator for the combined "Avg / week"
# figure below, so a week containing ONLY a hike (no run) still counts
# toward the average rather than being silently excluded.
run_or_hike_weeks_year = run_weeks_year | hike_weeks_year
num_run_weeks = max(len(run_weeks_year), 1)
num_s_weeks = max(len(s_weeks_year), 1)
num_activity_weeks = max(len(activity_weeks_year), 1)
num_run_or_hike_weeks = max(len(run_or_hike_weeks_year), 1)

# NEW — folds hike km into the Running tile's "Avg / week" figure, per
# explicit athlete direction: hike distance stacks on top of run distance
# in both volume charts AND this headline average (see also the yearly
# progress bar below and the combined weekly delta a few lines down). A
# dedicated "hike_distance_this_year_km" field is surfaced separately in
# dashboard_metrics so index.html can render a distinct-colored note
# showing how much of the combined total came from hiking specifically —
# this figure is never silently blended without attribution.
avg_run_dist_per_week = (year_dist + year_hike_dist) / num_run_or_hike_weeks
avg_runs_per_week = len(year_runs) / num_run_weeks
avg_run_dist_per_run = year_dist / max(len(year_runs), 1)
avg_sess_per_week = len(year_sessions) / num_s_weeks

month_runs = [r for r in year_runs if datetime.strptime(r["date"], "%Y-%m-%d").date().strftime("%b") == this_month_short]
month_sessions = [s for s in year_sessions if datetime.strptime(s["date"], "%Y-%m-%d").date().strftime("%b") == this_month_short]
month_run_weeks = {_iso_week_key(datetime.strptime(r["date"], "%Y-%m-%d").date()) for r in month_runs}
month_s_weeks = {_iso_week_key(datetime.strptime(s["date"], "%Y-%m-%d").date()) for s in month_sessions}
month_activity_weeks = month_run_weeks | month_s_weeks
num_month_run_weeks = max(len(month_run_weeks), 1)
num_month_s_weeks = max(len(month_s_weeks), 1)
num_month_activity_weeks = max(len(month_activity_weeks), 1)

total_run_mins_year = sum(_run_mins(r) for r in year_runs)
month_run_mins = sum(_run_mins(r) for r in month_runs)
month_strength_mins = sum(float(s.get("duration_min") or 0) for s in month_sessions)
total_activity_mins_year = total_run_mins_year + total_strength_min_year
avg_activity_mins_per_week = total_activity_mins_year / num_activity_weeks
avg_activity_mins_per_week_month = (month_run_mins + month_strength_mins) / num_month_activity_weeks

last_complete_week = _iso_week_key(last_complete_date)
prev_complete_week = _shift_week_key(last_complete_week, -1)

def _week_run_dist(wk):
    return sum(float(r.get("distance_km") or 0) for r in complete_run_rows if r.get("date") and _iso_week_key(datetime.strptime(r["date"], "%Y-%m-%d").date()) == wk)

def _week_hike_dist(wk):
    return sum(float(h.get("distance_km") or 0) for h in complete_hike_rows if h.get("date") and _iso_week_key(datetime.strptime(h["date"], "%Y-%m-%d").date()) == wk)

def _week_strength_count(wk):
    return sum(1 for s in complete_strength_rows if s.get("date") and _iso_week_key(datetime.strptime(s["date"], "%Y-%m-%d").date()) == wk)

# Combined (run + hike) week-over-week delta — same "fold hikes into the
# Running tile" direction as avg_run_dist_per_week above.
run_week_delta = (_week_run_dist(last_complete_week) + _week_hike_dist(last_complete_week)) - \
                  (_week_run_dist(prev_complete_week) + _week_hike_dist(prev_complete_week))
strength_week_delta = _week_strength_count(last_complete_week) - _week_strength_count(prev_complete_week)

RUN_STABLE_THRESHOLD = 1
STRENGTH_STABLE_THRESHOLD = 1
STEPS_STABLE_THRESHOLD_QUAL = 500
INTENSITY_STABLE_THRESHOLD = 15
RECOVERY_STABLE_THRESHOLD = 15

# ── Steps overview ─────────────────────────────────────────────────────────────
steps_complete = {d: v for d, v in steps_data.items() if v > 0 and d <= last_complete_str}
cutoff_7d_date = today_date - timedelta(days=7)
cutoff_30d_date = today_date - timedelta(days=30)
steps_7d = [v for d, v in steps_data.items() if v > 0 and datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_7d_date and datetime.strptime(d, "%Y-%m-%d").date() < today_date]
steps_30d = [v for d, v in steps_complete.items() if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_30d_date]
steps_avg_all = round(sum(steps_complete.values()) / len(steps_complete)) if steps_complete else None
steps_avg_7d = round(sum(steps_7d) / len(steps_7d)) if steps_7d else None
steps_avg_30d = round(sum(steps_30d) / len(steps_30d)) if steps_30d else None
steps_delta_7v30 = (steps_avg_7d - steps_avg_30d) if (steps_avg_7d is not None and steps_avg_30d is not None) else None

step_discrepancies_30d = []
for d in set(list(iphone_steps_data.keys()) + list(polar_steps_data_for_coach.keys())):
    iv, pv = iphone_steps_data.get(d), polar_steps_data_for_coach.get(d)
    if iv and pv and datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_30d_date and d <= last_complete_str:
        step_discrepancies_30d.append(abs(iv - pv))
steps_discrepancy_avg = round(sum(step_discrepancies_30d) / len(step_discrepancies_30d)) if step_discrepancies_30d else None

# ── Recovery (sleep) overview ─────────────────────────────────────────────────
def _sleep_avg(entries):
    mins_vals = [float(v["total_sleep_min"]) for v in entries if v.get("total_sleep_min")]
    score_vals = [float(v["sleep_score"]) for v in entries if v.get("sleep_score")]
    return {
        "mins": (sum(mins_vals) / len(mins_vals)) if mins_vals else None,
        "score": (sum(score_vals) / len(score_vals)) if score_vals else None
    }

sleep_complete = {d: v for d, v in sleep_data.items() if float(v.get("total_sleep_min") or 0) > 0 and d <= str(today_date)}
sleep_week_entries = [v for d, v in sleep_complete.items() if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_7d_date]
sleep_month_entries = [v for d, v in sleep_complete.items() if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_30d_date]
sleep_year_entries = [v for d, v in sleep_complete.items() if d.startswith(this_year_str)]
recovery_week = _sleep_avg(sleep_week_entries)
recovery_month = _sleep_avg(sleep_month_entries)
recovery_year = _sleep_avg(sleep_year_entries)
recovery_delta = (recovery_week["mins"] - recovery_month["mins"]) if (recovery_week["mins"] is not None and recovery_month["mins"] is not None) else None

# ── Intensity streak vs 210min/week goal ──────────────────────────────────────
INTENSITY_GOAL_MINS = 210
week_min_map = {}
for r in year_runs:
    if r.get("date"):
        wk = _iso_week_key(datetime.strptime(r["date"], "%Y-%m-%d").date())
        week_min_map[wk] = week_min_map.get(wk, 0) + _run_mins(r)
for s in year_sessions:
    if s.get("date"):
        wk = _iso_week_key(datetime.strptime(s["date"], "%Y-%m-%d").date())
        week_min_map[wk] = week_min_map.get(wk, 0) + float(s.get("duration_min") or 0)

sorted_week_keys = sorted(week_min_map.keys())
current_week_complete = last_complete_date == (_week_start_date(last_complete_week) + timedelta(days=6))
streak_anchor = last_complete_week if (week_min_map.get(last_complete_week, 0) >= INTENSITY_GOAL_MINS or current_week_complete) else _shift_week_key(last_complete_week, -1)
intensity_week_keys = _week_range(sorted_week_keys[0], streak_anchor) if sorted_week_keys else []
intensity_streak, best_intensity_streak, current_streak_count = 0, 0, 0
for wk in intensity_week_keys:
    if week_min_map.get(wk, 0) >= INTENSITY_GOAL_MINS:
        current_streak_count += 1
        best_intensity_streak = max(best_intensity_streak, current_streak_count)
    else:
        current_streak_count = 0
for wk in reversed(intensity_week_keys):
    if week_min_map.get(wk, 0) >= INTENSITY_GOAL_MINS:
        intensity_streak += 1
    else:
        break

# ── 16-week chart ──────────────────────────────────────────────────────────────
week_map_16 = {}
for r in complete_run_rows:
    if r.get("date"):
        wk = _iso_week_key(datetime.strptime(r["date"], "%Y-%m-%d").date())
        week_map_16[wk] = week_map_16.get(wk, 0) + (float(r.get("distance_km") or 0))
s_week_map_16 = {}
for s in complete_strength_rows:
    if s.get("date"):
        wk = _iso_week_key(datetime.strptime(s["date"], "%Y-%m-%d").date())
        s_week_map_16[wk] = s_week_map_16.get(wk, 0) + 1
# NEW — hike km per week, stacked on top of run km in index.html (own
# distinct color). Kept as its own map (not merged into week_map_16) so
# run-only distance is still available separately for anything that
# shouldn't include hikes.
h_week_map_16 = {}
for h in complete_hike_rows:
    if h.get("date"):
        wk = _iso_week_key(datetime.strptime(h["date"], "%Y-%m-%d").date())
        h_week_map_16[wk] = h_week_map_16.get(wk, 0) + (float(h.get("distance_km") or 0))

# Include hike-only weeks (a week with a hike but no run/strength) in the
# window union so such a week isn't silently dropped from the chart.
all_week_keys_16 = sorted(set(list(week_map_16.keys()) + list(s_week_map_16.keys()) + list(h_week_map_16.keys())))[-16:]
month_names_short = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
chart_16wk_month_boundaries = []
chart_16wk_month_labels = []
for i, wk in enumerate(all_week_keys_16):
    wk_month = _week_start_date(wk).month
    is_boundary = i > 0 and _week_start_date(all_week_keys_16[i - 1]).month != wk_month
    chart_16wk_month_boundaries.append(is_boundary)
    chart_16wk_month_labels.append(month_names_short[wk_month - 1])

chart_16wk = {
    "labels": [wk.split("-W")[1] for wk in all_week_keys_16],
    "distance_km": [round(week_map_16.get(wk, 0), 1) for wk in all_week_keys_16],
    "hike_distance_km": [round(h_week_map_16.get(wk, 0), 1) for wk in all_week_keys_16],
    "strength_sessions": [s_week_map_16.get(wk, 0) for wk in all_week_keys_16],
    "month_boundaries": chart_16wk_month_boundaries,
    "month_labels": chart_16wk_month_labels
}

# ── Annual progress chart ─────────────────────────────────────────────────────
# Combined (run + hike) totals — per explicit athlete direction, hike km
# stacks on top of run km here too, so the progress bar reflects true
# combined volume vs. last year's combined volume (apples-to-apples: last
# year's hikes are folded into the comparison target as well, not just
# this year's). year_dist / prev_year_dist below remain the pure-running
# figures (still used for the monthly RUN-only chart series further down).
combined_year_dist = year_dist + year_hike_dist
prev_year_dist = summary.get("total_distance_prev_year_km")
combined_prev_year_dist = (prev_year_dist or 0) + total_hike_distance_prev_year
target_km = combined_prev_year_dist if combined_prev_year_dist > 0 else 2000
target_label = f"vs {today_date.year - 1} total ({target_km:.0f} km)" if combined_prev_year_dist > 0 else "Progress to 2,000 km"
raw_pct = (combined_year_dist / target_km) * 100 if target_km else 0
is_overflow = combined_year_dist > target_km

month_dist = [0.0] * 12
for r in year_runs:
    if r.get("date"):
        month_dist[datetime.strptime(r["date"], "%Y-%m-%d").date().month - 1] += float(r.get("distance_km") or 0)
current_month_idx = today_date.month - 1
month_strength = [0] * 12
for s in year_sessions:
    if s.get("date"):
        month_strength[datetime.strptime(s["date"], "%Y-%m-%d").date().month - 1] += 1
# NEW — monthly hike km, stacked on top of month_dist in index.html (own
# distinct color, same treatment as the 16-week chart above).
month_hike_dist = [0.0] * 12
for h in year_hikes:
    if h.get("date"):
        month_hike_dist[datetime.strptime(h["date"], "%Y-%m-%d").date().month - 1] += float(h.get("distance_km") or 0)

total_strength_prev_year = sum(1 for s in all_strength_rows if s.get("date", "").startswith(prev_year_str))
strength_pct = round((len(year_sessions) / total_strength_prev_year) * 100) if total_strength_prev_year > 0 else None
strength_overflow = total_strength_prev_year > 0 and len(year_sessions) > total_strength_prev_year

chart_annual = {
    "target_label": target_label,
    "target_km": round(target_km, 1),
    "raw_pct": round(raw_pct, 1),
    "display_pct": round(min(raw_pct, 130), 1),
    "is_overflow": is_overflow,
    "year_dist_km": round(combined_year_dist, 1),
    "km_to_target_km": round(abs(target_km - combined_year_dist), 1),
    # Colored-note figure — how much of year_dist_km above came from hiking,
    # never silently blended without attribution (see index.html).
    "hike_dist_this_year_km": round(year_hike_dist, 1),
    "strength_target_label": (f"vs {prev_year_str} total ({total_strength_prev_year} sessions)" if total_strength_prev_year > 0 else "Sessions this year"),
    "strength_pct": strength_pct,
    "strength_display_pct": min(strength_pct, 130) if strength_pct is not None else 0,
    "strength_overflow": strength_overflow,
    "strength_sessions_done": len(year_sessions),
    "strength_sessions_to_match": (total_strength_prev_year - len(year_sessions)) if total_strength_prev_year > 0 else None,
    "month_labels": month_names_short[:current_month_idx + 1],
    "month_distance_km": [round(v, 1) for v in month_dist[:current_month_idx + 1]],
    "month_hike_distance_km": [round(v, 1) for v in month_hike_dist[:current_month_idx + 1]],
    "month_strength_sessions": month_strength[:current_month_idx + 1],
    "current_month_index": current_month_idx
}

# ── LT card (zones + trend chart) ─────────────────────────────────────────────
def _calc_zones(lt_pace_str, lt_hr):
    lt_sec = parse_pace_sec(lt_pace_str)
    if not lt_sec:
        return None
    z1 = round(lt_hr * 0.80) if lt_hr else None
    z2 = round(lt_hr * 0.90) if lt_hr else None
    z3 = round(lt_hr * 0.99) if lt_hr else None
    z4 = round(lt_hr * 1.05) if lt_hr else None
    return [
        {"name": "Z1 EASY", "pace_label": f"> {sec_to_pace(lt_sec + 75)}", "hr_label": (f"< {z1} bpm" if z1 else ""), "color": "#4a9eff", "pct": 40},
        {"name": "Z2 AEROBIC", "pace_label": f"{sec_to_pace(lt_sec + 45)}–{sec_to_pace(lt_sec + 74)}", "hr_label": (f"{z1}–{z2} bpm" if z1 else ""), "color": "#7bc67e", "pct": 60},
        {"name": "Z3 TEMPO", "pace_label": f"{sec_to_pace(lt_sec + 10)}–{sec_to_pace(lt_sec + 44)}", "hr_label": (f"{z2}–{z3} bpm" if z2 else ""), "color": "#e8ff5a", "pct": 78},
        {"name": "Z4 LTHR", "pace_label": f"{sec_to_pace(lt_sec - 10)}–{sec_to_pace(lt_sec + 9)}", "hr_label": (f"{z3}–{z4} bpm" if z3 else ""), "color": "#ff9f40", "pct": 90},
        {"name": "Z5 HARD", "pace_label": f"< {sec_to_pace(lt_sec - 11)}", "hr_label": (f"> {z4} bpm" if z4 else ""), "color": "#ff6b35", "pct": 100},
    ]

def _linear_trend(values):
    n = len(values)
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(valid) < 2:
        return [None] * n
    x_mean = sum(i for i, _ in valid) / len(valid)
    y_mean = sum(v for _, v in valid) / len(valid)
    num = sum((i - x_mean) * (v - y_mean) for i, v in valid)
    den = sum((i - x_mean) ** 2 for i, _ in valid)
    slope = num / den if den != 0 else 0
    intercept = y_mean - slope * x_mean
    return [slope * i + intercept for i in range(n)]

lt_zones = _calc_zones(latest_lt["lt_pace"], latest_lt.get("lt_hr")) if latest_lt else None
lt_trend = None
if len(lt_history) > 1:
    lt_sorted = sorted(lt_history, key=lambda x: x["date"])
    lt_secs = [parse_pace_sec(r["lt_pace"]) for r in lt_sorted]
    lt_trend = {
        "labels": [r["date"][5:] for r in lt_sorted],
        "pace_sec": lt_secs,
        "trend_sec": _linear_trend(lt_secs)
    }

# ── Personal bests (with LT gap for 10K/Half only) ────────────────────────────
def _calc_best_efforts(runs):
    cats = [("5 km", 4), ("10 km", 8), ("Half", 18), ("Marathon", 38), ("50 km", 45)]
    results = []
    for label, min_dist in cats:
        eligible = [r for r in runs if float(r.get("distance_km") or 0) >= min_dist and r.get("avg_pace_min_km")]
        if not eligible:
            results.append({"label": label, "pace": None, "time": None, "dist": None, "date": None, "name": None})
            continue
        best = min(eligible, key=lambda r: parse_pace_sec(r["avg_pace_min_km"]) or 9999)
        results.append({"label": label, "pace": best["avg_pace_min_km"], "time": best.get("moving_time"), "dist": round(float(best.get("distance_km") or 0), 1), "date": best.get("date"), "name": best.get("name", "")})
    if runs:
        longest = max(runs, key=lambda r: float(r.get("distance_km") or 0))
        results.append({"label": "Longest", "pace": longest.get("avg_pace_min_km"), "time": longest.get("moving_time"), "dist": round(float(longest.get("distance_km") or 0), 1), "date": longest.get("date"), "name": longest.get("name", "")})
    return results

outdoor_complete_runs = [r for r in complete_run_rows if r.get("type") == "outdoor"]
pbs = _calc_best_efforts(outdoor_complete_runs)
lt_pace_sec_for_gap = parse_pace_sec(latest_lt["lt_pace"]) if latest_lt else None
for pb in pbs:
    pb["lt_gap_sec"] = None
    if pb["label"] in ("10 km", "Half") and lt_pace_sec_for_gap and pb["pace"]:
        pb_sec = parse_pace_sec(pb["pace"])
        if pb_sec:
            pb["lt_gap_sec"] = lt_pace_sec_for_gap - pb_sec

# ── Calendar (all-history activity dates, compact) ────────────────────────────
calendar_run_dates = sorted({r["date"] for r in all_run_rows if r.get("date")})
calendar_strength_dates = sorted({s["date"] for s in all_strength_rows if s.get("date")})
# NEW (July 2026) — hikes previously weren't captured anywhere at all (see
# fetch_activities.py's is_hike() note); now read from hikes.csv for
# calendar visibility only, same pattern as run/strength dates.
calendar_hike_dates = sorted({h["date"] for h in _load_csv("hikes.csv") if h.get("date")})

# ── HR ZONE BOUNDS ─────────────────────────────────────────────────────────────
# Moved up from the RUN ZONE-TIME PROJECT section (further down this file) —
# needed earlier now, since per-session zone breakdowns are computed as part
# of the unified run/strength HR context below, not only in the weekly
# zone-time aggregate. The zone-time section further down reuses these same
# definitions rather than redefining them.
def _hr_zone_bounds(lt_hr):
    if not lt_hr:
        return None
    return (
        round(lt_hr * 0.80),
        round(lt_hr * 0.90),
        round(lt_hr * 0.99),
        round(lt_hr * 1.05)
    )

def _classify_hr_zone(hr, bounds):
    if bounds is None or hr is None:
        return None
    z1, z2, z3, z4 = bounds
    if hr < z1:
        return "Z1"
    elif hr < z2:
        return "Z2"
    elif hr < z3:
        return "Z3"
    elif hr < z4:
        return "Z4"
    else:
        return "Z5"

_lt_hr_bounds = _hr_zone_bounds(latest_lt.get("lt_hr")) if latest_lt else None

def _time_to_seconds(t):
    try:
        h, m, s = (int(p) for p in t.split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return None

def _zone_minutes_from_timeseries(samples, bounds, gap_max_sec=60):
    """Time-weighted zone attribution from a sorted (elapsed_sec, hr) series
    — e.g. Garmin's real per-second run_hr_samples. Skips gaps > gap_max_sec
    (sensor dropout) rather than misattributing them to whichever zone
    preceded the gap. Same approach as the weekly zone-time aggregate
    further down this file, just applied to one session at a time."""
    zone_minutes = {"Z1": 0.0, "Z2": 0.0, "Z3": 0.0, "Z4": 0.0, "Z5": 0.0}
    if not bounds or len(samples) < 2:
        return zone_minutes
    for i in range(len(samples) - 1):
        t0, hr0 = samples[i]
        t1, _ = samples[i + 1]
        delta_sec = t1 - t0
        if delta_sec <= 0 or delta_sec > gap_max_sec:
            continue
        zone = _classify_hr_zone(hr0, bounds)
        if zone:
            zone_minutes[zone] += delta_sec / 60
    return zone_minutes

def _zone_breakdown_from_raw_samples(hr_values, duration_min, bounds):
    """hr_values: individual HR readings (e.g. Polar continuous samples
    matched to a run/strength window). Polar's continuous HR is
    event-driven, NOT a clean time grid (confirmed — see PROJECT_CONTEXT),
    so sample spacing isn't reliable enough to time-weight the way real
    per-second Garmin data is above. Instead every sample is weighted
    EQUALLY (a simple count fraction per zone), then scaled against the
    session's real wall-clock duration. Less precise than true
    time-weighting, but avoids misattributing long silent gaps between
    samples to whichever zone happened to precede them."""
    zone_minutes = {"Z1": 0.0, "Z2": 0.0, "Z3": 0.0, "Z4": 0.0, "Z5": 0.0}
    if not bounds or not hr_values or not duration_min or duration_min <= 0:
        return zone_minutes
    counts = {"Z1": 0, "Z2": 0, "Z3": 0, "Z4": 0, "Z5": 0}
    for hr in hr_values:
        zone = _classify_hr_zone(hr, bounds)
        if zone:
            counts[zone] += 1
    total = sum(counts.values())
    if total == 0:
        return zone_minutes
    for z in zone_minutes:
        zone_minutes[z] = round(duration_min * (counts[z] / total), 1)
    return zone_minutes

def _zone_summary_str(zone_minutes):
    """Compact 'Z1 12m Z2 8m Z5 4m' string for the coach prompt — zero
    zones omitted so the line stays short and the nonzero zones stand
    out."""
    if not zone_minutes:
        return ""
    parts = [f"{z} {round(m)}m" for z, m in zone_minutes.items() if m and round(m) > 0]
    return " ".join(parts) if parts else ""

# ── Garmin-vs-Polar run HR comparison (three-way; diagnostic) ─────────────────
# Moved earlier in the file (previously computed after the strength/dropout
# sections) only because the calibration offsets it produces are now needed
# by the unified run+strength HR context below — its own logic is
# unchanged. Extended July 2026 to also include Polar's exercise-list HR
# (when a matched entry exists in polar_exercises.csv) alongside the
# original Garmin-vs-continuous-HR comparison. Purpose is diagnostic, per
# the athlete's own two questions: does continuous HR actually gap during a
# Loop-auto-detected exercise window (a run can now show continuous-HR
# data as None while still having exercise data, or vice versa — both are
# informative), and is exercise-level HR any closer to Garmin/H10 than
# continuous HR is. NOT used for any fallback/backfill decision directly —
# see build_run_hr_context / build_strength_hr_context below for where the
# actual fallback logic lives.
def build_run_hr_source_comparison():
    comparison = []
    for r in all_run_rows:
        start_time_full = r.get("start_time", "")
        if not start_time_full or not r.get("avg_hr"):
            continue
        run_date = start_time_full[:10]

        polar_continuous_avg = None
        polar_sample_count = 0
        if run_date in _polar_hr_by_date:
            try:
                run_start_sec = _time_to_seconds(start_time_full[11:19])
                moving_parts = (r.get("moving_time") or "").split(":")
                if len(moving_parts) == 3:
                    h, m, s = (int(p) for p in moving_parts)
                    duration_sec = h * 3600 + m * 60 + s
                    if run_start_sec is not None and duration_sec > 0:
                        run_end_sec = run_start_sec + duration_sec
                        matched_polar_hrs = []
                        for (t, hr) in _polar_hr_by_date[run_date]:
                            t_sec = _time_to_seconds(t)
                            if t_sec is not None and run_start_sec <= t_sec <= run_end_sec:
                                try:
                                    matched_polar_hrs.append(float(hr))
                                except (ValueError, TypeError):
                                    continue
                        if matched_polar_hrs:
                            polar_continuous_avg = sum(matched_polar_hrs) / len(matched_polar_hrs)
                            polar_sample_count = len(matched_polar_hrs)
            except Exception:
                pass

        polar_exercise_avg = None
        exercise_row = _polar_exercise_by_garmin_id.get(r.get("activity_id", ""))
        if exercise_row and exercise_row.get("avg_hr"):
            try:
                polar_exercise_avg = float(exercise_row["avg_hr"])
            except (ValueError, TypeError):
                polar_exercise_avg = None

        if polar_continuous_avg is None and polar_exercise_avg is None:
            continue  # nothing from either Polar source to compare against this run

        garmin_avg = float(r["avg_hr"])
        entry = {
            "date": run_date,
            "name": r.get("name", ""),
            "garmin_avg_hr": round(garmin_avg),
            "polar_avg_hr": round(polar_continuous_avg) if polar_continuous_avg is not None else None,
            "diff": round(polar_continuous_avg - garmin_avg, 1) if polar_continuous_avg is not None else None,
            "polar_sample_count": polar_sample_count,
            "polar_exercise_avg_hr": round(polar_exercise_avg) if polar_exercise_avg is not None else None,
            "polar_exercise_diff": round(polar_exercise_avg - garmin_avg, 1) if polar_exercise_avg is not None else None
        }
        comparison.append(entry)
    return sorted(comparison, key=lambda x: x["date"], reverse=True)

run_hr_source_comparison = build_run_hr_source_comparison()

# ── UNIFIED HR CONTEXT (rebuilt July 2026) ────────────────────────────────────
# Replaces the previously-separate build_strength_hr_overlay() and
# build_hr_dropout_fallback() with one consistent approach applied to BOTH
# runs and strength sessions, per explicit athlete direction: the Polar
# overlap/fallback logic should apply anywhere Garmin/H10 has no pulse —
# runs AND strength sessions, not runs alone. Strength sessions never have
# H10 data at all (tattoo blocks the optical sensor, no chest strap while
# lifting), so every strength session is a "dropout" in this sense, not
# just an occasional gap.
#
# ALSO fixes a real accuracy gap the athlete found by direct comparison
# against Garmin's own registration: the coach was previously given only a
# single avg_hr number per run, which can average out a spiky effort (e.g.
# repeated hard sprints inside an otherwise easy run) to look identical to
# a genuinely steady easy run. Every session below now also carries a
# per-session zone-minute breakdown and max_hr (max_hr already existed in
# runs.csv but was never surfaced to the coach prompt) so the coach can see
# SHAPE, not just an average — this feeds the coach prompt in the next
# iteration of this file (see PROJECT_CONTEXT COACH DISTRIBUTION /
# per-session HR context notes).
#
# NEVER presented as real H10 data when it isn't — every Polar-sourced
# entry below carries an explicit source label and, where applicable, a
# calibration note. runs.csv/strength.csv themselves are never touched;
# all of this is computed fresh here every run, same discipline as the
# original H10-dropout fallback design.

def _compute_hr_calibration_offsets(comparison_list):
    """Average (polar − garmin) offset per Polar source, computed from
    whatever real matched comparisons currently exist in
    run_hr_source_comparison. Self-correcting: as more genuine dropout-free
    runs accumulate real comparisons, this offset gets more reliable
    automatically — nothing hardcoded or guessed."""
    continuous_diffs = [c["diff"] for c in comparison_list if c.get("diff") is not None]
    exercise_diffs = [c["polar_exercise_diff"] for c in comparison_list if c.get("polar_exercise_diff") is not None]
    return {
        "continuous": (sum(continuous_diffs) / len(continuous_diffs)) if continuous_diffs else None,
        "continuous_n": len(continuous_diffs),
        "exercise": (sum(exercise_diffs) / len(exercise_diffs)) if exercise_diffs else None,
        "exercise_n": len(exercise_diffs)
    }

_hr_calibration = _compute_hr_calibration_offsets(run_hr_source_comparison)

def build_run_hr_context():
    """One entry per run with any HR information at all (Garmin OR Polar),
    keyed by activity_id.
    source == 'garmin': real H10 data — real per-second zone breakdown
    from run_hr_samples.csv when available.
    source == 'polar_exercise' / 'polar_continuous': H10 dropout — avg/max
    estimated from Polar and calibrated against the offset in
    _hr_calibration; zone breakdown computed from the underlying raw
    samples (continuous only — exercise entries carry no raw sample list,
    only avg/max, so no zone breakdown is possible from that source).
    Exercise-entry HR is still preferred over continuous HR when both
    exist for a dropout run, same preference as the original design — a
    judgment call pending more real comparison data (see
    PROJECT_CONTEXT)."""
    context = {}
    for r in all_run_rows:
        aid = r.get("activity_id", "")
        start_time_full = r.get("start_time", "")
        if not aid or not start_time_full:
            continue
        run_date = start_time_full[:10]
        has_garmin_hr = r.get("avg_hr") not in (None, "", "0")

        if has_garmin_hr:
            zone_minutes = None
            if aid in _run_hr_by_activity and _lt_hr_bounds:
                zone_minutes = _zone_minutes_from_timeseries(_run_hr_by_activity[aid], _lt_hr_bounds)
            context[aid] = {
                "activity_id": aid,
                "date": run_date,
                "name": r.get("name", ""),
                "source": "garmin",
                "avg_hr": round(float(r["avg_hr"])),
                "max_hr": round(float(r["max_hr"])) if r.get("max_hr") not in (None, "", "0") else None,
                "zone_minutes": zone_minutes,
                "zone_summary": _zone_summary_str(zone_minutes) if zone_minutes else "",
                "label": None,
                "calibration_note": None
            }
            continue

        # H10 dropout — attempt a calibrated Polar-sourced estimate.
        raw_avg, raw_max, source, sample_info = None, None, None, ""

        exercise_row = _polar_exercise_by_garmin_id.get(aid)
        if exercise_row and exercise_row.get("avg_hr"):
            try:
                raw_avg = float(exercise_row["avg_hr"])
                raw_max = float(exercise_row["max_hr"]) if exercise_row.get("max_hr") else None
                source = "exercise"
                sample_info = f"Polar exercise entry {exercise_row.get('polar_exercise_id', '')}"
            except (ValueError, TypeError):
                raw_avg = None

        matched_continuous = []
        if run_date in _polar_hr_by_date:
            try:
                run_start_sec = _time_to_seconds(start_time_full[11:19])
                elapsed_parts = (r.get("elapsed_time") or "").split(":")
                if len(elapsed_parts) == 3 and run_start_sec is not None:
                    h, m, s = (int(p) for p in elapsed_parts)
                    duration_sec = h * 3600 + m * 60 + s
                    if duration_sec > 0:
                        run_end_sec = run_start_sec + duration_sec
                        for (t, hr) in _polar_hr_by_date[run_date]:
                            t_sec = _time_to_seconds(t)
                            if t_sec is not None and run_start_sec <= t_sec <= run_end_sec:
                                try:
                                    matched_continuous.append(float(hr))
                                except (ValueError, TypeError):
                                    continue
            except Exception:
                pass

        if raw_avg is None and matched_continuous:
            raw_avg = sum(matched_continuous) / len(matched_continuous)
            raw_max = max(matched_continuous)
            source = "continuous"
            sample_info = f"{len(matched_continuous)} continuous HR sample(s)"

        if raw_avg is None:
            continue  # no Polar data of any kind for this dropout run

        offset = _hr_calibration.get(source)
        offset_n = _hr_calibration.get(f"{source}_n", 0)
        if offset is not None and offset_n > 0:
            estimated_avg = round(raw_avg - offset)
            estimated_max = round(raw_max - offset) if raw_max is not None else None
            calibration_note = f"calibrated using {offset_n} comparison run(s), offset {offset:+.1f} bpm"
        else:
            estimated_avg = round(raw_avg)
            estimated_max = round(raw_max) if raw_max is not None else None
            calibration_note = "uncalibrated — no comparison data yet for this source"

        zone_minutes = None
        if source == "continuous" and matched_continuous and _lt_hr_bounds:
            calibrated_samples = [hr - offset for hr in matched_continuous] if offset is not None else matched_continuous
            try:
                elapsed_parts = (r.get("elapsed_time") or "").split(":")
                h, m, s = (int(p) for p in elapsed_parts)
                duration_min = (h * 3600 + m * 60 + s) / 60
            except Exception:
                duration_min = None
            if duration_min:
                zone_minutes = _zone_breakdown_from_raw_samples(calibrated_samples, duration_min, _lt_hr_bounds)

        context[aid] = {
            "activity_id": aid,
            "date": run_date,
            "name": r.get("name", ""),
            "source": source,
            "raw_polar_hr": round(raw_avg),
            "estimated_garmin_hr": estimated_avg,
            "avg_hr": estimated_avg,
            "max_hr": estimated_max,
            "zone_minutes": zone_minutes,
            "zone_summary": _zone_summary_str(zone_minutes) if zone_minutes else "",
            "calibration_note": calibration_note,
            "sample_info": sample_info,
            "label": "Polar fallback — H10 dropout"
        }
    return context

def build_strength_hr_context():
    """Every strength session has NO Garmin HR at all (no chest strap
    while lifting) — this is the sole HR source for strength, always
    Polar-derived, always calibrated against the same continuous-HR offset
    computed from real running comparisons. Deliberate judgment call worth
    flagging: the calibration offset itself was derived from RUNNING
    comparisons (no strength-specific Garmin/Polar comparison is possible,
    since Garmin never has strength HR to compare against) — applied here
    to lifting anyway, as the best calibration data available. Revisit if
    a strength-specific comparison ever becomes possible."""
    context = {}
    offset = _hr_calibration.get("continuous")
    offset_n = _hr_calibration.get("continuous_n", 0)
    for s in all_strength_rows:
        start_time_full = s.get("start_time", "")
        duration_min_raw = s.get("duration_min", "")
        if not start_time_full or not duration_min_raw:
            continue
        try:
            session_date = start_time_full[:10]
            session_start_sec = _time_to_seconds(start_time_full[11:19])
            duration_min = float(duration_min_raw)
        except Exception:
            continue
        if session_start_sec is None or duration_min <= 0:
            continue
        session_end_sec = session_start_sec + duration_min * 60

        day_samples = _polar_hr_by_date.get(session_date, [])
        matched_hrs = []
        for (t, hr) in day_samples:
            t_sec = _time_to_seconds(t)
            if t_sec is not None and session_start_sec <= t_sec <= session_end_sec:
                try:
                    matched_hrs.append(float(hr))
                except (ValueError, TypeError):
                    continue

        if not matched_hrs:
            continue

        raw_avg = sum(matched_hrs) / len(matched_hrs)
        raw_max = max(matched_hrs)
        if offset is not None and offset_n > 0:
            estimated_avg = round(raw_avg - offset)
            estimated_max = round(raw_max - offset)
            calibration_note = f"calibrated using {offset_n} comparison run(s), offset {offset:+.1f} bpm"
        else:
            estimated_avg = round(raw_avg)
            estimated_max = round(raw_max)
            calibration_note = "uncalibrated — no comparison data yet"

        zone_minutes = None
        if _lt_hr_bounds:
            calibrated_samples = [hr - offset for hr in matched_hrs] if offset is not None else matched_hrs
            zone_minutes = _zone_breakdown_from_raw_samples(calibrated_samples, duration_min, _lt_hr_bounds)

        key = (session_date, s.get("name", ""))
        context[key] = {
            "date": session_date,
            "name": s.get("name", ""),
            "avg_hr": estimated_avg,
            "max_hr": estimated_max,
            "sample_count": len(matched_hrs),
            "zone_minutes": zone_minutes,
            "zone_summary": _zone_summary_str(zone_minutes) if zone_minutes else "",
            "label": "Polar (calibrated) — no H10 during strength",
            "calibration_note": calibration_note
        }
    return context

_run_hr_context = build_run_hr_context()
_strength_hr_context = build_strength_hr_context()

# Backward-compatible views for the existing dashboard_metrics.json keys /
# index.html chart (strength_hr_overlay, hr_dropout_fallback) — same shapes
# as before, now sourced from the unified context above and enriched with
# zone_minutes/zone_summary. index.html's existing chart code reads only
# avg_hr/max_hr/date/name/sample_count and ignores unknown extra fields, so
# this is safe without an index.html change. The actual UI restructure
# (folding this into per-session coach annotations instead of a standalone
# card, per athlete direction) is a separate, later step.
strength_hr_overlay = sorted(_strength_hr_context.values(), key=lambda x: x["date"], reverse=True)
hr_dropout_fallback = sorted(
    [v for v in _run_hr_context.values() if v.get("label")],
    key=lambda x: x["date"], reverse=True
)
_hr_dropout_fallback_by_activity = {f["activity_id"]: f for f in hr_dropout_fallback if f.get("activity_id")}

# ── RUN ZONE-TIME PROJECT ─────────────────────────────────────────────────────
# (zone bounds / classify function / _lt_hr_bounds now defined earlier in
# this file, above the unified HR context — not redefined here.)

ATTIA_Z2_TARGET_MIN = 180
ATTIA_Z2_TARGET_MAX = 240

Z1_TARGET_MIN_PCT = 75
Z1_TARGET_MAX_PCT = 80
GREY_CEILING_PCT = 10
Z5_TARGET_MIN_PCT = 15
Z5_TARGET_MAX_PCT = 20

def _compute_zone_time(window_start, window_end, window_label, window_days):
    zone_minutes = {"Z1": 0.0, "Z2": 0.0, "Z3": 0.0, "Z4": 0.0, "Z5": 0.0}
    running_sessions_with_zone_data = 0
    strength_sessions_with_zone_data = 0
    dropout_fallback_sessions_with_zone_data = 0

    if _lt_hr_bounds:
        runs_in_window = [r for r in all_run_rows
            if r.get("date") and window_start <= datetime.strptime(r["date"], "%Y-%m-%d").date() <= window_end
            and r.get("activity_id") in _run_hr_by_activity]
        for r in runs_in_window:
            samples = _run_hr_by_activity[r["activity_id"]]
            if len(samples) < 2:
                continue
            running_sessions_with_zone_data += 1
            for i in range(len(samples) - 1):
                t0, hr0 = samples[i]
                t1, _ = samples[i + 1]
                delta_sec = t1 - t0
                if delta_sec <= 0 or delta_sec > 60:
                    continue
                zone = _classify_hr_zone(hr0, _lt_hr_bounds)
                if zone:
                    zone_minutes[zone] += delta_sec / 60

        # H10-DROPOUT FALLBACK runs: no per-sample HR exists (Garmin
        # recorded nothing), so — same coarse approach already used for
        # strength sessions below — the whole run's duration is attributed
        # to a single zone based on the calibrated fallback estimate,
        # rather than excluded from zone-time entirely. This is the actual
        # payoff of the fallback: a run like 2026-07-11 (real H10 dropout)
        # still counts toward Zone 2 targets and streaks instead of
        # silently vanishing from every zone-based metric.
        dropout_runs_in_window = [r for r in all_run_rows
            if r.get("date") and window_start <= datetime.strptime(r["date"], "%Y-%m-%d").date() <= window_end
            and r.get("activity_id") in _hr_dropout_fallback_by_activity]
        for r in dropout_runs_in_window:
            fb = _hr_dropout_fallback_by_activity[r["activity_id"]]
            try:
                dur_min = float(_time_to_seconds(r.get("elapsed_time") or "") or 0) / 60
            except Exception:
                dur_min = 0
            if dur_min <= 0:
                continue
            zone = _classify_hr_zone(fb["estimated_garmin_hr"], _lt_hr_bounds)
            if zone:
                zone_minutes[zone] += dur_min
                dropout_fallback_sessions_with_zone_data += 1

        strength_in_window = [s for s in strength_hr_overlay
            if s.get("date") and window_start <= datetime.strptime(s["date"], "%Y-%m-%d").date() <= window_end]
        for s in strength_in_window:
            match = next((row for row in all_strength_rows
                if row.get("date") == s["date"] and row.get("name") == s["name"]), None)
            if not match:
                continue
            try:
                dur_min = float(match.get("duration_min") or 0)
            except (ValueError, TypeError):
                continue
            if dur_min <= 0:
                continue
            zone = _classify_hr_zone(s.get("avg_hr"), _lt_hr_bounds)
            if zone:
                zone_minutes[zone] += dur_min
                strength_sessions_with_zone_data += 1

    total_zone_minutes = sum(zone_minutes.values())
    z1_pct = round((zone_minutes["Z1"] / total_zone_minutes) * 100, 1) if total_zone_minutes else None
    grey_pct_val = round(((zone_minutes["Z3"] + zone_minutes["Z4"]) / total_zone_minutes) * 100, 1) if total_zone_minutes else None
    z5_pct_val = round((zone_minutes["Z5"] / total_zone_minutes) * 100, 1) if total_zone_minutes else None

    return {
        "label": window_label,
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "minutes": {k: round(v, 1) for k, v in zone_minutes.items()},
        "total_minutes": round(total_zone_minutes, 1),
        "z1_pct": z1_pct,
        "z2_pct": round((zone_minutes["Z2"] / total_zone_minutes) * 100, 1) if total_zone_minutes else None,
        "grey_pct": grey_pct_val,
        "z5_pct": z5_pct_val,
        "z2_target_min": ATTIA_Z2_TARGET_MIN,
        "z2_target_max": ATTIA_Z2_TARGET_MAX,
        "z2_vs_target_pct": round((zone_minutes["Z2"] / ATTIA_Z2_TARGET_MIN) * 100) if ATTIA_Z2_TARGET_MIN else None,
        "z1_target_min_pct": Z1_TARGET_MIN_PCT,
        "z1_target_max_pct": Z1_TARGET_MAX_PCT,
        "z1_vs_target_pct": round((z1_pct / Z1_TARGET_MIN_PCT) * 100) if (z1_pct is not None and Z1_TARGET_MIN_PCT) else None,
        "grey_ceiling_pct": GREY_CEILING_PCT,
        "grey_vs_ceiling_pct": round((grey_pct_val / GREY_CEILING_PCT) * 100) if (grey_pct_val is not None and GREY_CEILING_PCT) else None,
        "z5_target_min_pct": Z5_TARGET_MIN_PCT,
        "z5_target_max_pct": Z5_TARGET_MAX_PCT,
        "z5_vs_target_pct": round((z5_pct_val / Z5_TARGET_MIN_PCT) * 100) if (z5_pct_val is not None and Z5_TARGET_MIN_PCT) else None,
        "running_sessions_with_data": running_sessions_with_zone_data,
        "strength_sessions_with_data": strength_sessions_with_zone_data,
        "dropout_fallback_sessions_with_data": dropout_fallback_sessions_with_zone_data,
        "has_lt_data": _lt_hr_bounds is not None
    }

_rolling_start = today_date - timedelta(days=7)
zone_time_rolling = _compute_zone_time(_rolling_start, today_date, "This week (rolling 7d)", 7)

# REPLACED July 2026 (athlete request): the original toggle was
# this-week/last-completed-week. Now a 4-way toggle — this week / last
# month / year to date / all time. "Last month" means the last FULLY
# COMPLETED calendar month (e.g. all of June while currently in July),
# matching this project's existing convention of "last completed period"
# (see last_completed_week elsewhere) rather than a trailing-30-days
# window — flag to the athlete if a trailing window was actually wanted
# instead.
_this_month_first = today_date.replace(day=1)
_last_month_end = _this_month_first - timedelta(days=1)
_last_month_start = _last_month_end.replace(day=1)
zone_time_last_month = _compute_zone_time(
    _last_month_start, _last_month_end, "Last month",
    (_last_month_end - _last_month_start).days + 1
)

_ytd_start = today_date.replace(month=1, day=1)
zone_time_ytd = _compute_zone_time(
    _ytd_start, last_complete_date, "Year to date",
    (last_complete_date - _ytd_start).days + 1
)

_all_time_dates = [r["date"] for r in all_run_rows if r.get("date")] + [s["date"] for s in all_strength_rows if s.get("date")]
if _all_time_dates:
    _all_time_start = datetime.strptime(min(_all_time_dates), "%Y-%m-%d").date()
else:
    _all_time_start = last_complete_date
zone_time_all_time = _compute_zone_time(
    _all_time_start, last_complete_date, "All time",
    (last_complete_date - _all_time_start).days + 1
)

zone_time = {
    "rolling": zone_time_rolling,
    "last_month": zone_time_last_month,
    "ytd": zone_time_ytd,
    "all_time": zone_time_all_time,
    "default_view": "rolling"
}



# ── Calendar commentary ────────────────────────────────────────────────────────
def _calendar_commentary():
    streak = summary.get("current_weekly_streak") or 0
    best = summary.get("longest_weekly_streak") or 0
    if streak == 0:
        return "No active running streak right now — a session this week starts a new one."
    elif streak >= best and streak > 0:
        return f"{streak}-week streak — currently matching your all-time best. Keep it going."
    else:
        return f"{streak}-week streak going (best: {best}). Consistency is compounding."

# ── Data freshness indicator ──────────────────────────────────────────────────
def _data_freshness():
    if not os.path.exists("fetch_status.json"):
        return {"status": "failed", "last_success_date": None, "age_days": None}
    try:
        with open("fetch_status.json", "r", encoding="utf-8") as f:
            fs = json.load(f)
        last_date = fs.get("last_success_date")
        if not last_date:
            return {"status": "failed", "last_success_date": None, "age_days": None}
        age_days = (today_date - datetime.strptime(last_date, "%Y-%m-%d").date()).days
        status = "fresh" if age_days <= 1 else "stale" if age_days <= 3 else "failed"
        return {"status": status, "last_success_date": last_date, "age_days": age_days}
    except Exception:
        return {"status": "failed", "last_success_date": None, "age_days": None}

# ── Per-tile freshness indicators (added July 2026) ───────────────────────────
# Real question the athlete asked: "is the data in THIS specific tile
# actually from yesterday?" — distinct from the single global dot, which
# only says the pipeline itself ran successfully.
#
# DELIBERATE SPLIT (option B, chosen after sparring — see PROJECT_CONTEXT):
# Steps, Recovery, and Load are genuinely continuous (Polar Loop worn all
# day every day), so "is there a real row for yesterday" is an honest,
# meaningful check for those three — computed here as real per-day
# presence checks. Running, Strength, and Intensity are NOT daily — a
# literal per-day check would show stale/failed on every ordinary rest
# day, which isn't a freshness problem at all, and a signal that's wrong
# that often gets ignored (same "cries wolf" failure mode the coverage
# checker was built to avoid elsewhere in this project). Those three
# tiles deliberately reuse the SAME pipeline-level data_freshness object
# already computed above — index.html renders it directly for those
# three rather than a separate per-tile field, so no new backend value is
# needed for them.
def _tile_freshness():
    steps_fresh = steps_data.get(last_complete_str, 0) > 0
    recovery_fresh = last_complete_str in sleep_complete
    load_fresh = cardio_load.get("latest_date") == last_complete_str
    return {
        "steps": "fresh" if steps_fresh else "stale",
        "recovery": "fresh" if recovery_fresh else "stale",
        "load": "fresh" if load_fresh else "stale"
    }

# ── Strength test summary (for a new UI card — July 2026) ────────────────────
# strength_test_history was previously only used internally for evidence
# catalog item #10 (a delta only appears once a SECOND test is logged for
# an exercise — currently DORMANT, single July 4 2026 baseline entries
# only). This exposes the same underlying data as a standalone
# dashboard_metrics.json key so a UI card can show current baseline values
# grouped by category, with a delta line that appears automatically the
# moment a second test lands — same "grows into a trend, no rebuild
# needed" pattern as the LT/VO2max card. Deliberately independent from the
# evidence-catalog delta logic above rather than refactored to share it —
# lower risk of an accidental regression to a working evidence category
# for a same-day UI addition.
def _strength_test_summary():
    summary_list = []
    for exercise, history in strength_test_history.items():
        if not history:
            continue
        latest = history[-1]
        entry = {
            "exercise": exercise,
            "category": latest.get("category", ""),
            "latest_value": latest.get("value"),
            "unit": latest.get("unit", ""),
            "latest_date": latest.get("date"),
            "prior_value": None,
            "prior_date": None,
            "delta_pct": None
        }
        if len(history) >= 2:
            prior = history[-2]
            try:
                latest_val, prior_val = float(latest["value"]), float(prior["value"])
                if prior_val != 0:
                    entry["prior_value"] = prior.get("value")
                    entry["prior_date"] = prior.get("date")
                    entry["delta_pct"] = round(((latest_val - prior_val) / prior_val) * 100, 1)
            except (ValueError, TypeError):
                pass
        summary_list.append(entry)
    # Group by category, then alphabetically within each — stable, readable
    # ordering rather than insertion order (which follows CSV row order).
    return sorted(summary_list, key=lambda e: (e["category"], e["exercise"]))

strength_tests_summary = _strength_test_summary()

dashboard_metrics = {
    "last_updated": last_updated_str,
    "last_complete_date": last_complete_str,
    "this_year": this_year_str,
    "this_month_short": this_month_short,

    "running": {
        # avg_per_run_km / runs_this_year deliberately stay RUN-ONLY — "per
        # run" and "count of runs" don't have a sensible hike-inclusive
        # meaning (a hike isn't a run). avg_per_week_km, total_distance_
        # this_year_km, and week_delta_km/week_qual are the combined
        # (run + hike) figures per athlete direction — hike_distance_
        # this_year_km below is the colored-note figure index.html uses to
        # show how much of the combined total came from hiking, so it's
        # never silently blended without attribution.
        "avg_per_run_km": round(avg_run_dist_per_run, 1),
        "runs_this_year": summary.get("total_runs_this_year", len(year_runs)),
        "avg_per_week_km": round(avg_run_dist_per_week, 1),
        "avg_runs_per_week": round(avg_runs_per_week, 1),
        "total_distance_this_year_km": round(year_dist + year_hike_dist, 1),
        "hike_distance_this_year_km": round(year_hike_dist, 1),
        "current_weekly_streak": summary.get("current_weekly_streak"),
        "longest_weekly_streak": summary.get("longest_weekly_streak"),
        "week_delta_km": round(run_week_delta, 1),
        "week_qual": _qual(run_week_delta, RUN_STABLE_THRESHOLD)
    },
    "strength": {
        "avg_sessions_per_week": round(avg_sess_per_week, 1),
        "sessions_this_year": summary.get("total_strength_this_year", len(year_sessions)),
        "avg_hours_per_week": round(total_strength_min_year / num_s_weeks / 60, 1),
        "total_hours_this_year": round(total_strength_min_year / 60, 1),
        "current_weekly_streak": summary.get("current_strength_weekly_streak"),
        "longest_weekly_streak": summary.get("longest_strength_weekly_streak"),
        "week_delta_sessions": strength_week_delta,
        "week_qual": _qual(strength_week_delta, STRENGTH_STABLE_THRESHOLD)
    },
    "steps": {
        "avg_all_time": steps_avg_all,
        "avg_7d": steps_avg_7d,
        "avg_30d": steps_avg_30d,
        "delta_7d_vs_30d": steps_delta_7v30,
        "qual": _qual(steps_delta_7v30, STEPS_STABLE_THRESHOLD_QUAL),
        "discrepancy_avg": steps_discrepancy_avg,
        "discrepancy_days": len(step_discrepancies_30d)
    },
    "intensity": {
        "avg_week_year_mins": round(avg_activity_mins_per_week),
        "avg_week_year_run_mins": round(total_run_mins_year / num_run_weeks),
        "avg_week_year_lift_mins": round(total_strength_min_year / num_s_weeks),
        "avg_week_month_mins": round(avg_activity_mins_per_week_month),
        "avg_week_month_run_mins": round(month_run_mins / num_month_run_weeks),
        "avg_week_month_lift_mins": round(month_strength_mins / num_month_s_weeks),
        "delta_month_vs_year": round(avg_activity_mins_per_week_month - avg_activity_mins_per_week),
        "qual": _qual(avg_activity_mins_per_week_month - avg_activity_mins_per_week, INTENSITY_STABLE_THRESHOLD),
        "streak_weeks": intensity_streak,
        "best_streak_weeks": best_intensity_streak,
        "goal_mins": INTENSITY_GOAL_MINS
    },
    "recovery": {
        "week": recovery_week,
        "month": recovery_month,
        "year": recovery_year,
        "delta_week_vs_month": recovery_delta,
        "qual": _qual(recovery_delta, RECOVERY_STABLE_THRESHOLD)
    },

    "cardio_load": cardio_load,

    "chart_16wk": chart_16wk,
    "chart_annual": chart_annual,

    "lt": {
        "latest": ({"lt_pace": latest_lt["lt_pace"], "lt_hr": latest_lt.get("lt_hr"), "source_date": latest_lt.get("lt_source_date", latest_lt.get("date"))} if latest_lt else None),
        "zones": lt_zones,
        "trend": lt_trend
    },

    "pbs": pbs,
    "strength_tests": strength_tests_summary,

    "calendar": {
        "run_dates": calendar_run_dates,
        "strength_dates": calendar_strength_dates,
        "hike_dates": calendar_hike_dates,
        "commentary": _calendar_commentary()
    },

    "data_freshness": _data_freshness(),
    "tile_freshness": _tile_freshness(),

    "fitness_metrics": fitness_metrics,

    "strength_hr_overlay": strength_hr_overlay,

    "zone_time": zone_time,
    "run_hr_source_comparison": run_hr_source_comparison,
    "hr_dropout_fallback": hr_dropout_fallback,

    # NEW (July 2026) — full per-session HR context (avg/max/zone breakdown,
    # for BOTH runs and strength) backing the unified HR unification work.
    # Not yet consumed by index.html or the coach prompt — that's the next
    # two steps (coach prompt update, then the UI restructure that folds
    # H10-dropout annotations into per-session commentary instead of a
    # standalone card). Capped to the most recent 30 sessions each; not
    # meant as a full-history data source, just enough for "recent" framing.
    "run_hr_context": sorted(_run_hr_context.values(), key=lambda x: x["date"], reverse=True)[:30],
    "strength_hr_context": sorted(_strength_hr_context.values(), key=lambda x: x["date"], reverse=True)[:30]
}

with open("dashboard_metrics.json", "w", encoding="utf-8") as f:
    json.dump(dashboard_metrics, f, indent=2)

print(f"dashboard_metrics.json written ({len(calendar_run_dates)} run dates, {len(calendar_strength_dates)} strength dates)")

# ── Data-confidence score ──────────────────────────────────────────────────────
def compute_confidence():
    components = []

    if latest_lt:
        lt_age_days = (last_complete_date - datetime.strptime(latest_lt["date"], "%Y-%m-%d").date()).days
        lt_pts = max(0, min(20, 20 * (1 - max(0, lt_age_days - 14) / 46)))
        components.append(("LT freshness", lt_pts, 20, f"LT reading {lt_age_days}d old"))
    else:
        components.append(("LT freshness", 0, 20, "No LT reading available"))

    recent_complete_runs = [r for r in recent_runs if r.get("date") and r["date"] <= last_complete_str][-8:]
    hr_valid = [r for r in recent_complete_runs if r.get("avg_hr") not in (None, "", "0")]
    if recent_complete_runs:
        hr_pts = 15 * (len(hr_valid) / len(recent_complete_runs))
        components.append(("HR data validity", hr_pts, 15, f"HR data on {len(hr_valid)}/{len(recent_complete_runs)} recent runs"))
    else:
        components.append(("HR data validity", 0, 15, "No recent runs to check HR data"))

    complete_sleep_nights = [d for d in recent_sleep.keys() if d <= last_complete_str]
    sleep_pts = 15 * (min(len(complete_sleep_nights), 14) / 14)
    components.append(("Sleep completeness", sleep_pts, 15, f"Sleep tracked {min(len(complete_sleep_nights), 14)}/14 nights"))

    complete_step_days = [d for d in recent_steps.keys() if d <= last_complete_str]
    cutoff_7d = today_date - timedelta(days=7)
    complete_step_days_7d = [d for d in complete_step_days if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_7d]
    steps_pts = 10 * (min(len(complete_step_days_7d), 7) / 7)
    components.append(("Steps completeness", steps_pts, 10, f"Steps tracked {min(len(complete_step_days_7d), 7)}/7 days"))

    fetch_status = None
    if os.path.exists("fetch_status.json"):
        try:
            with open("fetch_status.json", "r", encoding="utf-8") as f:
                fetch_status = json.load(f)
        except Exception:
            fetch_status = None

    if fetch_status and fetch_status.get("last_success_date"):
        try:
            fetch_age_days = (today_date - datetime.strptime(fetch_status["last_success_date"], "%Y-%m-%d").date()).days
        except Exception:
            fetch_age_days = 99
        pipeline_pts = max(0, min(15, 15 * (1 - max(0, fetch_age_days - 1) / 2)))
        if fetch_age_days <= 1:
            pipeline_reason = "Data fetch up to date"
        else:
            pipeline_reason = f"Last successful fetch {fetch_age_days}d ago"
    else:
        pipeline_pts = 0
        pipeline_reason = "No confirmed successful fetch found"
    components.append(("Pipeline freshness", pipeline_pts, 15, pipeline_reason))

    cutoff_3d = today_date - timedelta(days=3)
    has_recent_run = any(r.get("date", "") >= str(cutoff_3d) for r in recent_runs)
    has_recent_strength = any(s.get("date", "") >= str(cutoff_3d) for s in recent_strength)
    has_recent_steps = any(d >= str(cutoff_3d) for d in recent_steps.keys())
    has_recent_sleep = any(d >= str(cutoff_3d) for d in recent_sleep.keys())
    recent_signals = sum([has_recent_run, has_recent_strength, has_recent_steps, has_recent_sleep])
    context_pts = 25 * (recent_signals / 4)
    if recent_signals >= 3:
        context_reason = "Multiple data sources active in last 3 days"
    elif recent_signals > 0:
        context_reason = "Limited data sources active in last 3 days"
    else:
        context_reason = "No recent activity data in last 3 days"
    components.append(("Recent context", context_pts, 25, context_reason))

    score = sum(pts for _, pts, _, _ in components)
    reasons = [reason for _, _, _, reason in components]

    pct = round(score)
    if pct >= 85:
        label = "High data confidence"
    elif pct >= 65:
        label = "Moderate data confidence"
    else:
        label = "Low data confidence"

    attention = None
    shortfalls = [(name, max_pts - pts, max_pts, reason) for name, pts, max_pts, reason in components if max_pts > 0]
    if shortfalls:
        worst = max(shortfalls, key=lambda x: x[1] / x[2])
        if worst[1] / worst[2] > 0.15:
            attention = worst[3]

    return pct, label, reasons, attention

confidence_pct, confidence_label, confidence_reasons, confidence_attention = compute_confidence()

# ── YoY / PB / context for the coach prompt ───────────────────────────────────
try:
    today_last_year = today_date.replace(year=today_date.year - 1)
except ValueError:
    today_last_year = today_date.replace(year=today_date.year - 1, day=28)

dist_ytd = sum(float(r.get("distance_km") or 0) for r in runs_this_year)
dist_last_year_ytd = sum(
    float(r.get("distance_km") or 0) for r in runs_prev_year
    if datetime.strptime(r["date"], "%Y-%m-%d").date() <= today_last_year
)

pb_cats = [("5K", 4), ("10K", 8), ("Half", 18), ("Marathon", 38), ("50K", 45)]
pb_lines = []
for label, min_dist in pb_cats:
    eligible = [r for r in all_run_rows
        if float(r.get("distance_km") or 0) >= min_dist and r.get("avg_pace_min_km")]
    if eligible:
        best = min(eligible, key=lambda r: parse_pace_sec(r["avg_pace_min_km"]) or 9999)
        pb_lines.append(f"{label}: {best['avg_pace_min_km']} /km on {best['date']} ({best.get('distance_km')} km)")

# HR detail per run now pulls from the unified _run_hr_context (built
# earlier in this file) instead of the raw avg_hr field alone — includes
# max_hr and a per-session zone-minute breakdown so a spiky effort (e.g.
# several hard intervals inside an otherwise easy run) doesn't average out
# to look identical to a genuinely steady session at the same avg HR. This
# was a real, athlete-confirmed gap: a run with repeated hard sprints was
# previously described by the coach as a uniform "low HR" run because only
# a single averaged number was ever visible in this prompt.
def _run_hr_prompt_str(r):
    hr_ctx = _run_hr_context.get(r.get("activity_id", ""))
    if not hr_ctx:
        return "n/a"
    bits = f"avg {hr_ctx['avg_hr']}"
    if hr_ctx.get("max_hr") is not None:
        bits += f"/max {hr_ctx['max_hr']}"
    if hr_ctx.get("zone_summary"):
        bits += f" ({hr_ctx['zone_summary']})"
    if hr_ctx.get("label"):
        bits += f" [{hr_ctx['label']}]"
    return bits

run_details = []
for r in reversed(recent_runs[-8:]):
    elev = f" | ↑{r.get('elevation_gain_m','?')}m" if r.get('elevation_gain_m') else ""
    run_details.append(
        f"  {r['date']} | {r.get('distance_km','?')} km | {r.get('avg_pace_min_km','?')} /km | "
        f"HR {_run_hr_prompt_str(r)} | load {r.get('training_load','?')} | "
        f"ATE {r.get('aerobic_training_effect','?')}{elev} | {r.get('type','?')}"
    )

# Same per-session HR/zone detail for strength — previously the coach only
# ever saw an aggregate session COUNT for strength, never any per-session
# detail at all (no HR, no duration breakdown). Every strength session now
# always carries Polar-derived HR (see build_strength_hr_context — no H10
# during lifting, ever), so this is a straightforward, always-labeled
# addition rather than a fallback case.
def _strength_hr_prompt_str(s):
    hr_ctx = _strength_hr_context.get((s.get("date", ""), s.get("name", "")))
    if not hr_ctx:
        return "n/a"
    bits = f"avg {hr_ctx['avg_hr']}"
    if hr_ctx.get("max_hr") is not None:
        bits += f"/max {hr_ctx['max_hr']}"
    if hr_ctx.get("zone_summary"):
        bits += f" ({hr_ctx['zone_summary']})"
    bits += " [Polar, no H10]"
    return bits

strength_details = []
for s in reversed(recent_strength[-8:]):
    strength_details.append(
        f"  {s.get('date','?')} | {s.get('name','?')} | {s.get('duration_min','?')} min | "
        f"HR {_strength_hr_prompt_str(s)}"
    )

cutoff_12wk = today_date - timedelta(days=84)
runs_12wk = [r for r in all_runs_sorted
    if r.get("date") and datetime.strptime(r["date"], "%Y-%m-%d").date() >= cutoff_12wk]
dist_12wk = sum(float(r.get("distance_km") or 0) for r in runs_12wk)
avg_weekly_dist_12wk = dist_12wk / 12
avg_weekly_runs_12wk = len(runs_12wk) / 12

ate_values = [float(r.get("aerobic_training_effect") or 0)
    for r in all_run_rows if r.get("aerobic_training_effect")
    and r.get("date", "").startswith(str(today_date.year))]
avg_ate_ytd = round(sum(ate_values) / max(len(ate_values), 1), 2)

strength_per_week_ytd = round(
    summary.get('total_strength_this_year', 0) / max(today_date.timetuple().tm_yday / 7, 1), 1)

# Real strength profile for the coach prompt — latest value per exercise
# from strength_tests_summary (built above from strength_tests.csv), rather
# than a hardcoded five-lift string that silently went stale and omitted
# most of the logged tests. Grouped by the CSV's `category` field: known
# categories first in a deliberate order, then any unknown category
# appended alphabetically — derived from the known order rather than a
# hardcoded fixed list, so an unrecognised category is surfaced at the end
# instead of being silently dropped. One "Exercise Value Unit" token per
# exercise within each group, joined by " | ". Latest value only — no
# historical rows.
STRENGTH_CATEGORY_ORDER = ["strength", "power", "durability", "bodyweight"]
_strength_by_category = {}
for e in strength_tests_summary:
    _strength_by_category.setdefault(e.get("category", ""), []).append(e)
_ordered_strength_categories = (
    [c for c in STRENGTH_CATEGORY_ORDER if c in _strength_by_category]
    + sorted(c for c in _strength_by_category if c not in STRENGTH_CATEGORY_ORDER)
)
_strength_profile_lines = []
for _cat in _ordered_strength_categories:
    _cat_exs = " | ".join(
        f"{e['exercise']} {e['latest_value']} {e['unit']}".strip()
        for e in _strength_by_category[_cat]
    )
    _cat_label = _cat.capitalize() if _cat else "Other"
    _strength_profile_lines.append(f"{_cat_label}: {_cat_exs}")
strength_profile_str = "\n    ".join(_strength_profile_lines) if _strength_profile_lines else "No strength test data logged yet"

# Per-session HR shape for the coach prompt (NEW) — most recent 8 runs and
# 8 strength sessions from the unified HR context, each with avg/max HR, a
# zone-minute breakdown, and any Polar/H10-dropout source label. Gives the
# coach per-session SHAPE (not just weekly aggregates) for the ZONES and
# HR_COMPARISON sections. The same context is written to
# dashboard_metrics.json capped at 30; here we feed only the most recent 8
# of each to keep prompt size bounded.
def _hr_ctx_prompt_line(c):
    bits = f"avg {c['avg_hr']}"
    if c.get("max_hr") is not None:
        bits += f"/max {c['max_hr']}"
    if c.get("zone_summary"):
        bits += f" ({c['zone_summary']})"
    if c.get("label"):
        bits += f" [{c['label']}]"
    return f"  {c.get('date', '?')} | {c.get('name', '?')} | HR {bits}"

_recent_run_hr_ctx = sorted(_run_hr_context.values(), key=lambda x: x["date"], reverse=True)[:8]
_recent_strength_hr_ctx = sorted(_strength_hr_context.values(), key=lambda x: x["date"], reverse=True)[:8]
run_hr_shape_block = "\n".join(_hr_ctx_prompt_line(c) for c in _recent_run_hr_ctx) if _recent_run_hr_ctx else "  No per-session run HR data yet"
strength_hr_shape_block = "\n".join(_hr_ctx_prompt_line(c) for c in _recent_strength_hr_ctx) if _recent_strength_hr_ctx else "  No per-session strength HR data yet"

# ── Build prompt ──────────────────────────────────────────────────────────────
system_prompt = """You are an experienced hybrid performance coach with a strong sports science background working with a Danish athlete with serious ultra-endurance and multi-sport capacity.

Your role is to generate a short daily training status summary based on the data provided.

ATHLETE IDENTITY:
The athlete is not a one-dimensional runner. They train for dual capacity: ultra-endurance readiness (long efforts, back-to-back durability, time on feet, trail) AND speed/explosive capacity (fast paces, sprint ability). These are complementary qualities within a broader athletic philosophy. They also train strength seriously, and have broader athletic interests including bouldering, martial arts, swimming, and longevity/mobility work. Their daily commute includes 8 km by bike on weekdays. Do not treat them as a runner who also lifts — treat them as a complete athlete.

STANDING PHILOSOPHY:
The athlete's training orientation is long-term progression across endurance, speed, strength, mobility and athleticism. Recommendations should favour sustainable progression over short-term optimisation, while recognising their capacity and willingness to push hard when appropriate.

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
- Do not infer causation unless the supplied data directly supports it. Prefer "is consistent with" over "because" — e.g. "the pace improvement is consistent with the added strength volume" rather than "the pace improved because of the added strength volume," unless the data actually demonstrates that causal link.
- You will be given a RECENT COACHING HISTORY block showing your last few days' headlines and the most recent WATCH items. Use it only for continuity — to avoid reusing the same framing two days running, and to notice if something previously flagged has resolved, worsened, or changed. Never treat it as evidence, never quote it back verbatim, and never let it substitute for today's actual data.
- Sleep data (from Polar Loop) is supporting context only. Note it when relevant, but do not change training recommendations or caution level based on sleep — this data stream is new and not yet validated enough to drive advice.
- Cardio load strain/tolerance (from Polar Loop, Training Load Pro) is likewise supporting context, not yet validated enough on its own to drive advice. When both sleep and the strain:tolerance ratio point the same direction (e.g. reduced sleep alongside an elevated ratio), that convergence is worth naming as a pattern — but treat each figure as its own signal in its own unit, never combine them into a single score or imply one measures the other.
- Every run and strength session's HR line includes max HR and a zone-minute breakdown (e.g. "avg 150/max 178 (Z2 1m Z3 2m)"), not just an average. Use the breakdown and max HR together with the average to judge the actual character of the effort — an average alone can make a session built from several hard efforts with easy recovery between them look identical to a genuinely steady effort at the same average. Do not describe a session's intensity from the average number alone when a zone breakdown is present; if the zone breakdown shows meaningful time in Z4/Z5, that is a materially different session than one sitting entirely in Z1/Z2, even at a matching average.
- Some HR entries are Polar-derived rather than real Garmin/H10 chest-strap data, marked "[Polar fallback — H10 dropout]" (a run where the H10 recorded nothing) or "[Polar, no H10]" (every strength session — no chest strap is worn while lifting). These are calibrated estimates, not raw H10-precision readings — treat them as directionally reliable and usable exactly like other HR data for judging effort and trends, but do not discuss the sensor, calibration, or data-source mechanics themselves in your output; that's plumbing the athlete already knows about, not part of the coaching story.

DECISION FRAMEWORK — ask these questions before writing:
1. What changed since the prior period? Is the change meaningful or within normal variance?
2. Does it matter — and if so, why?
3. What should the athlete notice or consider as a result?

Every paragraph must implicitly answer at least one of these. Never merely restate statistics.

Tone and style:
- Data-anchored: root every observation in specific numbers from the data.
- Tonally neutral: neither cheerleader nor alarm bell.
- Conversational when flagging outliers or standout efforts — name them directly and briefly reflect on what they might signal.
- Assume the athlete understands training concepts — no need to explain basics.
- No generic encouragement phrases.

Output format — respond using exactly this structure, with these literal delimiter lines. Do not restate specific numeric figures in ANY section below — every number you might cite (distances, paces, HR values, percentages, session counts) is ALREADY shown directly on the dashboard card that section corresponds to. Your job in every section is interpretation and judgement, never a restatement of numbers the athlete can already see right next to your text. Referring to a trend direction or magnitude in words (e.g. "meaningfully higher", "barely changed") is fine — writing the actual figure is not.

HEADLINE:
Up to 3 genuinely short sentences (this renders as a compact few-line brief at the very top of the dashboard — keep it tight). Aim for about 30 words total across the whole headline and stop well under 45; each sentence must be crisp and punchy, never a paragraph packed into one line. The single most important takeaway from today's data, across ALL of training, not just one metric. Not a generic state label like "Building" or "Consolidating". Be specific without citing numbers. Do not restate the athlete's name or date.

OVERVIEW:
1 short paragraph covering Running, Strength, Steps, Intensity Minutes, Recovery, and Load together as a single coherent picture — how these pieces of the week relate to each other, not six separate mini-verdicts. This is the largest section; the rest are each shorter.

TRENDS:
1 short paragraph interpreting the 16-week weekly trend and the year-to-date monthly progression together — is the shape of training building, plateauing, cyclical, or irregular, and does that shape make sense given the athlete's standing goals.

THRESHOLD:
1 short paragraph on lactate threshold and VO2max together — what the current trend in aerobic/threshold fitness actually means for the athlete right now.

ZONES:
1 short paragraph on time-in-zone distribution (Z1 through Z5, the Zone 2 target, the "grey zone") — is the intensity distribution appropriate for what this training block is trying to accomplish. You now also have a PER-SESSION HR SHAPE block (avg/max HR and zone-minute breakdown for the most recent runs and strength sessions), not just the weekly zone aggregate — use it to judge whether individual sessions' intensity distribution matches the block's intent.

HR_COMPARISON:
1 short paragraph on the strength-session HR data and the Garmin-vs-Polar comparison data together — anything genuinely worth noting about HR patterns in strength work or sensor-source agreement. The PER-SESSION HR SHAPE block also marks which sessions are Polar-derived (an H10 dropout on a run, or every strength session) — draw on it for per-session HR patterns, but as noted above do not discuss the sensor/calibration plumbing itself in your output. If there is truly nothing notable, it is fine for this to be a single plain sentence saying so.

CALENDAR:
1 short paragraph on training consistency/frequency patterns visible in the activity calendar — separate from the deterministic streak-count line the dashboard already shows beneath it; add genuine interpretation, not a repeat of "N-week streak."

PERSONAL_BESTS:
1 short paragraph on personal bests — whether current fitness suggests any are within realistic reach soon, or how the existing bests relate to current training focus. If nothing is currently relevant to say, a single plain sentence is fine.

WATCH:
2–4 short bullet points (one per line, starting with "- ") naming specific things worth paying attention to over the coming week — not prescribed workouts or mileage targets, since the training programme is already structured elsewhere. Frame these as things to observe or monitor, e.g. "Watch whether easy-run HR continues to decline." This section is also read back to you as your own continuity memory tomorrow (see RECENT COACHING HISTORY below), so treat it as seriously as you would want your own future notes to be. Do NOT cite specific numbers here. Each item must add a genuinely new angle not already said above, or be omitted. If there is nothing meaningfully worth flagging this week, write a single line: "- Nothing notable to flag this week — steady state."

Do not add any text outside these nine sections, and use the exact delimiter labels (HEADLINE:, OVERVIEW:, TRENDS:, THRESHOLD:, ZONES:, HR_COMPARISON:, CALENDAR:, PERSONAL_BESTS:, WATCH:) on their own lines."""

MEMORY_HISTORY_MAX = 5
coach_context_history = []
if os.path.exists("coach_context.json"):
    try:
        with open("coach_context.json", "r", encoding="utf-8") as f:
            coach_context_history = json.load(f).get("history", [])
    except Exception:
        coach_context_history = []

if coach_context_history:
    _recent_headlines = "\n".join(
        f"  {h.get('date', '?')}: {h.get('headline', '')}" for h in coach_context_history[-MEMORY_HISTORY_MAX:]
    )
    _last_entry = coach_context_history[-1]
    _last_watch = _last_entry.get("watch_items", [])
    _last_watch_text = "\n".join(f"  - {w}" for w in _last_watch) if _last_watch else "  (none flagged)"
    memory_summary_text = f"""RECENT COACHING HISTORY (last {min(len(coach_context_history), MEMORY_HISTORY_MAX)} days — for continuity only; do not repeat these verbatim, and do not treat them as new evidence. Use only to avoid reusing the same framing two days running, and to notice whether something flagged last time has since resolved, changed, or is still relevant):
{_recent_headlines}

Things flagged to watch as of {_last_entry.get('date', '?')}:
{_last_watch_text}"""
else:
    memory_summary_text = "RECENT COACHING HISTORY: none yet (first run, or history not yet accumulated)."

cardio_load_context = "No cardio load data yet (Polar cardio-load feed not yet accumulated)."
if cardio_load.get("strain") is not None:
    cardio_load_context = (
        f"Strain (7d avg load): {cardio_load['strain']:.0f} | Tolerance (28d avg load): {cardio_load['tolerance']:.0f} | "
        f"Ratio: {cardio_load['ratio']:.2f}" + (f" | Status: {cardio_load['status']}" if cardio_load.get("status") else "")
    )

user_prompt = f"""Today: {today_date} (week {today_date.isocalendar()[1]} of {today_date.year})

{memory_summary_text}

ATHLETE PROFILE:
- Experienced hybrid athlete: ultra-endurance durability, speed development, strength, and long-term athleticism
- Longest effort: 137 km (Møn/Vordingborg 100-mile attempt)
- Running PBs: see PERSONAL BESTS below
- Current demonstrated strength profile:
    {strength_profile_str}
- Daily commute: 8 km by bike on weekdays (untracked background load)
- Active modalities: running, strength, occasional bouldering, martial arts, swimming
- Training frequency: ~4x/week running + regular strength
- Running streak: {summary.get('current_weekly_streak', '?')} weeks current (best: {summary.get('longest_weekly_streak', '?')} weeks)
- Strength streak: {summary.get('current_strength_weekly_streak', '?')} weeks current

STANDING TRAINING FOCUS:
Long-term progression across ultra-endurance, speed capacity, strength, mobility and broad athletic readiness. Not strictly periodising toward a single event — building and maintaining durable hybrid capacity year-round.

UPCOMING EVENTS (context only — do not prescribe training toward these, the training programme is already structured elsewhere; mention only if genuinely relevant to today's story, e.g. proximity might explain a deliberate taper or volume choice):
{chr(10).join(f"  {e['date']}: {e['event_name']}" + (f" ({e['distance_km']} km)" if e['distance_km'] else "") + (f" — {e['notes']}" if e['notes'] else "") for e in upcoming_events) if upcoming_events else "  None logged."}

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

STRENGTH (last 4 weeks: {len(recent_strength)} sessions | year to date: {summary.get('total_strength_this_year', '?')} sessions, {strength_per_week_ytd}/week average):
{chr(10).join(strength_details) if strength_details else "  No strength sessions in the last 4 weeks"}

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

CARDIO LOAD (from Polar Loop, Training Load Pro — supporting context only, same caution as sleep above; strain/tolerance are Polar's own rolling 7d/28d averages, not computed here):
{cardio_load_context}

PER-SESSION HR SHAPE (most recent 8 runs and 8 strength sessions — avg/max HR and a zone-minute breakdown per session, so you can judge each session's intensity distribution and sensor-source pattern rather than only the weekly zone aggregate; entries marked with a Polar/H10-dropout label are calibrated estimates — use them normally, but do not discuss the sensor/calibration plumbing in your output):
Runs:
{run_hr_shape_block}
Strength:
{strength_hr_shape_block}

Generate the coaching briefing now."""

# ── Call Anthropic API ────────────────────────────────────────────────────────
api_key = os.environ.get("ANTHROPIC_API_KEY", "")
coach_text = None
token_usage = None

PRICE_INPUT_PER_M  = 5.00
PRICE_OUTPUT_PER_M = 25.00
USD_TO_DKK = 6.90

if api_key:
    try:
        payload = json.dumps({
            "model": "claude-opus-4-8",
            "max_tokens": 1600,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "thinking": {"type": "disabled"}
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

        # Timeout must comfortably exceed the model's generation time for a
        # full max_tokens response. When max_tokens was 700 a 30s read timeout
        # was fine; raising it to 1600 pushed non-streaming Opus generations
        # past 30s, so the read timed out ("The read operation timed out"),
        # coach_text stayed None, and the dashboard fell back to "coach summary
        # unavailable today". 120s gives ~2x headroom for a 1600-token reply.
        with urllib.request.urlopen(req, timeout=120) as resp:
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

if not coach_text:
    coach_text = "Training data updated — coach summary unavailable today."

SECTION_KEYS = ["overview", "trends", "threshold", "zones", "hr_comparison", "calendar", "personal_bests"]
SECTION_DELIMITERS = ["OVERVIEW", "TRENDS", "THRESHOLD", "ZONES", "HR_COMPARISON", "CALENDAR", "PERSONAL_BESTS"]

def parse_coach_sections(text):
    """Parses the new 9-part format: HEADLINE + 7 named narrative sections
    (see SECTION_KEYS/SECTION_DELIMITERS) + WATCH. Evidence is gone
    entirely (July 2026, athlete direction — data-import confirmation is
    the freshness dots' job, not evidence's; the athlete wasn't interested
    in the specific evidence line items). Falls back gracefully: if
    HEADLINE can't be found at all, the ENTIRE raw text is dumped into the
    "overview" section so at least something renders, and every other
    section stays empty rather than guessing at a split."""
    headline = ""
    sections = {k: "" for k in SECTION_KEYS}
    watch_items = []
    try:
        headline_match = re.search(r"HEADLINE:\s*(.+?)(?=\n\s*OVERVIEW:)", text, re.DOTALL)
        if headline_match:
            headline = headline_match.group(1).strip()

            # Each section runs until the next known delimiter, or until
            # WATCH: for the last one (PERSONAL_BESTS).
            for i, key in enumerate(SECTION_KEYS):
                delim = SECTION_DELIMITERS[i]
                next_delim = SECTION_DELIMITERS[i + 1] if i + 1 < len(SECTION_DELIMITERS) else "WATCH"
                m = re.search(rf"{delim}:\s*(.+?)(?=\n\s*{next_delim}:)", text, re.DOTALL)
                if m:
                    sections[key] = m.group(1).strip()

            watch_match = re.search(r"WATCH:\s*(.+)", text, re.DOTALL)
            if watch_match:
                watch_raw = watch_match.group(1).strip()
                watch_items = [
                    line.strip().lstrip("- ").strip()
                    for line in watch_raw.split("\n")
                    if line.strip().startswith("-")
                ]
        else:
            # Couldn't even find HEADLINE — don't guess at a section split,
            # just surface the raw text somewhere visible.
            sections["overview"] = text.strip()
    except Exception as parse_err:
        print(f"Coach section parsing failed, dumping raw text into overview: {parse_err}")
        sections["overview"] = text.strip()
    return headline, sections, watch_items

coach_headline, coach_sections, coach_watch_items = parse_coach_sections(coach_text)

if coach_headline:
    coach_context_history = [h for h in coach_context_history if h.get("date") != str(today_date)]
    coach_context_history.append({
        "date": str(today_date),
        "headline": coach_headline,
        "watch_items": coach_watch_items
    })
    coach_context_history = coach_context_history[-MEMORY_HISTORY_MAX:]
    with open("coach_context.json", "w", encoding="utf-8") as f:
        json.dump({"history": coach_context_history}, f, indent=2)
    print(f"coach_context.json: {len(coach_context_history)} day(s) in rolling history")
else:
    print("coach_context.json: skipped (no headline produced this run — fallback/failed day)")

usage_file = "api_usage.json"
usage_history = []
if os.path.exists(usage_file):
    with open(usage_file, "r", encoding="utf-8") as f:
        usage_history = json.load(f)

if token_usage:
    usage_history = [u for u in usage_history if u.get("date") != str(today_date)]
    usage_history.append(token_usage)
    usage_history = sorted(usage_history, key=lambda x: x["date"], reverse=True)

with open(usage_file, "w", encoding="utf-8") as f:
    json.dump(usage_history, f, indent=2)

this_month = today_date.strftime("%Y-%m")
this_year = str(today_date.year)

mtd_entries = [u for u in usage_history if u.get("date", "").startswith(this_month)]
ytd_entries = [u for u in usage_history if u.get("date", "").startswith(this_year)]

mtd_cost_usd = sum(u.get("cost_usd", 0) for u in mtd_entries)
ytd_cost_usd = sum(u.get("cost_usd", 0) for u in ytd_entries)
mtd_cost_dkk = mtd_cost_usd * USD_TO_DKK
ytd_cost_dkk = ytd_cost_usd * USD_TO_DKK

coach_summary = {
    "last_updated": last_updated_str,
    "headline": coach_headline,
    "confidence_pct": confidence_pct,
    "confidence_label": confidence_label,
    "confidence_reasons": confidence_reasons,
    "confidence_attention": confidence_attention,
    "sections": coach_sections,
    "watch_items": coach_watch_items,
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
print(f"  Headline: {coach_headline[:160]}")
print(f"  Watch items: {len(coach_watch_items)}")
print(f"  Sections populated: {sum(1 for v in coach_sections.values() if v)}/{len(coach_sections)}")
print(f"  Confidence: {confidence_pct}% ({confidence_label})")

# ── Model A/B shadow logging (14-day experiment) ─────────────────────────────
# PURE SIDE-LOGGING — ZERO effect on what the dashboard renders. The live Opus
# coach and the coach_summary.json write above are already complete and are
# NEVER touched by anything below. This block fires a parallel Sonnet call on
# the IDENTICAL prompts, an Opus judge scoring both outputs blind (A/B
# randomized per run), and appends the full record to model_ab_log.json.
# The ENTIRE block is wrapped in try/except: any failure (Sonnet call, judge
# call, parse, or file write) is logged and swallowed so a shadow failure can
# never affect the live coach output or fail the build.
SONNET_PRICE_INPUT_PER_M = 3.00
SONNET_PRICE_OUTPUT_PER_M = 15.00
SHADOW_MAX_TOKENS = 1600

def _anthropic_message(model, sys_prompt, usr_prompt, max_tokens):
    """Minimal Messages API call, same shape as the live coach call above.
    Returns (text, usage_dict). Raises on any HTTP/parse error — the caller's
    try/except handles it."""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": sys_prompt,
        "messages": [{"role": "user", "content": usr_prompt}],
        "thinking": {"type": "disabled"}
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        method="POST"
    )
    # Same 1600-token generations as the live call — use the same generous
    # read timeout so a slow Sonnet/judge reply doesn't spuriously fail the
    # (non-critical) shadow experiment.
    with urllib.request.urlopen(request, timeout=120) as resp:
        parsed = json.loads(resp.read().decode("utf-8"))
    return parsed["content"][0]["text"].strip(), parsed.get("usage", {})

def _shadow_cost(usage, price_in, price_out):
    it = usage.get("input_tokens", 0)
    ot = usage.get("output_tokens", 0)
    cost_usd = (it * price_in / 1_000_000) + (ot * price_out / 1_000_000)
    return {
        "input_tokens": it,
        "output_tokens": ot,
        "cost_usd": round(cost_usd, 6),
        "cost_dkk": round(cost_usd * USD_TO_DKK, 5)
    }

def _parse_judge_json(text):
    """Defensive parse — the judge is asked for strict JSON, but if it wraps
    the object in prose (or returns invalid JSON) we recover the first {...}
    block, and failing that we log the raw text rather than crashing."""
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return {"parse_error": True, "raw_text": text}

# Only run when the live Opus call genuinely succeeded (token_usage is set only
# inside that call's success path) — never shadow a fallback/failed day.
if api_key and token_usage:
    try:
        # 1. Parallel Sonnet call on the IDENTICAL system_prompt + user_prompt.
        sonnet_text, sonnet_usage = _anthropic_message(
            "claude-sonnet-5", system_prompt, user_prompt, SHADOW_MAX_TOKENS)
        opus_cost = _shadow_cost(token_usage, PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M)
        sonnet_cost = _shadow_cost(sonnet_usage, SONNET_PRICE_INPUT_PER_M, SONNET_PRICE_OUTPUT_PER_M)

        # 2. Blind A/B assignment — randomize which model is A vs B per run so
        #    the judge can't pattern-match on position. coach_text is the live
        #    Opus output.
        opus_is_a = random.random() < 0.5
        output_a = coach_text if opus_is_a else sonnet_text
        output_b = sonnet_text if opus_is_a else coach_text
        ab_mapping = {
            "A": "opus" if opus_is_a else "sonnet",
            "B": "sonnet" if opus_is_a else "opus"
        }

        # 3. Judge call — Opus scores both outputs blind against the rubric,
        #    returning STRICT JSON only.
        judge_system = (
            "You are an impartial evaluator comparing two AI-generated daily "
            "training-coaching briefings, labelled OUTPUT A and OUTPUT B. Both "
            "were generated from the identical data context shown below. Score "
            "each output on a 1-5 integer scale for each rubric criterion, with "
            "a one-line reason per score, then state which output you prefer "
            "overall and why. Do not reveal or guess which model produced which "
            "output. Return STRICT JSON only — no prose, no markdown fences, "
            "nothing outside the JSON object."
        )
        judge_rubric = (
            "RUBRIC (score each output 1-5 on all four):\n"
            "1. context_awareness: did it surface genuinely important "
            "situational context (e.g. an upcoming event's specific demands) "
            "rather than just restating metrics?\n"
            "2. artefact_handling: did it correctly identify and disregard "
            "known-bad signals rather than treating them as real?\n"
            "3. no_fabrication: did it avoid stating specific numbers/figures "
            "(the spec forbids restating figures — all numbers already live on "
            "the dashboard cards)?\n"
            "4. coaching_usefulness: overall usefulness of the coaching to the "
            "athlete.\n\n"
            "Return EXACTLY this JSON shape (scores are integers 1-5):\n"
            '{"output_a": {"context_awareness": {"score": 0, "reason": ""}, '
            '"artefact_handling": {"score": 0, "reason": ""}, '
            '"no_fabrication": {"score": 0, "reason": ""}, '
            '"coaching_usefulness": {"score": 0, "reason": ""}}, '
            '"output_b": {"context_awareness": {"score": 0, "reason": ""}, '
            '"artefact_handling": {"score": 0, "reason": ""}, '
            '"no_fabrication": {"score": 0, "reason": ""}, '
            '"coaching_usefulness": {"score": 0, "reason": ""}}, '
            '"preferred": "A", "preference_reason": ""}'
        )
        judge_user = (
            judge_rubric
            + "\n\n=== ORIGINAL DATA CONTEXT (given to both) ===\n" + user_prompt
            + "\n\n=== OUTPUT A ===\n" + output_a
            + "\n\n=== OUTPUT B ===\n" + output_b
        )
        judge_text, judge_usage = _anthropic_message(
            "claude-opus-4-8", judge_system, judge_user, SHADOW_MAX_TOKENS)
        judge_result = _parse_judge_json(judge_text)
        judge_cost = _shadow_cost(judge_usage, PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M)

        # 4. Append one entry to model_ab_log.json (a list; whole history kept).
        ab_log_file = "model_ab_log.json"
        ab_log = []
        if os.path.exists(ab_log_file):
            try:
                with open(ab_log_file, "r", encoding="utf-8") as f:
                    ab_log = json.load(f)
                if not isinstance(ab_log, list):
                    ab_log = []
            except Exception:
                ab_log = []
        ab_log.append({
            "date": str(today_date),
            "ab_mapping": ab_mapping,
            "output_a": output_a,
            "output_b": output_b,
            "opus_usage": opus_cost,
            "sonnet_usage": sonnet_cost,
            "judge": judge_result,
            "judge_usage": judge_cost
        })
        with open(ab_log_file, "w", encoding="utf-8") as f:
            json.dump(ab_log, f, indent=2)
        print(f"Model A/B shadow log appended (model_ab_log.json: {len(ab_log)} run(s)). "
              f"A={ab_mapping['A']} B={ab_mapping['B']} | "
              f"judge preferred: {judge_result.get('preferred', 'unparsed')}")

    except Exception as shadow_err:
        # A shadow failure must NEVER affect the live coach or fail the build.
        print(f"Model A/B shadow logging failed (non-fatal, ignored): {shadow_err}")
