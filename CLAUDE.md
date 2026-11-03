# CLAUDE.md — fit CLI workout tracker

Terminal workout tracker (Strava/Runna in spirit). Linux primary, macOS
secondary. Entirely local: no accounts, no sync, no cloud. The whole dataset is
one folder you can zip and move, and it lives on one machine.

Read this before changing anything. It holds the decisions the code can't state
for itself; the code holds its own API — don't look for function signatures here.

---

## Rules

- **Functions only. No classes.** Data is plain dicts throughout: no models, no
  dataclasses, no Pydantic. The sole exception is `garmin.GarminAuthError`, a
  bare `Exception` subclass with no state.
- **Module boundaries**, strictly:
  | module | may do | must not |
  |---|---|---|
  | `storage.py` | all filesystem access | any conditional or arithmetic — that belongs in `compute.py` |
  | `compute.py` | pure functions over dicts | I/O, side effects |
  | `display.py` | Rich rendering | I/O, arithmetic (delegate to `compute`) |
  | `importers.py` | parse TCX/FIT/Strava | import `storage`, dedupe, read config |
  | `planner.py` | build Garmin payloads | I/O, network, import `garminconnect` or `training` |
  | `training.py` | expand the plan description | I/O, network (may import `compute`/`planner`/`templates`) |
  | `templates.py` | the plan's weekly sessions only | import anything from `fit` |
  | `garmin.py` | the network API | build fit's activity shape, touch the data dir |
  | `cli.py` | storage → compute → display | business logic |
- **Always `.get()`** activity fields — older activities lack later additions.
- **Atomic writes** (`os.replace`) for any full-file overwrite. Never
  `open(path, "w")` on config, `pbs.json`, or an activity file.
- Paths stored in dicts are **relative to the data dir**, never absolute.
- **British English**, title case for table titles.
- Add helpers when a caller needs them, **not speculatively**, and delete one
  when it loses its last caller.
- Keep the dependency footprint minimal. Check stdlib and existing deps first.
- Everything in `examples/` and `tests/data/` is synthetic. Real personal data
  never enters the repo — keep it outside and point `FIT_DATA_DIR` at it.

**Dependencies**: Python 3.11+, `typer`, `rich`, `fitparse`, stdlib
`xml.etree.ElementTree` for TCX (no lxml). Two optional extras:
`garmin` (`garminconnect`) and `train` (`pyyaml`, lazily imported inside
`training.parse_plan_spec`). No database, no ORM.

---

## Storage

```
~/.fit/
├── activities/2024-01-15T08:30:00.json   ← one file per activity, named by id
├── config                                 ← plain text key = value
├── pbs.json                               ← cached PBs
├── plans/<created>.json                   ← `fit plan` workouts
└── train/plan.json                        ← the training plan
```

`storage.resolve_data_dir()` is the only path authority: `FIT_DATA_DIR` else
`~/.fit`. Everything else builds on it.

One file per activity (not a JSONL) so edit/delete need no read-modify-write,
`ls`/`cat`/`rm` work, and corruption is isolated.

**Original files are not kept.** An import parses what it needs and discards the
source. Consequences below.

---

## Data shapes

```python
activity = {
    "id": "2024-01-15T08:30:00",   # ISO 8601, also the filename
    "type": "run",                  # run|cycle|walk|hike|swim|squash|canoe|strength
    "date": "2024-01-15",
    "distance_km": 10.2,
    "duration_seconds": 3120,
    "source": "garmin",             # garmin (TCX/FIT) | strava (CSV row)
    # all optional, all absent unless the import could derive them:
    "elevation_gain_m": 45,
    "avg_heart_rate": 152, "max_heart_rate": 171,
    "avg_power": 187,                                        # TCX lap / FIT session
    "best_power": {"1min": 304, "5min": 227, "20min": 141},  # FIT only
    "splits": {"5k_seconds": 1423},
    "hr_zones": {"zone1_seconds": 120.0, ...},
    "exercises": [{"name": "deadlift",                       # strength only
                   "sets": [{"reps": 10, "weight_kg": 100.0}]}],
}

pbs = {
    "computed_from": 47,                    # activity count; != len() means stale
    "run": {"fastest_5k_seconds": 1423, "fastest_5k_date": "2024-03-12",
            "fastest_5k_split_seconds": 1390, "fastest_5k_split_date": "...",
            "longest_distance_km": 21.1, "most_elevation_gain_m": 148},
    "strength": {"deadlift": {"heaviest_set_kg": 130.0, "heaviest_set_date": "...",
                              "best_e1rm_kg": 143.0, "best_e1rm_date": "..."}},
}
```

