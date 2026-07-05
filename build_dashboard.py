"""
Fred's Feats — aggregation + AI coach.

Reads runs.csv, strength.csv, lactate.json, sleep.csv, polar_steps.csv,
steps.csv, strength_tests.csv (all written by fetch_activities.py or, for
steps.csv/strength_tests.csv, by other sources) and computes EVERY derived
dashboard number (streaks, deltas, chart data, PBs, LT zones/trend, calendar
date-sets, "what's changed" digest) into dashboard_metrics.json. Then calls
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
import urllib.request
from datetime import datetime, timedelta

today = datetime.today()

# ── Load raw data from disk ───────────────────────────────────────────────────
# Always loaded fresh and in full — this script does not care whether today's
# fetch_activities.py run was incremental, full-refresh, or didn't run at all;
# it just aggregates whatever is currently in these files.
def _load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

all_run_rows = sorted(_load_csv("runs.csv"), key=lambda x: x.get("date", ""), reverse=True)
all_strength_rows = sorted(_load_csv("strength.csv"), key=lambda x: x.get("date", ""), reverse=True)

# Strength session duration sanity cap (added July 2026, same principle as
# the existing LT pace sanity filter). A watch left running (forgot to stop
# the timer) can report an implausible duration_min — e.g. one real session
# observed at 875.8 min (14.6 hours) for what was actually a normal workout.
# Capped rather than deleted/zeroed: the session still happened and should
# still count toward frequency/streaks, but its reported duration shouldn't
# be trusted beyond a sane ceiling for any hours-based total. Mutates the
# dicts in place, before any filtering — every downstream subset (
# complete_strength_rows, year_sessions, month_sessions, etc.) references
# these same dict objects, so the cap applies everywhere automatically.
STRENGTH_DURATION_SANITY_CAP_MIN = 240  # 4 hours
for _row in all_strength_rows:
    try:
        _dur = float(_row.get("duration_min") or 0)
        if _dur > STRENGTH_DURATION_SANITY_CAP_MIN:
            _row["duration_min"] = str(STRENGTH_DURATION_SANITY_CAP_MIN)
    except (ValueError, TypeError):
        pass

# polar_hr.csv — written by fetch_activities.py (8-day rolling window,
# 5-minute-interval continuous HR). Loaded here for the strength-session HR
# overlay below. Indexed by date -> list of (time_str, heart_rate) for fast
# per-session lookup.
_polar_hr_by_date = {}
for row in _load_csv("polar_hr.csv"):
    d = row.get("date")
    t = row.get("time")
    hr = row.get("heart_rate")
    if d and t and hr not in (None, ""):
        _polar_hr_by_date.setdefault(d, []).append((t, hr))

# run_hr_samples.csv — written by fetch_activities.py via Garmin's
# get_activity_details() (H10-sourced, per-run). Indexed by activity_id ->
# list of (elapsed_seconds, heart_rate), used for the RUN ZONE-TIME PROJECT's
# zone-binning below.
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

# ── Helpers (shared formatting/parsing — kept identical to fetch_activities.py
# so pace strings etc. round-trip consistently between the two scripts) ──────
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
# Same logic the dashboard/coach have always used — average when both sources
# report a day, fallback to whichever single source exists. If the two
# sources disagree by more than 50%, use the larger (more complete) reading
# instead of averaging — most likely one device wasn't worn the full day
# rather than both being equally valid partial counts.
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

# ── Strength/power/durability test history (manually maintained) ─────────────
# strength_tests.csv is edited directly via GitHub whenever a test happens —
# it is NOT written by fetch_activities.py and never goes through that
# script's resilient-commit loop. Loaded here so latest-vs-prior deltas can
# feed the evidence catalog, same pattern as every other evidence category.
strength_test_history = {}
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
    """Direction word for a delta given a per-metric 'meaningful change'
    threshold — value-neutral (no good/bad framing)."""
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
year_dist = sum(float(r.get("distance_km") or 0) for r in year_runs)
total_strength_min_year = sum(float(s.get("duration_min") or 0) for s in year_sessions)

run_weeks_year = {_iso_week_key(datetime.strptime(r["date"], "%Y-%m-%d").date()) for r in year_runs if r.get("date")}
s_weeks_year = {_iso_week_key(datetime.strptime(s["date"], "%Y-%m-%d").date()) for s in year_sessions if s.get("date")}
activity_weeks_year = run_weeks_year | s_weeks_year
num_run_weeks = max(len(run_weeks_year), 1)
num_s_weeks = max(len(s_weeks_year), 1)
num_activity_weeks = max(len(activity_weeks_year), 1)

avg_run_dist_per_week = year_dist / num_run_weeks
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

# ── Running / Strength week-over-week deltas ──────────────────────────────────
last_complete_week = _iso_week_key(last_complete_date)
prev_complete_week = _shift_week_key(last_complete_week, -1)

def _week_run_dist(wk):
    return sum(float(r.get("distance_km") or 0) for r in complete_run_rows if r.get("date") and _iso_week_key(datetime.strptime(r["date"], "%Y-%m-%d").date()) == wk)

def _week_strength_count(wk):
    return sum(1 for s in complete_strength_rows if s.get("date") and _iso_week_key(datetime.strptime(s["date"], "%Y-%m-%d").date()) == wk)

run_week_delta = _week_run_dist(last_complete_week) - _week_run_dist(prev_complete_week)
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

all_week_keys_16 = sorted(set(list(week_map_16.keys()) + list(s_week_map_16.keys())))[-16:]
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
    "strength_sessions": [s_week_map_16.get(wk, 0) for wk in all_week_keys_16],
    "month_boundaries": chart_16wk_month_boundaries,
    "month_labels": chart_16wk_month_labels
}

# ── Annual progress chart ─────────────────────────────────────────────────────
prev_year_dist = summary.get("total_distance_prev_year_km")
target_km = prev_year_dist if (prev_year_dist and prev_year_dist > 0) else 2000
target_label = f"vs {today_date.year - 1} total ({target_km:.0f} km)" if (prev_year_dist and prev_year_dist > 0) else "Progress to 2,000 km"
raw_pct = (year_dist / target_km) * 100 if target_km else 0
is_overflow = year_dist > target_km

month_dist = [0.0] * 12
for r in year_runs:
    if r.get("date"):
        month_dist[datetime.strptime(r["date"], "%Y-%m-%d").date().month - 1] += float(r.get("distance_km") or 0)
current_month_idx = today_date.month - 1
month_strength = [0] * 12
for s in year_sessions:
    if s.get("date"):
        month_strength[datetime.strptime(s["date"], "%Y-%m-%d").date().month - 1] += 1

total_strength_prev_year = sum(1 for s in all_strength_rows if s.get("date", "").startswith(prev_year_str))
strength_pct = round((len(year_sessions) / total_strength_prev_year) * 100) if total_strength_prev_year > 0 else None
strength_overflow = total_strength_prev_year > 0 and len(year_sessions) > total_strength_prev_year

chart_annual = {
    "target_label": target_label,
    "target_km": round(target_km, 1),
    "raw_pct": round(raw_pct, 1),
    "display_pct": round(min(raw_pct, 130), 1),
    "is_overflow": is_overflow,
    "year_dist_km": round(year_dist, 1),
    "km_to_target_km": round(abs(target_km - year_dist), 1),
    "strength_target_label": (f"vs {prev_year_str} total ({total_strength_prev_year} sessions)" if total_strength_prev_year > 0 else "Sessions this year"),
    "strength_pct": strength_pct,
    "strength_display_pct": min(strength_pct, 130) if strength_pct is not None else 0,
    "strength_overflow": strength_overflow,
    "strength_sessions_done": len(year_sessions),
    "strength_sessions_to_match": (total_strength_prev_year - len(year_sessions)) if total_strength_prev_year > 0 else None,
    "month_labels": month_names_short[:current_month_idx + 1],
    "month_distance_km": [round(v, 1) for v in month_dist[:current_month_idx + 1]],
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
        {"name": "Z4 LT", "pace_label": f"{sec_to_pace(lt_sec - 10)}–{sec_to_pace(lt_sec + 9)}", "hr_label": (f"{z3}–{z4} bpm" if z3 else ""), "color": "#ff9f40", "pct": 90},
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

# ── Strength-session HR overlay (first build, July 2026) ─────────────────────
# Matches polar_hr.csv's continuous 5-min-interval samples against each
# strength session's [start_time, start_time + duration_min] window, using
# the start_time field added to strength.csv for exactly this purpose.
# Only produces an entry for sessions that both (a) have a start_time (i.e.
# were fetched/refreshed after start_time was added) and (b) fall on a date
# present in polar_hr.csv (i.e. within the last ~8 days the Loop was worn
# and fetched). Silently skips everything else — this is expected to be
# sparse at first and will naturally fill in as more overlapping days
# accumulate. NOT yet using Polar's exercise list (/v3/exercises) for a
# possible finer-sampled auto-detected-session HR — continuous HR only,
# per the "start simple" plan; see PROJECT CONTEXT for the fuller plan.
def _time_to_seconds(t):
    try:
        h, m, s = (int(p) for p in t.split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return None

def build_strength_hr_overlay():
    overlay = []
    for s in all_strength_rows:
        start_time_full = s.get("start_time", "")
        duration_min = s.get("duration_min", "")
        if not start_time_full or not duration_min:
            continue
        try:
            session_date = start_time_full[:10]
            session_start_sec = _time_to_seconds(start_time_full[11:19])
            duration_sec = float(duration_min) * 60
        except Exception:
            continue
        if session_start_sec is None:
            continue
        session_end_sec = session_start_sec + duration_sec

        day_samples = _polar_hr_by_date.get(session_date, [])
        matched_hrs = []
        for (t, hr) in day_samples:
            t_sec = _time_to_seconds(t)
            if t_sec is not None and session_start_sec <= t_sec <= session_end_sec:
                try:
                    matched_hrs.append(float(hr))
                except (ValueError, TypeError):
                    continue

        if matched_hrs:
            overlay.append({
                "date": session_date,
                "name": s.get("name", ""),
                "avg_hr": round(sum(matched_hrs) / len(matched_hrs)),
                "max_hr": round(max(matched_hrs)),
                "sample_count": len(matched_hrs)
            })
    return sorted(overlay, key=lambda x: x["date"], reverse=True)

strength_hr_overlay = build_strength_hr_overlay()

# ── RUN ZONE-TIME PROJECT (July 2026) ─────────────────────────────────────────
# Quantifies actual time-in-physiological-zone (using the same Z1-Z5 HR
# boundaries as the LT card, NOT Garmin's own zone definitions) across both
# running and strength, so the dashboard's numbers are comparable to real
# training-science recommendations rather than just flat duration totals.
#
# Framework: Dr. Peter Attia's Zone 2 / polarized model — 80% of cardio time
# in Zone 2, 20% in Zone 5, Zone 2 target 180-240 min/week, Zone 3-4 ("grey
# zone") explicitly something to minimize rather than a neutral bucket.
#
# Source split: running uses Garmin/H10 raw per-second HR (run_hr_samples.csv)
# as authoritative for zone classification. Strength uses Polar continuous HR
# (already computed above as strength_hr_overlay's avg_hr) since Garmin has
# no HR data for strength at all. Polar's continuous HR for RUNS is NOT
# discarded despite Garmin being authoritative there — it's kept separately
# and compared against Garmin for the same run window (see
# run_hr_source_comparison below), mirroring the existing steps.csv vs
# polar_steps.csv reconciliation pattern. Neither source replaces the other.

def _hr_zone_bounds(lt_hr):
    """Returns (z1, z2, z3, z4) HR boundaries, same formula as _calc_zones()
    above — kept separate so classification logic doesn't need the
    display-string building _calc_zones() does."""
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

# Attia-target comparison. Z2 target 180-240 min/week (his stated 3-4 hrs).
# "Grey zone" = Z3+Z4 combined (his framework treats moderate-hard as one
# thing to minimize, not two separate zones) — Z5 = his VO2max/hard zone.
ATTIA_Z2_TARGET_MIN = 180
ATTIA_Z2_TARGET_MAX = 240

def _compute_zone_time(window_start, window_end, window_label, window_days):
    """Computes zone-minutes and the Attia comparison for a given inclusive
    date range [window_start, window_end]. Factored out so the same logic
    can be run for both the rolling-7-day window (default, always full,
    good for live pace-tracking against the weekly target) and the last
    COMPLETED calendar week (Mon-Sun, matching the athlete's actual training
    blocks — always requested only once a week is fully over, so it's never
    partial/misleadingly low the way a live current-calendar-week view
    would be)."""
    zone_minutes = {"Z1": 0.0, "Z2": 0.0, "Z3": 0.0, "Z4": 0.0, "Z5": 0.0}
    running_sessions_with_zone_data = 0
    strength_sessions_with_zone_data = 0

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
                    continue  # skip implausible gaps (sensor dropout) rather than misattribute
                zone = _classify_hr_zone(hr0, _lt_hr_bounds)
                if zone:
                    zone_minutes[zone] += delta_sec / 60

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

    return {
        "label": window_label,
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "minutes": {k: round(v, 1) for k, v in zone_minutes.items()},
        "total_minutes": round(total_zone_minutes, 1),
        "z2_pct": round((zone_minutes["Z2"] / total_zone_minutes) * 100, 1) if total_zone_minutes else None,
        "grey_pct": round(((zone_minutes["Z3"] + zone_minutes["Z4"]) / total_zone_minutes) * 100, 1) if total_zone_minutes else None,
        "z5_pct": round((zone_minutes["Z5"] / total_zone_minutes) * 100, 1) if total_zone_minutes else None,
        "z2_target_min": ATTIA_Z2_TARGET_MIN,
        "z2_target_max": ATTIA_Z2_TARGET_MAX,
        "z2_vs_target_pct": round((zone_minutes["Z2"] / ATTIA_Z2_TARGET_MIN) * 100) if ATTIA_Z2_TARGET_MIN else None,
        "running_sessions_with_data": running_sessions_with_zone_data,
        "strength_sessions_with_data": strength_sessions_with_zone_data,
        "has_lt_data": _lt_hr_bounds is not None
    }

# Rolling 7-day window (default view): today minus 7 days through today.
_rolling_start = today_date - timedelta(days=7)
zone_time_rolling = _compute_zone_time(_rolling_start, today_date, "This week (rolling 7d)", 7)

# Last COMPLETED calendar week (Mon-Sun), reusing the same ISO-week helpers
# already built for the streak/delta logic elsewhere in this file — this is
# always a full week (never partial), matching the athlete's actual
# training blocks rather than an arbitrary rolling window.
#
# IMPORTANT: last_complete_week (yesterday's ISO week) is only a genuinely
# FINISHED week if yesterday was a Sunday (i.e. today is Monday) — every
# other day of the week, last_complete_week is still the in-progress CURRENT
# week, and using it directly here would have mislabeled an unfinished week
# as "completed." Caught via a runtime test before shipping, not assumed.
if last_complete_date.weekday() == 6:  # Sunday=6 in Python's date.weekday()
    _true_last_completed_week = last_complete_week
else:
    _true_last_completed_week = _shift_week_key(last_complete_week, -1)

_last_week_monday = _week_start_date(_true_last_completed_week)
_last_week_sunday = _last_week_monday + timedelta(days=6)
zone_time_last_completed_week = _compute_zone_time(_last_week_monday, _last_week_sunday, "Last completed week", 7)

zone_time = {
    "rolling": zone_time_rolling,
    "last_completed_week": zone_time_last_completed_week,
    "default_view": "rolling"
}

# ── Garmin-vs-Polar run HR comparison ─────────────────────────────────────────
# For runs with BOTH a start_time (added to runs.csv same round as this
# feature — existing rows before that will simply be skipped, same
# backfill-via-FULL_REFRESH pattern already used for strength.csv) and
# polar_hr.csv coverage for that date, compares Garmin's own avg_hr (H10,
# already stored per run) against Polar's avg HR over the same time window —
# mirrors the existing steps.csv vs polar_steps.csv reconciliation. Neither
# source is treated as "correct" here — this is presented as a comparison,
# not a discrepancy to resolve.
def build_run_hr_source_comparison():
    comparison = []
    for r in all_run_rows:
        start_time_full = r.get("start_time", "")
        if not start_time_full or not r.get("avg_hr"):
            continue
        run_date = start_time_full[:10]
        if run_date not in _polar_hr_by_date:
            continue
        try:
            run_start_sec = _time_to_seconds(start_time_full[11:19])
            moving_parts = (r.get("moving_time") or "").split(":")
            if len(moving_parts) != 3:
                continue
            h, m, s = (int(p) for p in moving_parts)
            duration_sec = h * 3600 + m * 60 + s
        except Exception:
            continue
        if run_start_sec is None or duration_sec <= 0:
            continue
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
            garmin_avg = float(r["avg_hr"])
            polar_avg = sum(matched_polar_hrs) / len(matched_polar_hrs)
            comparison.append({
                "date": run_date,
                "name": r.get("name", ""),
                "garmin_avg_hr": round(garmin_avg),
                "polar_avg_hr": round(polar_avg),
                "diff": round(polar_avg - garmin_avg, 1),
                "polar_sample_count": len(matched_polar_hrs)
            })
    return sorted(comparison, key=lambda x: x["date"], reverse=True)

run_hr_source_comparison = build_run_hr_source_comparison()

# ── What's Changed digest ─────────────────────────────────────────────────────
# Volume threshold/label kept aligned with the evidence catalog's own
# "4-week volume" item (both threshold=1) so the two sections can never
# disagree on direction for the same underlying numbers again.
digest_lines = []
if latest_lt and baseline_lt:
    d_sec = parse_pace_sec(baseline_lt["lt_pace"]) - parse_pace_sec(latest_lt["lt_pace"])
    arrow = "▲" if d_sec > 1 else "▼" if d_sec < -1 else "➔"
    word = "improved" if d_sec > 0 else "slowed" if d_sec < 0 else "unchanged"
    digest_lines.append(f"{arrow} LT pace {word} {abs(d_sec)}s/km over 30 days ({baseline_lt['lt_pace']} → {latest_lt['lt_pace']}/km)")
elif latest_lt:
    digest_lines.append(f"➔ LT established at {latest_lt['lt_pace']}/km (baseline building)")
else:
    digest_lines.append("➔ No LT reading yet")

vol_delta = recent_dist - prior_dist
vol_arrow = "▲" if vol_delta > 1 else "▼" if vol_delta < -1 else "➔"
vol_word = "building" if prior_dist <= 0 else ("unchanged" if abs(vol_delta) <= 1 else ("increased" if vol_delta > 0 else "reduced"))
digest_lines.append(f"{vol_arrow} 4-week volume {vol_word} ({recent_dist:.0f} vs {prior_dist:.0f} km prior block)")

str_delta = len(recent_strength) - len(prior_strength)
str_arrow = "▲" if str_delta > 0.5 else "▼" if str_delta < -0.5 else "➔"
digest_lines.append(f"{str_arrow} Strength frequency {len(recent_strength)} vs {len(prior_strength)} sessions prior block")

steps_recent_4wk = [v for d, v in steps_data.items() if datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_4wk and d <= last_complete_str]
steps_prior_4wk = [v for d, v in steps_data.items() if cutoff_8wk <= datetime.strptime(d, "%Y-%m-%d").date() < cutoff_4wk]
if steps_recent_4wk and steps_prior_4wk:
    r_avg, p_avg = sum(steps_recent_4wk) / len(steps_recent_4wk), sum(steps_prior_4wk) / len(steps_prior_4wk)
    d = r_avg - p_avg
    arrow = "▲" if d > 300 else "▼" if d < -300 else "➔"
    digest_lines.append(f"{arrow} Steps averaging {round(r_avg):,} vs {round(p_avg):,}/day prior block")
else:
    digest_lines.append("➔ Steps data still accumulating")

sleep_recent_4wk = [float(v["total_sleep_min"]) for d, v in sleep_data.items() if v.get("total_sleep_min") and datetime.strptime(d, "%Y-%m-%d").date() >= cutoff_4wk and d <= str(today_date)]
sleep_prior_4wk = [float(v["total_sleep_min"]) for d, v in sleep_data.items() if v.get("total_sleep_min") and cutoff_8wk <= datetime.strptime(d, "%Y-%m-%d").date() < cutoff_4wk]
if sleep_recent_4wk and sleep_prior_4wk:
    r_avg, p_avg = sum(sleep_recent_4wk) / len(sleep_recent_4wk), sum(sleep_prior_4wk) / len(sleep_prior_4wk)
    d = r_avg - p_avg
    arrow = "▲" if d > 5 else "▼" if d < -5 else "➔"
    def _hm(mins):
        return f"{int(mins) // 60}h{int(mins) % 60:02d}m"
    digest_lines.append(f"{arrow} Sleep averaging {_hm(r_avg)} vs {_hm(p_avg)} prior block")
else:
    digest_lines.append("➔ Sleep data still accumulating")

digest_lines.append(f"➔ Running streak: {summary.get('current_weekly_streak', '—')} wks (best: {summary.get('longest_weekly_streak', '—')})")

# ── Assemble and write dashboard_metrics.json ─────────────────────────────────
dashboard_metrics = {
    "last_updated": today.strftime("%Y-%m-%d %H:%M UTC"),
    "last_complete_date": last_complete_str,
    "this_year": this_year_str,
    "this_month_short": this_month_short,

    "running": {
        "avg_per_run_km": round(avg_run_dist_per_run, 1),
        "runs_this_year": summary.get("total_runs_this_year", len(year_runs)),
        "avg_per_week_km": round(avg_run_dist_per_week, 1),
        "total_distance_this_year_km": summary.get("total_distance_this_year_km", round(year_dist, 1)),
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

    "chart_16wk": chart_16wk,
    "chart_annual": chart_annual,

    "lt": {
        "latest": ({"lt_pace": latest_lt["lt_pace"], "lt_hr": latest_lt.get("lt_hr"), "source_date": latest_lt.get("lt_source_date", latest_lt.get("date"))} if latest_lt else None),
        "zones": lt_zones,
        "trend": lt_trend
    },

    "pbs": pbs,

    "calendar": {
        "run_dates": calendar_run_dates,
        "strength_dates": calendar_strength_dates
    },

    "strength_hr_overlay": strength_hr_overlay,

    "zone_time": zone_time,
    "run_hr_source_comparison": run_hr_source_comparison,

    "digest": digest_lines
}

with open("dashboard_metrics.json", "w", encoding="utf-8") as f:
    json.dump(dashboard_metrics, f, indent=2)

print(f"dashboard_metrics.json written ({len(calendar_run_dates)} run dates, {len(calendar_strength_dates)} strength dates)")

# ── Data-confidence score ──────────────────────────────────────────────────────
# Deliberately NOT generated by the LLM. Measures DATA QUALITY/FRESHNESS only —
# never training volume or behavior.
def compute_confidence():
    # Each component tracked as (name, points_earned, max_points, reason_text)
    # so we can both sum a total score AND identify the single weakest
    # component afterward for a short "attention" line on the dashboard —
    # rather than just a wall of reasons no one reads on mobile.
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

    # Pipeline freshness — reads fetch_status.json, written by fetch_activities.py
    # only on a fully successful run. Full points if that file's last_success_date
    # is today or yesterday (accounts for the script running just after local
    # midnight relative to when the athlete checks the dashboard); score decays
    # to 0 by 3+ days stale, and 0 if the file is missing entirely (fetch has
    # never succeeded, or this is the very first run before it exists).
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

    # Attention line: the single weakest component by how far short of its
    # max it falls, but only surfaced if it's actually meaningfully short
    # (>15% of that component's own max) — a component at 90%+ of its max
    # isn't worth flagging even if it's technically the "weakest" one.
    attention = None
    shortfalls = [(name, max_pts - pts, max_pts, reason) for name, pts, max_pts, reason in components if max_pts > 0]
    if shortfalls:
        worst = max(shortfalls, key=lambda x: x[1] / x[2])
        if worst[1] / worst[2] > 0.15:
            attention = worst[3]

    return pct, label, reasons, attention

confidence_pct, confidence_label, confidence_reasons, confidence_attention = compute_confidence()

# ── Evidence catalog ────────────────────────────────────────────────────────────
def _trend_arrow(delta, threshold=0.0001):
    if delta > threshold:
        return "▲"
    elif delta < -threshold:
        return "▼"
    return "▬"

def _priority_tier(abs_delta, high, medium):
    if abs_delta >= high:
        return "High"
    elif abs_delta >= medium:
        return "Medium"
    return "Low"

def build_evidence_catalog():
    items = []

    if latest_lt and baseline_lt:
        latest_sec = parse_pace_sec(latest_lt["lt_pace"])
        baseline_sec = parse_pace_sec(baseline_lt["lt_pace"])
        if latest_sec is not None and baseline_sec is not None:
            delta_sec = baseline_sec - latest_sec
            arrow = _trend_arrow(delta_sec)
            text = (f"{arrow} LT pace {'improved' if delta_sec > 0 else 'slowed' if delta_sec < 0 else 'unchanged'} "
                    f"{abs(delta_sec)}s/km over 30 days ({baseline_lt['lt_pace']} → {latest_lt['lt_pace']}/km)")
            items.append((text, _priority_tier(abs(delta_sec), high=5, medium=2)))

    if prior_dist > 0:
        vol_delta_pct = ((recent_dist - prior_dist) / prior_dist) * 100
        arrow = _trend_arrow(recent_dist - prior_dist, threshold=1)
        text = (f"{arrow} 4-week volume {recent_dist:.0f} km vs {prior_dist:.0f} km prior block "
                f"({vol_delta_pct:+.0f}%)")
        items.append((text, _priority_tier(abs(vol_delta_pct), high=15, medium=5)))

    current_streak = summary.get("current_weekly_streak")
    if current_streak:
        text = f"▬ {current_streak}-week running streak (best: {summary.get('longest_weekly_streak', '?')} wks)"
        items.append((text, "Medium"))

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

    # Sleep SCORE trend (added — was computed as avg_sleep_score for the coach
    # prompt's supporting-metrics text but never fed into the evidence catalog
    # itself; audit gap closed here). Same threshold-tier pattern as duration.
    prior_sleep_scores = [float(s["sleep_score"]) for s in prior_sleep.values() if s.get("sleep_score")]
    if sleep_scores and prior_sleep_scores:
        avg_prior_score = sum(prior_sleep_scores) / len(prior_sleep_scores)
        avg_recent_score = sum(sleep_scores) / len(sleep_scores)
        delta_score = avg_recent_score - avg_prior_score
        arrow = _trend_arrow(delta_score, threshold=3)
        text = f"{arrow} Sleep score averaging {avg_recent_score:.0f} vs {avg_prior_score:.0f} prior period"
        items.append((text, _priority_tier(abs(delta_score), high=10, medium=4)))

    # Zone 2 / Attia-target comparison (added — zone_time was fully built and
    # rendered on the dashboard but never surfaced to the coach; audit gap
    # closed here). Uses the rolling 7-day window (current-status framing,
    # not a vs-prior-period trend) since that's what the coach is assessing
    # "right now," matching the dashboard's default view. Priority tiered by
    # shortfall from target rather than a delta, since this is a target
    # comparison, not a trend.
    zt = zone_time.get("rolling", {})
    if zt.get("has_lt_data") and zt.get("total_minutes", 0) > 0:
        z2_min = zt["minutes"]["Z2"]
        z2_ratio = zt.get("z2_vs_target_pct") or 0
        grey_pct = zt.get("grey_pct") or 0
        arrow = "▲" if z2_ratio >= 100 else "▼" if z2_ratio < 65 else "▬"
        text = (f"{arrow} Zone 2: {z2_min:.0f} min vs {zt['z2_target_min']}-{zt['z2_target_max']} min/week target "
                f"({z2_ratio:.0f}% of minimum); grey zone (Z3+Z4): {grey_pct:.1f}%")
        priority = "High" if z2_ratio < 50 else "Medium" if z2_ratio < 100 else "Low"
        items.append((text, priority))

    if len(recent_strength) or len(prior_strength):
        delta_strength = len(recent_strength) - len(prior_strength)
        arrow = _trend_arrow(delta_strength)
        text = f"{arrow} Strength consistency: {len(recent_strength)} sessions vs {len(prior_strength)} prior 4wk"
        items.append((text, _priority_tier(abs(delta_strength), high=2, medium=1)))

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

    for exercise, history in strength_test_history.items():
        if len(history) < 2:
            continue
        latest, prior = history[-1], history[-2]
        try:
            latest_val, prior_val = float(latest["value"]), float(prior["value"])
        except (ValueError, TypeError):
            continue
        if prior_val == 0:
            continue
        delta_pct = ((latest_val - prior_val) / prior_val) * 100
        arrow = _trend_arrow(latest_val - prior_val, threshold=0.01)
        unit = latest.get("unit", "")
        text = (f"{arrow} {exercise} {latest_val:g}{unit} vs {prior_val:g}{unit} "
                f"({latest['date']} vs {prior['date']}, {delta_pct:+.0f}%)")
        items.append((text, _priority_tier(abs(delta_pct), high=8, medium=3)))

    return items

evidence_catalog_tiered = build_evidence_catalog()
evidence_catalog = [text for text, _priority in evidence_catalog_tiered]
evidence_priority_map = {text: priority for text, priority in evidence_catalog_tiered}

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

run_details = []
for r in reversed(recent_runs[-8:]):
    elev = f" | ↑{r.get('elevation_gain_m','?')}m" if r.get('elevation_gain_m') else ""
    run_details.append(
        f"  {r['date']} | {r.get('distance_km','?')} km | {r.get('avg_pace_min_km','?')} /km | "
        f"HR {r.get('avg_hr','?')} | load {r.get('training_load','?')} | "
        f"ATE {r.get('aerobic_training_effect','?')}{elev} | {r.get('type','?')}"
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
- Assume the athlete understands training concepts — no need to explain basics.
- No generic encouragement phrases.

Output format — respond using exactly this structure, with these literal delimiter lines:

HEADLINE:
One sentence (roughly 12-20 words), the single most important insight from today's data. This is the answer to "what is today's story?" — not a generic state label like "Building" or "Consolidating". Be specific and data-anchored. Examples of the right level of specificity: "Aerobic fitness remains stable despite reduced peak mileage." / "Threshold fitness continues to strengthen." Do not restate the athlete's name or date.

SUMMARY:
1–3 short paragraphs — as many as the data genuinely warrants, no more. This section is your COACHING JUDGEMENT, not a restatement of numbers: the EVIDENCE section below will carry the specific figures, so do not repeat exact stats here (no "127 km vs 125 km", no "HR 161", no "Load 239" — that level of detail belongs in EVIDENCE only). Instead, lead with the conclusion and only add a sentence of plain-language reasoning if it materially changes how the reader should interpret the conclusion. Someone should be able to read this section alone, in under 30 seconds, and understand the coaching takeaway. No bullet points, no headers, no greeting, no sign-off. Write in second person ("your threshold...", "you've...").

EVIDENCE:
The AVAILABLE EVIDENCE list below (in the data section) is numbered. Write ONLY the numbers of items that genuinely support the headline and summary you wrote — one number per line, nothing else on that line (no text, no restating the item). Select as many or as few as are genuinely relevant, in any order — there is no fixed count, but prefer fewer, stronger items over many weak ones; comprehensiveness is not the goal. If nothing in the list meaningfully supports today's story, write a single line: "0"

WATCH:
2–4 short bullet points (one per line, starting with "- ") naming specific things worth paying attention to over the coming week — not prescribed workouts or mileage targets, since the training programme is already structured elsewhere. Frame these as things to observe or monitor, e.g. "Watch whether easy-run HR continues to decline." / "Sleep quality may become more important after the long run." Do NOT cite specific numbers here (no "157 to 147", no "4:24/km") — WATCH items are directional and forward-looking only; exact figures belong in EVIDENCE. If there is nothing meaningfully worth flagging this week, write a single line: "- Nothing notable to flag this week — steady state."

Do not add any text outside these four sections, and use the exact delimiter labels (HEADLINE:, SUMMARY:, EVIDENCE:, WATCH:) on their own lines."""

