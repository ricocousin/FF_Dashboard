# Fred's Feats

A personal training dashboard that pulls together running, strength, sleep, and steps data from multiple sources into a single daily view — plus an AI coach that reads the same data and writes a short, evidence-backed training briefing.

**Live dashboard:** https://ricocousin.github.io/FF_Dashboard/

---

## What it does

- Pulls runs and strength sessions from **Garmin Connect** (via a Polar H10 chest strap for accurate HR — optical HR is unreliable on tattooed skin)
- Pulls sleep and step data from a **Polar Loop** via the Polar AccessLink API
- Pulls iPhone step data via **Apple Health → Health Auto Export → Pipedream**
- Reconciles step counts from both wrist (Polar) and phone (iPhone) sources into one number — averages when both agree, falls back to the more complete reading when they disagree by more than 50%
- Calls **Claude (claude-sonnet-4-6)** once a day to generate a short, structured coaching briefing — headline, confidence score, evidence, things to watch, and a "what's changed" digest — built entirely from real computed data, not free-form generation
- Tracks strength, power, and durability test results (1RMs, dead hang time, jump distance, etc.) in a manually-maintained log, so progress in those domains — not just running — can show up as real evidence in the coach's briefing
- Renders everything as a single static dashboard page, hosted on GitHub Pages, rebuilt automatically every morning

## Why it's built this way

Garmin is the source of truth for run and strength *sessions* (accurate timing, GPS, pace). Polar is the source of truth for *sleep* (which Garmin doesn't provide at all) and supplements steps. The two are deliberately never merged for exercise data — the Polar Loop mis-tags H10-strap runs as "indoor" activities, so pulling Polar's version of a run would just create duplicate, less-accurate entries.

**Why two wearables at all:** heavily tattooed skin blocks Garmin's optical heart-rate sensor, so runs are recorded via Garmin watch + a **Polar H10 chest strap** for accurate HR. A **Polar Loop** is also worn continuously for sleep and step tracking, and is being explored as an HR source for strength sessions too — Garmin currently has no HR data for lifting, same tattoo issue.

## Repo naming note

If this repo is ever renamed, two things need updating:
1. The GitHub Pages URL changes — update any bookmarks and any links inside this README.
2. **Polar's developer portal** has the redirect URI registered as the old Pages URL. Only matters if the access token is ever revoked and needs re-authorizing.

## Architecture

```
Garmin Connect ─┐
Polar AccessLink ─┼─→ fetch_activities.py (GitHub Actions, daily) ─→ dashboard_metrics.json ─→ index.html
Apple Health ────┘         │                                        + coach_summary.json      (pure renderer)
                            └─→ Anthropic API (coaching summary)
```

**Single source of truth:** every derived number the dashboard shows (streaks, weekly deltas, chart data, personal bests, lactate threshold zones/trend, calendar activity dates, the "what's changed" digest) is computed once, in Python, and written to `dashboard_metrics.json`. `index.html` is a pure renderer — it formats and draws, it doesn't calculate. This exists specifically because the project hit real bugs where the same calculation lived separately in both JS and Python and quietly drifted apart. The one exception is the Activity Calendar's "today"/"future day" highlighting, which stays client-side since it's tied to the actual moment you're viewing the page, not to whenever the daily cron last ran.

Health Auto Export → Pipedream → `receive_steps.yml` runs independently, triggered by iPhone shortcut/automation rather than on the daily cron.

## Data files

| File | Source | Contents |
|---|---|---|
| `runs.csv` | Garmin | All logged runs, pace/HR/elevation/training load, deduplicated by Garmin's own activity ID |
| `strength.csv` | Garmin | Strength sessions, date and duration only (no load/reps — see below) |
| `strength_tests.csv` | **Manual** | Dated strength/power/durability test results — 1RMs, dead hang time, jump distance, etc. Edited directly on GitHub whenever you retest, roughly every 16-week block. |
| `steps.csv` | Apple Health | Daily step counts (phone) |
| `sleep.csv` | Polar Loop | Nightly sleep score/duration |
| `polar_steps.csv` | Polar Loop | Daily step counts (wrist) |
| `lactate.json` | Garmin | Lactate threshold history |
| `dashboard_metrics.json` | computed | Every derived number the dashboard displays — single source of truth |
| `coach_summary.json` | Claude | Daily coaching briefing |
| `api_usage.json` | computed | AI coach token/cost tracking |

`strength.csv` only captures *that* a session happened, not weights or reps — Garmin has no mechanism to record barbell loads. `strength_tests.csv` is the workaround: a lightweight, manually-updated log that lets real strength/power/durability progress show up as evidence in the coach's briefing, not just running metrics.

## AI coach design

- **Confidence score** (how complete/fresh the underlying data is) is computed entirely in Python from real signals — the model never generates this number, since an LLM has no calibrated access to "how confident am I."
- **Evidence** (the specific facts backing the day's headline) comes from a fixed catalog of pre-computed, verified deltas — LT vs 30 days ago, volume vs prior block, sleep trend, strength/power/durability test results vs the prior test, etc. The model only selects *which* items are relevant, by number from a numbered list — it never writes the evidence text itself.

## Scheduling

Runs daily at 6am Danish time year-round, via two crons gated by a runtime check of the actual local hour (GitHub Actions cron doesn't understand DST). Manual runs bypass the hour check.

A manual full-refresh option is available when triggering the workflow by hand — rebuilds `runs.csv`/`strength.csv` from Garmin's full history instead of just fetching what's new. Also runs automatically on the first Sunday of each month. Useful after any change to how activities are deduplicated or parsed.

## Required secrets

- `GARMIN_EMAIL`, `GARMIN_PASSWORD`
- `POLAR_ACCESS_TOKEN`
- `ANTHROPIC_API_KEY`

## Known quirks

- Garmin logins from GitHub Actions' shared IPs occasionally get a transient `429` on the first attempt — self-resolves via retry.
- GitHub Pages deploys occasionally fail with a generic "try again later" — unrelated to commit content, just re-run.
- Python 3.11 disallows backslashes inside an f-string's `{}` expression — compile-check locally before committing new f-string edits.
- Pasting workflow YAML on iOS has caused truncation issues — scroll to confirm the full file made it in before committing.
