# TODO

Ordered by priority (importance x ease). Work top to bottom.

1. The Linux machine may still hold a pre-stateless `plan.json` — no `pushed`
   key, push state stamped onto each entry of a top-level `sessions` list.

   This is no longer dangerous: `fit train import` checks the plan's shape
   before trusting its ledger, refuses a file it cannot read, and prints the
   `scheduled_workout_id` of everything that file says is on the calendar.
   Before the check it read the missing key as an empty ledger and replaced
   the plan anyway, stranding three real entries on 2026-09-11.

   It still needs doing once, whenever that machine is next used or its copy
   reaches the Mac through fit-sync: run the import, unschedule whatever it
   names in Garmin Connect, delete `~/.fit/train/plan.json`, import again.

   While in there: `garmin.py` has no delete-workout call, so
   `unschedule_workout` clears the calendar but leaves the workout in the
   Garmin library. Templates accumulate over a plan's life — four are sitting
   there from 2026-09-11 alone.
