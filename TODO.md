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
   - The multi-week periodised `fit train` feature is now DONE
     (import/show/sync/clear; engine in src/fit/training.py; all ten goal
     templates). Design reference: docs/training-plan-feature.md; behaviour:
     CLAUDE.md "Training plans". Still to do:
     (a) verify the newer workout payloads against a live push with
         scripts/diff_workout.py: the four steady types (fit's first
         single-step workouts) remain unverified. The bare baselines are
         done — strength/baseline and cycle/baseline round-tripped clean on
         2026-09-05. diff_workout.py now takes --session YYYY-MM-DD to diff a
         training-plan session directly, so once week 1 is synced the cycle
         long ride on 2026-09-20 is one command;
     (b) a real `fit train sync` + `fit train clear` round-trip against Garmin
         (sync now confirms before pushing; --dry-run previews it).
         THIS IS THE ONE TO DO NEXT — `fit train` is now stateless and the
         round-trip is the only part that cannot be checked offline. Steps:
           1. `fit train sync --days 7` on a real plan, confirm at the prompt.
           2. Check train/plan.json: every "pushed" row must have a non-null
              `schedule_id`. **This is the real risk.** cli.train_sync reads it
              from `placed.get("workoutScheduleId")`, a key name inherited from
              the pre-stateless code and never verified against a live
              response. If Garmin names it something else the row stores None,
              and `train clear` will then skip the unschedule call, drop the
              row anyway, and leave an orphaned calendar entry with nothing
              tracking it. If it is null, fix the key before clearing.
           3. `fit train show` — those sessions should read "on watch".
           4. Re-run `fit train sync`: it must find fewer (idempotency).
           5. Import an activity that beats a current target, then
              `fit train show`: the pushed sessions must keep their old pace
              and show "on watch*", while unpushed ones move.
           6. `fit train clear`, then check the Garmin calendar is empty and
              the "pushed" list is back to [].
   - Note: `fit train retarget` no longer exists. The plan re-derives from
     history on every command, so doing the test and running `fit garmin-sync`
     is all that is needed — see CLAUDE.md "The plan is derived, not stored".
     Still open: swim stays the least-measured discipline (Strava-CSV swims
     carry no splits, and importers.import_tcx still files swims as runs).
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
     renders. Trash the stale pbs.json leftovers in /my-files/.fit afterwards,
     and the gpx/ directory too — fit no longer keeps original files, so
     nothing writes or reads it (the ~9MB already in ~/.fit/gpx on the Mac is
     likewise now inert; delete it whenever you like).
