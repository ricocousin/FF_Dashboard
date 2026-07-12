"""
Fred's Feats — Polar exercise-transaction diagnostic (ONE-OFF, NOT part of
the daily pipeline).

Purpose: decide between two real options for the H10-dropout fallback
project — (1) build the real Polar exercise transaction flow properly
(create -> list -> get each summary -> commit), or (2) abandon
polar_exercises.csv entirely and rely on continuous HR + a calibrated
offset as the only fallback source — WITHOUT committing to the heavier
option 1 build until we actually know whether the transaction model
surfaces recent data at all.

Deliberately DOES NOT COMMIT the transaction it opens. Per Polar's own
AccessLink docs: "if you commit without successfully processing and
storing the data, you will not be able to retrieve it again through the
standard transaction flow." This run is read-only inspection — whatever
we see gets printed, nothing gets written to disk, nothing gets
consumed/discarded. Safe to run as many times as needed while deciding.

NOT added to fetch_activities.yml's git-add list (writes no files).
NOT scheduled — run manually via workflow_dispatch, once, to look at the
printed output and decide next steps.

Uses the existing POLAR_ACCESS_TOKEN and POLAR_USER_ID secrets already
present in the repo (POLAR_USER_ID was previously stored but unused —
this is the first real use of it, since the transaction endpoints are
scoped per user_id unlike the simple GETs used elsewhere).
"""
import os
import json
import urllib.request
import urllib.error

polar_token = os.environ["POLAR_ACCESS_TOKEN"]
polar_user_id = os.environ["POLAR_USER_ID"]


def polar_request(url, method="GET"):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {polar_token}",
            "Accept": "application/json"
        },
        method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
            body = json.loads(raw) if raw else None
            return status, body
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            body = json.loads(raw) if raw else raw
        except Exception:
            body = raw
        return e.code, body
    except Exception as e:
        return None, f"Request failed: {e}"


print("=" * 70)
print("POLAR EXERCISE TRANSACTION DIAGNOSTIC — read-only, nothing committed")
print("=" * 70)

# KNOWN_OPEN_TRANSACTION: the first diagnostic run (July 12) opened
# transaction 334708890 and deliberately never committed it. Per Polar's
# docs, an open transaction blocks new data from surfacing via a fresh
# POST until it's committed or rolled back — so a second POST correctly
# returns 204 (confirmed: that's exactly what happened on the second run)
# rather than either re-returning the same transaction or surfacing
# anything new. The 12 exercises are still sitting there, reachable
# directly via this known resource-uri, without needing a new POST.
KNOWN_OPEN_TRANSACTION_ID = "334708890"

print(f"\nStep 1: POST create-transaction for user {polar_user_id}")
create_url = f"https://www.polaraccesslink.com/v3/users/{polar_user_id}/exercise-transactions"
status, body = polar_request(create_url, method="POST")
print(f"  Status: {status}")
print(f"  Body: {body}")

resource_url = None
transaction_id = None

if status == 204:
    print("\n  204 No Content — expected, since transaction "
          f"{KNOWN_OPEN_TRANSACTION_ID} from the first diagnostic run was")
    print("  deliberately never committed, and Polar blocks new data from")
    print("  surfacing until an open transaction is committed or rolled back.")
    print(f"\n  Falling back to directly GETting the known still-open")
    print(f"  transaction {KNOWN_OPEN_TRANSACTION_ID} instead of creating a new one —")
    print("  same 12 exercises, still not committing anything.")
    fallback_url = f"https://www.polaraccesslink.com/v3/users/{polar_user_id}/exercise-transactions/{KNOWN_OPEN_TRANSACTION_ID}"
    status_fb, body_fb = polar_request(fallback_url, method="GET")
    print(f"\n  Fallback GET status: {status_fb}")
    if status_fb == 200 and isinstance(body_fb, dict):
        transaction_id = KNOWN_OPEN_TRANSACTION_ID
        resource_url = fallback_url
    else:
        print(f"  Fallback body: {body_fb}")
        print("\n  Fallback also failed — the known transaction may have expired")
        print("  or been cleared server-side. See status/body above.")

elif status in (200, 201):
    transaction_id = body.get("transaction-id") if isinstance(body, dict) else None
    resource_url = body.get("resource-uri") if isinstance(body, dict) else None
    print(f"  transaction-id: {transaction_id}")
    print(f"  resource-uri: {resource_url}")

if resource_url:
    print(f"\nStep 2: GET exercise list within this transaction")
    status2, body2 = polar_request(resource_url, method="GET")
    print(f"  Status: {status2}")
    print(f"  Body: {body2}")

    exercise_urls = []
    if isinstance(body2, dict):
        exercise_urls = body2.get("exercises", [])
    print(f"\n  {len(exercise_urls)} exercise(s) listed in this transaction")

    for i, ex_url in enumerate(exercise_urls[:25]):
        print(f"\nStep 3.{i+1}: GET {ex_url}")
        status3, body3 = polar_request(ex_url, method="GET")
        # Printing the FULL raw body this time, not cherry-picked fields —
        # the first pass guessed underscored key names (start_time) based
        # on the OLD bare-GET endpoint's shape, but the transaction-id/
        # resource-uri fields seen earlier are hyphenated, suggesting the
        # real per-exercise summary likely uses different key names
        # entirely (e.g. start-time, not start_time). Don't guess twice —
        # just look at everything that's actually there.
        print(f"  Status: {status3}")
        print(f"  Full body: {body3}")

    if len(exercise_urls) > 25:
        print(f"\n  ({len(exercise_urls) - 25} more exercise(s) not printed — first 25 shown)")

    print("\n" + "=" * 70)
    print(f"*** TRANSACTION {transaction_id} DELIBERATELY NOT COMMITTED ***")
    print("This was read-only inspection only. Nothing was consumed or lost.")
    print("Decide next steps (build the full flow vs abandon this source)")
    print("BEFORE ever calling commit on a real transaction going forward.")
    print("=" * 70)

elif status not in (200, 201, 204):
    print(f"\n  Unexpected status {status} — see body above for details.")
