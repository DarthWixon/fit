# STRENGTH_PLAN.md — full strength integration (with Garmin Connect)

Context file for Claude Code. Read this alongside `CLAUDE.md` before touching any
code — it assumes and extends every convention in that file (functions only,
dicts not classes, `storage.py` as the sole I/O boundary, pure `compute.py`, etc.)
rather than repeating them.

---

## What this adds

Strength training becomes a full sport on par with run/cycle/swim: loggable
(manually and via Garmin FIT import), tracked (PBs, dashboard, calendar),
plannable (`fit plan --sport strength`), and periodizable inside `fit train`
with real progression toward a target working weight — not the placeholder
`extras: {strength: N}` count that exists today.

Two consequences worth stating up front because they change existing output
rather than only adding to it:

- **Strength sessions start feeding the fitness index.** A `MET_TABLE` entry
  (Phase 1) means every logged gym session contributes training load to the
  EWMA behind `fit fitness` and the dashboard's headline number. That is
  correct — a gym session *is* training load — but it moves a number the user
  already reads, going forward from the first strength import.
- **Existing goal templates change shape.** Adding real strength sessions to
  the triathlon templates (Phase 4) changes what those goals prescribe and how
  their opening volume is measured. See Phase 4 for the specifics.

This is a strict superset of the "Option B" logging-and-PBs feature. Build in
that order — logging + PBs first, planner/training/Garmin second — so there is
a working, useful state after each phase rather than one large all-or-nothing
change.

## Non-goals

- Rebuilding Garmin's entire exercise catalog. Map only the four lifts in
  scope (deadlift, back squat, bench press, overhead/shoulder press) plus
  whatever a real FIT fixture turns up; extend the map later the same way
  `importers.FIT_SPORT_CODE_MAP` grew — from an encountered fixture, not
  speculatively.
- Superset/circuit/EMOM representation. Straight sets only for v1.
- Bodyweight-relative or accessory-exercise modeling (bands, dumbbells at
  arbitrary increments). Barbell lifts with a plate-loadable weight only.
- A manual `fit log strength` CLI command. Garmin FIT import is the primary
  ingestion path per this plan; add manual entry later only if it turns out
  to be needed (e.g. a session done without a watch). Because of this,
  `"source"` for a strength activity is `"garmin"` like every other imported
  type — there is no `"manual"` source value until such a command exists.

---

## Phase 0 — schema discovery

**Most of this is already done.** The findings are recorded at the bottom of
this file, gathered from the two references that were sitting on disk the
whole time: `garminconnect` 0.3.6's `workout.py` in `.venv/`, and `fitparse`
1.2.0's bundled FIT profile. Read the Findings section before Phase 2 or
Phase 3 — the design notes in those phases assume it.

**Phase 0 is now complete** — the live dump was gathered on 2026-09-05 and is
recorded in Findings. The rest of this section is kept for the record of how
it was obtained; `scripts/dump_workout.py` re-runs it if the schema ever needs
re-checking.

**What a Garmin strength workout actually looks like over the wire.**
`workout.py` models *nothing* strength-specific beyond
`SportType.STRENGTH_TRAINING = 5` — no exercise field, no weight target (see
Findings). So unlike the cardio payloads, there is no reference model to
replicate, and its silence is not evidence that the real API has no such
fields; Connect's own UI plainly builds workouts with named exercises and
weights. Ground truth has to come from the account:

1. Build a small strength workout by hand in the Connect web UI (one exercise,
   3 sets, a target weight, plus a second exercise so multi-exercise
   composition is visible).
2. Pull it back with **`scripts/dump_workout.py`** (written for this;
   `--list` finds the id, then `dump_workout.py <id> --out strength.json`).
   `scripts/diff_workout.py` can't do this — it diffs a locally built payload
   against the stored one, and there is no local payload yet. The dump script
   only ever reads.
3. Record what comes back in the Findings section below — specifically: what
   carries the exercise identity (a step-level `exerciseName`/`exerciseCategory`
   pair is the likely shape), whether a weight target exists at all and under
   what key, and how multiple exercises compose inside one workout's step list.

A remaining, smaller unknown blocks Phase 2's parsing: **a real strength FIT
file**. The field names, units and enums are all confirmed already (Findings),
so what a fixture adds is only which fields Garmin actually populates, and
whether `category` arrives as a scalar or a list. Phase 2 can be written
against the confirmed shape and corrected by the fixture; it should not be
*merged* without one.

If neither the account action nor a fixture is available yet, Phases 1–2 can
still proceed against the activity data shape below (which depends on
neither). Phase 3's payload builder must not be written speculatively — stub
it with a clear `NotImplementedError` and a comment pointing back here.

---

## Phase 1 — activity data shape & `compute.py` — **DONE**

Landed: `compute.estimated_1rm`, `compute._strength_pbs`,
`compute._strength_new_pbs`, `MET_TABLE`/`NO_DISTANCE_TYPES` entries,
`display._render_strength_pbs_table` and the strength branches in
`_parse_pb_key`/`_format_pb_metric`/`render_new_pb_messages`. Tests in
`tests/test_compute.py`. The design notes below are what was built, with one
correction, marked.

### Activity shape

```python
{
    "id": "2026-09-04T18:00:00",
    "type": "strength",
    "date": "2026-09-04",
    "duration_seconds": 2700,
    "source": "garmin",
    "exercises": [
        {
            "name": "deadlift",         # normalized lowercase/underscore, open string like `type` itself
            "sets": [
                {"reps": 10, "weight_kg": 100.0},
                {"reps": 10, "weight_kg": 100.0},
                {"reps": 10, "weight_kg": 100.0},
            ],
        },
        {"name": "squat", "sets": [...]},
    ],
}
```

