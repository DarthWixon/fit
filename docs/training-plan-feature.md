# Long-term training plans (`fit train`) — implementation plan + reference

> **Status:** IMPLEMENTED 2026-08-24 — all eight goal templates. This document
> is kept as the *design* record and reconnaissance appendix; for what the code
> actually does now, read CLAUDE.md's "Training plans" section. Three places the
> build knowingly departed from this plan, each documented there: session sizing
> uses the weekly multiplier alone rather than multiplier + a separate per-week
> growth increment (which compounds into nonsense over 12+ weeks); template
> session priorities interleave the sports so trimming a week never drops a whole
> discipline; and `fit train sync` confirms before pushing (added after an
> accidental 14-workout push during development).
>
> Original status line: APPROVED DESIGN, NOT YET IMPLEMENTED. This document is the durable,
> in-repo record of the `fit train` feature design (moved here from a planning-session
> scratch file so a future session can find it). It is self-contained: full design
> decisions AND a reconnaissance appendix (exact `file:line` anchors, signatures, data
> shapes) so implementation needs no re-exploration. Run tests with `.venv/bin/pytest`;
> format with `.venv/bin/isort src tests && .venv/bin/black src tests` (a pre-commit
> hook enforces this — see `hooks/pre-commit`). The `file:line` anchors were accurate
> when written (branch `main`, just after the canoe + `--schedule` work) — **re-verify
> them before relying on them; the code may have drifted.**

---

## Context

Today `fit plan` builds and schedules **one** workout. The user wants a **multi-week
periodised training plan** driving toward a goal event, built on the scheduling seam
already shipped (`garmin.schedule_workout` + `planner.parse_schedule_date`). Workflow:
a user chats with an external AI bot to decide goals; the bot emits a compact **plan
description** in a standard YAML form defined here; `fit` ingests it, **expands it into
a full periodised schedule**, derives paces from history, builds a real Garmin workout
per session, and rolls sessions onto the Garmin calendar. Extra activities
(yoga/strength) are tracked in fit only. This is the deferred "Stretch feature"
(`CLAUDE.md:884-897`), to be built *on top of* the atomic scheduling seam, never
replacing it.

## Decisions locked in (from 4 rounds of design Q&A with the user)
| Axis | Choice | Notes |
|---|---|---|
| Intelligence | **fit is the periodisation engine** | description is high-level, fit expands |
| Bot/fit boundary | Description = goal + prefs; **fit owns per-goal templates** | thinnest description |
| Session spec | High-level intent; fit derives concrete params | reuse history derivation |
| Progression | **Build/recover cycles (3:1 default) + weekly ramp + taper** | overridable in description |
| Intensities | **History-derived** (reuse `recommend_defaults`) | optional per-sport goal override |
| Workout coverage | **Extend planner** with steady/long/endurance/continuous types | all pushed as real workouts |
| Steady targets | **Distance where natural** (long by km, easy by time) | easy target bands from history |
| Yoga/strength | **Local to fit only** | Garmin API has NO calendar-note endpoint (confirmed) |
| Rollout | **Rolling window** sync (default 14 days), idempotent | re-run to advance |
| `show` | **Plan + completion** | mark planned vs done against `activities/` |
| Format | **YAML** | add PyYAML as opt-in `[train]` extra, lazily imported |
| Naming | **`fit train`** | subcommands `import` / `show` / `sync` / `clear` |
| Concurrency | **One active plan** | `~/.fit/train/plan.json` |
| Extras placement | Description gives counts; **fit auto-places** | on easy/rest days |
| Initial scope | **All 8 goal templates** in the first pass | |

## The 8 goals
`run_5k`, `run_10k`, `run_half`, `cycle_25k_tt`, `cycle_40k_tt`, `cycle_100k_sportive`,
`sprint_triathlon`, `standard_triathlon`.

---

## The standard form (YAML description)

Contract between the AI bot and fit. Only `goal` + `event_date` required; the rest
defaults from the goal template.

```yaml
goal: sprint_triathlon        # one of the 8 goal ids
event_date: 2026-06-14        # anchor; the plan counts back from here
start_date: 2026-03-23        # optional; default = event_date - template length
days_per_week: 6              # optional; default per template
rest_day: Mon                 # optional preferred rest day
extras: {strength: 2, yoga: 1}   # optional weekly counts; fit auto-places, never pushes
targets:                      # optional per-sport goal overrides (else history-derived)
  run_5k: "24:00"
  bike_ftp: 250
  swim_css_100m: "1:45"
progression:                  # optional overrides of template defaults
  build_recover: [3, 1]
  weekly_ramp_pct: 8
  taper_weeks: 2
```