user_prompt = f"""Today: {today_date} (week {today_date.isocalendar()[1]} of {today_date.year})

ATHLETE PROFILE:
- Experienced hybrid athlete: ultra-endurance durability, speed development, strength, and long-term athleticism
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
# WARNING: hardcoded, must be updated manually if Anthropic changes pricing.
# Last verified: 2026-07-01. Model: claude-sonnet-4-6.
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

if not coach_text:
    coach_text = "Training data updated — coach summary unavailable today."

def parse_coach_sections(text):
    headline = ""
    summary_text = text
    evidence_items = []
    watch_items = []
    try:
        headline_match = re.search(r"HEADLINE:\s*(.+?)(?=\n\s*SUMMARY:)", text, re.DOTALL)
        summary_match = re.search(r"SUMMARY:\s*(.+?)(?=\n\s*EVIDENCE:)", text, re.DOTALL)
        evidence_match = re.search(r"EVIDENCE:\s*(.+?)(?=\n\s*WATCH:)", text, re.DOTALL)
        watch_match = re.search(r"WATCH:\s*(.+)", text, re.DOTALL)

        if headline_match and summary_match:
            headline = headline_match.group(1).strip()
            summary_text = summary_match.group(1).strip()

            if evidence_match:
                evidence_raw = evidence_match.group(1).strip()
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
    return headline, summary_text, evidence_items, watch_items

coach_headline, coach_summary_text, coach_evidence_items, coach_watch_items = parse_coach_sections(coach_text)

# ── Accumulate usage history ──────────────────────────────────────────────────
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

# ── Write coach_summary.json ──────────────────────────────────────────────────
coach_summary = {
    "last_updated": today.strftime("%Y-%m-%d %H:%M UTC"),
    "headline": coach_headline,
    "confidence_pct": confidence_pct,
    "confidence_label": confidence_label,
    "confidence_reasons": confidence_reasons,
    "confidence_attention": confidence_attention,
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