**Corrected during implementation: `distance_km` stays present.** The plan
originally dropped the key entirely. It is kept (usually `0.0`, whatever the
session message reports) for one reason: squash is the existing precedent for
a type whose distance is meaningless, and it keeps the field and relies on
`NO_DISTANCE_TYPES` to suppress it everywhere. Dropping it would have made
strength the only type missing one of `_base_activity`'s five always-present
fields, for no gain — every consumer already reads distance defensively, so
nothing downstream can tell the difference.

Weight is stored in kg. FIT already delivers kg (Findings), so this is a
storage decision rather than a conversion; if a fixture ever shows otherwise,
convert at the import boundary and nothing downstream branches on units — the
same "normalize once at the boundary" pattern `calc_pace` follows for pace vs
speed.

### `compute.py`

- `NO_DISTANCE_TYPES`: add `"strength"`.
- `MET_TABLE`: add a flat `"strength"` value (~6.0, ACSM vigorous
  free-weight training), same treatment as `hike`/`squash`. Note this is what
  puts gym sessions into the fitness index — see "What this adds".
- `estimated_1rm(weight_kg: float, reps: int) -> float` — Epley formula
  (`weight * (1 + reps/30)`), the single source of truth for e1RM, used by
  both PB tracking here and target derivation in Phase 4.
- `_strength_pbs(activities: list[dict]) -> dict` — a fifth helper alongside
  `_longest_distance_pb`/`_milestone_pbs`/`_split_pbs`/`_elevation_pb`,
  dispatched by `all_personal_bests` when `type == "strength"` instead of
  the generic `_candidate_pbs` path (this type's PB shape is genuinely
  different — keyed by exercise name, not by distance/time label). Per
  exercise name, across every set in every strength activity:
  - `heaviest_set_kg` / `heaviest_set_date` — heaviest single set at any
    rep count
  - `best_e1rm_kg` / `best_e1rm_date` — best `estimated_1rm` across all sets
- `detect_new_pbs`: extend to check an incoming strength activity's sets
  against `current_pbs.get("strength", {})` per exercise name, returning the
  same structured `{"key", "value"}` shape every other category uses.

### `pbs.json` shape addition

```json
{
    "strength": {
        "deadlift": {"heaviest_set_kg": 120.0, "heaviest_set_date": "2026-08-01",
                      "best_e1rm_kg": 133.3, "best_e1rm_date": "2026-08-15"},
        "squat": {...}
    }
}
```

### `display.py`

- `render_pbs_table`: strength branch — one row per exercise (columns
  Exercise / Heaviest set / Best e1RM), not one row per label like every
  other type.