---

## Architecture (respecting the module boundaries — see Appendix B)

New **pure** module `src/fit/training.py` (may import `compute` + `planner`, pure→pure;
no I/O, no network, no typer) is the engine. `cli.py` reads the description file text
and hands it in, keeping `training.py` pure (same split as cli reading paths for
importers).

### `training.py` (NEW, pure)
- `parse_plan_spec(text: str) -> dict` — lazy `import yaml` (raise a
  `garmin.INSTALL_HINT`-style message if PyYAML missing), `yaml.safe_load`, validate
  (`goal` in `GOAL_TEMPLATES`; `event_date`/`start_date` via
  `planner.parse_schedule_date`; field types), normalise defaults from the goal
  template. Raise `ValueError` with a clear message on bad input.
- `GOAL_TEMPLATES: dict` — one profile per goal (plain dict data, no classes): plan
  length (weeks), phase structure (base/build/peak/taper lengths), weekly session mix
  per sport, which sessions are quality vs easy/long, long-session start distance +
  weekly growth, taper shape, default `days_per_week`. Standard endurance-coaching
  defaults; all tunable constants. This is the training-domain content.
- `expand_plan(spec, activities, reference: date) -> dict` — the engine. Returns the
  full plan dict (metadata + flat list of dated **session intents**). Steps:
  1. Weeks = whole ISO weeks from `start_date` (or `event_date − template length`) to
     `event_date`; reuse `compute._week_start` + `timedelta(weeks=…)` (promote
     `_week_start` to public or re-expose).
  2. Assign each week a phase + build/recover role + a volume multiplier (ramp
     `weekly_ramp_pct` within build blocks, recovery weeks −~40%, taper down over the
     final `taper_weeks`).
  3. Lay the template's weekly session mix onto weekdays (long ride/run on the
     weekend, quality mid-week, easy fillers; respect `rest_day`); auto-place `extras`
     on easy/rest days, avoiding quality/long days.
  4. Scale each session's duration/distance by the week multiplier; grow long sessions
     across weeks per the template.
  5. Derive intensities from history — reuse `planner._recent_run_5k` /
     `_recent_swim_css` / `_recent_ride_watts` (**promote these to public**, e.g.
     `planner.derive_run_5k(activities, reference)` etc.) unless `targets` overrides
     that sport.
  Session intent shape: `{date, week, phase, sport, session_type, distance_km OR
  duration_s, target, is_extra}` + empty sync-state fields (see plan file shape).
- `session_to_build_args(session) -> tuple[str, str, dict] | None` — map a session
  intent to `planner.build_plan(sport, workout_type, params, created)` inputs; return
  `None` for extras (never built/pushed).
- `match_completion(sessions, activities) -> list[dict]` — pure: mark each non-extra
  session done if an `activities/` entry matches (same sport, date within ±1 day). Use
  `.get()` for all activity access.

### `planner.py` (EXTEND — see Appendix A for its current shape)
- New `WORKOUT_TYPES` entries: run `easy`, `long`; cycle `endurance`, `long`; swim
  `continuous`. Corresponding `_PARAM_SPECS` + `_BUILDERS`: a single duration step
  (easy, by time) or single distance step (long, by km) carrying an **easy pace/power
  target band** derived from history (e.g. easy run ≈ 5k pace × ~1.3; endurance bike ≈
  ~65-75% FTP). Reuse `_step` (`planner.py:469`), `_pace_target` (`:445`),
  `_power_target` (`:457`), `_estimate_seconds` (`:643`). Add `_FALLBACK_SPEED_MPS`
  entries if any new distance step lacks a pace target.
- Bricks (triathlon): represent as **two same-day sessions** (bike then run) tagged
  `brick` — NOT a Garmin multisport workout (that schema is unverified; deferred).