`plans/<id>.json`: `{id, sport, workout_type, params, workout_name, payload}`,
plus `garmin_workout_id` after a push and `scheduled_date` after a `--schedule`.

`train/plan.json` holds **only what cannot be derived**:

```python
{"spec": {"start_date": "2026-11-04", "end_date": "2027-01-31",
          "days_per_week": [5, 6], "targets": {}},   # the normalised description
 "created": "2026-11-03",
 "volume": {"start_scale": 0.6, "why": "..."},       # pinned at import
 "pushed": [{"date": "2026-11-05", "sport": "cycle", "workout_id": 1,
             "schedule_id": 2, "workout_name": "...", "params": {...}}]}
```

The schedule itself is re-expanded by `training.expand_plan` on every command.
See "The plan is derived, not stored".

---

## Decisions that look like bugs

Do not "fix" these.

**Derived fields are going-forward only.** `splits`, `hr_zones` and `best_power`
are computed once at import from a transient point stream that is then
discarded, and no original file is kept. Adding a split distance, setting
`max_heart_rate`, or changing anything else affects **future imports only**.
There is no backfill and no recompute-on-config-change; recovering old values
means re-importing from wherever the originals still live.

**New PBs are announced once per import batch**, not once per activity.
`detect_new_pbs` takes the whole batch and reduces it via `all_personal_bests`.
Comparing each activity against a fixed pre-import snapshot announces every
activity that beats what was on disk *before* the import, in file order — nine
paddles make a 26-line flood. So `cli._import_and_report` writes first and
announces after.

**Dedicated and split PBs coexist per type.** `fastest_5k_seconds` (a whole ~5k
activity) and `fastest_5k_split_seconds` (the best 5k inside any run) are stored
separately, because `detect_new_pbs` tracks each. `compute.best_pb_per_label`
collapses them to one row **at display time only**.

**Strength PBs are keyed by exercise, not distance label** — which is why
`pbs.json`'s `"strength"` nests one level deeper and `_strength_pbs` sits outside
`_candidate_pbs`'s four helpers. Heaviest set and best e1RM are tracked
independently: a heavy triple and a set of ten are different achievements and
either can be the more recent.

**Volume is measured in time, not distance.** An hour of cycling covers ~4x an
hour of running, ~40x an hour of swimming, and squash covers nothing real at
all. `duration_seconds` is what the sparklines plot.

**`NO_DISTANCE_TYPES` (`squash`, `strength`) have a `distance_km` that is present
and meaningless** — accelerometer noise (~0.07km) and 0.0 respectively. Dropping
the key would make them the only types missing one of the five always-present
fields, so the set suppresses it at display and PB time instead.

**Intensity is measured, never projected.** `fit plan`'s defaults come from your
history and do not drift upward on the assumption you'll improve: a pace you
might not have by week 10 is work you cannot complete. The training plan's rides
carry no intensity at all; only lifting load progresses.

**Strength inverts both axes.** Volume is fixed (5x5 stays 5x5) and *load*
ramps, because that is what linear progression is and a weight that turns out
too heavy is a failed rep, not a failed session. So strength sessions carry no
`scale`, `_SCALED_SPORTS` leaves them out, and the clamp test exempts them.

