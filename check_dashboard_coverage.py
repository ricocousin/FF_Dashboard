"""
Fred's Feats — dashboard coverage checker.

Guards against exactly the failure mode that caused real data loss (July
2026): build_dashboard.py computes a field into dashboard_metrics.json or
coach_summary.json, but a later index.html rewrite silently drops the code
that reads it. Nothing crashes — the feature just goes dark. Six real
features (freshness indicator, VO2max card, calendar commentary, coach card
collapse, banner monogram, and a regression back to the pre-fix duplicate-
message overview pattern) went missing this way before being caught by
manual inspection. This script exists so that never has to happen again by
luck.

Two layers:
  LAYER 1 (auto-derived, zero maintenance): parses every top-level key out
    of build_dashboard.py's dashboard_metrics and coach_summary dict
    literals, then checks index.html actually references each one. New
    fields are covered automatically — nothing to remember to add here.
  LAYER 2 (hand-maintained): a short list of "must contain" markers for
    features that aren't a single JSON key (a toggle button, an SVG, a
    specific function existing) — Layer 1 structurally cannot catch these.
    Add one line here whenever a genuinely new such feature ships (see
    CODE DELIVERY FORMAT in PROJECT CONTEXT — this list is the enforcement
    mechanism for "don't let a future rewrite drop this").

Exit code 0 = all checks passed. Exit code 1 = at least one failure —
CI fails the run; a human (or Claude, before ever handing over a file)
must resolve it before index.html is considered safe to ship.

Run standalone: python3 check_dashboard_coverage.py
(expects build_dashboard.py and index.html in the current directory)
"""
import re
import sys
import os

FAILURES = []
WARNINGS = []


def fail(msg):
    FAILURES.append(msg)
    print(f"FAIL: {msg}")


def warn(msg):
    WARNINGS.append(msg)
    print(f"WARN: {msg}")


def ok(msg):
    print(f"OK:   {msg}")


def load(path):
    if not os.path.exists(path):
        fail(f"{path} not found in current directory — run this from the repo root")
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_dict_keys(source, dict_var_name):
    """Extract top-level keys from a `<dict_var_name> = { ... }` literal.
    Only matches keys at 4-space indent (i.e. genuinely top-level, not keys
    belonging to a nested dict one level deeper) — mirrors how both
    dashboard_metrics and coach_summary are actually formatted in
    build_dashboard.py. Key names may contain digits (e.g. chart_16wk)."""
    m = re.search(rf'{dict_var_name} = \{{(.*?)\n\}}\n', source, re.S)
    if not m:
        return None
    block = m.group(1)
    return re.findall(r'^    "([a-zA-Z0-9_]+)":', block, re.M)


def check_layer1_metrics_coverage(build_src, html_src):
    print("\n--- LAYER 1: dashboard_metrics.json key coverage ---")
    keys = extract_dict_keys(build_src, "dashboard_metrics")
    if keys is None:
        fail("Could not find 'dashboard_metrics = {...}' in build_dashboard.py — check the variable name/format hasn't changed")
        return
    # Deliberately backend-only fields — documented in PROJECT CONTEXT's
    # CONVENTIONS section as Python-side parameters, never meant to render
    # directly in the UI. Keep this list short and only add to it with an
    # explicit reason (same pattern as coach_summary's KNOWN_UNUSED below);
    # a growing list here is a signal something should probably just be
    # rendered instead of quietly excused.
    KNOWN_UNUSED = {
        "last_complete_date",  # backend cutoff param — historical stats already reflect it via their computed values, no separate UI display needed
        "run_hr_context",      # staging key — written for a not-yet-shipped UI/coach step; not consumed by index.html yet (see build_dashboard.py note)
        "strength_hr_context", # staging key — same as run_hr_context; per-session HR context awaiting its UI/coach consumer
        "hr_dropout_fallback", # consumed internally by build_dashboard.py for zone-time attribution; standalone UI card was intentionally removed
    }
    for k in keys:
        if k in KNOWN_UNUSED:
            ok(f"dashboard_metrics.{k} is deliberately backend-only (documented) — skipping")
            continue
        # Matches m.key, m['key'], m["key"] — covers direct access and any
        # bracket-notation fallback for keys that aren't valid JS identifiers.
        pattern = rf'm\.{re.escape(k)}\b|m\[[\'"]{ re.escape(k) }[\'"]\]'
        if re.search(pattern, html_src):
            ok(f"dashboard_metrics.{k} is referenced in index.html")
        else:
            fail(f"dashboard_metrics.{k} is computed by build_dashboard.py but NEVER referenced in index.html — this field is being silently dropped")


def check_layer1_coach_coverage(build_src, html_src):
    print("\n--- LAYER 1: coach_summary.json key coverage ---")
    keys = extract_dict_keys(build_src, "coach_summary")
    if keys is None:
        fail("Could not find 'coach_summary = {...}' in build_dashboard.py — check the variable name/format hasn't changed")
        return
    # Deliberately-vestigial fields documented as legacy/unused — not a bug
    # if these are never referenced. Keep this list short and only add to
    # it with an explicit reason, mirroring the DELIBERATELY EXCLUDED
    # pattern used elsewhere in this project for intentional omissions.
    KNOWN_UNUSED = {"watch_items"}  # backend continuity memory only — persisted to coach_context.json and fed into the next day's prompt, never rendered in the UI
    for k in keys:
        if k in KNOWN_UNUSED:
            ok(f"coach_summary.{k} is deliberately unused (documented) — skipping")
            continue
        pattern = rf'coachData\.{re.escape(k)}\b|coachData\[[\'"]{ re.escape(k) }[\'"]\]'
        if re.search(pattern, html_src):
            ok(f"coach_summary.{k} is referenced in index.html")
        else:
            fail(f"coach_summary.{k} is computed by build_dashboard.py but NEVER referenced in index.html — this field is being silently dropped")