- Promote the three history-derivation helpers (`_recent_run_5k` `:767`,
  `_recent_swim_css` `:793`, `_recent_ride_watts` `:827`) to public functions so
  `training.py` reuses them (don't duplicate the derivation logic).

### `garmin.py` (EXTEND — two thin wrappers, mirroring `schedule_workout:84`)
- `get_scheduled_workouts(client, year, month) -> dict` — over
  `client.get_scheduled_workouts(year, month)`.
- `unschedule_workout(client, scheduled_workout_id)` — over
  `client.unschedule_workout(scheduled_workout_id)`.
- (Both confirmed to exist in installed garminconnect 0.3.6 — see Appendix C.)

### `storage.py` (EXTEND — mirror `write_plan:159`/`read_plans:163`)
- `train_dir() -> Path` (built on `resolve_data_dir()`); `ensure_data_dir()` creates it.
- `write_training_plan(plan)` → `train/plan.json` via `_write_json_atomic`.
- `read_training_plan() -> dict | None`.
- Only add these once the cli callers exist (per `CLAUDE.md:171-173` — no speculative
  helpers).

### `display.py` (EXTEND — render-only, receives pre-computed rows)
- `render_training_plan(weeks)` — per-week table: Week N (phase) → each session's date,
  sport, description (from a step-line helper / `planner.describe_plan:724`), and
  `✓`/`–` completion mark; header with goal, event date, weeks-to-go.
- `render_training_synced(summary)` / `render_training_cleared(summary)` — one-line
  confirmations (mirror `render_plan_scheduled:489`).
- No computation here — the row data comes from `training.py`/`compute.py`.

### `cli.py` — a `train` sub-Typer via `app.add_typer(...)`, thin subcommands
- `train import <path>` — `Path(path).read_text()` → `training.parse_plan_spec` →
  `training.expand_plan` (with `_load_activities()`, `date_cls.today()`) →
  `storage.write_training_plan` → `display.render_training_plan`. If replacing a plan
  that still has future scheduled sessions, warn and suggest `fit train clear` first
  (clearing needs Garmin; import may be offline).
- `train show` — read plan + activities → `training.match_completion` →
  `display.render_training_plan`.
- `train sync` — `garmin.login()`; for each planned, non-extra session dated within
  `[today, today + train_sync_window_days]` with `status == "planned"`:
  `planner.build_plan` → `garmin.push_workout` → `garmin.schedule_workout(date)`,
  storing `garmin_workout_id`, `scheduled_workout_id` (from the schedule response),
  `scheduled_date`, `status="scheduled"`; skip already-scheduled (idempotent); rewrite
  plan after each (save-before-crash, like `cli.plan`); render summary. Extras never
  pushed.
- `train clear` — `garmin.login()`; `garmin.unschedule_workout` each future
  `status=="scheduled"` session; reset its sync state; rewrite plan.

### `config` (one functional key — see Appendix B §2)
- Add `train_sync_window_days` (int, default 14) to `DEFAULTS` (`storage.py:43`) and a
  matching `_CONFIG_COMMENTS` (`storage.py:56`) entry. Behavioural (controls the
  rolling-sync horizon), not cosmetic — satisfies the user's functional-config rule.

### Dependency
- Add `pyyaml` as an **optional extra** `[train]` in `pyproject.toml`; `import yaml`
  lazily inside `training.parse_plan_spec` with an install hint (mirrors the
  `garminconnect` lazy pattern, `garmin.py:30`). `show`/`sync`/`clear` read the stored
  JSON and need NO PyYAML — only `import` parses YAML. A deliberate, user-approved
  exception to the minimal-deps rule; document it in CLAUDE.md.

---

## Plan file shape (`~/.fit/train/plan.json`)
```python
{
  "goal": "sprint_triathlon",
  "event_date": "2026-06-14",
  "created": "2026-08-24T09:15:02",
  "spec": {...},                      # normalised description, for re-expansion
  "sessions": [
    {"date": "2026-03-24", "week": 1, "phase": "base", "sport": "run",
     "session_type": "easy", "duration_s": 2400, "target": "5:45-6:15/km",
     "is_extra": false,
     "garmin_workout_id": null, "scheduled_workout_id": null,
     "scheduled_date": null, "status": "planned"},
    {"date": "2026-03-25", "week": 1, "phase": "base", "sport": "strength",
     "session_type": "strength", "duration_s": 1800, "is_extra": true,
     "status": "planned"}
  ]
}
```

---

## Verification
1. **Unit tests (pure, minimal — subtle engine math only)** in new
   `tests/test_training.py` (style: `tmp_path` + `monkeypatch.setenv("FIT_DATA_DIR",
   ...)`, parametrised accept/reject like `tests/test_planner.py:18`):
   `parse_plan_spec` accept/reject; `expand_plan` for a representative goal asserts week
   count, phase + build/recover pattern, taper, per-sport session counts, extras land on
   non-key days, long-session distance grows, targets come from history and honour
   overrides; `match_completion` correctness; planner new-builder payload shape;
   `session_to_build_args` mapping. Run `.venv/bin/pytest` after each pure-module edit
   (`compute`/`storage`/`importers`/`planner`/`training`).
2. **End-to-end offline** (no Garmin): write a sample YAML per goal to scratchpad;
   `FIT_DATA_DIR=<tmp> .venv/bin/fit train import sample.yaml` then `fit train show`; add
   a synthetic completed activity JSON and confirm the `✓` appears; confirm extras show
   but carry no Garmin build.
3. **Live half (user-run, real Garmin/calendar)**: `fit train sync` schedules the next
   window; verify on Garmin Connect; `fit train clear` removes them. Use the existing
   `scripts/diff_workout.py` (built earlier) to verify the **new steady/long payload
   schemas** round-trip — extends the 2026-08-24 verification to the new workout types.

## Phasing within this build
Engine + storage + `import`/`show` (fully offline-testable) first, then the planner
steady/long builders, then `sync`/`clear` (needs Garmin). All 8 goal templates are data
in `GOAL_TEMPLATES`; the pipeline is written once and every goal flows through it.

## Explicitly out of scope (v1)
- Garmin multisport/brick single-workout files (bricks = two same-day sessions instead).
- Adaptive reflow on missed sessions (`show` reports adherence but doesn't rewrite).
- Multiple concurrent plans; pushing yoga/strength to the watch; a FIT-file USB path.

---
---

# APPENDIX A — Existing workout-generation & scheduling machinery (reference)

All paths under `src/fit/`. Anchors are `file:line` as of the planning session (branch
`main`, just after the canoe + `--schedule` work). Verify line numbers on resume — they
may have drifted.

## `planner.py` (pure; no I/O, no network; may import `compute`)
Docstring `:1-21` records the payload schema is *replicated* from garminconnect 0.3.6's
`workout.py` (not imported, to avoid its pydantic extra), and that a live `run`/
`intervals` round-trip on 2026-08-24 verified target-value field names + step numbering.

**Constants:**
- `SPORT_TYPES` `:28-32`: `{"run": {sportTypeId:1, sportTypeKey:"running", displayOrder:1},
  "cycle": {2,"cycling",2}, "swim": {4,"swimming",3}}`. (Internal keys run/cycle/swim;
  Garmin keys running/cycling/swimming.)
- `WORKOUT_TYPES` `:34-38`: `run:[intervals,tempo,hills,baseline]`, `swim:[intervals]`,
  `cycle:[intervals,hills,baseline]`.
- Tolerances `:42-44`: `PACE_TOLERANCE_S_PER_KM=10`, `SWIM_PACE_TOLERANCE_S_PER_100M=5`,
  `POWER_TOLERANCE_W=10`.
- `RECENT_MONTHS=6`, `TEMPO_FACTOR=1.07`, `REPS_CAP=10` `:50-52`; `SHORT_REP_MAX_M=1000`,
  `SHORT_REP_FACTOR=0.97` `:59-60`.
- `_FALLBACK_SPEED_MPS` `:64`: `{"run":1000/360, "swim":100/120, "cycle":25/3.6}`.
- Id maps `:66-75`: `_STEP_TYPES={warmup:1,cooldown:2,interval:3,recovery:4,rest:5,
  repeat:6}`; `_END_CONDITIONS={"lap.button":1,time:2,distance:3,iterations:7}`;
  `_TARGET_TYPES={"no.target":1,"power.zone":2,"pace.zone":6}`.

**Parsers:** `parse_pace(text)->int` `:78` (strict m:ss→sec); `parse_duration(text)->int`
`:90` (digits sec, or m:ss); `parse_schedule_date(text)->str` `:102-117` (validates
`YYYY-MM-DD`, returns normalised, raises ValueError; the pure seam the multi-week
scheduler routes each date through — no past/future policy); `_positive_int` `:120`;
`_optional_pace` `:127`; `pace_zone_mps(sec_per_km,tol)->(low,high)` `:131`;
`swim_pace_zone_mps(sec_per_100m,tol)` `:140`.

**`workout_params(sport, workout_type)->list[dict]` `:401-412`:** returns a COPY of the
spec list from `_PARAM_SPECS` `:153-398`; raises `ValueError` listing valid combos —
the single `--sport`/`--type` validation point. Spec dict:
`{"key":str,"label":str,"default":int|str,"parse":callable}`; a `"derive"` callable may
replace `"default"` at runtime (injected by `recommend_defaults`/cli). The 8 spec lists
(keyed by `(sport,type)`): `(run,intervals)` `:154` warmup_minutes/reps/rep_distance_m/
target_pace/recovery/cooldown_minutes; `(run,tempo)` `:192`; `(run,hills)` `:218`;
`(run,baseline)` `:250`; `(swim,intervals)` `:270`; `(cycle,intervals)` `:308`;
`(cycle,hills)` `:346`; `(cycle,baseline)` `:378`.

**Payload builders `:415-689`:** `_step_type` `:418`, `_end_condition` `:426`,
`_no_target` `:435`, `_pace_target(low,high)` `:445` (emits targetValueOne=low,
targetValueTwo=high), `_power_target(watts,tol)` `:457`, `_step(order,step_key,end_key,
end_value,target)` `:469` → `{"type":"ExecutableStepDTO","stepOrder","stepType",
"endCondition","endConditionValue":float,**target}`, `_repeat(order,iterations,steps)`
`:482` → RepeatGroupDTO with nested `workoutSteps`. Per-combo builders return
`(name,steps)` with GLOBAL step numbering (warmup=1, repeat=2, interval=3,
recovery/rest=4, cooldown=5): `_run_intervals` `:505`, `_run_tempo` `:531`,
`_hills(word,params)` `:547` (run+cycle via lambdas, NO pace target),
`_run_baseline` `:566`, `_swim_intervals` `:577` (optional pace target),
`_cycle_intervals` `:598` (power target), `_cycle_baseline` `:621`. `_BUILDERS` dispatch
`:631-640`. Helpers `_format_mmss` `:495`, `_format_meters` `:500`,
`_estimate_seconds(steps,sport)` `:643-658` (recursive; distance steps ÷ pace-midpoint
or `_FALLBACK_SPEED_MPS[sport]`).

**`build_plan(sport,workout_type,params,created)->dict` `:661-689`:** re-validates via
`workout_params`, builds `(name,steps)`, then:
```
payload = {"workoutName":name, "sportType":dict(SPORT_TYPES[sport]),
  "estimatedDurationInSecs":round(_estimate_seconds(steps,sport)), "author":{},
  "description":"generated by fit",
  "workoutSegments":[{"segmentOrder":1,"sportType":dict(SPORT_TYPES[sport]),
    "workoutSteps":steps}]}
```
Returns `{"id":created,"sport","workout_type","params","workout_name":name,"payload"}`.
(`params` = PARSED values, e.g. paces as seconds, not strings.) `garmin_workout_id` /
`scheduled_date` are added later by cli, not here.

**`describe_plan(plan)->list[str]` `:724-737`:** one line per top-level step;
RepeatGroup → `"{n} x {…}"`. Helpers `_describe_target` `:695`, `_describe_extent`
`:708`, `_describe_step` `:714`. Example `['Warmup 10:00', '6 x 800m @ 4:20-4:40/km,
2:00 recovery', 'Cooldown 10:00']`.

**History-derived defaults `:740-920`:** `recommended_interval_pace(five_k_seconds,
rep_distance_m)->int` `:747`; `_best_of_keys(pbs,keys)` `:757`;
`_recent_run_5k(recent)->(sec,why)|None` `:767` (best recent 5k from
`compute.all_personal_bests(recent)["run"]`, fallback fastest avg pace of a recent run
≥3 km); `_recent_swim_css(recent)->(sec_per_100m,why)|None` `:793` (two-point CSS
`(T_1k−T_500m)/5`, fallbacks); `_recent_ride_watts(recent)->(watts,why)|None` `:827`
(max avg_power over recent rides ≥1200 s, round 5 W);
`recommend_defaults(sport,workout_type,activities,previous_plans,reference:date)->dict`
`:847-920` returns `{key:{"default":v,"why":s}}` or `{key:{"derive":callable,"why":s}}`;
filters via `compute.filter_by_date(activities, compute.months_ago(reference,
RECENT_MONTHS), reference.isoformat())`. Rep progression `:904-918` reads
`storage.read_plans()` (the ONLY consumer), takes latest same sport+type plan, recommends
`last_reps+1` if `< REPS_CAP`.

## `cli.py` (Typer app)
**`plan` command `:391-475`:** options `--sport`(req), `--type`(req),
`--push/--no-push`(default push) `:399`, `--schedule DATE`(default None) `:402`. Flow:
`workout_params`→ validate schedule up front (`--schedule` rejects `--no-push`;
`parse_schedule_date`) `:419-428` → `_load_activities()` → `recommend_defaults(...,
storage.read_plans(), today)` → merge recs into specs → `_prompt_params` →
`build_plan` → `storage.write_plan` (BEFORE login) → `render_plan_saved` → if `--no-push`
return → `garmin.login()` → `push_workout`, store `garmin_workout_id`, rewrite,
`render_plan_pushed` → if schedule: guard missing id, `schedule_workout`, store
`scheduled_date`, rewrite, `render_plan_scheduled`.
**`_prompt_params(specs)->dict` `:373-388`:** resolves `"derive"` at prompt time.
**`_load_activities()->list[dict]` `:17-23`:** `ensure_data_dir`→
`read_activities_with_warnings`→`render_warnings`.
**Command surface:** `dashboard` `:138`, `dash` `:192`, `pbs` `:210`, `fitness` `:227`,
`fitness-reset` `:237`, `stats` `:251`, `import` `:295`, `gs` `:328`, `garmin-sync`
`:334`, `plan` `:391`, `history` `:478`, `calendar` `:483`, `usage` `:489`. Sub-Typer
via `app.add_typer(...)` is a new but clean pattern for the `train` group.

## `garmin.py` (Garmin Connect boundary; lazy `import garminconnect`)
`_garminconnect()` `:30`, `GarminAuthError` `:26`, `TOKEN_STORE="~/.garminconnect"` `:18`
(outside `~/.fit`), `INSTALL_HINT` `:20`. `login()->client` `:38-66`.
`push_workout(client,payload)->dict` `:69-73` (`upload_workout`; response has
`"workoutId"`). `get_workout(client,id)->dict` `:76-81`. `schedule_workout(client,id,
date_str)->dict` `:84-92` (`client.schedule_workout`; ATOMIC — one workout, one date;
date pre-validated). `list_recent_activities` `:95`, `download_activity_fit` `:102`.
**NOT yet wrapped (need adding):** `get_scheduled_workouts(year,month)`,
`unschedule_workout(scheduled_workout_id)` — both exist in the library (Appendix C).

## `storage.py` (the ONLY filesystem module)
`resolve_data_dir()` `:70` (`FIT_DATA_DIR` else `~/.fit`); `plans_dir()` `:97`,
`activities_dir()` `:77`, `gpx_dir()` `:93`, `pbs_path()` `:85`, `fitness_path()` `:89`,
`config_path()` `:81`. `ensure_data_dir()` `:101-105` makes activities/gpx/plans +
default config. `_write_atomic(path,text)` `:108-122` (mkstemp+fsync+os.replace),
`_write_json_atomic(path,data)` `:125` (json.dumps indent=2), `_read_json` `:129`.
`write_plan(plan)` `:159-160` → `plans_dir()/f"{plan['id']}.json"`. `read_plans()->list`
`:163-174` (sorted glob, silently skips corrupt). `read_activities_with_warnings()->
(list,list[str])` `:136-148`; `write_activity` `:151`; `activity_exists` `:155`. Test
precedent: `tests/test_storage.py:77` plan round-trip with `monkeypatch.setenv`.

## Plan file shape (existing single-workout, `~/.fit/plans/<id>.json`)
`{"id":ISO-sec, "sport","workout_type","params"(parsed), "workout_name","payload"(full
Garmin dict), "garmin_workout_id"(after push, may be None), "scheduled_date"(after
--schedule)}`. The plan FILE (not Garmin) is the reconstructable record.

## Deferred-feature status
`CLAUDE.md:884-897` "Stretch features": multi-week periodised plans built on the atomic
`schedule_workout`/`parse_schedule_date` seam; "Do not design current modules around
these features." `TODO.md` item 2 tracks it. Gaps a multi-week feature hits: no
scheduled-workout list/remove wrappers; plan files have no parent grouping; rep
progression only looks at the single latest same-type plan.

---
---

# APPENDIX B — Architecture conventions & data-model (guardrails the feature must respect)

Source of truth `CLAUDE.md`, esp. "Conventions" `:1058-1080`.

## 1. Hard rules
- **Functions only, no classes** `:36-47,1060`. Plain dicts everywhere; no dataclasses/
  Pydantic. Sole exception: `garmin.GarminAuthError` (bare `Exception`).
- **Module boundaries** `:142-581,1063-1064`: `storage.py` = only FS module ("if there's
  a conditional or arithmetic in storage.py it belongs in compute.py" `:175-176`; never
  prints — returns warnings). `compute.py` PURE (no I/O, no side effects, never reads
  files). `display.py` render-only (no I/O, no computation). `cli.py` thin wrappers
  (storage→compute→display). `importers.py`/`garmin.py` boundary modules that never
  touch the data dir or build fit's dict shape; `importers.py` doesn't import storage.
  `planner.py` pure, may import compute, no I/O/network/typer `:486-490`. → `training.py`
  follows planner's rule: pure, imports compute+planner, cli reads the file for it.
- **`.get()` for ALL activity-dict field access** `:301-302,604-606,1065` (older
  activities lack newer fields).
- **British English** in comments/docstrings/user-facing text; title case for display
  table/section titles `:1061-1062`.
- **Atomic writes** (`os.replace`) for any full-file overwrite `:128-138,1067`; never
  `open(path,"w")` on managed files.
- **`FIT_DATA_DIR`** overrides data dir; `resolve_data_dir()` is the single source; no
  other module hardcodes a path `:113-126,1068`.
- **Add storage helpers only when a caller needs them** `:171-173` (repo removed
  speculative ones).
- **Minimise dependencies** `:29-31` — hence PyYAML is a deliberately-flagged exception.

## 2. Config system (`storage.py`)
`DEFAULTS` `:43-54`: `sports`(list,[]), `pbs_window_months`(int,0), `history_count`(int,5),
`dashboard_weeks`(int,12), `max_heart_rate`(int,0), + 5 bools `show_sparkline/show_pbs/
show_sports_summary/show_fitness_index/show_calendar`. Parallel `_CONFIG_COMMENTS`
`:56-67`. Type is INFERRED from the `DEFAULTS` value type in `_parse_config_text`
`:177-200` (bool→truthy-set, list→comma-split, int→int(), else str); unknown keys
ignored, malformed lines skipped. `_serialize_config_text` `:203-214` writes the fully
commented default file (`ensure_data_dir` drops it first run). `read_config()`
`:217-221` = `{**DEFAULTS,**stored}`. **User rule (documented `:816-821`): config keys
must be FUNCTIONAL (change behaviour), not cosmetic.** To add one: add to DEFAULTS +
_CONFIG_COMMENTS; parsing/typing come free. → add only `train_sync_window_days` (int,14).

## 3. Storage layout
`~/.fit/`: `activities/` (one JSON per activity, id=ISO timestamp), `plans/`, `config`,
`pbs.json`, `fitness.json`, `gpx/`. → add `train/` (single `plan.json`). Mirror
`write_plan`/`read_plans` for `write_training_plan`/`read_training_plan`. Read-with-
warnings pattern for activities `:136-148`.

## 4. Display patterns (`display.py`)
`render_calendar(months)` `:207-215` renders `compute.activity_calendar`'s
`[{"label","weeks","active_days"}]` grids side by side (`Table.grid`, active days bold
green). A plan-calendar could build the SAME grid shape from scheduled/planned dates via
a sibling compute fn so `render_calendar` renders it unchanged — but a per-week table is
likely clearer for a plan; either way display stays computation-free. Plan renderers:
`render_plan_recommendations` `:460`, `render_plan_saved` `:473`, `render_plan_pushed`
`:482`, `render_plan_scheduled` `:489`. Split idiom: compute/planner returns structured
data, display formats it (`detect_new_pbs`→`render_new_pb_messages`; `describe_plan`→
`render_plan_saved`).

## 5. Testing conventions (`tests/`)
"Small, deliberately non-exhaustive suite covering the subtlest PURE functions"
`:1069-1072`. Run `.venv/bin/pytest` after changes to compute/storage/importers/planner.
`test_compute.py` (~327 ln), `test_planner.py` (~346 ln incl. `test_rep_progression_
from_previous_plans:313`, parametrised reject lists `:18`), `test_storage.py` (~93 ln,
`tmp_path`+`monkeypatch.setenv("FIT_DATA_DIR",...)`), `test_importers.py` (~233 ln). All
fixtures synthetic. **Feedback/memory: do NOT over-expand the suite** — add tests only
for genuinely subtle pure logic (the periodisation date/load math), matching existing
style. Don't chase CLI/display coverage.

## 6. Reusable date/time utilities (`compute.py`)
`months_ago(reference,n)->str` `:175-183` (ISO n months back, day clamped). `stats_date_
range(window,reference)` `:161-172`. `parse_timerange(text,reference)->(start,end)`
`:189-222` (rolling `<N><d|w|m|y>`). `activity_calendar(activities,reference,months=2)->
list[dict]` `:308-337`. `weekly_volumes(activities,through=None)->list[dict]` `:248+`
(one bucket per ISO week, ZERO-FILLS every week; volume = time not distance). Week
primitives (currently private): `_iso_week_key(date_iso)` `:225`, `_week_start(date_iso)`
`:231` (Monday of ISO week), `is_current_week(week_key,reference)` `:241`. A scheduler
laying weekly cadence reuses `_week_start` + `timedelta(weeks=…)`.

## 7. Fitness/load model (`compute.py`) — for "progressively increase load"
`met_for_activity(activity)->float` `:634-660` (coarse MET from `MET_TABLE:62`, banded by
pace, or km/h speed for cycle/canoe, flat for hike/squash). `median_hr_by_type` `:663`.
`activity_load(activity,median_hr)->float` `:680-696` — **base = `met_for_activity ×
duration_hours`**, scaled by clamped `avg_hr/peer_median` in `[0.8,1.25]`. This is the
per-session load number. `daily_load_totals` `:699`, `fitness_ewma_daily(activities,
as_of)` `:715-742` (dense daily 42-day EWMA, seeded value[first]=load[first]),
`compute_baseline_value` `:745`, `rescale_to_index` `:753` (index=100*value/baseline),
`weekly_fitness_index` `:769`. Baseline STICKY in fitness.json (reset only via
`fit fitness-reset`). **Implication:** progression targets per-session MET-hours
(`met_for_activity × duration_hours`) and/or the weekly load/duration sum (mirroring
`weekly_volumes` = time). Coarse-by-design, so progression math can stay simple. A
PLANNED session's load must be ESTIMATED (duration/distance known, no HR yet) — ramp
weekly planned duration + grow long-session distance.

---
---

# APPENDIX C — Installed Garmin API surface (garminconnect 0.3.6)

Confirmed via introspection of the installed `garminconnect.Garmin` (needs the
`[garmin]` extra; `pip install -e '.[garmin]'`). Relevant workout/calendar methods:
- `upload_workout(workout_json)->dict` (returns `workoutId`) — wrapped as
  `garmin.push_workout`.
- `get_workout_by_id(id)->dict` — wrapped as `garmin.get_workout`.
- `schedule_workout(workout_id, date_str)->dict` — wrapped as `garmin.schedule_workout`.
- `get_scheduled_workouts(year, month)->dict` — **NOT wrapped; add.**
- `get_scheduled_workout_by_id(id)->dict` — available if needed.
- `unschedule_workout(scheduled_workout_id)->Any` — **NOT wrapped; add.**
- `delete_workout(id)`, `download_workout(id)`, `get_workouts(start,limit)` — available.
- `upload_{running,cycling,swimming,walking,hiking}_workout` — sport-specific uploaders
  (not needed; generic `upload_workout` is used).

**Key fact driving the yoga/strength decision:** there is **NO** calendar-note / generic
non-workout scheduling endpoint. `schedule_workout` only takes a `workout_id`. So
non-Garmin activities (yoga/strength) can only be LOCAL to fit (chosen), or pushed as
real workouts under a strength/yoga sportType (rejected for v1). `get_scheduled_workouts`
+ `unschedule_workout` make the rolling-window sync idempotent and `clear` possible.

---
---

# APPENDIX D — Design Q&A decision log (why each choice was made)

The user was asked 15 questions across 4 rounds; selected options:
- **R1:** Intelligence = *fit expands rules* (fit is the engine, not a dumb executor).
  Session spec = *high-level intent* (fit derives concrete params from history).
  Workout coverage = *extend planner* with steady/long types (every session a real
  workout). Yoga/strength = *track locally in fit only*.
- **R2:** Plan spec boundary = *goal + prefs; fit owns per-goal templates* (thin
  description). Progression = *build/recover cycles + taper* (3:1, ~8%/wk, 2-wk taper
  defaults, overridable). Intensities = *history, goal override*. Naming = *train*.
- **R3:** Rollout = *rolling window* (next ~14 days, re-run to advance). Show = *plan +
  completion* (planned vs done vs `activities/`). Steady targets = *distance where
  natural* (long by km, easy by time, easy target bands). Format = *YAML* (user accepted
  the added dependency).
- **R4:** Initial scope = *all 8 goal templates at once*. Concurrency = *one active
  plan*. Extras placement = *fit auto-places* on easy/rest days.

Two later mechanical choices to make during implementation (not user-blocking):
representing bricks as two same-day sessions (multisport FIT deferred); estimating
planned-session load from duration/distance since HR isn't known ahead of time.
