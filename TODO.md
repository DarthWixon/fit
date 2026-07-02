# TODO

No priority at the moment, look into what needs doing

1. Strava import silently drops activity types not in STRAVA_TYPE_MAP.
   A real export (~/fit-dev-data/strava, 727 rows) contained Workout (99) and
   Surfing (1) — 100 activities dropped with no warning by _parse_strava_row
   returning None (importers.py). Decide per type: map to an existing fit type,
   add a new type, or explicitly skip — and at minimum surface a skipped count
   so dropped activities aren't invisible.

2. ~~Installation and initialisation instructions.~~ Done — see README.md
   (setup with uv, per-machine venv recreation, FIT_DATA_DIR usage).

3. High-effort code review, and choose a style guide / code formatting / linting
   setup (formatter, linter, config) for the project.

4. Shorten the default timerange of the dashboard's weekly volume graph — it's
   far too long right now (spans full history, wrapping over multiple lines).
   Cap it to a sensible recent window (e.g. last N weeks) rather than every week
   on record.

5. ~~Make a clear distinction between code and data throughout the project, so we
    can upload to github safely. Also look into creating some fake example data~~
    Done — real data moved to ~/fit-dev-data/ (outside the repo); committed
    fixtures/examples are all synthetic (examples/, tests/data/).

6. What do we want to do with .fit/.tcx files once they've been imported?
