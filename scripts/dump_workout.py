#!/usr/bin/env python3
"""Dump a Garmin Connect workout as fit's `garmin.get_workout` returns it.

For reading Garmin's workout schema off the account when planner.py needs a
shape it does not build yet. garminconnect's workout.py models are no guide
here: they model no exercise field and no weight target, yet ExecutableStep is
`extra="allow"`, so the API passes through whatever Garmin sends. Strength's
whole payload shape was learned this way.

  1. In Connect (web), build a small workout by hand with the shape you need —
     for strength, two different exercises with a target weight on one, since
     that is the only way to see how exercises compose in one step list.
  2. Find its id. `--list` prints your recent workouts with ids, or take it
     from the URL: connect.garmin.com/modern/workout/<id>.
  3. scripts/dump_workout.py <id> --out workout.json

Record what it shows in CLAUDE.md's "Payload schema" section and planner.py's
docstring, then confirm what planner.py builds with scripts/diff_workout.py.

This only ever reads. It does not create, schedule or modify anything on the
account. Its sibling scripts/diff_workout.py is the other direction: it
compares a payload fit built against what Garmin stored.

Usage:
    scripts/dump_workout.py --list
    scripts/dump_workout.py 123456789
    scripts/dump_workout.py 123456789 --out workout.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _login():
    from fit import garmin

    return garmin, garmin.login()


def _list_workouts(garmin, client, limit: int) -> int:
    """Recent workouts, newest first, as (id, sport, name) lines.

    garmin.py has no list-workouts wrapper — nothing in fit needs one, and
    adding a function to the API boundary for a one-off discovery script
    would be the wrong place for it. The underlying client call is used
    directly here instead, and stays here.
    """
    workouts = client.get_workouts(0, limit)
    if not workouts:
        print("no workouts found on this account")
        return 1
    for workout in workouts:
        sport = (workout.get("sportType") or {}).get("sportTypeKey", "?")
        print(f"{workout.get('workoutId')}\t{sport}\t{workout.get('workoutName')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "workout_id", nargs="?", type=int, help="the workout id to dump"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list recent workouts with their ids instead of dumping one",
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="how many to list (default 20)"
    )
    parser.add_argument(
        "--out", type=Path, help="write the dump here as well as printing it"
    )
    args = parser.parse_args()

    if not args.list and args.workout_id is None:
        parser.error("give a workout id, or --list to find one")

    try:
        garmin, client = _login()
    except Exception as exc:  # garmin.GarminAuthError, or anything the lib raises
        print(f"login failed: {exc}", file=sys.stderr)
        return 1

    if args.list:
        return _list_workouts(garmin, client, args.limit)

    workout = garmin.get_workout(client, args.workout_id)
    text = json.dumps(workout, indent=2, sort_keys=True)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
