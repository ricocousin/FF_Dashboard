"""
Fred's Feats — clear stuck Polar exercise transactions (ONE-OFF, run once).

Two transactions (334708890, 334709649) were opened during read-only
diagnosis of the Polar exercise-transactions flow and deliberately never
committed. That was correct at the time — Polar warns that committing
without successfully processing/storing the data means it can't be
retrieved again. Now that fetch_activities.py's real implementation is
built and confirmed to process/store this data correctly, that same
caution has become a liability: a real production fetch got 204 No
Content, because Polar blocks new transaction creation while an older one
is still open uncommitted.

This script clears the blockage by doing exactly what production code
would have done with these two known transactions: fetch each one's
exercise list, match against the CURRENT runs.csv already checked out in
this workflow, write the results into polar_exercises.csv, and ONLY THEN
commit both transactions — same commit-after-successful-write discipline
as fetch_activities.py itself.

NOT part of the daily pipeline. NOT scheduled. Run once via
workflow_dispatch, then this script and its one-off workflow should be
deleted — this is cleanup scaffolding for a specific known situation, not
a permanent tool. Going forward, fetch_activities.py's own transaction
handling is the only thing that should ever touch this endpoint.

LESSON for next time: don't leave a real Polar transaction open across
multiple manual/diagnostic sessions once production code exists to
process it properly — commit or roll back promptly, since Polar allows
only one open transaction at a time and a forgotten one blocks the real
pipeline silently (a 204 with no error, easy to miss).
"""
import os
import csv
import json
import re
import urllib.request
import urllib.error

polar_token = os.environ["POLAR_ACCESS_TOKEN"]
polar_user_id = os.environ["POLAR_USER_ID"]

KNOWN_STUCK_TRANSACTION_IDS = ["334708890", "334709649"]


def polar_get(url):
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {polar_token}", "Accept": "application/json"},
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  GET {url} failed — HTTP {e.code} (likely already expired/cleared — skipping)")
        return None


def polar_commit(transaction_url, transaction_id):
    req = urllib.request.Request(
        transaction_url,
        headers={"Authorization": f"Bearer {polar_token}", "Accept": "application/json"},
        method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"  Transaction {transaction_id} committed (status {resp.status})")
            return True
    except urllib.error.HTTPError as e:
        print(f"  Transaction {transaction_id} commit FAILED — HTTP {e.code} — left uncommitted, will need another look")
        return False


def _iso_time_to_seconds_of_day(iso_str):
    try:
        time_part = iso_str.split("T")[1]
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
if os.path.exists("runs.csv"):
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
if os.path.exists("polar_exercises.csv"):
    with open("polar_exercises.csv", "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("polar_exercise_id"):
                existing_exercises[row["polar_exercise_id"]] = row

processed_transactions = []  # (transaction_id, resource_url) pairs ready to commit
total_matched = 0

for tx_id in KNOWN_STUCK_TRANSACTION_IDS:
    print(f"\n=== Transaction {tx_id} ===")
    resource_url = f"https://www.polaraccesslink.com/v3/users/{polar_user_id}/exercise-transactions/{tx_id}"
    list_resp = polar_get(resource_url)
    if not list_resp:
        print(f"  No data (already expired/cleared, or genuinely empty) — nothing to commit for this one")
        continue

    exercise_urls = list_resp.get("exercises", [])
    print(f"  {len(exercise_urls)} exercise(s) in this transaction")

    matched_this_tx = 0
    for ex_url in exercise_urls:
        ex = polar_get(ex_url)
        if not ex:
            continue
        ex_id = str(ex.get("id", ""))
        ex_start = ex.get("start-time", "")  # already local — confirmed live, no offset applied
        ex_duration_raw = ex.get("duration", "")
        if not ex_id or not ex_start:
            continue

        ex_date = ex_start[:10]
        ex_start_sec = _iso_time_to_seconds_of_day(ex_start)
        ex_duration_sec = _parse_iso8601_duration_to_seconds(ex_duration_raw)
        if ex_start_sec is None or ex_duration_sec is None:
            print(f"  Exercise {ex_id}: could not parse start-time/duration — skipping")
            continue
        ex_end_sec = ex_start_sec + ex_duration_sec

        hr_block = ex.get("heart-rate") or {}
        avg_hr = hr_block.get("average")
        max_hr = hr_block.get("maximum")
        sport = ex.get("sport", "")

        matched_run = None
        for r in runs_by_date.get(ex_date, []):
            run_start = r.get("start_time", "")
            if not run_start:
                continue
            run_start_sec = _iso_time_to_seconds_of_day(run_start)
            run_dur_sec = _duration_str_to_seconds(r.get("moving_time", ""))
            if run_start_sec is None or run_dur_sec is None:
                continue
            run_end_sec = run_start_sec + run_dur_sec
            if ex_start_sec <= run_end_sec and run_start_sec <= ex_end_sec:
                matched_run = r
                break

        if not matched_run:
            print(f"  Exercise {ex_id} ({ex_date}, {ex_duration_raw}): no matching Garmin run — not written")
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
        matched_this_tx += 1
        print(f"  Exercise {ex_id} ({ex_date}, {ex_duration_raw}) MATCHED Garmin activity {matched_run.get('activity_id','')}")

    total_matched += matched_this_tx
    print(f"  {matched_this_tx}/{len(exercise_urls)} matched in this transaction")
    processed_transactions.append((tx_id, resource_url))

# ── Write once, after processing BOTH transactions ────────────────────────────
ex_fieldnames = ["date", "garmin_activity_id", "polar_exercise_id", "start_time", "duration_min", "sport", "avg_hr", "max_hr"]
with open("polar_exercises.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=ex_fieldnames, extrasaction="ignore")
    writer.writeheader()
    for eid in sorted(existing_exercises.keys(), key=lambda k: existing_exercises[k].get("date", ""), reverse=True):
        writer.writerow(existing_exercises[eid])

print(f"\npolar_exercises.csv written: {total_matched} newly matched, {len(existing_exercises)} total rows")

# ── Commit ONLY now that the write above succeeded ────────────────────────────
print("\n=== Committing processed transactions ===")
for tx_id, resource_url in processed_transactions:
    polar_commit(resource_url, tx_id)

print("\nDone. If both committed successfully, the next regular Sync Training")
print("Data run should get a fresh 201 (or a genuine 204 if nothing new has")
print("happened since) instead of being blocked by these two.")