- `render_new_pb_messages`: format strength-shaped entries ("New heaviest
  deadlift: 120kg" / "New estimated 1RM squat: 133kg").
- Everything else (history table, sparkline, sports summary, calendar) needs
  no changes — already generic over `type`/`duration_seconds`, and
  `_format_distance`/`_format_effort` show `"—"` off `NO_DISTANCE_TYPES`.
- `sports` config / `--sport strength` need no code change either, but this is
  the point at which `fit dashboard --sport strength` becomes meaningful.

### Tests

e1RM correctness and rounding; `_strength_pbs` across multiple sessions
(heaviest-set and best-e1RM tracked independently per exercise); PB
detection firing correctly; `NO_DISTANCE_TYPES`/`MET_TABLE` coverage.

**This phase is testable, not yet user-facing.** With manual entry a non-goal,
the only way to get a strength activity in is a hand-written JSON file in
`activities/` — fine as a development stopgap, not a shipping story. The first
genuinely useful checkpoint is Phase 2.

---

## Phase 2 — FIT import (`importers.py`) — **DONE**

Landed: `importers.FIT_STRENGTH_SUB_SPORTS`, `_fit_exercise_name`,
`_parse_fit_sets`, the strength branch in `import_fit`, and a widened record
filter so HR-only records survive. Fixture at `tests/data/test_strength.fit`
(synthetic, written by a throwaway generator kept outside the repo, same as
`test_run.fit`); tests in `tests/test_importers.py`.

Written against the confirmed `set`-message shape in Findings.

- `import_fit` gains strength-session handling: detect the strength session on
  the session message (`sub_sport == "strength_training"`, to be confirmed
  against the fixture), and where the existing run/cycle/swim path builds a
  point stream for splits/HR-zones/power-windows, branch to a new
  `_parse_fit_sets(fitfile) -> list[dict]` that reads `set` messages instead.
- `_parse_fit_sets` filters to `set_type == "active"` (fitparse decodes this
  to the string `"active"`/`"rest"`, so the comparison is against a name, not
  an int), groups consecutive same-category sets into one `exercises` entry,
  and reads `weight` straight through as kg.
- **No `FIT_EXERCISE_CATEGORY_MAP` for `category`.** fitparse's profile already
  decodes `category` to a name via the `exercise_category` field type, and all
  four lifts in scope are in it (`bench_press`, `deadlift`, `shoulder_press`,
  `squat`), along with an `unknown` sentinel. The `FIT_SPORT_CODE_MAP`-style
  gap this plan originally assumed does not exist for this field. Handle an
  unrecognised value by falling back to `"unknown_<int>"` rather than raising
  — the raw code is kept in the name so two unmapped exercises don't merge
  into one PB line.

  **Corrected during implementation: no warning plumbing.** The plan called for
  a warning in `_strava_skip_warnings`' style, but `import_fit` returns a bare
  dict and is called from five places; changing its signature to an
  `(activity, warnings)` tuple would ripple through `cli.py`,
  `import_directory`, `import_by_extension` and `_import_strava_linked_file`
  for one edge case. The set is not dropped, so the information surfaces as
  *data* rather than a log line: an unmapped exercise gets its own row in the
  strength PB table, named `unknown_4242`. Revisit if a real fixture turns out
  to produce these routinely.
- **Where a map *is* needed: `category_subtype`.** It is a bare `uint16` that
  indexes into per-category `<category>_exercise_name` enums (e.g.
  `squat_exercise_name` 2 = `back_squats`) — this is the front-squat vs
  back-squat granularity. v1 can ignore it and key PBs on the category name
  alone; if the fixture shows it matters, add the lookup keyed by
  `(category, category_subtype)` and name it for what it resolves —
  `FIT_EXERCISE_NAME_MAP`, not a category map.
- `max_heart_rate` threading is unaffected — but "reuse the `hr_zones` path
  unchanged" needed one change to be true at all: the FIT record loop required
  a record to carry distance *or* power, and a gym session's records carry
  neither. The filter now also keeps a record carrying only a heart rate, so
  strength sessions get real HR zones. Harmless for the cardio sports
  (`_compute_splits` and `best_power_window` each filter for what they need);
  the only other effect is that HR-only records at the very start of a run,
  before GPS lock, now count toward its zones too.

### Tests

A synthetic strength FIT fixture (`tests/data/test_strength.fit`, following
`tests/data/test_run.fit`'s "synthetic, not real" convention) covering:
normal sets, an unmapped category (warning path), and rest-set filtering.

**`fit garmin-sync` now pulls real strength sessions automatically** — this
is the point at which Garmin ingestion actually lands; Phases 3–4 are about
*planning* strength, not logging it.

---

## Phase 3 — `planner.py` — **DONE**

Landed: `SPORT_TYPES["strength"]`, `WORKOUT_TYPES["strength"]`,
`STRENGTH_CATEGORIES`, `parse_exercise`/`parse_weight_kg`/`_optional_rest`,
`EXERCISE_PARAM_SPECS` + `repeated_param_specs`, the `_strength_straight_sets`
and `_strength_baseline` builders with their `_lift_step`/`_rest_step`/
`_weight_fields` helpers, the `reps`/`lap.button` branches in
`_estimate_seconds`, and lifting-aware `describe_plan` output. `cli.py` gained
`_prompt_optional_value`/`_prompt_repeated_params`. Tests in
`tests/test_planner.py`.

**No longer blocked** — the live schema is in Findings, and it is friendlier
than feared: one `RepeatGroupDTO` per exercise (the shape `planner.py` already
builds for run intervals), reps as an end condition, and a real weight field.
Build against Findings, not against `garminconnect`'s models.

- `SPORT_TYPES`: add `"strength"` as `{"sportTypeId": 5, "sportTypeKey":
  "strength_training", "displayOrder": 4}` — confirmed verbatim from the dump.
- `WORKOUT_TYPES["strength"] = ["straight_sets", "baseline"]`.
- **`straight_sets`** — the 3×10-style prescription. Params: a list of
  `{"exercise", "sets", "reps", "target_weight_kg"}` entries (multiple
  exercises per single Garmin workout, matching a real gym session — this
  differs from every existing sport/type combo, which is single-exercise by
  construction). Prompt spec needs one exercise added at a time
  (`_prompt_params` may need a small loop-until-done extension here,
  analogous to how `("run", "intervals")` loops reps but for whole
  exercises instead of a fixed field list — flag this as the one place
  `cli.py`'s prompt-spec pattern doesn't map 1:1 and needs a small bespoke
  path).
- **`baseline`** — an e1RM test: one exercise, an open (untargeted) top
  set at a prescribed rep count, mirroring the run/cycle/swim baseline
  philosophy ("the recorded activity must BE the test"). No warmup/cooldown
  concept applies the way it does for cardio; the "warmup" *is* the ramp of
  lighter sets before the test, which Phase 1's PB tracking already handles
  correctly (only the heaviest/best-e1RM set counts) — no special payload
  handling needed beyond a single untargeted `interval` step at the target
  rep count.
- **Weight goes on the step, not in a target.** Settled by the dump: set
  `weightValue` (kilograms) and `weightUnit`
  (`{"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}`) on the work step,
  and leave `targetType` as `no.target` like every other field of this shape.
  The rep count is the `endCondition` (`reps`, id 10) with the count in
  `endConditionValue`. So the watch really does display the prescribed load —
  the "informational only, carried in workout_name" fallback this plan hedged
  for is not needed.
- **Exercise identity**: `category` (uppercase, e.g. `"DEADLIFT"`) plus
  optional `exerciseName` (the variant, e.g. `"BARBELL_BACK_SQUAT"`). These
  are FIT's `category`/`category_subtype` vocabulary in caps, which is the
  same vocabulary `importers._fit_exercise_name` already stores lowercase —
  so the builder upper-cases the stored name and leaves `exerciseName` empty
  for v1, since all four lifts in scope are categories in their own right.
  Worth a `_STRENGTH_CATEGORIES` whitelist so a typo becomes a `ValueError`
  here rather than a silently blanked exercise on the watch.
- **One `RepeatGroupDTO` per exercise**, siblings in the segment — sets are
  its `numberOfIterations`, and each group holds the work step plus a `rest`
  step ending on `lap.button`. `planner.py`'s existing repeat builder covers
  this; the multi-exercise case is just several of them.
- **`_estimate_seconds`**: currently only handles `time`/`distance` end
  conditions. A reps-based end condition needs a new branch — a coarse
  per-rep time constant (e.g. ~4s/rep) plus inter-set rest, analogous to
  `_FALLBACK_SPEED_MPS`'s role for distance steps with no pace target. Name
  it `_FALLBACK_SECONDS_PER_REP` for symmetry. Phase 4 depends on this: the
  training engine sizes a week through `planner.estimate_seconds`, so a
  strength session with no duration estimate silently weighs nothing.
- `workout_params`/`workout_name`/`estimate_seconds`/`build_plan`'s existing
  generic dispatch (`_BUILDERS[(sport, workout_type)]`) needs no structural
  change — strength's builders just return a different step shape, same
  contract.

### Verification

**Done offline, and it is worth being precise about what that proves.**
`scripts/diff_workout.py` was run with `--fetched` against the workout dumped
from Connect, on a payload built with matching params. Every
strength-specific field matched: the two `RepeatGroupDTO`s and their
iterations, `category`, the `reps` end condition and its value,
`weightValue`, `weightUnit`, the `no.target` targetType, and global step
numbering across groups. The only differences were the ones that should
differ — workout name, description, `estimatedDurationInSecs` (Garmin stores
0 for strength; fit estimates its own), and a timed warmup where the dumped
workout had a press-lap one.

One convention was adopted from that diff: a `lap.button` step is sent with
`endConditionValue` 10.0, which is what Connect writes, rather than 0.

**Then verified live, 2026-09-05.** A two-exercise `straight_sets` workout
(deadlift 3x5 @ 62.5kg, squat 3x10 @ 40kg, 1:30 timed rests, 10-minute
warmup) was pushed to the account as workout 1687505906 and diffed back:
**every field fit sent survived unchanged.** What that settles:

- **`weightValue` is kilograms inbound.** 62.5 came back as 62.5 under
  `unitKey: "kilogram"`. Not grams, and not rounded to a whole number — a
  half-kilo plate survives, which matters for a progression moving in 1.25kg
  steps.
- **A timed rest is accepted.** Connect's own UI writes `lap.button`; the
  `time`-ended rest step fit builds is fine, and is the shape that lets
  `estimate_seconds` size a week honestly.
- **A time-ended `CARDIO` warmup is accepted**, again where the UI wrote
  `lap.button`.
- **A bare `category` is enough.** `exerciseName` came back unset, exactly as
  sent — no need to model the variant enums to prescribe the four lifts.
- Garmin kept fit's `estimatedDurationInSecs` (1320) rather than zeroing it,
  though it stores 0 for its own UI-built strength workouts.

`strength`/`baseline` reuses the same builders and step shapes but has not
been pushed on its own.

---

## Phase 4 — `training.py` — **DONE**

Landed: the strength progression constants and functions
(`LIFT_INCREMENT_KG`, `STRENGTH_DELOAD_FACTOR`/`STRENGTH_TAPER_FACTOR`,
`PLAUSIBLE_LIFT_E1RM_KG`, `FALLBACK_LIFT_E1RM_KG`, `working_weight_from_1rm`,
`strength_weekly_e1rm`, `reachable_e1rm`, `derive_lift_1rm`/
`derive_lift_target`), `template_lifts`/`volume_sports`, the
`targets["strength"]` shape with `_derive_lift_targets` and
`_attach_weekly_lifts`, `plan_week_roles`, `_intensity_snapshot`, the strength
branches in `_apply_target`/`retarget_sessions`, an optional `scale` on
`_session`, the `strength_program` goal plus strength sessions in both
triathlon goals, the strength benchmark, and the display side. Tests in
`tests/test_training.py`.

Three deviations from the plan below, each marked in place: the deload factor,
the benchmark's rep scheme, and how the volume measurement treats strength.

The novel part of the feature, and the part that fits the existing engine
least. Three shared structures assume one intensity value per sport, fixed for
the life of the plan; strength is four values per plan, each moving every week.
Each of the three needs a decision, not a branch.

### Target derivation — the per-sport structures are single-valued

`_DERIVERS[sport](recent)` returns one value; `derive_target` rounds it with
`int(round(...))`; `PLAUSIBLE_TARGETS[sport]` is one `(low, high)` pair;
`_SPORT_TARGETS` maps one sport to one target key. None of those survive
contact with four lifts at 2.5kg granularity — `int(round(102.5))` alone loses
a real working weight.

**Decided: strength stays out of the `derive_target` front door.**
`training.py` calls a strength-specific derivation directly, and
`PLAUSIBLE_TARGETS`, `_DERIVERS` and `derive_target`'s `int(round(...))` stay
exactly as they are for the cardio three. The cost is a small strength-shaped
twin of the plausibility guard rather than one shared one; the benefit is that
none of this touches code all three cardio sports depend on. (The alternative
considered was making all four structures exercise-keyed and giving
`derive_target` a float path — rejected for blast radius.)

Either way:

- `derive_lift_1rm(recent: list[dict], exercise: str) -> tuple[float, str] |
  None` — best `compute.estimated_1rm` for that exercise across the recent
  window, one per lift in scope. Public like
  `derive_run_5k`/`derive_swim_css`/`derive_ride_watts`, for the same reason —
  `fit plan --sport strength` should read the same derivation `fit train` uses.
- Per-exercise e1RM sanity bounds, generous, rejecting the impossible not the
  unusual — mirror the existing bounds' spirit, e.g. reject an e1RM outside
  roughly 20–400kg for these four lifts.

### Progression model — the actual new mechanism

Unlike run/cycle/swim (where volume grows toward the event and *pace is
measured, never projected*), a strength goal here is explicitly a **target
working weight** (the percentile figures from your research), not just a
volume ramp at a fixed intensity. This needs a genuinely new progression
shape:

- `strength_target_weight_for_week(current_kg, goal_kg, week, total_weeks,
  deload_weeks) -> float` — linearly interpolates from `current_kg` (this
  lift's `straight_sets` working weight, itself derived from
  `derive_lift_1rm` at a conventional %1RM for 10 reps — Epley/Brzycki both
  land near 75%) to `goal_kg` (the percentile target, supplied via
  `targets:` in the plan description, same precedence rule as every other
  sport: explicit override → derived → fallback).

  **Decided: the goal weight is derived when the description doesn't give
  one**, rather than being required. A forward-looking goal genuinely isn't
  measurable from history the way a pace is, so the derivation is a modest
  gain on current e1RM over the plan's own length — `MAX_WEEKLY_INCREMENT_KG`
  × the build weeks, which is by construction a rate the plan can actually
  deliver and therefore never trips its own warning. A `targets:` entry
  overrides it. This keeps the derive-or-fall-back precedent every other sport
  follows and lets a strength plan expand with no `targets:` block at all;
  someone chasing a specific percentile figure still supplies it.
- Clamp the **per-week increment**, not just the endpoints — a realistic
  linear-progression rate differs by lift (upper-body presses progress
  slower than deadlift/squat). Add `MAX_WEEKLY_INCREMENT_KG` per exercise
  (small, conservative constants — e.g. squat/deadlift ~2.5kg/week, bench/
  press ~1.25kg/week, in the same spirit as `VOLUME_RAMP_WARN` protecting
  against an unrealistic ask) and emit the same kind of `plan["warnings"]`
  entry `derive_volume_scale` already does when the straight-line path from
  current to goal exceeds what the clamp allows in the plan's given length
  — **this is the mechanism that answers "the 10k target seems very hard"
  for strength too**: if the gap is too large for the requested timeline,
  the plan says so explicitly rather than silently prescribing an
  unachievable curve.
- Deload weeks dip and do not advance, so progression resumes where it left
  off rather than resetting.

  **Deviation: the factor is not `RECOVERY_FACTOR`.** That constant is 0.6 and
  is a *volume* cut; taking 40% off a working weight would leave a bar so light
  it stops being training. `STRENGTH_DELOAD_FACTOR` is 0.85, with
  `STRENGTH_TAPER_FACTOR` at 0.8 for taper weeks.

### `_apply_target` is stateless, and this target is not

`_apply_target(sport, session_type, params, targets)` has no idea which week
it is filling in — deliberately, because for the cardio sports intensity is a
pure function of the target. `strength_target_weight_for_week` needs the week
index and the plan's length, and *both* `expand_plan` and `retarget_sessions`
call `_apply_target`. So this is a signature change to shared machinery, and
there are two ways to take it:

**Built as the table**, and it needed a little of both: `_attach_weekly_lifts`
bakes the week structure into `targets["strength"][lift]["by_week"]`, and
`_apply_target` gained a `week` parameter that does nothing but index it (the
session already carries `week`, so nothing new is threaded through
`_build_session`). The stateless property survives — for every other sport the
parameter is ignored — and `retarget_sessions` rebuilds the table through
`plan_week_roles`, since it depends on the plan's length as well as its
targets.

Also: `_INTENSITY_PARAMS = ("target_pace", "target_watts", "target_pace_100m")`
must gain `"target_weight_kg"`, or `retarget_sessions`' change detection
compares an unchanged tuple and silently reports every strength session as
unchanged.

### Volume and weight are two ramps, and only one may apply

Every template session takes a **required** `scale` positional (`_session`) and
grows by `week_multiplier × volume_scale`. Strength progression is weight — an
intensity axis on its own independent line. If a strength session also carries
a volume scale on sets or reps, the plan ramps volume and intensity
simultaneously, which is precisely what linear progression does not do: you
hold 3×10 and add weight.

So **strength sessions carry no `scale`**, like benchmark sessions. That is an
engine change, not a template detail:

- `_session`'s `scale` becomes optional (or takes an explicit empty scale) —
  it is currently positional and required.
- `test_scaled_params_keep_headroom_inside_their_clamps` iterates every goal;
  scale-less strength sessions need the same exemption benchmarks already have.
- `_week_seconds` sizes a week from `planner.estimate_seconds`, which for
  strength depends on Phase 3's `_FALLBACK_SECONDS_PER_REP` branch. Until that
  exists, a strength session contributes zero seconds and silently skews
  `derive_volume_scale`.

### Goal templates

Two changes, not one new template:

1. **Extend `sprint_triathlon`/`standard_triathlon`** to place real
   `straight_sets` sessions (2–3/week) where `extras: {strength: N}`
   currently places an unstructured block. Keep `extras.strength` parsing for
   backward compatibility so existing saved plans still load.

   **This is not behaviour-preserving for new plans, and the doc should not
   pretend otherwise.** Adding real sessions changes `template_week_seconds`,
   which is what `derive_volume_scale` measures the user's recent training
   against — so a new triathlon plan built by someone with no imported strength
   history (i.e. everyone, before Phase 2 has had time to collect any) measures
   itself against a bigger template week and scales *down*. Two things follow:
   **Deviation, and it resolves the concern rather than absorbing it:**
   strength is excluded from the volume measurement entirely.
   `volume_sports` drops it and `_week_seconds` counts only sessions that
   carry a `scale`. A strength session's size never moves, so including it put
   time into the ratio that the multiplier could never adjust — a triathlete
   who does no gym work would have been scaled down for the swimming. New
   triathlon plans therefore measure exactly what they did before.

   Still true: a plan carrying both legacy `extras.strength` entries and real
   strength sessions double-counts, and the extras show as permanently undone
   since `match_completion` never matches them. Drop `extras: {strength: N}`
   from a description once the goal schedules real sessions.
2. **A pure-strength goal is still not a triathlon goal** — for a plan whose
   *only* aim is the four lifts (no swim/bike/run demanded), add a small
   `strength_program` template: a flat N-week linear block per lift toward its
   `targets:` goal weight, `days_per_week` covering however many lift days fit
   wants. This is the one genuinely new template; everything else above is
   extending existing ones.

   It still has to satisfy the phase machinery every template feeds:
   `_assign_phases` needs at least one phase and `taper_weeks` may be 0;
   `derive_weekly_ramp` guards `builds <= 1` already. With no scaled params the
   volume multiplier has nothing to act on, which is the intended outcome —
   but say so in the template's comment, because a template whose ramp does
   nothing looks like a bug otherwise.

### Benchmark rotation

Strength is in `BENCHMARK_SESSIONS`, replacing a week's `straight_sets` session
with a `baseline` on the same turn-taking rotation, same recovery-week-only
placement, same "unscaled and untargeted" exemption (which strength sessions
have anyway, per above). `"straight_sets"` had to join the list of session
types a benchmark may stand in for.

**Deviation: it is a heavy triple, not a one-rep max**, and the lift is taken
from the session being replaced rather than fixed per sport — a plan that
squats and benches tests whichever its week leads with, so `_build_benchmark`
gained a `replaced` argument. `compute.estimated_1rm` reads a 3RM perfectly
well, and a plan should not send anybody to a genuine single alone in a gym
every few weeks.

### Retargeting

`retarget_sessions` needs a strength-aware branch: re-deriving a strength
target isn't "recompute intensity from a fixed %", it's "recompute where
this week should sit on the current→goal line using the *new* current
figure from the latest e1RM test" — i.e. rebuilding the per-week weight table
from an updated `current_kg` and the same `goal_kg`, then refilling the
sessions still ahead.

**Weight is intensity, so this does not violate "intensity only, never
volume".** Worth saying explicitly, because rewriting a prescribed weight
looks like a volume change if you come at it from the cardio sports: sets and
reps are the volume axis here and retargeting must leave them alone, exactly
as `_INTENSITY_PARAMS` enforces for the other three sports. Frozen/extras/past
exclusions all carry over unchanged.

### Tests

Progression math (interpolation, clamping, deload dip, warning threshold);
goal template session generation for both the extended triathlon templates
and the new `strength_program`; benchmark rotation including strength;
retargeting recomputing correctly off an updated current-weight reading, and
leaving sets/reps untouched while doing it.

---

## Phase 5 — `display.py` / `cli.py` — **DONE, and mostly empty**

It turned out to need almost nothing:

- `render_training_plan` needed **no** strength branch. `describe_session`
  already reads `workout_name`, and `planner.workout_name` composes
  "Strength squat 3x5 @ 87.5kg" for a strength session like any other.
- The multi-exercise prompt loop landed in Phase 3, where it was needed.
- The dashboard and PB table were done in Phase 1.

What was actually added: `display._format_lift_target` (each lift shown as a
current → goal e1RM pair in `fit train show`'s header) and strength lines in
`render_training_retargeted`, which report the *measured* figure moving —
quoting the goal would hide the only thing a re-test tells you.

The original plan for this phase follows.

## Phase 5 (as planned) — `display.py` / `cli.py`

- `render_training_plan`: a strength session's row needs its own
  description format ("Squat 3x10 @ 102.5kg") — reuse `training.
  describe_session`'s existing per-sport dispatch, add a strength branch.
- `render_plan_recommendations`/prompt flow: `fit plan --sport strength
  --type straight_sets` needs the multi-exercise prompt loop from Phase 3
  wired through `cli._prompt_params` (or its bespoke strength-specific
  sibling, per Phase 3's note).
- No dashboard/PB-table changes beyond what Phase 1 already covers.

---

## Suggested build order (recap)

1. ~~Phase 1 (shape + PBs)~~ — **done**.
2. ~~Phase 2 (FIT import)~~ — **done**. `fit garmin-sync` now pulls gym
   sessions; they appear in the history table, calendar, sports summary,
   fitness index and their own strength PB table.
3. ~~Phase 3 (planner payloads)~~ — **done and verified against a live
   upload** (2026-09-05). `fit plan --sport strength --type
   straight_sets|baseline` builds and pushes a real strength workout.
4. ~~Phase 4 (training/progression)~~ — **done**. `strength_program`, strength
   in both triathlon goals, the load progression, benchmarks and retargeting.
5. ~~Phase 5 (display polish)~~ — **done**, and it needed almost nothing.

Phase 3's payload builder must not be written until the live dump is recorded
in Findings; Phase 2 should not merge without a real fixture.

**Sequencing:** this was going to be a decision — whether to invert 3 and 4
if the schema dump stalled — but it didn't stall. Phases 3 and 4 can now run
in their stated order, and Phase 4's `_week_seconds` sizing gets Phase 3's
reps branch for free by doing so.

---

## Open questions / risks

- ~~**Does Garmin's workout schema support a literal weight target at all?**~~
  — settled twice over: **yes**, as `weightValue`/`weightUnit` on the work
  step with `targetType` left at `no.target` (Findings), and a live push
  confirmed `weightValue` is read as **kilograms** inbound, halves included
  (Phase 3's Verification). The `factor: 1000.0` is metadata; do not
  pre-apply it.
- ~~**Units on the import side**~~ — closed, not investigated: this is a
  kg-only setup, the watch is never set to lb, and fitparse scales `weight`
  to kg anyway. `weight_display_unit` is read by nothing. Revisit only if a
  file from someone else's watch ever needs importing.
- **Exercise granularity.** `category` gives four lifts by name for free;
  the variant (`category_subtype` in FIT, `exerciseName` in the payload)
  distinguishes back squat from front squat. v1 keys PBs on the category and
  may therefore merge two different squats into one PB line. Acceptable, and
  reversible — but it is a data decision, not just a display one, since
  `pbs.json` is keyed by whatever name is chosen. Note both sides of the app
  now use the same vocabulary, so raising the granularity means changing
  import and payload together.
- **Exercise catalog completeness.** Four lifts in scope; anything logged
  outside that set (accessory work in the same session) should map through
  `exercise_category`'s own name where it decodes and to `"unknown"` where it
  doesn't, and be preserved either way, not dropped — so a session's total
  volume/MET load stays accurate even for exercises this feature doesn't
  specially track.
- ~~**`targets:` becoming required for strength**~~ — settled: it is not
  required. The goal weight derives from current e1RM plus what the plan's
  length can honestly deliver, and `targets:` overrides. See Phase 4.
- **The fitness index moves.** Once strength sessions carry MET load, the
  headline number on `fit dashboard` reflects gym work it previously ignored.
  Correct, but it makes fitness values from before and after the change not
  strictly comparable — the same going-forward-only rule `hr_zones` and
  `best_power` already follow, and worth one line in the eventual commit
  message.

---

## Findings

### Garmin Connect strength schema — `garminconnect` 0.3.6 `workout.py`

*Superseded by the live dump below, which is the authority. Kept because it
explains why the usual replicate-from-reference method could not be used.*

Read from `.venv/lib/python3.14/site-packages/garminconnect/workout.py` (417
lines, `garminconnect-0.3.6.dist-info`).

**It models nothing strength-specific.** What it does give:

- `SportType.STRENGTH_TRAINING = 5` — the sport id, confirmed.
- `ConditionType.REPS = 10` and `ConditionType.FIXED_REPETITION = 9` — a
  rep-count end condition exists (`LAP_BUTTON 1, TIME 2, DISTANCE 3,
  CALORIES 4, POWER 5, HEART_RATE 6, ITERATIONS 7, FIXED_REST 8,
  FIXED_REPETITION 9, REPS 10`).
- `StepType` adds `OTHER = 7` and `MAIN = 8` beyond the six fit already uses.

What it does **not** give, and what therefore cannot be replicated from it the
way the cardio payloads were:

- **No exercise identity anywhere.** `ExecutableStep` has
  `stepOrder, stepType, endCondition, endConditionValue, targetType,
  strokeType, equipmentType, childStepId` and `model_config =
  ConfigDict(extra="allow")` — no exercise name, no exercise category. The
  `extra="allow"` is the tell: the library passes through fields it doesn't
  model, so the real API very likely carries them.
- **No weight target.** `TargetType` is `NO_TARGET 1, POWER_ZONE 2, CADENCE 3,
  HEART_RATE_ZONE 4, SPEED_ZONE 5, PACE_ZONE 6, GRADE 7, HEART_RATE_LAP 8,
  POWER_LAP 9, RESISTANCE 15`. `RESISTANCE` is the only near-miss and is
  almost certainly for indoor bike/rower resistance, not barbell load.
- No strength counterpart to `RunningWorkout`/`CyclingWorkout`. The client's
  convenience uploaders tell the same story: `upload_running_workout`,
  `upload_cycling_workout`, `upload_swimming_workout`, `upload_walking_workout`
  and `upload_hiking_workout` exist; there is no strength one. (`upload_workout`
  takes a raw payload, which is what fit uses anyway.)

**Conclusion: this file's silence is not an answer.** Get the live dump (Phase
0) before writing `_strength_*` builders.

### FIT `set` message — `fitparse` 1.2.0 bundled profile

Read from the bundled profile (`fitparse-1.2.0.dist-info`), message number
**225**, name `set`. This answers everything except which fields Garmin
actually populates.

| Field | def_num | Type | Scale / units |
|---|---|---|---|
| `timestamp` | 254 | `date_time` | — |
| `duration` | 0 | uint32 | scale 1000, **seconds** |
| `repetitions` | 3 | uint16 | — |
| `weight` | 4 | uint16 | scale 16, **kg** |
| `set_type` | 5 | `set_type` | — |
| `start_time` | 6 | `date_time` | — |
| `category` | 7 | `exercise_category` | — |
| `category_subtype` | 8 | uint16 | — |
| `weight_display_unit` | 9 | `fit_base_unit` | — |
| `message_index` | 10 | `message_index` | — |
| `wkt_step_index` | 11 | `message_index` | — |

Decoded enums, all already in fitparse's profile:

- **`set_type`**: `{0: 'rest', 1: 'active'}` — compare against the string
  `"active"`, not an int.
- **`exercise_category`**: decodes to names, and **all four lifts in scope are
  present**: `bench_press = 0`, `deadlift = 8`, `shoulder_press = 24`,
  `squat = 28`, plus `unknown = 65534`. (Also `calf_raise, cardio, carry, chop,
  core, crunch, curl, flye, hip_raise, hip_stability, hip_swing,
  hyperextension, lateral_raise, leg_curl, leg_raise, lunge, olympic_lift,
  plank, …` — a full catalog.) **No `FIT_SPORT_CODE_MAP`-style gap here.**
- **`weight`**: scale 16 with units kg, so fitparse returns **kilograms
  already**. `weight_display_unit` (`{0: 'other', 1: 'kilogram', 2: 'pound'}`)
  is a display preference, not a unit the value arrives in — a lb→kg
  conversion at import would be a no-op. Not pursued further: kg-only setup.
- **`category_subtype`** is an undecoded `uint16`. It indexes into the
  per-category `<category>_exercise_name` enums, of which the profile carries
  33 (`bench_press_exercise_name`, `squat_exercise_name`, …) — e.g.
  `squat_exercise_name` `0: leg_press, 1: back_squat_with_body_bar,
  2: back_squats, 3: weighted_back_squats, 4: balancing_squat, …`. **This** is
  where a lookup map is needed if exercise-level (not category-level)
  granularity is wanted.

**Session sport/sub_sport, confirmed:** a gym session is sport 10
(`"training"`) with sub_sport 20 (`"strength_training"`), both decoded to
names by fitparse. Sport 10 is *not* in `FIT_SPORT_MAP`, so the strength check
has to come first in `import_fit` or a gym session silently imports as a run.

**Still needs a real fixture:** which of these fields Garmin actually
populates, and whether `category` arrives as a scalar or a list (the FIT SDK
defines it as an array field — `_fit_exercise_name` handles both). The
synthetic `tests/data/test_strength.fit` exercises the parser but cannot
answer what a watch really writes.

### Live Garmin strength workout dump

Gathered 2026-09-05 from a two-exercise workout built by hand in Connect
(workout 1687491618, "Strength Workout"), via `scripts/dump_workout.py`.
**This is the schema Phase 3 builds against**, and it settles both open
questions: an exercise *is* named on the step, and a weight target *does*
exist.

**Sport.** `"sportType": {"sportTypeId": 5, "sportTypeKey":
"strength_training", "displayOrder": 4}` — on the workout and on each segment,
same as the cardio sports.

**Structure: one RepeatGroupDTO per exercise**, siblings in the segment's
`workoutSteps`. So a multi-exercise session is not a new shape at all — it is
the repeat machinery `planner.py` already builds for run intervals, repeated.
The observed workout was: warmup step, then `3 × (deadlift + rest)`, then
`3 × (back squat + rest)`.

```python
{"type": "RepeatGroupDTO", "stepOrder": 5, "childStepId": 2,
 "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
 "numberOfIterations": 3,
 "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations",
                  "displayOrder": 7, "displayable": False},
 "endConditionValue": 3.0,
 "smartRepeat": False, "skipLastRestStep": False,
 "workoutSteps": [ <work step>, <rest step> ]}
```

**The work step — exercise identity and weight.**

```python
{"type": "ExecutableStepDTO", "stepOrder": 6, "childStepId": 2,
 "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
 "category": "SQUAT",                    # uppercase; "" is not used, CARDIO for a warmup
 "exerciseName": "BARBELL_BACK_SQUAT",   # the variant; "" when only a category was picked
 "weightValue": 50.0,
 "weightUnit": {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0},
 "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps",
                  "displayOrder": 10, "displayable": True},
 "endConditionValue": 10.0,              # reps
 "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target",
                "displayOrder": 1},
 "equipmentType": {"equipmentTypeId": 0, "displayOrder": 0},
 "strokeType": {"strokeTypeId": 0, "displayOrder": 0}}
```

Four things follow directly:

1. **Weight is not a `targetType`.** `targetType` stays `no.target`; the load
   rides on its own `weightValue` + `weightUnit` pair. This is why
   `garminconnect`'s `TargetType` listing no weight member was a red herring —
   the answer to "does a weight target exist" is **yes**, just not as a target.
   `weightValue` is in kilograms (a 50kg squat stored as `50.0`); `factor`
   1000.0 is unit metadata, not a multiplier to pre-apply — but confirm that
   on the first round-trip, since sending grams would be a silent 1000×.
2. **`category` + `exerciseName` mirror FIT's `category` + `category_subtype`
   exactly**, uppercased. `SQUAT`/`DEADLIFT` are the FIT `exercise_category`
   names in caps, and `BARBELL_BACK_SQUAT` is a `squat_exercise_name` variant.
   So the import side and the plan side can share one vocabulary: fit stores
   the lowercase FIT name, and the payload builder upper-cases it.
   `exerciseName` may be `""` — Connect accepts a bare category — which is the
   easy path for v1, since the four lifts in scope are all categories in their
   own right.
3. **Reps are an end condition**, `conditionTypeId` 10 / `"reps"`, with the rep
   count in `endConditionValue`. Confirms `ConditionType.REPS = 10`, and
   confirms `planner._estimate_seconds` needs the reps branch (Garmin returned
   `estimatedDurationInSecs: 0` for the whole workout — it does not estimate
   strength for you).
4. **Rest between sets is a step, not a recovery**: `stepTypeId` 5 / `"rest"`,
   ending on `lap.button` (`conditionTypeId` 1) rather than a duration —
   press-lap-when-ready, which is what a gym rest actually is. A timed rest
   presumably uses `time` (2) instead; unverified. Note `endConditionValue` is
   `10.0` on the lap.button steps too, and looks inert.

Also worth knowing: **the warmup step carries `"category": "CARDIO"`** with an
empty `exerciseName`, so every step in a strength workout has a category.

**Still unverified:** what Connect accepts on the way *in* (this is what it
returns), whether an unrecognised `category`/`exerciseName` is rejected or
silently blanked, and the timed-rest variant. `scripts/diff_workout.py` on the
first strength push answers the first two.