def check_layer2_feature_markers(html_src):
    """Hand-maintained list — add a line here whenever a genuinely new
    non-JSON-key feature ships (a button, a toggle, an SVG, a named
    function). Each entry: (marker regex, human description)."""
    print("\n--- LAYER 2: named feature markers (hand-maintained) ---")
    MARKERS = [
        (r'freshBar|data_freshness', "Freshness indicator dot (overview panel)"),
        (r'coachDetailsToggle', "Coach card 'Show details' collapse toggle"),
        (r'ltVo2Chart', "Merged LT + VO2max dual-axis trend chart"),
        (r'tileFreshDot', "Per-tile freshness dots (overview panel)"),
        (r'commentary', "Calendar streak commentary line"),
        (r'ztCard|zt-toggle', "Time in zone card + rolling/last-completed-week toggle"),
        (r'strengthHrChart', "Strength session HR overlay chart"),
        (r'hrCompareChart', "Garmin vs Polar run HR comparison chart"),
        (r'polar_exercise_avg_hr', "Three-way HR comparison: Polar exercise-entry line"),
        # H10 dropout fallback card removed July 2026 — dropout HR is now folded
        # into zone-time attribution (build_dashboard.py) rather than shown as a
        # standalone card, so there is no longer a marker to guard here.
        # (r'hr_dropout_fallback|dropoutFallback', "H10 dropout fallback card"),
        (r'<svg[^>]*>.*?</svg>', "Banner monogram SVG in the header"),
        (r'changeLine', "Single-sentence overview change function (changeLine)"),
        (r'optionAChart', "16-week distance/strength chart"),
        (r'monthChart', "Annual distance/strength chart"),
    ]
    for pattern, description in MARKERS:
        if re.search(pattern, html_src, re.S):
            ok(f"{description} — marker found")
        else:
            fail(f"{description} — marker '{pattern}' NOT FOUND in index.html")


def check_known_regressions(html_src):
    """Named checks for specific bugs that have already happened once and
    must never silently reappear. Unlike Layer 2 (does a marker exist),
    these check for a BAD pattern's absence, or a bad pattern's co-occurrence
    with something that makes it wrong. Add a new named check here any time
    a real regression is found and fixed — this is the permanent fix for
    'this exact bug happened before.'"""
    print("\n--- REGRESSION GUARDS (named, one per historical incident) ---")

    # July 2026: overview tiles regressed to a deltaBadge()+qualLine() pair
    # that both rendered for the same comparison — the same message twice
    # ("▲ 2.3 km vs last wk" immediately followed by "Increased vs last
    # week"). Fixed once via a single merged changeLine(). If both
    # deltaBadge and qualLine ever exist again as function definitions,
    # that's this regression coming back, even if changeLine also exists.
    has_delta_badge_fn = re.search(r'const\s+deltaBadge\s*=', html_src)
    has_qual_line_fn = re.search(r'const\s+qualLine\s*=', html_src)
    if has_delta_badge_fn and has_qual_line_fn:
        fail("REGRESSION: both deltaBadge() and qualLine() are defined as separate functions — this is the pre-fix duplicate-message pattern (each overview comparison rendering the same info twice). Must be merged into a single changeLine().")
    else:
        ok("deltaBadge()+qualLine() duplicate-message pattern not present")

    # July 2026: index.html was rewritten from a copy that had already lost
    # the zone-time and Garmin-vs-Polar comparison cards, and the loss
    # wasn't caught until manual inspection. Guard both cards' core render
    # calls explicitly (not just Layer 2 markers) since this was the
    # single most consequential miss.
    if not re.search(r'renderZoneTimeCard', html_src):
        fail("REGRESSION: renderZoneTimeCard() function is missing — the entire Time in Zone card has been dropped again")
    else:
        ok("renderZoneTimeCard() present")

    if not re.search(r'run_hr_source_comparison', html_src) or not re.search(r'strength_hr_overlay', html_src):
        fail("REGRESSION: strength HR overlay / Garmin-vs-Polar comparison card data is not referenced — this card has been dropped again")
    else:
        ok("Strength HR overlay / Garmin-vs-Polar comparison card data referenced")


def main():
    build_src = load("build_dashboard.py")
    html_src = load("index.html")

    if build_src is None or html_src is None:
        print(f"\n{len(FAILURES)} failure(s) — could not run full check.")
        sys.exit(1)

    check_layer1_metrics_coverage(build_src, html_src)
    check_layer1_coach_coverage(build_src, html_src)
    check_layer2_feature_markers(html_src)
    check_known_regressions(html_src)

    print(f"\n{'='*60}")
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S), {len(WARNINGS)} warning(s)")
        for f in FAILURES:
            print(f"  - {f}")
        print("\nindex.html is NOT safe to ship as-is — a documented feature has gone silent.")
        sys.exit(1)
    else:
        print(f"RESULT: All checks passed ({len(WARNINGS)} warning(s))")
        sys.exit(0)


if __name__ == "__main__":
    main()
