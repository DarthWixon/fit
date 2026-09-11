# TODO

Ordered by priority (importance x ease). Work top to bottom.

1. How do we name garmin workout plans so that they're readable on the watch?
   MEASURED 2026-09-11, then DEPRIORITISED (not worth doing yet, my call).
   They truncate at ~23 characters: "Strength baseline bench press 3-rep test
   (warm up first)" renders as "Strength baseline bench". Length is not really
   the problem — ordering is. "Strength baseline " is 18 characters of
   boilerplate spent before the only distinguishing word (the lift) begins, and
   the two-exercise names never show the second lift at all, so two different
   sessions both read "Strength squat 4x6 @ 65k". A fix front-loads the
   identity ("Deadlift 3RM test" is 17): planner.build_plan, every sport, and
   test_the_stored_name_matches_the_rebuilt_one holds names to the plan.
2. Add scheduling support for garmin workouts, ability to make multiple
   workouts at once. Unblocked by the verified live push;
   client.schedule_workout exists in garminconnect. While in there: diff
   client.get_workout_by_id() against a generated payload and update the
   "not yet verified" note in planner.py's docstring.
   - Single-workout scheduling is now DONE: `fit plan --schedule DATE` +
     garmin.schedule_workout, verified live 2026-08-24.
   - The multi-week periodised `fit train` feature is now DONE
     (import/show/sync/clear; engine in src/fit/training.py; four goal
     templates — cut from ten on 2026-09-11, one per discipline). Design
     reference: docs/training-plan-feature.md; behaviour: CLAUDE.md
     "Training plans". Still to do:
     (a) verify the newer workout payloads against a live push with
         scripts/diff_workout.py: the five steady combos (run easy/long,
         cycle endurance/long, swim continuous; `long` is built twice, so one
         diff does not cover both) remain unverified. Scoped 2026-09-11: being
         single-step is not the novelty (strength/baseline is single-step and
         passed). run easy/long and swim continuous only move a proven
         pace.zone target to a top-level step — low risk. cycle
         endurance/long are the gap: the only sessions sending power.zone,
         never round-tripped in any position. Do that one first. The bare baselines are
         done — strength/baseline and cycle/baseline round-tripped clean on
         2026-09-05. diff_workout.py now takes --session YYYY-MM-DD to diff a
         training-plan session directly, so once week 1 is synced the cycle
         long ride on 2026-09-20 is one command;
     (b) a real `fit train sync` + `fit train clear` round-trip against Garmin
         (sync now confirms before pushing; --dry-run previews it).
         PARTLY DONE 2026-09-11. Steps 1-3 passed against the live account
         with a one-session window (`--days 1`, deliberately: a wrong key
         would have orphaned one calendar entry, not a week of them).
           1. DONE. `fit train sync --days 1` pushed the 2026-09-11 strength
              baseline; the confirm prompt needs a real TTY.
           2. DONE — **`workoutScheduleId` is the right key.** The live
              response gave workout_id 1694161608, schedule_id 1773688951,
              both non-null, so `train clear` will find a schedule id to
              unschedule and cannot orphan the entry. This was the one real
              risk in the stateless rewrite; it is closed.
           3. DONE. `fit train show` renders it "on watch" (no asterisk —
              nothing has moved off it), header counts 1 on the calendar.
           4. TODO. Widen to `fit train sync --days 7` (5 more sessions), then
              re-run it: the second run must find fewer (idempotency).
           5. TODO. Import an activity that beats a current target, then
              `fit train show`: the pushed sessions must keep their old pace
              and show "on watch*", while unpushed ones move.
           6. DONE. `fit train clear` unscheduled it with the captured
              schedule_id, "pushed" is back to [], the file back to 663 bytes,
              and the session re-derives as an ordinary "planned" one — so the
              whole push/unschedule path is now proven end to end.
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
