# TODO

Ordered by priority (importance x ease). Work top to bottom.

1. `garmin.py` has no delete-workout call, so `unschedule_workout` clears the
   calendar but leaves the workout in the Garmin library. Templates accumulate
   over a plan's life — four are sitting there from 2026-09-11 alone.

   The abandoned Linux machine's old plan may also have left calendar entries
   that nothing tracks. Clear any that turn up by hand in Garmin Connect.
