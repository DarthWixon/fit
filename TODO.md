# TODO

Ordered by priority (importance x ease). Work top to bottom.

1. How do we name garmin workout plans so that they're readable on the watch?
   Live push verified 2026-07-03 — check how the current names render on the
   watch and shorten the format in planner.build_plan if they truncate.
2. Add scheduling support for garmin workouts, ability to make multiple
   workouts at once. Unblocked by the verified live push;
   client.schedule_workout exists in garminconnect. While in there: diff
   client.get_workout_by_id() against a generated payload and update the
   "not yet verified" note in planner.py's docstring.
3. How can we set up the database to work with a cloud service like
   protondrive? Likely a docs/setup task (one-file-per-activity was chosen for
   rsync-style sync), but Proton Drive has no official Linux client — research
   spike first.
