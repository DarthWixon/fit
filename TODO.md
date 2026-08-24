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
   - Single-workout scheduling is now DONE: `fit plan --schedule DATE` +
     garmin.schedule_workout, verified live 2026-08-24.
   - The multi-week periodised `fit train` feature (build on that seam) has a
     full approved design + implementation reference in
     docs/training-plan-feature.md — start there. Not yet implemented.
3. Set up fit-sync on the Linux machine (Mac side done 2026-07-04; script,
   data, and usage guide all live in /my-files/.fit on Proton Drive):
   - Download the official proton-drive CLI (linux-x64 build, v0.4.6+) from
     proton.me/download/drive/cli to ~/bin, chmod +x, `proton-drive auth
     login` (needs a running Secret Service — gnome-keyring/KWallet — for the
     session token).
   - Pull the script + guide: `proton-drive filesystem download
     /my-files/.fit/fit-sync /my-files/.fit/fit-sync-usage.md ~/bin`,
     chmod +x, then edit ROLE to "primary" (the Mac is secondary).
   - Before the first run: check the Linux box's ~/.fit config and
     fitness.json are the ones that should win — the primary's copies
     overwrite the remote (and then the Mac) from the first sync onward.
   - Run ~/bin/fit-sync, then `fit dashboard` to confirm the merged history
     renders. Optionally trash the stale gpx/ and pbs.json leftovers in
     /my-files/.fit afterwards.