**Strength progresses on what you lifted, not on a schedule.** Each lift's next
load comes from `training.next_working_weight` over that lift's own sessions in
the plan — the StrongLifts rule: every set's reps done at the heaviest weight
earns one increment, a miss holds the weight, and the third miss running at one
weight resets it 10% lower. It judges what was *lifted*, not what was
prescribed, so loading 65kg against a planned 62.5kg is judged at 65kg. Future
sessions assume each one before them goes to plan, capped at the goal; a past
session shows what its history prescribed then. Every session lifted inside the
plan's dates is judged, and **a skipped session does not advance**: no record,
no step. A schedule-based ramp (today's best e1RM plus a step per week) would
count success twice — every good session raises the base *and* the week count
keeps adding. Each lift has one rep scheme in the plan, which the rule depends
on.

**A lift starts at its ten-rep weight, whatever the scheme**
(`STRENGTH_START_REPS`). This is StrongLifts' start for anyone who has lifted
before. An e1RM read back at five reps is a true 5RM, and five sets of a 5RM
fail by the second set, so starting there would open with a week of misses
until the reset caught up.

**Bodyweight work progresses on reps, by the same rule.** An exercise carrying
a `rep_cap` (pull-ups) has no load to add, so `training.next_rep_target` adds a
rep instead: every set reaching the target earns one, a miss holds it, and
there is no reset — there is no lighter bodyweight. It stops at the cap, which
warns that it is time to move on; the next stage is a `templates.py` edit. Rep
counts are corrected **on the watch**: a later app correction disagrees with
the FIT's reps, and `apply_garmin_exercise_sets` abandons its merge on exactly
that. Only the five barbell lifts get e1RM targets (`PLAN_LIFTS`) or identify a
session for completion matching (`_lifts`).

**Every prescribed load is a whole kg or a multiple of 2.5kg.** Each lift rounds
to its own increment, and every increment in `LIFT_INCREMENT_KG` is StrongLifts'
default 2.5kg. An increment must keep loads loadable: 1.25kg would prescribe
23.75kg, which the watch shows as 23.8 and no bar can hold. Rounding to the
nearest loadable weight instead would make the ramp uneven — 67 + 2.5 lands on
69 and 70 by turns. The e1RMs in `train show` are estimates, not bar weights,
so they print as whole kg.

**A strength warmup is the lift itself, and nobody is asked about it.** The
warmup for a squat is lighter squats, so `planner._warmup_sets` derives a ramp
from the working weight — the empty bar, then 55/70/85% at 5/3/2 reps, rounded
to 2.5kg — and each exercise gets its own, because you do not bench cold having
squatted. Three consequences that read as gaps:

- **No load means no ramp.** There is nothing to ramp *to*, so an exercise with
  no `target_weight_kg` is all working sets, and `strength`/`baseline` — an
  e1RM test, which prescribes no load on purpose — carries "(warm up first)" in
  its name instead.
- **Rungs that round onto each other are dropped, not repeated.** Each is kept
  only if strictly heavier than the last and strictly lighter than the work
  set, so a 35kg bench ramps 20/25/30 rather than 20/20/25/30. That one rule is
  the whole of the small-weight handling.
- **A deadlift ramp starts at 40kg, not the bar** (`_WARMUP_START_KG`), because
  the bar alone is too light to deadlift with proper form. A deadlift working
  at 40kg or less therefore gets no ramp, and a 55% rung under 40kg is dropped
  by the same strictly-heavier rule.

There is no config key and no prompt for any of this: a ramp is a training
decision fit owns, the same way it owns the progression.

**The plan is derived, not stored.** `expand_plan` is deterministic and re-runs
on every `fit train` command, so loads always track what you actually lifted —
importing a session is what applies it; there is nothing to re-run. Session
dates are a pure function of the spec (only `params` depend on history), which
is what makes `(date, sport)` a stable identity across re-derivations.

Two things are deliberately **not** re-derived:

- **Starting volume is pinned at import** (`plan["volume"]`). It is a decision
  about where you were when the plan began; re-measuring it weekly would rewrite
  session sizes as you train.
- **A pushed session renders from the ledger**, not from a fresh derivation.
  Garmin has no edit endpoint, so the watch holds the copy that was sent — the
  plan must show that. When the live derivation moves away from it the session
  is flagged `stale` and rendered `on watch*`; `fit train clear` is how you drop
  it so it re-pushes at current loads.

**Config keys are functional, never cosmetic.** Every key changes *what* a
command shows, not how it's formatted.

---

## External systems

### Garmin Connect (`garmin.py`)

Only module touching the network. Returns raw bytes and raw dicts — never fit's
activity shape. `garminconnect` is imported lazily so every other command works
without the extra. The session token lives at `~/.garminconnect`, deliberately
outside the data dir, so a zipped backup carries no login.

`schedule_workout` and `unschedule_workout` are atomic (one workout, one date) so
a multi-week plan calls them per session rather than needing a batch endpoint.
`unschedule_workout` takes the **schedule** id, not the workout id.

**`schedule_workout` returns that id as `workoutScheduleId`** — verified live
2026-09-11 (workout 1694161608 → schedule 1773688951). `garminconnect` passes the
POST response through unshaped, so the key is Garmin's and nothing offline can
confirm it; `cli.train_sync` stores it into the ledger and `train clear` needs it.
Were it wrong, the row would take `None`, `train clear` would skip the unschedule
call, drop the row anyway, and strand a calendar entry with nothing tracking it.

**A pushed workout is frozen.** There is no update-or-delete endpoint, so
anything already on the calendar can't be rewritten locally without
desynchronising the two. Unscheduling clears the calendar but leaves the
workout in the Garmin library.

### Payload schema (`planner.py`)

Replicated from garminconnect 0.3.6's `workout.py` models rather than imported
(that needs the pydantic extra fit doesn't carry). Ids: sport
running=1/cycling=2/swimming=4/strength=5; step warmup=1/cooldown=2/interval=3/
recovery=4/rest=5/repeat=6; end condition lap.button=1/time=2/distance=3/
iterations=7/reps=10; target no.target=1/power.zone=2/pace.zone=6. Absent fields
are omitted, not null.

Verified by live round-trip (`scripts/diff_workout.py`; the same table is in
planner.py's docstring — keep both current):

| combo | date | confirmed |
|---|---|---|
| `run`/`intervals` | 2026-08-24 | `targetValueOne`/`Two` = low/high m/s in that order; step numbering |
| `strength`/`straight_sets` | 2026-09-05 | `weightValue` is **kilograms** (62.5 → 62.5); reps end condition; timed rest; bare `category` |
| `strength`/`baseline`, `cycle`/`baseline` | 2026-09-05 | untargeted top set / open-target timed block survive as sent |
| `cycle`/`long` | 2026-09-11 | `power.zone` id 2 correct; `targetValueOne`/`Two` = low/high **watts** on a top-level step (120/170 as sent); one-step workout with a target accepted |
| `cycle`/`long` (easy) | 2026-10-13 | an untargeted one-step ride |
| `strength` warmup ramp | 2026-10-13 | lift `category` *and* `weightValue` together on a `warmup` step; a dated 63-char name kept |
| `cycle`/`endurance` | 2026-11-03 | the plan's Thursday ride: an untargeted one-step timed ride |
| `strength` workout B | 2026-11-03 | a strength step's `exerciseName` (the pull-ups' `JUMPING_PULL_UPS`); a dated 94-char name kept |

**Still unverified: `run` easy/long and `swim` continuous.** They pair a
`pace.zone` proven *nested* with a top-level position proven *with power*
(`cycle`/`long`); that exact combination has not been sent. Low risk, not zero.
Diff one when one is next pushed, then update both tables.

**Also unverified:** the `ROW` category, which only workout A carries — diff
one after its first push. Workout B's dated name reaches **98 characters** at
its heaviest; 94 is proven. `cycle`/`intervals` with `sets` (sibling
`RepeatGroupDTO`s with a timed `recovery` between) has not been sent either.
The barbell row has no Garmin variant: the only barbell one in the FIT profile
is reverse-grip. So, like the other four lifts, it goes as the bare category.

**Strength's sport `displayOrder` is 3, not the 4 its dump showed.** Every
strength push comes back stored as 3 (seen 2026-10-13), so fit sends 3 and the
diff stays clean. It is a sort key only; `sportTypeId` 5 is kept as sent.

Strength is shaped unlike every cardio combo, all forced by Garmin's own schema:
load is **not** a target (`no.target` + `weightValue`/`weightUnit` on the step),
reps are the `endCondition`, one `RepeatGroupDTO` per exercise as siblings
numbered globally, and `params` carries a **list** of exercises. `exerciseName`
(the variant) is unset for the barbell lifts — each is a category in its own
right — and sent only for an accessory's `variant` (`PULL_UP` alone does not
say "jumping pull-ups"). An exercise may carry its own `rest` over the
session's: a set of pull-ups does not need a squat's three minutes. The
exercise vocabulary is shared with the import side by construction:
`importers.py` stores FIT's lowercase name, the payload upper-cases it.

A warmup set is the same step with `stepTypeKey` `warmup` — `_lift_step` takes
the kind, because a ramp set differs from a working set in nothing else.

### FIT / TCX quirks

- **fitparse doesn't decode every sport enum** (it jumps 48 → 254), so codes
  Garmin added between arrive as bare ints — `FIT_SPORT_CODE_MAP` handles
  64=squash, 19/41=paddling→canoe. Anything unlisted defaults to `"run"`.
- **A gym session has no distinct sport**: it is sport 10 (`training`) +
  sub_sport 20, so `import_fit` checks `FIT_STRENGTH_SUB_SPORTS` *before* the
  sport map, which would otherwise default it to `"run"`.
- **A real watch array-encodes `set.category`** and fitparse doesn't enum-render
  array elements, so it arrives as `[28]`, not `["squat"]` — hence
  `FIT_EXERCISE_CATEGORY_MAP`. 65534 ("not classified") is left out on purpose.
  The synthetic fixtures write scalars, so `test_importers` covers the array
  form directly.
- **TCX has no Swimming value** (Running/Biking/Other only), so walk/hike/swim/
  canoe all default to `"run"`. Known mislabelling, not yet fixed.
- **Canoe**: Garmin records paddling as 19 or 41 (kayaking); both fold to
  `"canoe"` for this canoe-only setup. Revisit if kayaking is split out.
- **Records without distance are kept** — an indoor ride carries power and HR
  with none, a gym session carries only HR. Each consumer filters for itself.
- **`best_power` is FIT-only and stays that way.** TCX would need a
  namespace-tolerant `Watts` extractor plus a change to how its stream handles
  position-less trackpoints, for a format nothing in practice feeds —
  `garmin-sync` pulls FIT and Strava supplies CSV.

### Strava import

Rows that can't be imported (unmapped type, no date) are dropped but **never
silently**: tallied per type and returned as a warning line — the reason both
Strava importers return `(activities, warnings)`.

A linked activity file that fails to *parse* (including the GPX files Strava
exports, which fit doesn't read) is different: it falls back to the CSV row and
warns, costing only the track-derived extras rather than the whole session.

### Garmin's server-side strength record

The FIT fit downloads is the **original** upload, never rewritten, so an exercise
the watch guessed wrong stays wrong there however often it's fixed in the app.
`garmin.get_exercise_sets` fetches the corrections and
`importers.apply_garmin_exercise_sets` merges them during `garmin-sync`.

**Neither source is complete, so each contributes what it holds**: names from
Garmin, weights from the FIT — a real session came back with the corrected
`SQUAT` category and `weight: null` on those same six sets. A Garmin weight, when
present, wins (it arrives in grams). Sets pair **positionally**, active only; a
differing count or a disagreeing rep count abandons the merge, since a
misaligned pairing renames sets to whatever sat at that index.

---

## HR zones

Standard 5-band %-of-max model (`HR_ZONE_BOUNDARIES`), from a `max_heart_rate`
config key the user sets themselves, not derived. Computed at import from the
same point stream as splits, attributed to the earlier sample's zone, stored as
raw seconds. Shown as a segmented colour bar in the history table; `"—"` when
absent.

---

## Config

`~/.fit/config` is plain text, `key = value`, `#` comments, unknown keys and
malformed lines ignored. `ensure_data_dir()` writes a fully-commented default on
first run. Edits apply next invocation; nothing is cached.

```python
DEFAULTS = {
    "sports": [],                 # empty = all types
    "pbs_window_months": 0,       # 0 = all-time
    "history_count": 5,           # dashboard history rows
    "dashboard_weeks": 12,        # sparkline weeks (0 = all)
    "max_heart_rate": 0,          # bpm, 0 = unset
    "train_sync_window_days": 14,
    "show_sparkline": True, "show_pbs": True, "show_sports_summary": True,
    "show_calendar": True,
}
```

Precedence and scope, all of which have caught people out:

- `--timerange` **beats** `pbs_window_months` and, when present, also disables
  the `dashboard_weeks` cap — the explicit flag wins.
- A non-zero PB window **bypasses `pbs.json` entirely** (`cli._windowed_pbs`
  computes fresh), so a windowed view can never corrupt the all-time cache and
  `computed_from` stays meaningful.
- `--sport` narrows the history table, sparkline, calendar and PB rows. It does
  **not** narrow the sports summary, which is always a whole-dataset
  overview. `--minimal`/`fit dash` forces off `show_pbs` and
  `show_sports_summary` only — the calendar stays.
- `max_heart_rate` is read at **import time only** (see going-forward-only).
- `dashboard_weeks` has deliberately no CLI flag.

---

## Training plans (`fit train`)

One plan, fixed in `templates.WEEKLY_SESSIONS`: StrongLifts 5x5 on Mon/Wed/Fri,
an easy ride on Thursday, a long ride on Sunday and a second on Saturday. A short
YAML **description** dates it; every session is a real Garmin workout, rolled
onto the calendar a window at a time. Changing what the week holds is a
`templates.py` edit.

```yaml
start_date: 2026-11-04        # required
end_date: 2027-01-31          # required; the plan's last day
days_per_week: [5, 6]         # or 6 (the default); a range adds sessions later
volume: 70                    # % of the opening week's riding; else measured
targets: {squat_goal_kg: 105} # e1RM goal per lift; else what the plan can reach
```

**PyYAML is YAML 1.1 and `parse_plan_spec` defends against its coercions**: an
unquoted date resolves to `datetime.date`, so both forms are accepted; bools
are rejected where a number is expected (`isinstance(True, int)` is `True`).

### The week

`expand_plan` lays whole ISO weeks from `start_date`'s Monday through
`end_date`'s week and drops sessions outside the two dates. Then:

1. **Alternation** — StrongLifts alternates A (squat, bench, row) and B (squat,
   overhead press, deadlift 1x5, pull-ups as assistance) every time you lift,
   so Mon/Wed/Fri runs A/B/A one week and B/A/B the next. A weekly list has no
   way to say "alternate", so those sessions carry `week_parity` and the ones
   not matching the week go first, so the trim below sees one session per day.
2. **Frequency** — `days_per_week` keeps the highest-priority days. A range
   (`[5, 6]`) builds to its end value by the last week; Saturday ranks last, so
   that is what arrives. Later weeks only *add*; a falling range is rejected.
3. **Ride sizing** — `clamp(round(base x multiplier))`, the multiplier growing
   `WEEKLY_RAMP_PCT` (4%) a week, compounding. Deliberately **one mechanism**: a
   per-week increment *on top* would compound into nonsense over 12 weeks.
   There is no event to peak for, so there are no recovery weeks, taper or
   tests: the rides only climb and lifting is never deloaded by the calendar.
4. **Loads** — `_progress_lifts` (see "Strength progresses on what you lifted").

**Each ride scales along exactly one axis** — `scale["param"]`: distance for the
long rides, duration for Thursday's.

**The clamps are load-bearing.** Read a binding clamp the right way round:
sitting at the **ceiling** flattens the climb and means a base needs lowering.
`test_scaled_params_keep_headroom_inside_their_clamps` holds the plan to ≤10%
at-max, matching each session to its entry **by weekday** — the two long rides
carry different scales.

**Starting volume converges rather than scaling uniformly.** `volume:` wins, else
the user's **mean** weekly riding over the last 8 weeks is measured against the
plan's opening week (week 1's *own* session list, which is smaller when
frequency builds) and clamped to [0.6, 1.25]. Three easily-broken details: mean
not median (a median collapses to zero once half the weeks are empty, handing
somebody barely riding the *full* volume); the current week is dropped (it's
still filling up); and no history means *unknown*, not untrained, so the plan
stands. It applies fully in week 1 and interpolates back to the plan's own level
by the last week.

### Lift targets

`derive_targets` gives each lift a `current` e1RM, measured from the recent
window (thrown out, with its `why`, outside `PLAUSIBLE_LIFT_E1RM_KG`) else
`FALLBACK_LIFT_E1RM_KG`, and a `goal`. The goal cannot be measured — it's a
decision about the future — so the description wins, else where the
progression gets to by the last session. A goal further off than the plan is
long is **capped and warned about**; one the plan reaches early holds the lift
there from then on.

### Sync and the ledger

Applying progress is just: lift → `fit garmin-sync`. The plan re-derives from
it. `fit train import` is how you change a plan's *shape*, and refuses to
replace a plan with future ledger rows (clearing needs Garmin; importing may be
offline).

**The ledger is read as `stored["pushed"]`, never `.get("pushed", [])`.** A
missing ledger is not an empty one: reading it as "nothing scheduled" would let
`import` replace the file and strand whatever it put on the calendar, so a
malformed plan file fails loudly instead.

`train refresh` is `clear` then `sync`, for applying new loads to sessions the
watch already holds. It confirms **before** the clear rather than letting `sync`
ask for itself: answering no between the two steps would leave the calendar
emptied and nothing re-pushed, which is worse than either end state. It calls
both as plain functions, so it **passes every option explicitly** — an omitted
one is typer's `OptionInfo` default, which is truthy: a missing `dry_run` would
clear the calendar, push nothing and exit 0.

**A session already done is neither cleared nor re-pushed.** "Future" includes
today, so a `refresh` after the day's workout would otherwise unschedule it and
push a duplicate onto the calendar it was ridden from.

`train sync` appends a ledger row per pushed session — the row stores the
`params` and name that were **actually sent**, which is what lets a pushed
session render frozen. `train clear` removes future rows, after which those
sessions simply re-derive as ordinary planned ones; there is no per-session
state to reset.

`train sync` prints the batch and **asks before `garmin.login()` is even
called** — `login()` resumes silently and one sync creates a workout *and* a
calendar entry per session, so a whole batch could otherwise reach the account
with no visible step in between. `fit plan`'s single push stays unguarded. It
schedules only `planned` sessions inside the rolling window, so re-running is
safe, and rewrites the plan file after **every** session so a crash can't leave
it claiming less than what is on the calendar.

**A device that hasn't synced prompts with an old workout.** On 2026-10-13 an
Edge offered an unridden earlier session in place of that day's: the session
had reached Garmin only the day before. Two defences, neither of which can
force a sync. Pushed workouts are **named with their date** (`13/10 Cycle
...`, payload only — the ledger keeps the bare name, since every view prints the
date beside it), so a stale prompt reads as stale. And `garmin-sync` compares
each newly imported activity's `workoutId` against the ledger and warns when it
ran another day's workout (`training.followed_other_session`). It warns rather
than re-attributing completion: same-week matching still ticks the day off.
A session moved **within its own week onto a free day** does not warn — a ride
planned for Saturday and ridden on Sunday — so only a workout from another week,
or one displacing that day's own, reads as stale.

`match_completion` marks a session done when an activity of the same sport falls
in the **same ISO week**, each activity claiming at most one session so one ride
can't tick off a whole week. The week is the unit because the plan is laid in
whole weeks: Friday's lift done on the Sunday is still that week's work, where a
±1-day window would read it as missed. The mirror case is accepted: a Sunday
session done on the Monday after falls in the next week and does not count.

Pairs are claimed **closest first across the week**, not session by session,
and a lifting session only takes an activity that shares a barbell lift with
it (when both sides record one). Going session by session would let an early
session claim a later day's activity and leave the session actually trained
marked missed.

---

## Commands

```
fit dashboard [--sport S] [--timerange 3m] [--minimal]   fit dash = --minimal
fit pbs [--months N]          fit stats [--week|--month|--year]
fit import <path>             TCX/FIT file, folder, or Strava export
fit garmin-sync [--days N]    fit gs = --days 7
fit plan --sport S --type T [--no-push] [--schedule DATE]
fit train import <plan.yaml> [--weeks N]   fit train show [--weeks N]
fit train sync [--days N] [--yes] [--dry-run]
fit train clear               fit train refresh [--days N] [--yes]
fit history [N]               fit calendar        fit usage
```

Sport/type matrix (`planner.WORKOUT_TYPES`): run has intervals/tempo/baseline/
easy/long, cycle intervals/baseline/endurance/long, swim intervals/continuous/
baseline, strength straight_sets/baseline. Quality types are warmup → main →
cooldown; the five *steady* combos (run easy/long, cycle endurance/long, swim
continuous) are a **single block** with a wider target band, because a warmup
inside an easy run is just more easy running. The easy rides carry no target at
all and are named `... easy`, so the watch never beeps on them.
`_cycle_intervals` can also build Rønnestad-style sets (30/15s: `sets` repeat
groups with a timed `set_recovery` between) when its params carry them;
`fit plan` does not prompt for them, so nothing passes them at present.

The `baseline` types are benchmark tests, and follow one rule: **the recorded
activity must BE the test, unless the sport's split machinery can isolate it
from within.** So a run test keeps its warmup (`fastest_split` isolates the
5km — not 3km, which no split or milestone measures), a ride test is wrapped
(`best_power_window` recovers the 20 minutes), a swim test is a bare 1km (it
lands in the `fastest_1k` milestone), and a strength test is a heavy triple.
A bare test instructs no warmup and nothing in the payload can carry that, so
`planner._bare_suffix` puts it in the **workout name**.

`fit plan` **saves before pushing**, so a failed push never loses the workout;
the id and scheduled date are written back after each step succeeds, making the
plan file the reconstructable record of what fit put on the calendar.
`--schedule` is validated up front (fail fast, offline) and rejected with
`--no-push` rather than silently ignored.

`import_activity` is the one command with real branching — it inspects the path's
filesystem shape to pick an importer. That's classification of a CLI argument,
not business logic, so it stays inline.

---

## Tests

`.venv/bin/pytest` after any change to `compute`, `storage`, `importers`,
`planner`, `training`, or how `cli`'s `train` commands compose.

**The suite is small on purpose.** `test_cli.py` is the only CLI test and stays
that narrow — command composition that writes to Garmin, with every `garmin`
call stubbed. `test_display.py` only checks the plan view renders. Three things
do not earn a test:

1. **Restating a constant or one-line definition.** Assert direction through the
   pipeline that consumes it, not against the constant.
2. **Asserting what the code guarantees by construction.** A clamped value lies
   inside its clamp; two functions calling the same builder agree.
3. **Restating the week.** Test what the engine does with `WEEKLY_SESSIONS`
   (alternation, build order, headroom), not which session sits on which day.

**Before deleting a test, mutate the behaviour it names and confirm something
still fails.** This is not ceremony: it has caught over-cuts such as a swim
`rest` step silently becoming `recovery` (a live-account defect) and
`write_plan` clobbering every plan into one file. Tests earn their place by
covering maths you can't eyeball, things expensive or irreversible to check
live (Garmin payloads), and parsers of data you can't re-obtain.

---

## Not doing

Do not design current modules around these:

- **More than one training plan, or a choice of goals.** The plan is one week of
  data; change it in `templates.py`.
- **Adaptive reflow** when sessions are missed.
- **Garmin multisport/brick files** — that schema is unverified.
- **USB workout delivery** — it needs a FIT *encoder*; fitparse only reads.
- **A single fitness or load number.** At this volume one busy week moves any
  such index by double digits; the volume sparkline already shows load honestly.
- **Syncing the data dir between machines.** A merge by filename cannot carry
  an edit or a delete, and the Garmin ledger needs a single writer. Backup is
  zipping the data dir.
