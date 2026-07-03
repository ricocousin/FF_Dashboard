# FF_Dashboard

A personal training dashboard that pulls together running, strength, sleep, and steps data from multiple sources into a single daily view — plus an AI coach that reads the same data and writes a short, evidence-backed training briefing.

**Live dashboard:** https://ricocousin.github.io/Garmin-Dashboard/

---

## What it does

- Pulls runs and strength sessions from **Garmin Connect** (via a Polar H10 chest strap for accurate HR — optical HR is unreliable on tattooed skin)
- Pulls sleep and step data from a **Polar Loop** via the Polar AccessLink API
- Pulls iPhone step data via **Apple Health → Health Auto Export → Pipedream**
- Reconciles step counts from both wrist (Polar) and phone (iPhone) sources into one number, with a fallback if either device is left behind on a given day
- Calls **Claude (claude-sonnet-4-6)** once a day to generate a short, structured coaching briefing — headline, confidence score, evidence, things to watch, and a "what's changed" digest — built entirely from real computed data, not free-form generation
- Renders everything as a single static dashboard page, hosted on GitHub Pages, rebuilt automatically every morning

## Why it's built this way

Garmin is the source of truth for run and strength *sessions* (accurate timing, GPS, pace). Polar is the source of truth for *sleep* (which Garmin doesn't provide at all) and supplements steps. The two are deliberately never merged for exercise data — the Polar Loop mis-tags H10-strap runs as "indoor" activities, so pulling Polar's version of a run would just create duplicate, less-accurate entries.

**Why two wearables at all:** heavily tattooed skin blocks Garmin's optical heart-rate sensor, so runs are recorded via Garmin watch + a **Polar H10 chest strap** (paired to the watch) for accurate HR. A **Polar Loop** is also worn continuously for sleep and step tracking, and is being explored as an HR source for strength sessions too — Garmin currently has no HR data for lifting, same tattoo issue.

## Repo naming note

If you ever rename this repo, two things need updating to match:
1. The GitHub Pages URL changes (`ricocousin.github.io/<old-name>/` → `.../<new-name>/`) — update any bookmarks/shortcuts.
2. **Polar's developer portal** has the redirect URI registered as the exact old Pages URL. This only matters if the Polar access token is ever revoked and needs re-authorizing — the already-issued token keeps working regardless of the repo name, since the redirect URI is only used during the original OAuth handshake.

## AI coach design

The daily coaching summary is built to avoid two common LLM pitfalls:

- **Confidence score** — how complete/fresh the underlying data is — is computed entirely in Python from real signals (LT freshness, HR data validity, sleep/steps completeness, pipeline health). The model never generates this number itself, since an LLM has no real calibrated access to "how confident am I."
- **Evidence** — the specific facts backing up the day's headline — comes from a fixed catalog of pre-computed, verified deltas (LT vs 30 days ago, volume vs prior block, sleep trend, etc.). The model only selects *which* items are relevant by number; it never writes the evidence text itself, so nothing it says can be a fabricated statistic.

## Architecture

```
Garmin Connect ─┐
Polar AccessLink ─┼─→ fetch_activities.py (GitHub Actions, daily) ─→ CSV/JSON ─→ index.html (GitHub Pages)
Apple Health ────┘         │
                            └─→ Anthropic API (coaching summary)
```

Health Auto Export → Pipedream → `receive_steps.yml` runs independently, triggered by iPhone shortcut/automation rather than on the daily cron.

## Data files

| File | Source | Contents |
|---|---|---|
| `runs.csv` | Garmin | All logged runs, pace/HR/elevation/training load |
| `strength.csv` | Garmin | Strength sessions, duration |
| `steps.csv` | Apple Health | Daily step counts (phone) |
| `sleep.csv` | Polar Loop | Nightly sleep score/duration |
| `polar_steps.csv` | Polar Loop | Daily step counts (wrist) |
| `lactate.json` | Garmin | Lactate threshold history |
| `summary.json` | computed | Aggregate stats (streaks, totals) |
| `coach_summary.json` | Claude | Daily coaching briefing |
| `api_usage.json` | computed | AI coach token/cost tracking |

## Scheduling

Runs daily at 6am Danish time, year-round, via two crons (`0 4 * * *` and `0 5 * * *` UTC) gated by a runtime check of the actual local hour — GitHub Actions cron doesn't understand DST, so this covers both summer and winter offsets without maintaining two separate schedules by hand. Manual runs (`workflow_dispatch`) always execute immediately, bypassing the hour check, for testing.

## Required secrets

Set these under repo Settings → Secrets and variables → Actions:

- `GARMIN_EMAIL`, `GARMIN_PASSWORD`
- `POLAR_ACCESS_TOKEN` (long-lived, from a one-time OAuth authorization)
- `ANTHROPIC_API_KEY`

## Known quirks worth knowing before touching the code

- Garmin logins from GitHub Actions' shared IPs occasionally get a transient `429` on the first attempt — this self-resolves via retry within the same run.
- GitHub Pages deploys occasionally fail with a generic "try again later" — unrelated to commit content, just re-run.
- Python 3.11 (what this repo runs on in Actions) disallows backslashes inside an f-string's `{}` expression — worth a local compile check before committing new f-string edits.
