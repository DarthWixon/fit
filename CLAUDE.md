# CLAUDE.md — fit CLI workout tracker

Terminal workout tracker (Strava/Runna in spirit). Linux primary, macOS
secondary. Entirely local: no accounts, no sync, no cloud. The whole dataset is
one folder you can zip and move.

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
  | `planner.py` | build Garmin payloads | I/O, network, import `garminconnect` |
  | `training.py` | expand plan descriptions | I/O, network (may import `compute`/`planner`/`templates`) |
  | `templates.py` | goal data only | import anything from `fit` |
  | `garmin.py` | the network API | build fit's activity shape, touch the data dir |
  | `cli.py` | storage → compute → display | business logic |
- **Always `.get()`** activity fields — older activities lack later additions.
- **Atomic writes** (`os.replace`) for any full-file overwrite. Never
  `open(path, "w")` on config, `pbs.json`, or an activity file.
- Paths stored in dicts are **relative to the data dir**, never absolute.
- **British English**, title case for table titles.
- Add helpers when a caller needs them, **not speculatively**. Speculative
  helpers have been removed twice (unused storage helpers;
  `garmin.get_scheduled_workouts`, `planner.STEADY_TYPES`, `planner._TARGET_TYPES`).
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
├── fitness.json                           ← the fitness-index baseline
├── plans/<created>.json                   ← `fit plan` workouts
└── train/plan.json                        ← the single active training plan
```

`storage.resolve_data_dir()` is the only path authority: `FIT_DATA_DIR` else
`~/.fit`. Everything else builds on it.

One file per activity (not a JSONL) so edit/delete need no read-modify-write,
`ls`/`cat`/`rm` work, corruption is isolated, and rsync only moves what changed.

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
    "source": "garmin",             # garmin|strava
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

`train/plan.json` holds **only what cannot be derived** — about 400 bytes:

```python
{"spec": {...},            # the normalised description
 "created": "2026-08-24",
 "volume": {"start_scale": 0.83, "why": "..."},   # pinned at import
 "pushed": [{"date": "2026-08-25", "sport": "run", "workout_id": 1,
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

**The fitness baseline is sticky, and drift only warns.** `fitness.json` is
written once (lazily) and never auto-recomputed — 100 means the rolling load on
that day, and moving the anchor would silently change what every past index
value meant. But that only holds while the days *before* it stay put, so
`baseline_from` records how much history sat behind the anchor and
`compute.baseline_drift` compares it on every render. It **reports, never
repairs**: the fix is the user's to run (`fit fitness-reset --as-of <date>`
re-cuts it where it stands; bare `fitness-reset` moves it to today). This is the
exact opposite of `pbs.json`, which silently recomputes when stale —
deliberately so. Not hypothetical: a sync of two activities dated a month before
the anchor read as +9% when the real change was +1.4%.

**New PBs are announced once per import batch**, not once per activity.
`detect_new_pbs` takes the whole batch and reduces it via `all_personal_bests`.
Per-activity comparison against a fixed pre-import snapshot announced every
activity beating what was on disk *before* the import, in file order — a real
26-line flood from nine paddles. So `cli._import_and_report` writes first and
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

**Intensity is measured, never projected** (except strength — see below). Every
session is built against the targets found at expansion and does not drift
upward on the assumption you'll improve: prescribing a pace you might not have
by week 10 prescribes work you cannot complete, which is worse than work that is
slightly easy. A plan earns a faster pace by re-measuring.

**Strength inverts both axes.** Volume is fixed (3x10 stays 3x10) and *load*
ramps, because that is what linear progression is and a weight that turns out
too heavy is a failed rep, not a failed session. So strength sessions carry no
`scale`, `volume_sports` excludes them, and the clamp tests exempt them.

**The plan is derived, not stored.** `expand_plan` is deterministic and re-runs
on every `fit train` command, so targets always track your *measured* fitness —
importing a faster 5k is what applies it, with no retarget step in between.
Session dates are a pure function of the spec (only `params` depend on history),
which is what makes `(date, sport)` a stable identity across re-derivations.

Two things are deliberately **not** re-derived:

- **Starting volume is pinned at import** (`plan["volume"]`). It is a decision
  about where you were when the plan began; re-measuring it weekly would rewrite
  session sizes as you train.
- **A pushed session renders from the ledger**, not from a fresh derivation.
  Garmin has no edit endpoint, so the watch holds the copy that was sent — the
  plan must show that. When the live derivation moves away from it the session
  is flagged `stale` and rendered `on watch*`; `fit train clear` is how you drop
  it so it re-pushes at current fitness.

This is why there is no `fit train retarget`: re-deriving isn't an operation you
run, it's what every command already does.

**Extras are local-only.** Garmin has no calendar-note or non-workout endpoint
(`schedule_workout` takes a `workout_id`), so extras are never built or pushed.
Bricks are two same-day sessions, not a multisport workout — that schema is
unverified.

**Config keys are functional, never cosmetic.** Every key changes *what* a
command shows, not how it's formatted. Units/week-start/name knobs were dropped
as dead config.

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
desynchronising the two.

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
| `strength`/`straight_sets` | 2026-09-05 | `weightValue` is **kilograms** (62.5 → 62.5); reps end condition; timed rest; CARDIO warmup; bare `category` |
| `strength`/`baseline`, `cycle`/`baseline` | 2026-09-05 | untargeted top set / open-target timed block survive as sent |

**Still unverified: the five steady combos** (`run` easy/long, `cycle`
endurance/long, `swim` continuous). Four type *names*, five sport/type pairs:
`long` is built twice, by `_run_long` and `_cycle_long`, so verifying one does
not cover the other. Run the diff on each, then update both tables.

Their payloads are **nearly** covered by what is proven, so scope the risk before
spending a round trip on it. Being single-step is *not* the novelty — a bare
`strength`/`baseline` is one step too and round-tripped clean on 2026-09-05. The
only leaf paths no verified combo has sent are `targetValueOne`/`targetValueTwo`
directly on an `ExecutableStepDTO` rather than nested in a `RepeatGroupDTO`, and
those exact field names are the 2026-08-24 `run`/`intervals` row. So `run`
easy/long and `swim` continuous re-use a proven target type (`pace.zone`) in a
new position — low risk. **`cycle` endurance/long are the real gap**: they are
the only sessions fit sends with `power.zone`, a transcribed id (2) that has
never been echoed back by Garmin in any position. A wrong id there fails loudly
on the bike, not silently in the data — the ride shows no target or a nonsense
one — so it is cheap to defer, but it is untested, not merely unusual.

Strength is shaped unlike every cardio combo, all forced by Garmin's own schema:
load is **not** a target (`no.target` + `weightValue`/`weightUnit` on the step),
reps are the `endCondition`, one `RepeatGroupDTO` per exercise as siblings
numbered globally, and `params` carries a **list** of exercises. `exerciseName`
(the variant) stays unset — each lift fit plans is a category in its own right.
The exercise vocabulary is shared with the import side by construction:
`importers.py` stores FIT's lowercase name, the payload upper-cases it.

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
  Synthetic fixtures write scalars, which is why this was missed at first.
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

A linked TCX/FIT that fails to *parse* is different: it falls back to the CSV
row and warns, costing only the track-derived extras rather than the whole
session.

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

## Fitness index

One number, rescaled to a baseline of 100. Deliberately coarse — a "doesn't need
to be perfect" goal.

Per-activity load is **MET-hours** (`MET_TABLE`, banded by pace/speed). This is
the only base that works for every activity regardless of source:
`avg_heart_rate` and `avg_power` come only from TCX/FIT, never bare Strava CSV,
while type/distance/duration are always present. No FTP or threshold calibration
is asked of the user.

When `avg_heart_rate` *is* present the base is scaled by
`avg_heart_rate / median(same type)`, clamped to `[0.8, 1.25]` so one anomalous
reading can't swing a day. The median is **inclusive** of the activity being
scored, so a type's first HR-tagged activity gets a neutral 1.0 for free.

Daily totals feed a **42-day EWMA** (Coggan CTL) over *every calendar day*, so
rest decays the number. Seeded at the first day's own load rather than 0, to
avoid a fake multi-week ramp for anyone backfilling history.

**One combined index**, never filtered by `--sport`. `--timerange` narrows the
trend *sparkline* only (via `filter_series_by_date` on the computed series — not
by re-running the EWMA over a truncated list, which would discard pre-window
decay). The headline is always full-history as of today, which is why the
dashboard's fitness block renders *before* the empty-activities early return.

*Known limitation*: `median_hr_by_type` is recomputed from all current
activities, not frozen per activity, so an old activity's load shifts slightly as
more HR data arrives. Acceptable under "coarse, not perfect".

---

## HR zones

Standard 5-band %-of-max model (`HR_ZONE_BOUNDARIES`), from a `max_heart_rate`
config key the user sets themselves — not derived, unlike the fitness index's
self-calibrating multiplier. Computed at import from the same point stream as
splits, attributed to the earlier sample's zone, stored as raw seconds. Shown as
a segmented colour bar in the history table; `"—"` when absent.

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
    "show_fitness_index": True, "show_calendar": True,
}
```

Precedence and scope, all of which have caught people out:

- `--timerange` **beats** `pbs_window_months` and, when present, also disables
  the `dashboard_weeks` cap — the explicit flag wins.
- A non-zero PB window **bypasses `pbs.json` entirely** (`cli._windowed_pbs`
  computes fresh), so a windowed view can never corrupt the all-time cache and
  `computed_from` stays meaningful.
- `--sport` narrows the history table, sparkline, calendar and PB rows. It does
  **not** narrow the sports summary or the fitness index, which are always
  whole-dataset overviews. `--minimal`/`fit dash` forces off `show_pbs` and
  `show_sports_summary` only — the calendar stays.
- `max_heart_rate` is read at **import time only** (see going-forward-only).
- `dashboard_weeks` has deliberately no CLI flag; `fit fitness` stays
  full-history.

---

## Training plans (`fit train`)

A compact YAML **description** expands into a multi-week periodised schedule: a
real Garmin workout per session, intensities from the user's own history, rolled
onto the calendar a window at a time. The intended workflow is that the user
talks a goal through with an external bot, the bot emits the description, and
**fit owns all the periodisation** — so the description stays thin.

```yaml
goal: standard_triathlon      # required; one of templates.GOAL_TEMPLATES
event_date: 2026-11-15        # required; the plan counts back from here
start_date: 2026-08-24        # else event_date - template length
days_per_week: 6              # or [2, 4] to build frequency
rest_day: Mon
extras: {strength: 2, yoga: 1}
targets: {run_5k: "24:00", bike_ftp: 250, swim_css_100m: "1:45",
          squat_goal_kg: 140}
progression: {build_recover: [3, 1], weekly_ramp_pct: 8, taper_weeks: 2}
volume: 70                    # % of the template's opening week
test_week: true               # open with week 0, benchmarks only
benchmarks: true              # in-plan re-tests
```

**PyYAML is YAML 1.1 and `parse_plan_spec` defends against its coercions**: an
unquoted date resolves to `datetime.date`; unquoted `24:00` resolves to the
base-60 int 1440 (which is exactly the seconds meant, so a bare int is accepted);
`rest_day: no` resolves to `False`, so bools are rejected explicitly
(`isinstance(True, int)` is `True`).

### Periodisation

`expand_plan` lays whole ISO weeks from `start_date`'s Monday through the event's
week, then:

1. **Phases** — the taper takes the final `taper_weeks`; the template's phases
   share the rest by largest-remainder apportionment, so any length gets a
   sensible base/build/peak split.
2. **Volume multiplier** — build weeks ramp; recovery weeks dip to
   `RECOVERY_FACTOR` (0.6) *without advancing the level*; taper weeks step
   linearly from 0.75 to 0.45 of peak.
3. **Session sizing** — `clamp(round(base x multiplier))`. Deliberately **one
   mechanism**: an early design added a per-week growth increment *on top*,
   which compounds into nonsense over 12 weeks.
4. **Extras** — placed on the least-loaded non-key days, so rest days fill first.

Sessions before `start_date` or on/after the event are dropped: race day is not a
training day.

**The ramp is solved from the plan's actual length**, not fixed. Compounding a
fixed rate over a longer block produces a *higher* peak, not a longer climb,
pinning every session at its clamp. `intended_peak` derives what the template
reaches at its own length under `REFERENCE_RAMP_PCT` (8); `derive_weekly_ramp`
solves for that peak and caps at the reference, so a *shorter* plan peaks lower —
which is what a short run-up honestly buys. `progression.weekly_ramp_pct` is an
explicit override, which is why it is absent from `PROGRESSION_DEFAULTS`.

**Starting volume converges rather than scaling uniformly.** `volume:` wins, else
the user's **mean** weekly training in the goal's sports over the last 8 weeks is
measured against the template's opening week and clamped to [0.6, 1.25]. Three
easily-broken details: mean not median (a median collapses to zero once half the
weeks are empty, handing somebody barely training the *full* volume); the current
week is dropped (it's still filling up); and no history means *unknown*, not
untrained, so the template stands. It applies fully in week 1 and interpolates
back to the template's level by the last build week — uniform scaling would start
them right but leave them under-prepared for a fixed-distance event. Growth
beyond `VOLUME_RAMP_WARN` (2.2x) appends to `plan["warnings"]`.

**Frequency and volume are separate axes.** `days_per_week: [2, 4]` builds to the
end value by the last build week then **holds through the taper** — a taper cuts
volume, not frequency. A falling range is rejected. Sessions arrive in template
priority order, so later weeks only *add*; nothing is swapped out.

### Goal templates

Four goals in `templates.GOAL_TEMPLATES` — pure data, no engine logic, which is
the point of the split: adding or recalibrating a goal touches that file only.
One per discipline (`run_10k`, `cycle_strength`, `standard_triathlon`,
`strength_program`): the set was cut from ten on 2026-09-11 because the extras
were the same shapes at different numbers, and `start_date` already re-lengthens
a template. The two triathlons were structurally identical.

**Priorities interleave the sports** rather than ranking every long session
first, so trimming a week never strips a whole discipline. (Below three days
there aren't enough slots and the lowest-priority sports do drop out.)

**Each session scales along exactly one axis** — `scale["param"]`, a training
decision: distance for long sessions, duration for steady, rep *count* for most
intervals, but rep *duration* for `cycle_strength`'s sustained-block day, where
a 2–5 rep count is too coarse to express a progression.

**The clamps are load-bearing.** Peak volume depends on plan length (8 weeks
~1.36x, 12 ~1.59x, 16 ~2.2x), so `standard_triathlon`'s bases sit lower relative
to its clamps. Read a binding clamp the right way round: hitting the **floor** on
a recovery or taper week is the floor working; sitting at the **ceiling**
flattens the peak and means a base needs lowering.
`test_scaled_params_keep_headroom_inside_their_clamps` holds every goal to ≤10%
at-max. When measuring, match each session to its template entry **by weekday as
well as type** — two sessions of one type carry different scales, and matching on
type alone reads one against the other's clamps.

**`hills` is gone** (2026-09-11). Its only template user was
`cycle_100k_sportive`, and a workout type no goal can schedule is dead weight in
`WORKOUT_TYPES`, two prompt specs and a builder. It was also the one type with no
pace target — gradient makes pace meaningless — so nothing else now needs that
shape outside `baseline`. Re-adding it means the builder and prompts back, not
just a template entry.

### Targets and benchmarks

`derive_targets` resolves only the targets the goal's sports need, once per plan:
description → `planner.derive_*` over the recent window → `FALLBACK_TARGETS`.
Each carries a `why`, surfaced in `fit train show`, so a target derived from thin
history is visible rather than silently wrong. A `targets:` entry for an
untrained sport is a `ValueError`, not a silent no-op.

`planner.derive_target` guards every derivation against `PLAUSIBLE_TARGETS` and
returns `{"value", "why", "rejected"}`. `rejected` separates "measured and thrown
out" from "nothing to measure", so a bad reading is never silently swapped for a
default: `fit train` folds the `why` into the target's provenance, and
`recommend_defaults` emits a **why-only rec** (neither `default` nor `derive`) so
`fit plan` keeps its static default but still says what was discarded. Consumers
of a rec must test for the key rather than assume one is present. An explicit
`targets:` entry is the user's own word and is **not** bounded by these guards.

Strength targets are **per lift**, under `targets["strength"]`, deliberately
outside `_SPORT_TARGETS`/`PLAUSIBLE_TARGETS` — forcing them in would mean giving
`derive_target` a float path and per-exercise bounds, changing code all three
cardio sports depend on. `current` is measured; `goal` cannot be (it's a decision
about the future) so the description wins, else `reachable_e1rm`. A goal further
off than the plan is long is **capped and warned about**. `by_week` bakes the
week structure into a table once, so `_apply_target` stays a stateless lookup.

**Benchmark shape follows one rule:**

> The recorded activity must BE the test, unless the sport's split machinery can
> isolate the test from within it.

- **run**: 5km, warmup and cooldown kept — `fastest_split` isolates the window.
  5km not 3km because 3km is in neither `SPLIT_DISTANCES_KM` nor
  `MILESTONES_KM`, so a 3km effort was measurable *nowhere*, and the best 5k
  window containing it dragged in 2km of warmup. That made the old test worse
  than no test.
- **cycle**: a normal wrapped 20-minute test — `best_power_window` recovers the
  effort from wherever it sits. (`avg_power` alone is a whole-activity mean, so
  a 141W best-20min sat behind a 128W average on a real ride.)
- **strength**: a heavy triple on whichever lift that week's session leads with —
  `estimated_1rm` reads a 3RM fine, and a plan shouldn't send anybody to a real
  1RM alone in a gym every few weeks.
- **swim**: bare 1km — it lands in the existing `fastest_1k` milestone, and pool
  swims often carry no distance stream for splits.

A bare test instructs no warmup and nothing in the payload can carry that, so
`planner._bare_suffix` puts it in the **workout name**. `fit plan --type
baseline` produces the same test — the prompt defaults mirror
`BENCHMARK_SESSIONS`, and they drifted once already (the plan moved 3km→5km, the
prompt didn't), so a test now holds them together.

Benchmarks land **on recovery weeks only, never in the taper** (you test rested),
**replace** that sport's session rather than adding to it, and **take turns**
between sports. A week where no testable sport has a suitable session is skipped
*without consuming a turn*.

`test_week: true` opens with **week 0**: benchmarks and nothing else, because a
plan's targets are otherwise derived from whatever history exists and an
unmeasured target sets every session's intensity until week 4 at the earliest.
It **takes a week out of the plan, not off the front** — both dates are the
user's — so `plan["weeks"]` stays the *periodised* count and only
`_default_start_date` compensates. It is independent of `benchmarks:`: measuring
once up front and re-measuring as you go are separate decisions.

### Sync and the ledger

Applying a re-test is just: do the test → `fit garmin-sync`. The plan re-derives
from it. `fit train import` is how you change a plan's *shape*, and refuses to
replace a plan with future ledger rows (clearing needs Garmin; importing may be
offline).

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

`match_completion` marks a session done when an activity of the same sport falls
within ±1 day, each activity claiming at most one session (nearest first) so one
ride can't tick off a whole week.

---

## Commands

```
fit dashboard [--sport S] [--timerange 3m] [--minimal]   fit dash = --minimal
fit pbs [--months N]          fit stats [--week|--month|--year]
fit fitness                   fit fitness-reset [--as-of DATE]
fit import <path>             TCX/FIT file, folder, or Strava export
fit garmin-sync [--days N]    fit gs = --days 7
fit plan --sport S --type T [--no-push] [--schedule DATE]
fit train import|show|sync|clear
fit history [N]               fit calendar        fit usage
```

Sport/type matrix (`planner.WORKOUT_TYPES`): run has intervals/tempo/baseline/
easy/long, cycle intervals/baseline/endurance/long, swim intervals/continuous/
baseline, strength straight_sets/baseline. Quality types are warmup → main →
cooldown; the five *steady* combos (run easy/long, cycle endurance/long, swim
continuous) are a **single block** with a wider target band, because a warmup
inside an easy run is just more easy running.

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
`planner` or `training`.

**The suite is small on purpose** and has been trimmed twice (192 → 154 → 195
cases, having regrown in between; 199 now — the stateless-plan commit added
four). Three things do not earn a test:

1. **Restating a constant or one-line definition.** Assert direction through the
   pipeline that consumes it, not against the constant.
2. **Asserting what the code guarantees by construction.** A clamped value lies
   inside its clamp; two functions calling the same builder agree.
3. **Running goal-agnostic code once per goal.** `parametrize(ALL_GOALS)` is for
   properties of the *template data*.

The three `ALL_GOALS` loops are deliberate and should stay — they are the only
tests that catch mistakes in `GOAL_TEMPLATES`, which is where a plan actually
goes wrong. Prefer folding a new assertion into an existing loop over adding a
function beside it.

**Before deleting a test, mutate the behaviour it names and confirm something
still fails.** This is not ceremony: it caught six over-cuts in the last trim,
including a swim `rest` step silently becoming `recovery` (a live-account
defect), `write_plan` clobbering every plan into one file, and
`future_scheduled` losing its date filter. Tests earn their place by covering
maths you can't eyeball, things expensive or irreversible to check live (Garmin
payloads), and parsers of data you can't re-obtain.

---

## Not doing

Do not design current modules around these: further goal templates (pure data),
adaptive reflow when sessions are missed, Garmin multisport/brick files,
multiple concurrent training plans, USB workout delivery (needs a FIT *encoder*;
fitparse only reads).
