#!/usr/bin/env python3
"""Verify planner.py's Garmin workout-service payload schema against a live
round-trip — the check planner.py's module docstring flags as "not yet
verified against a live upload".

The payload fit *sends* is built offline by planner.build_plan and saved in
each plan file's "payload" field. This script fetches what Garmin *stored*
for that workout and reports every field fit sent whose name or value did not
survive the round-trip — exactly the target-value field names
(targetValueOne / targetValueTwo) and step numbering the docstring is unsure
about. A clean run (no differences) is the evidence needed to stamp the
docstring with a verified date.

The diff is deliberately one-directional: it walks the *payload* (what we
meant) and looks each leaf up in the fetched workout, ignoring the many extra
fields Garmin adds on its own (ids, timestamps, author, defaults). A field we
sent that comes back missing means Garmin renamed or dropped it; a field that
comes back with a different value means Garmin reinterpreted it. Both are
schema drift to fix in planner.py.

Usage:
    # Live: log in, fetch the workout the plan was pushed as, diff it.
    scripts/diff_workout.py ~/.fit/plans/2026-07-03T09:15:02.json

    # Offline: diff against a previously-saved get_workout() dump instead of
    # logging in (e.g. JSON you already pulled from the Garmin web app).
    scripts/diff_workout.py <plan.json> --fetched fetched-workout.json

Lists (e.g. workoutSteps) are aligned by index — Garmin may renumber
stepOrder or nest differently, so treat list-length or ordering differences
as a prompt to eyeball the raw dicts, not a hard failure.
"""

import argparse
import json
import sys
from pathlib import Path


def _diff(sent, stored, path=""):
    """Yield (path, sent_value, stored_value_or_sentinel) for every leaf in
    `sent` that is missing from or unequal in `stored`. One-directional:
    extra keys present only in `stored` are ignored."""
    if isinstance(sent, dict):
        if not isinstance(stored, dict):
            yield (path or "<root>", sent, stored)
            return
        for key, sent_value in sent.items():
            here = f"{path}.{key}" if path else key
            if key not in stored:
                yield (here, sent_value, _MISSING)
            else:
                yield from _diff(sent_value, stored[key], here)
    elif isinstance(sent, list):
        if not isinstance(stored, list):
            yield (path, sent, stored)
            return
        if len(sent) != len(stored):
            yield (f"{path}[len]", len(sent), len(stored))
        for i, sent_item in enumerate(sent):
            here = f"{path}[{i}]"
            if i < len(stored):
                yield from _diff(sent_item, stored[i], here)
            else:
                yield (here, sent_item, _MISSING)
    else:
        if not _values_match(sent, stored):
            yield (path, sent, stored)


class _Missing:
    def __repr__(self):
        return "<missing>"


_MISSING = _Missing()


def _values_match(sent, stored) -> bool:
    """Equality that tolerates Garmin echoing our numbers back as int<->float
    or as a stringified number, which is not real schema drift."""
    if sent == stored:
        return True
    if isinstance(sent, (int, float)) and isinstance(stored, (int, float)):
        return abs(sent - stored) < 1e-6
    if isinstance(sent, (int, float)):
        try:
            return abs(float(stored) - sent) < 1e-6
        except (TypeError, ValueError):
            return False
    return False


def _load_plan(plan_path: Path) -> dict:
    plan = json.loads(plan_path.read_text())
    if "payload" not in plan:
        sys.exit(f"error: {plan_path} has no 'payload' field — not a saved plan?")
    return plan


def _fetch_stored(plan: dict, fetched_path: Path | None) -> dict:
    if fetched_path is not None:
        return json.loads(fetched_path.read_text())
    workout_id = plan.get("garmin_workout_id")
    if workout_id is None:
        sys.exit(
            "error: plan has no 'garmin_workout_id' — it was never pushed, so "
            "there is nothing to fetch. Push it first (fit plan) or pass "
            "--fetched with a saved workout dump."
        )
    # Imported lazily so the offline --fetched path needs no garminconnect.
    from fit import garmin

    client = garmin.login()
    return garmin.get_workout(client, workout_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="path to a ~/.fit/plans/<id>.json file")
    parser.add_argument(
        "--fetched",
        type=Path,
        default=None,
        help="diff against this saved get_workout() JSON instead of logging in",
    )
    parser.add_argument(
        "--dump",
        type=Path,
        default=None,
        help="also write the fetched workout dict here (for later offline re-diffs)",
    )
    args = parser.parse_args()

    plan = _load_plan(args.plan)
    stored = _fetch_stored(plan, args.fetched)

    if args.dump is not None:
        args.dump.write_text(json.dumps(stored, indent=2))
        print(f"wrote fetched workout to {args.dump}")

    differences = list(_diff(plan["payload"], stored))
    print(f"\nplan:    {args.plan}")
    print(f"workout: {plan.get('workout_name', '(unnamed)')}\n")

    if not differences:
        print("✓ every field fit sent survived the round-trip unchanged.")
        print("  Safe to stamp planner.py's docstring with today's date.")
        return 0

    print(f"✗ {len(differences)} field(s) fit sent did not round-trip cleanly:\n")
    for field_path, sent_value, stored_value in differences:
        if stored_value is _MISSING:
            print(f"  {field_path}: sent {sent_value!r}, MISSING from stored workout")
        else:
            print(f"  {field_path}: sent {sent_value!r}, stored {stored_value!r}")
    print(
        "\nFields missing from the stored workout are likely renamed by Garmin "
        "(e.g. targetValueOne/Two) — reconcile against get_workout output and "
        "fix planner.py, then re-run."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
