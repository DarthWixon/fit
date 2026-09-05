# CLAUDE.md — fit CLI workout tracker

Context file for Claude Code. Summarises the architectural and design decisions made
during planning. Read this before making any changes to the codebase.

---

## What this project is

A terminal-based workout tracker inspired by Strava and Runna. It runs on Linux
(primary) and macOS (secondary). Everything is local — no accounts, no sync services,
no cloud dependency. The full dataset lives in a single folder that can be zipped and
moved to a new machine.

---

## Language and dependencies

- **Python 3.11+**
- `typer` — CLI flag parsing and help text
- `rich` — all terminal output: tables, coloured text, Unicode sparklines
- Standard library `xml.etree.ElementTree` for TCX parsing (no lxml)
- `fitparse` — required, for FIT file parsing (`importers.import_fit`); pinned
  `fitparse>=1.2,<2.0` (see importers.py's "Known limitation" on undecoded sport
  codes). The `garmin` extra (`garminconnect`) is the only genuinely optional
  dependency — see "Garmin integration"
- `pyyaml` — optional `train` extra (`pip install -e '.[train]'`), lazily
  imported inside `training.parse_plan_spec` only. `fit train import` parses a
  YAML plan description; `show`/`sync`/`clear` read the stored JSON and need
  nothing extra. A deliberate, user-approved exception to the minimal-deps rule
  — see "Training plans"
- No database, no ORM, no heavy framework

Keep the dependency footprint minimal. Before adding a new library, check whether the
standard library or an existing dependency already covers the need.

---

## Programming approach

**Functions only. No classes.**

Data is plain dicts throughout the codebase. There are no model classes, no dataclasses,
no Pydantic models. Functions take dicts in and return dicts or primitive values out.

This is a deliberate choice for simplicity and readability. Do not introduce classes
to represent activities, configs, or personal bests — use dicts with consistent keys
(see activity shape below) and document the expected keys in docstrings.

The one exception is `garmin.GarminAuthError`, a plain `Exception` subclass with no
state or methods — idiomatic Python for a distinguishable error type, not a data
model, so it doesn't conflict with the rule above.

---

## CLI design

Commands are subcommand-based. Each subcommand produces a single one-shot output and
exits. No persistent TUI.

```
fit dashboard                # summary: recent activity, weekly volume, trend sparklines
fit dashboard --sport cycle  # dashboard filtered to one sport for this run (overrides config)
fit dashboard --timerange 3m # dashboard windowed to a rolling 3 months (history, sparkline, sports summary, PBs)
fit dashboard --minimal      # dashboard with just the sparklines and recent activities for this run (hides PBs, sports summary)
fit dash                     # shorthand for `fit dashboard --minimal` (also takes --sport / --timerange)
fit pbs                      # personal bests table, grouped by activity type
fit pbs --months 3           # personal bests over just the last N months (overrides config)
fit stats                    # breakdown, accepts --week / --month / --year
fit fitness                  # current fitness index (baseline 100) + trend sparkline
fit fitness-reset            # re-anchor the fitness index baseline to today
fit import ./run.fit         # import a file, folder of files, or Strava export
fit garmin-sync --days 14    # pull recent activities from Garmin Connect (see "Garmin integration")
fit plan --sport run --type intervals  # generate a workout interactively, push to the watch (see "Workout planner")
fit plan --sport strength --type straight_sets  # a gym session: prompts for exercises until you leave one blank
fit plan --sport run --type intervals --schedule 2026-08-28  # also place it on that date in the Garmin calendar
fit train import plan.yaml   # expand a YAML goal description into a full periodised plan
fit train show --weeks 2     # the plan, per week, with planned/done marks
fit train retarget           # re-derive intensity targets from your latest history
fit train sync [--dry-run]   # push + schedule the next N days of sessions (see "Training plans")
fit train clear              # unschedule the plan's future sessions from Garmin
fit history 10               # last N activities as a table (default 10)
fit calendar                 # active days in the last 2 calendar months, marked on a text calendar
fit usage                    # short command cheat sheet (man-page substitute)
```

`cli.py` is thin wrappers only. Each subcommand function calls storage → compute →
display in sequence. No business logic lives in `cli.py`.

---

## File storage

### Folder structure

```
~/.fit/
├── activities/              ← one JSON file per activity
│   ├── 2024-01-15T08:30:00.json
│   ├── 2024-01-17T07:15:00.json
│   └── ...
├── config                   ← plain-text key=value, hand-editable (see "Config defaults")
├── pbs.json                 ← cached personal bests (recomputed when stale)
├── plans/                   ← generated workouts (fit plan), one JSON file each (see "Workout planner")
│   └── 2026-07-03T09:15:02.json
└── train/                   ← the single active training plan (see "Training plans")
    └── plan.json
```

**Original files are not kept.** An import parses what it needs — splits, HR
zones, totals — and the source file is not copied anywhere. See "Split PBs" for
what that costs.

### One file per activity

Each activity is stored as an individual JSON file in `activities/`, named by its ID
(the ISO 8601 start timestamp). This was chosen over a single JSONL file because:

- Edit and delete are trivial (no read-modify-write cycle on the whole dataset)
- The folder is human-readable: `ls`, `cat`, `rm` all work naturally
- Corruption is isolated to one activity, not the whole history
- Incremental cloud sync (rsync, rclone) only transfers changed files

Reading all activities means `os.listdir()` plus one `json.load()` per file. At the
scale of personal workout data (hundreds to low thousands of files) this is fast enough.

### Path resolution

`resolve_data_dir()` in `storage.py` is the single source of truth for the data path.
It checks the `FIT_DATA_DIR` environment variable first, then falls back to `~/.fit/`.

```python
FIT_DATA_DIR=./examples/data fit dashboard   # safe for development
```

All other path helpers (`activities_dir()`, `config_path()`, etc.) build on top of
`resolve_data_dir()`. No other module hardcodes a path.

Any path stored inside an activity dict is relative to the data directory, not
absolute, so the folder stays portable across machines.

### Atomic writes

`config`, `pbs.json`, and activity files are rewritten in full using a
write-to-temp-then-rename pattern (`os.replace()`) to prevent corruption on
interrupted writes. A shared helper `_write_atomic(path, text)` in `storage.py`
handles the temp-file/rename mechanics for any full-file text rewrite;
`_write_json_atomic(path, data)` is a thin `json.dumps` wrapper around it for
`pbs.json`/activity files. `config`'s plain-text serialization goes through
`_write_atomic` directly (see "Config defaults"). Never use `open(path, "w")`
directly on these files.

Individual activity files use the same atomic pattern on first write. Edits also use it.

---

## Module structure

### `storage.py`
The **only** module that touches the filesystem. Pure I/O, no logic.

Key functions:
- `resolve_data_dir() -> Path`
- `ensure_data_dir() -> None` — creates folder structure on first run
- `activities_dir() -> Path`, `config_path() -> Path`, `pbs_path() -> Path`, `fitness_path() -> Path`, `plans_dir() -> Path`
- `read_activities_with_warnings() -> tuple[list[dict], list[str]]` — reads all
  activity files, skipping any that fail to parse, and returns one warning string
  per skipped file instead of printing; `cli.py` passes these to
  `display.render_warnings()` rather than printing directly (storage.py never
  prints — that's a display.py concern)
- `write_activity(activity: dict) -> None` — writes single activity file atomically
- `activity_exists(activity_id: str) -> bool`
- `read_config() -> dict` — merges stored config (parsed from the plain-text `config`
  file) over `DEFAULTS`, never raises on missing file or malformed lines
- `read_pbs() -> dict`
- `write_pbs(pbs: dict) -> None`
- `read_fitness_baseline() -> dict` — `{}` if never initialized; see "Fitness index"
- `write_fitness_baseline(baseline: dict) -> None`
- `train_dir() -> Path`, `training_plan_path() -> Path`
- `write_plan(plan: dict) -> None` — writes single plan file atomically, named by
  the plan's `id` (mirrors `write_activity`)
- `write_training_plan(plan: dict) -> None` / `read_training_plan() -> dict | None` —
  the one active training plan at `train/plan.json`; only one exists at a time,
  so unlike `write_plan` there's no per-id filename. `read_training_plan` lets a
  corrupt file *raise* rather than skipping it like `read_plans` does — this is
  the whole feature's state, and silently dropping it would make `fit train
  clear` report success having unscheduled nothing
- `read_plans() -> list[dict]` — all saved plans, silently skipping unparseable
  files (unlike `read_activities_with_warnings`, no warnings: a corrupt plan just
  drops out of the rep-progression defaults, the only reason plans are read back)

Storage functions are added when a caller needs them, not speculatively — an
earlier draft carried read/edit/delete/plan helpers with no callers, and they
were removed. When an edit command lands, add its helpers then.

Rule: if there is a conditional or any arithmetic in `storage.py`, it belongs in
`compute.py` instead.

### `compute.py`
Pure functions. Takes lists of dicts, returns derived data. Never reads files.

Key functions:
- `calc_pace(distance_km: float, duration_seconds: int, activity_type: str = "") -> str` —
  per-km except `"swim"` (per 100m) and the `SPEED_TYPES` set — `"cycle"`,
  `"hike"`, and `"canoe"` — (speed in km/h, e.g. `"24.3km/h"`, since bike effort
  is conventionally read as speed rather than pace, and hiking/paddling pace both
  read more naturally as km/h). Always
  returns `"—"` for any type in `NO_DISTANCE_TYPES` (currently `"squash"`),
  regardless of `distance_km` — its nonzero distance is never a real pace signal
- `weekly_volumes(activities: list[dict], through: date | None = None) -> list[dict]` —
  one bucket per ISO week, oldest first: `{"week", "count", "duration_seconds",
  "distance_km"}`. **Volume is measured in time, not distance** — `duration_seconds`
  is what the dashboard/stats sparklines plot (as decimal hours), since distance is
  heavily biased across sports (an hour of cycling covers ~4x an hour of running,
  ~40x an hour of swimming, and squash covers nothing real at all). `distance_km`
  stays in the bucket as a secondary figure. The series spans **every calendar
  week** between the first and last activity — weeks with no activity zero-fill
  rather than collapsing, so a rest week is a trough and not a missing bar, and a
  `[-n:]` slice really is the last n weeks. `through` (callers pass today) extends
  the series to that date's week, so the current week always appears even with
  nothing logged in it yet
- `week_start(date_iso: str) -> date` — Monday of the ISO week containing
  `date_iso`. Public (unlike its `_iso_week_key` sibling) because `training.py`
  lays a plan's weekly cadence out from it: week N starts at `week_start +
  N weeks`
- `is_current_week(week_key: str, reference: date) -> bool` — whether a `weekly_*`
  series' final bucket is the still-filling-up current week; `display` uses it to
  dim that bar (see `render_sparkline`)
- `filter_by_type(activities: list[dict], type: str) -> list[dict]`
- `filter_by_types(activities: list[dict], types: list[str]) -> list[dict]` — like
  `filter_by_type` but for the config-driven `sports` list; empty/falsy `types`
  means no filtering
- `filter_by_date(activities: list[dict], start: str, end: str) -> list[dict]`
- `months_ago(reference: date, n: int) -> str` — ISO date `n` calendar months before
  `reference`, day clamped to the target month's last valid day. Used to compute the
  start of a "PBs in the last N months" window (`pbs_window_months` config /
  `--months` flag)
- `stats_date_range(window: str | None, reference: date) -> tuple[str, str] | None` —
  `window` is `"week" | "month" | "year" | None`; returns the `(start, end)` pair
  `fit stats --week/--month/--year` filters by, or `None` for no filtering
- `parse_timerange(text: str, reference: date) -> tuple[str, str]` — parses
  `"<N><unit>"` (`d`/`w`/`m`/`y` — days/weeks/months/years) into a `(start, end)`
  pair, `end == reference`. Unlike `stats_date_range`'s calendar-aligned windows
  (e.g. "month" = since the 1st of the current month), this is a rolling window:
  `"3m"` always means exactly 3 months back from `reference`. `m`/`y` delegate to
  `months_ago` (`y` as `n * 12` months) for its day-of-month clamping rather than
  reimplementing it. Raises `ValueError` on a malformed unit/count. Powers `fit
  dashboard --timerange`
- `summarize_by_type(activities: list[dict]) -> list[dict]` — one row per distinct
  activity type present, sorted alphabetically:
  `[{"type": ..., "count": ..., "duration_seconds": ..., "distance_km": ...}, ...]`. Shared by
  `display.render_stats`'s "By type" table and the dashboard's sports-summary block
  (`display.render_sports_summary`) — the single source of truth for "by type"
  aggregation
- `activity_calendar(activities: list[dict], reference: date, months: int = 2) -> list[dict]` —
  month grids for the `months` calendar months ending with `reference`'s, oldest
  first: `[{"label": "June 2026", "weeks": [[0, 1, ...], ...], "active_days":
  [3, 5, 12]}, ...]`. `weeks` comes from stdlib `calendar.monthcalendar`
  (Monday-first rows, 0 = padding cell); grid layout lives here so
  `display.render_calendar` stays computation-free. Powers `fit calendar`
- `total_weight_lifted(activity: dict) -> float` — tonnage: every rep of every
  set times what was on the bar. The one figure that says what a whole gym
  session was, the way distance does for a run — and unlike the heaviest set it
  moves with sets and reps as well as load, so a week that added volume shows
  up too. Unweighted sets contribute nothing
- `best_power_window(points: list[dict], window_seconds: int) -> int | None` —
  highest average power sustained over any window of that length in one
  activity's point stream; the time-axis counterpart to `fastest_split`'s
  distance axis, maximising a mean rather than minimising a duration. Integrates
  over elapsed time rather than per sample, because FIT records are not reliably
  one per second. See "Power windows"
- `fastest_split(points: list[dict], target_distance_km: float) -> dict | None` — best
  continuous segment of a given distance within one activity's (elapsed_seconds,
  distance_km) point stream; see "Split PBs" below
- `hr_zone_seconds(points: list[dict], max_heart_rate: int) -> dict` — time spent
  in each of the 5 HR zones from a point stream of `{"elapsed_seconds", "hr",
  ...}` dicts, attributing the gap to the earlier sample's zone; `{}` if
  `max_heart_rate <= 0` or fewer than 2 points carry `hr`; see "HR zones"
- `hr_zone_percentages(hr_zones: dict | None) -> dict | None` — converts a
  stored `hr_zones` seconds dict into `{"zone1": pct, ..., "zone5": pct}`
  summing to ~100, or `None` if missing/empty/all-zero; the display-time
  arithmetic behind `display._format_hr_zones`
- `estimated_1rm(weight_kg: float, reps: int) -> float` — Epley one-rep-max
  estimate for one set, rounded to 0.1kg; the single source of truth for e1RM,
  so a strength PB and a plan target can never disagree. A single rep returns
  the lift itself rather than Epley's 3%-above-it extrapolation
- `all_personal_bests(activities: list[dict]) -> dict` — groups activities by type and
  calls the private `_candidate_pbs` per type, which itself composes four
  single-purpose helpers: `_longest_distance_pb`, `_milestone_pbs`, `_split_pbs`,
  `_elevation_pb` — each returns its own slice of the PB dict, merged by `_candidate_pbs`.
  `"strength"` is dispatched to `_strength_pbs` instead: its PBs are keyed by
  exercise name rather than by distance/time label, so none of those four
  helpers apply (see "Strength sessions")
- `best_pb_per_label(type_pbs: dict) -> dict` — collapses a single type's
  dedicated (`fastest_{label}_seconds`) and split (`fastest_{label}_split_seconds`)
  PBs sharing a distance label down to whichever is faster, for display only;
  `pbs.json` keeps both stored independently (see "Split PBs"). Used by
  `display.render_pbs_table` to produce one row per label instead of two
- `detect_new_pbs(new_activities: list[dict], current_pbs: dict) -> list[dict]` —
  returns `[{"key": ..., "value": ...}, ...]` for each PB category broken, structured
  rather than pre-formatted; strength goes via `_strength_new_pbs`, which flattens the
  nested per-exercise shape into the same two-key entries (`"squat_best_e1rm_kg"`); `display.render_new_pb_messages()` turns these into the
  human-readable "New fastest 5k: 21:40"-style text (formatting is a display.py
  concern, not compute.py's).
  **Takes the whole import as one batch**, measured against `current_pbs` once,
  and delegates the best-of reduction to `all_personal_bests` — so an import of
  nine paddles of a type with no history breaks "longest distance" once, at the
  batch's best. Per-activity comparison against a fixed pre-import snapshot
  instead announced every activity that beat what was on disk before the import
  started, in file order rather than best-first (a real 26-line flood on
  2026-09-05). `cli._import_and_report` therefore writes the batch first and
  announces afterwards, rather than announcing inside the write loop
- `pbs_cache_is_valid(pbs: dict, activity_count: int) -> bool`
- `met_for_activity(activity: dict) -> float` — coarse MET value from `MET_TABLE`,
  banded by pace/speed; see "Fitness index"
- `median_hr_by_type(activities: list[dict]) -> dict[str, float]` — median
  `avg_heart_rate` per type across HR-tagged activities, inclusive of the activity
  being scored
- `activity_load(activity: dict, median_hr: dict[str, float]) -> float` —
  MET-hours base, scaled by a clamped HR-vs-peer-median multiplier when available
- `daily_load_totals(activities: list[dict]) -> dict[str, float]` — `{date_iso:
  summed load}`
- `fitness_ewma_daily(activities: list[dict], as_of: date) -> list[dict]` — dense
  `[{"date": ..., "value": ...}, ...]` daily 42-day-EWMA series
- `compute_baseline_value(activities: list[dict], as_of: date) -> float | None`
- `rescale_to_index(raw_series: list[dict], baseline_value: float) -> list[dict]`
- `filter_series_by_date(series: list[dict], start: str, end: str) -> list[dict]` —
  like `filter_by_date` but for a `{"date": ...}`-keyed series
- `weekly_fitness_index(index_series: list[dict]) -> list[dict]` — needs no
  zero-filling of its own (unlike `weekly_volumes`): its input daily series from
  `fitness_ewma_daily` is already dense over every calendar day, so the weeks come
  out contiguous. Resamples to one
  point per ISO week (the week's last value, not a sum — see "Fitness index")

Key constants:
- `MILESTONES_KM` / `MILESTONE_TOLERANCE` — "dedicated effort" distances per type
  (a whole activity within `[D, D*MILESTONE_TOLERANCE]` counts as a "D-distance effort")
- `POWER_WINDOWS_S` — durations-of-interest for the power-duration curve
  (`[(60, "1min"), (300, "5min"), (1200, "20min")]`), the time-axis sibling of
  `SPLIT_DISTANCES_KM`
- `SPLIT_DISTANCES_KM` — distances-of-interest per type for best-split extraction
- `MET_TABLE` — coarse per-type MET values, banded by pace/speed for most types
  (`cycle` and `canoe` band by km/h, matching their `SPEED_TYPES` display), or
  a single flat value for types with no meaningful pace/speed signal (`hike`,
  `squash`, `strength`); see "Fitness index"
- `SPEED_TYPES` — types shown/banded by km/h speed rather than min/km pace
  (`cycle`, `hike`, `canoe`); `calc_pace` and `met_for_activity` both branch on it
- `NO_DISTANCE_TYPES` — activity types (`squash`, `strength`) whose
  `distance_km` is present but never meaningful (e.g. accelerometer noise from
  an indoor court sport with no GPS track, or the 0.0 a gym session reports);
  `calc_pace` and `_candidate_pbs` both check it to suppress pace strings and
  the "longest distance" PB for these types
- `HR_ZONE_BOUNDARIES` — fraction-of-max-heart-rate lower bounds for the 5
  standard HR training zones (`[0.5, 0.6, 0.7, 0.8, 0.9]`); see "HR zones"

Always use `.get()` when accessing activity fields — older activities may not have
fields added after they were logged (e.g. `avg_heart_rate`, `elevation_gain_m`).

### `display.py`
Takes outputs of compute functions and renders Rich output to stdout. No file I/O,
no computation.

Key functions:
- `render_dashboard(activities: list[dict], pbs: dict, config: dict, fitness: dict, today: date, sports: list[str] | None = None, window_months: int = 0, window_label: str | None = None) -> None` —
  `config` is the `storage.read_config()` dict (supplies `history_count`,
  `dashboard_weeks`, and the five `show_*` toggles); `fitness` is
  `cli._fitness_snapshot()`'s dict
  (`current`/`baseline_date`/`weekly`), passed through whole rather than
  unpacked into separate parameters.
  Block order: fitness index -> weekly volume sparkline -> time range banner
  -> history table -> calendar -> personal bests -> sports summary. When
  `window_label` is set (from `--timerange`), prints a "Time range: ..."
  banner and threads the label into `render_pbs_table`'s title instead of
  the int-months wording. The calendar block (`render_calendar` +
  `compute.activity_calendar`, `months=2`) uses the same sport-filtered
  activity list as the history table right above it, so `--sport` narrows
  its active days too — unlike sports-summary/fitness, it is not a
  sport-agnostic overview block. The sports-summary block always renders
  *last*, over the unfiltered `activities` list, so it always shows every
  type present (never restricted by `--sport`/config `sports`) even when the
  sport filter itself matches nothing — in that case the
  sparkline/history/calendar/pbs blocks are replaced by a single "no
  activities match" message, but sports-summary still renders afterward.
  The fitness-index block renders *first*, before
  even the empty-activities early return, so the headline (always
  full-history/as-of-today) never disappears just because
  `--sport`/`--timerange` matches nothing elsewhere on the page — see
  "Fitness index". The weekly-volume sparkline plots each week's summed
  `duration_seconds` as decimal hours (see `compute.weekly_volumes`), not
  distance. Both it and the fitness-trend sparkline are
  capped to the last `config["dashboard_weeks"]` weeks (0 = all) so long
  history doesn't wrap over multiple lines; the cap is skipped when
  `--timerange` (a truthy `window_label`) is already driving the window.
- `render_history_table(activities: list[dict], n: int) -> None` — the "Effort"
  column uses `_format_effort()`, which shows total weight lifted ("5,880kg", via
  `compute.total_weight_lifted`) for strength, `avg_power` ("187W avg") for
  cycle activities that have it, and falls back to `compute.calc_pace()`
  otherwise — whatever says how hard the session was in that sport's own
  terms.
  "Distance" uses `_format_distance()`, showing `"—"` for any
  `compute.NO_DISTANCE_TYPES` member instead of a meaningless value, same as
  "Pace" already does. "HR Zones" uses `_format_hr_zones()`, a segmented
  colour bar (Z1 blue -> Z5 red, built from `compute.hr_zone_percentages()`)
  showing each zone's share of the activity's time; shows `"—"` when an
  activity has no `hr_zones` (predates the feature, Strava-CSV-only, or
  `max_heart_rate` wasn't configured at import time) — see "HR zones"
- `render_pbs_table(pbs: dict, sports: list[str] | None = None, window_months: int = 0, window_label: str | None = None) -> None` —
  `window_label`, when set, overrides the `window_months`-based title text
  (`"Personal bests (last N months)"`) with `"Personal bests ({window_label})"`,
  for windows expressed in units other than months (e.g. `--timerange 10d`).
  Strength is skipped in the main table and rendered afterwards by the private
  `_render_strength_pbs_table` (title `"Strength {title}"`, one row per
  exercise, columns Exercise / Heaviest Set / Best e1RM, each value carrying
  its own dimmed date since the two metrics rarely share one) — the generic
  Type/Metric/Value/Date shape can't express a per-exercise PB
- `render_sports_summary(activities: list[dict]) -> None` — dashboard block; the
  passed-in `activities` must not be pre-filtered by sport (pre-filtering by date
  is fine — this is how it respects `--timerange` while ignoring `--sport`).
  Shares its table-building logic with `render_stats` via the private
  `_render_type_summary_table(activities, title)`, which wraps
  `compute.summarize_by_type`. Columns are Type / Count / Time / Distance —
  Time (the volume measure) leads Distance, and Distance shows `"—"` for any
  `compute.NO_DISTANCE_TYPES` member, same rule as the history table's
  `_format_distance`
- `render_fitness_index(current_index: float | None, baseline_date: str | None, weekly_series: list[dict], window_label: str | None = None) -> None` —
  headline (always full-history/as-of-today) + trend sparkline (windowed by
  `window_label`/`--timerange` if the caller passes an already-windowed
  `weekly_series`); prints a "not enough data yet" line instead when
  `current_index is None`
- `render_fitness_reset(old_baseline: dict, new_baseline: dict) -> None` — prints
  old→new baseline values; `old_baseline == {}` (never initialized) gets a
  simpler "baseline set" message
- `render_calendar(months: list[dict]) -> None` — `fit calendar`'s output: one
  month grid per `compute.activity_calendar` dict, rendered side by side as
  columns of a borderless `Table.grid()`, active days in bold green, padding
  cells blank
- `render_stats(activities: list[dict], today: date) -> None`
- `render_sparkline(data: list[float], label: str, partial_last: bool = False) -> None` —
  `partial_last` dims the final bar (Rich `[dim]`), for a week that is only low
  because it isn't over yet. Callers set it via the private `_last_week_partial`,
  which wraps `compute.is_current_week`. Deliberately *not* set for the
  fitness-trend sparkline: that series plots each week's closing EWMA *level*, not
  a sum, so the current week's value is a legitimate as-of-today reading rather
  than a half-filled bucket
- `render_new_pb_messages(new_pbs: list[dict]) -> None` — formats
  `compute.detect_new_pbs()`'s structured output into "New fastest 5k: 21:40"-style
  lines; uses its own mm:ss colon formatter (`_format_seconds_colon`), deliberately
  distinct from the tables' `_format_duration` ("21m40s") style
- `render_warnings(messages: list[str]) -> None` — prints `storage.py`-sourced
  warning strings (e.g. corrupt activity files) as plain `warning: ...` lines
- `render_usage() -> None` — static one-screen command cheat sheet for `fit
  usage`; no computation, no file I/O
- `render_plan_recommendations(recs: dict) -> None` — dim "Recommended from your
  history" lines from `planner.recommend_defaults`' `why` strings; prints nothing
  when `recs` is empty
- `render_plan_saved(plan: dict, step_lines: list[str]) -> None` — workout name +
  `planner.describe_plan`'s step lines + saved-file path (pace/power formatting
  happens in planner, not here)
- `render_plan_pushed(plan: dict) -> None` — one-line Garmin push confirmation
  with the workout id
- `render_plan_scheduled(plan: dict) -> None` — one-line confirmation that the
  workout was placed on its `scheduled_date` in the Garmin calendar (only
  printed when `fit plan --schedule DATE` was used)
- `render_training_plan(summary: dict, weeks: list[dict]) -> None` — `fit train
  show`'s output: a header (goal, event, done/scheduled counts, the derived
  targets) then one table sectioned per week, each row a session's date,
  description, Garmin state and completion mark. `summary` is
  `training.plan_summary`'s dict, `weeks` is `training.group_by_week`'s — all
  grouping and counting happens there
- `render_training_synced(summary) -> None` / `render_training_cleared(summary)` —
  one-line confirmations plus any per-session failure warnings
- `render_training_retargeted(summary, dry_run=False) -> None` — the old → new
  target lines from `training.retarget_sessions`, following
  `render_fitness_reset`'s pair shape. One line per target that moved, never one
  per session: intensity is a pure function of the target, so eighty session
  lines would say nothing the two target lines do not
- `render_training_missing() -> None` — the "no active plan, create one with"
  message

### `importers.py`
Parses external formats and returns a correctly-shaped activity dict. Uses stdlib
`xml.etree.ElementTree` for TCX (no external XML library) and `fitparse` for FIT.

Key functions:
- `import_tcx(path: str, max_heart_rate: int = 0) -> dict`
- `import_fit(path: str, max_heart_rate: int = 0) -> dict` — branches to
  `_parse_fit_sets` for strength sessions (see "Strength sessions")
- `import_strava_csv(path: str) -> tuple[list[dict], list[str]]` — a single bare
  Strava `activities.csv`; second element is warning strings (see below)
- `import_strava_export(export_dir: str, max_heart_rate: int = 0) -> tuple[list[dict], list[str]]` —
  a full Strava bulk-export archive (`activities.csv` + linked, possibly gzipped,
  TCX/FIT files); second element is warning strings (see below)
- `import_directory(dir_path: str, max_heart_rate: int = 0) -> list[dict]` — a
  loose folder of `.tcx`/`.fit` files (e.g. a mounted Garmin watch's
  `GARMIN/ACTIVITY` folder), not a Strava bulk export
- `import_by_extension(path: str, suffix: str, max_heart_rate: int = 0) -> dict` —
  dispatches to `import_tcx`/`import_fit` by suffix, raising
  `ValueError` for anything else; the single source of truth for extension
  dispatch, used by both `cli.py`'s `import` command and
  `_import_strava_linked_file` (for linked/gzipped files inside a bulk export)

`max_heart_rate` (all three `import_*` plus every function above that dispatches
to them) is threaded down from `cli.py`'s `storage.read_config()["max_heart_rate"]`
call — importers.py never reads config/storage itself (see below), so this is an
explicit parameter rather than a lookup, the same pattern `planner.py`'s pure
functions use.

Importers never check for duplicates themselves — they always return every activity
they parse. `cli.py`'s shared `_import_and_report` helper (the common tail of both
`import_activity` and `garmin_sync`) is the single place that calls
`activity_exists()` per activity to decide what to skip; this keeps `importers.py`
fully decoupled from `storage.py`'s on-disk state (it doesn't import `storage` at all).

Both Strava importers drop rows they can't import — a row whose `Activity Type`
isn't in `STRAVA_TYPE_MAP`, or with no date — but never *silently*: they tally the
drops (`_parse_strava_row` returns `(None, skip_label)` naming the reason, keyed by
raw type or `"(no date)"`) and return a per-type summary line via
`_strava_skip_warnings` (e.g. `"skipped 100 Strava rows fit can't import: Workout
x99, Surfing x1"`) in their second return value. `cli.py` passes these through
`display.render_warnings` alongside any linked-file warnings, so dropped activities
are always surfaced. This is the single reason `import_strava_csv`/
`import_strava_export` return `(activities, warnings)` tuples rather than bare lists.

`import_tcx`/`import_fit` each also build a transient (elapsed_seconds,
distance_km) point stream while parsing, purely in memory, and use it to compute
best-effort split times (see "Split PBs" below) before attaching the result to the
returned dict's `splits` field and discarding the raw stream — it is never persisted.
The same point stream also carries `hr` where the format provides it (TCX's
per-trackpoint `<HeartRateBpm>`, read
by `_tcx_trackpoint_heart_rate` — a new extractor alongside the existing
`_tcx_trackpoint_elevation`, since only lap-level HR was read before; FIT's
per-record `heart_rate` field, already decoded by `fitparse` but previously
unread) and feeds `compute.hr_zone_seconds` alongside the split computation,
attaching the result to `hr_zones` before the stream is discarded — see "HR zones".

**Known limitation**: the Garmin TCX schema's `Sport` attribute only supports
`Running`/`Biking`/`Other` — there is no native `Swimming` value. `import_tcx`
currently defaults anything outside Running/Biking (walk, hike, swim, canoe) to `"run"`,
which silently mislabels those activities rather than leaving them unclassified.
Not fixed as part of adding swim support — a separate, independent concern.

**Known limitation**: `fitparse`'s bundled profile (pinned via
`fitparse>=1.2,<2.0`) doesn't decode every Garmin FIT `sport` enum code to a
name — e.g. it jumps straight from 48 (`floor_climbing`) to 254 (`all`), so
codes Garmin added in between (64 = squash) come back from the FIT session
message as a raw, undecoded int rather than a string. `import_fit` checks
`FIT_SPORT_CODE_MAP` for these raw ints (`{64: "squash", 19: "canoe",
41: "canoe"}`), falling back to `"run"` for any other undecoded code — the same
default an unrecognized *string* sport already gets via `FIT_SPORT_MAP`. Extend
`FIT_SPORT_CODE_MAP` if a future fixture surfaces another undecoded code
worth naming.

**Strength sessions have no distinct FIT sport**: a gym session arrives as
sport 10 (`"training"`) with sub_sport 20 (`"strength_training"`), so
`import_fit` checks `FIT_STRENGTH_SUB_SPORTS` *before* the sport map — sport
10 isn't in `FIT_SPORT_MAP` and would otherwise default to `"run"`. There is
deliberately **no** `FIT_EXERCISE_CATEGORY_MAP`: unlike the sport codes,
fitparse decodes the `set` message's `category` to a name already and its
enum covers everything in scope, so a map would only add a staler second
source of truth. See "Strength sessions".

**Canoe mapping assumption**: there is no canoe-specific FIT `sport` — Garmin
records paddling as `19` (paddling) or `41` (kayaking). Both are folded to
`"canoe"` (in `FIT_SPORT_CODE_MAP`/`FIT_SPORT_MAP`), and Strava's `"Canoeing"`
maps to `"canoe"` too, because this is a canoe-only setup; Strava
Kayaking/Rowing/SUP are left to drop-with-warning. Revisit the `41 → canoe`
mapping if kayaking is ever split into its own type. TCX can't represent canoe
at all (Running/Biking/Other only), so canoe TCX imports hit the `"run"`
default noted above.

### `planner.py`
Pure functions building Garmin Connect workout-service payloads as plain dicts.
No file I/O, no network, no garminconnect import, no typer — cli.py wires
prompts, storage, and the push around it. May import `compute` (pure → pure).
See "Workout planner" for the feature-level design and payload schema notes.

Key functions:
- `parse_pace(text: str) -> int` — `"4:30"` → 270 seconds (per km or per 100m,
  caller's unit); strict m:ss, raises `ValueError` otherwise
- `parse_exercise(text: str) -> str` — normalises and validates one lift name
  against `STRENGTH_CATEGORIES`, raising `ValueError` listing the four;
  Garmin blanks an exercise it doesn't recognise rather than rejecting it, so
  a typo would otherwise reach the watch as a nameless step
- `parse_weight_kg(text: str) -> float | None` — kilograms, blank → `None`
  (no prescribed load: a bodyweight or work-up set)
- `repeated_param_specs(sport, workout_type) -> tuple[str, list[dict]]` —
  `("exercises", EXERCISE_PARAM_SPECS)` for strength straight sets, `("", [])`
  for every other combo. The one combo whose prompts repeat into a list rather
  than answering one key each; this is how `cli.py` asks *whether* a combo
  loops instead of hard-coding which one does
- `parse_duration(text: str) -> int` — `"90"` → 90, `"2:00"` → 120; raises `ValueError`
- `parse_distance_km(text: str) -> int` — `"16"`/`"16.5"` → metres. Steady
  sessions are prompted in km (nobody thinks of a long run as "16000") but
  every payload `endConditionValue` is metres, so the parse converts
- `parse_schedule_date(text: str) -> str` — validates a `YYYY-MM-DD` calendar
  date and returns it normalized; raises `ValueError` otherwise. The single
  scheduling primitive today (the pure seam a future multi-week planner's
  day-picking logic will build on and route each computed date through);
  imposes no past/future policy — see `fit plan --schedule` under "Workout
  planner"
- `pace_zone_mps(seconds_per_km, tolerance_s) -> tuple[float, float]` /
  `swim_pace_zone_mps(seconds_per_100m, tolerance_s)` — (low, high) m/s bounds for
  a pace ± tolerance band
- `workout_params(sport: str, workout_type: str) -> list[dict]` — prompt specs
  (`{"key", "label", "default", "parse"}`) for one sport/type combo; raises
  `ValueError` listing valid combos — the single validation point for `--sport`/`--type`
- `recommend_defaults(sport, workout_type, activities, previous_plans, reference: date) -> dict` —
  `{key: {"default": ..., "why": ...}}` history-derived prompt defaults (see
  "Workout planner"); keys with nothing to derive from are absent. Run intervals'
  target_pace carries a `"derive"` callable instead of `"default"` (its value
  depends on the not-yet-prompted rep distance)
- `recommended_interval_pace(five_k_seconds: int, rep_distance_m: int) -> int` —
  seconds/km target for a given rep length from a recent 5k time, applying the
  short-rep discount (see "History-derived defaults" below); wrapped by
  `recommend_defaults`'s run-intervals `"derive"` callable
- `estimate_seconds(sport, workout_type, params) -> int` — how long one workout
  is expected to take, without building the payload around it; `training.py`
  sums these to measure a planned week against the user's actual recent volume
- `workout_name(sport, workout_type, params) -> str` — the human workout name
  without building the payload around it. `training.py` stores this on each
  expanded session so `fit train show` reads well without the plan file
  carrying ~80 full payloads; the payload is rebuilt from `params` at sync time
- `recent_activities(activities, reference: date) -> list[dict]` — the last
  `RECENT_MONTHS` of activities, the window every `derive_*` helper expects.
  Public so `training.py` windows history identically rather than repeating the
  same two `compute` calls
- `derive_target(sport, recent) -> dict` — `{"value", "why"}`, the
  **plausibility-guarded front door** to the three derivations below and the one
  callers should use. `value` is `None` both when there was nothing to measure
  and when what was measured falls outside `PLAUSIBLE_TARGETS`; `why` says
  which, so a rejected reading is never silently swapped for a default
- `derive_run_5k(recent) -> tuple[int, str] | None` / `derive_swim_css(recent)` /
  `derive_ride_watts(recent)` — the three history-derivation helpers (each
  returns `(value, why)`), public rather than private precisely so `training.py`
  reuses them instead of duplicating the derivation logic
- `easy_pace_from_5k(five_k_seconds) -> int` / `tempo_pace_from_5k(...)` /
  `endurance_watts_from_ftp(ftp_watts) -> int` — the single source of truth for
  each steady/tempo intensity factor, shared by `recommend_defaults` and
  `training._apply_target`
- `build_plan(sport, workout_type, params, created: str) -> dict` — the saved-plan
  dict: `{"id", "sport", "workout_type", "params", "workout_name", "payload"}`,
  where `payload` is the complete workout-service dict for `garmin.push_workout`
- `describe_plan(plan: dict) -> list[str]` — human step lines ("6 x 800m @
  4:20–4:40/km, 2:00 recovery") so `display.render_plan_saved` stays
  computation-free (same split as `detect_new_pbs` → `render_new_pb_messages`)

Guard constants: `PLAUSIBLE_TARGETS` (per-sport bounds a *derived* target must
fall inside — an explicit `targets:` override is the user's own word and is not
bounded), `CSS_PAIR_MIN_RATIO` / `CSS_PAIR_MAX_DAYS` (the two-point
critical-speed model only means something when both efforts are maximal, from
comparable fitness — the shorter must actually be faster per 100m, which the old
`t1k > t500` check never established), `FTP_FROM_20MIN` (0.95, the conventional
correction, worth applying now the cycle benchmark is a bare test) and
`RIDE_POWER_MIN_SECONDS` (1140 — a watch stopped on the beep records 1199s, and
disqualifying a test that was actually done is the worst failure available).

Key constants: `SPORT_TYPES` / `WORKOUT_TYPES` (valid combos),
`STRENGTH_CATEGORIES` (the four barbell lifts fit plans — the FIT
`exercise_category` names `importers.py` stores, uppercased for the payload),
`EXERCISE_PARAM_SPECS`, `_FALLBACK_SECONDS_PER_REP` / `_FALLBACK_REST_SECONDS`
(the strength siblings of `_FALLBACK_SPEED_MPS`, so a lifting session doesn't
estimate as free when `training.py` sizes a week), `STEADY_TYPES`
(the four single-block types — see "Workout planner"), the target-band
tolerances (`PACE_TOLERANCE_S_PER_KM = 10`, `SWIM_PACE_TOLERANCE_S_PER_100M = 5`,
`POWER_TOLERANCE_W = 10`, plus the wider `EASY_PACE_TOLERANCE_S_PER_KM = 20` /
`ENDURANCE_POWER_TOLERANCE_W = 25` for steady work), and the recommendation
knobs (`RECENT_MONTHS = 6`, `TEMPO_FACTOR = 1.07`, `EASY_FACTOR = 1.3`,
`ENDURANCE_FTP_FACTOR = 0.70`, `REPS_CAP = 10`, `SHORT_REP_MAX_M = 1000` /
`SHORT_REP_FACTOR = 0.97` — see "Workout planner" for the short-rep pace discount).

### `templates.py`
The training content behind `fit train`: one entry per goal in `GOAL_TEMPLATES`
— plan length, phase structure and the weekly session mix — plus the two
constructors that build it, `_scale` and `_session`.

Imports nothing from `fit` at all, which is the point of the split: adding a
goal or recalibrating a base is a change to this file only, and never touches
the code that expands one. `training.py` imports `GOAL_TEMPLATES` (it expands
them); `cli.py` imports the module to validate a stored plan's goal id.

The `_scale` clamps and `_session` fields are consumed by `training._scaled`
and `training._build_session` — this file documents what a template *author*
needs, `training.py` documents what the engine does with it. See "Goal
templates" and "Periodisation" for the domain reasoning.

### `training.py`
Pure functions turning a compact YAML plan description into a full multi-week
periodised schedule. No file I/O, no network, no typer — `cli.py` reads the
description text and hands it in (same split as `importers.py` and path
reading). May import `compute`, `planner` and `templates` (pure → pure). See
"Training plans" for the feature-level design.

Key functions:
- `parse_plan_spec(text: str) -> dict` — lazy `import yaml`, `yaml.safe_load`,
  validate, and normalise every optional field from the goal template. Raises
  `ValueError` (with the install hint if PyYAML is missing) on anything
  malformed. Defends against YAML 1.1's coercions — see "Training plans"
- `expand_plan(spec, activities, reference: date) -> dict` — the engine; the
  full plan dict (metadata, targets, flat dated session list)
- `derive_targets(spec, activities, reference) -> dict` — the intensities the
  whole plan is built against, each with a `why` provenance string: description
  overrides win, else `planner.derive_*` over the recent window, else
  `FALLBACK_TARGETS`
- `derive_volume_scale(spec, activities, reference, template_week_seconds) ->
  tuple[float, str]` — how big the plan's opening week should be, measured
  against what the user is actually training now; see "Starting volume"
- `intended_peak(template) -> float` / `derive_weekly_ramp(template,
  phase_by_week, build_recover) -> float` / `_week_roles(...)` — the
  length-independent ramp; see "Plan length"
- `template_sports(goal) -> set` — the sports a goal's session mix uses; scopes
  target derivation
- `volume_sports(goal) -> set` — the sports whose volume the plan can actually
  *scale*, i.e. every sport except strength (whose sessions carry no `scale`).
  What the volume measurement uses, so a triathlete who does no gym work isn't
  scaled down for it
- `template_lifts(goal) -> list` — the barbell lifts a goal prescribes, in
  first-appearance order; the strength counterpart to `template_sports`
- `derive_lift_1rm(recent, exercise)` / `derive_lift_target(recent, exercise)` —
  the raw e1RM measurement and its plausibility-guarded front door, mirroring
  `planner.derive_run_5k` / `planner.derive_target`. A separate guard rather
  than an entry in `PLAUSIBLE_TARGETS` because strength needs one bound per
  lift, not one per sport
- `strength_weekly_e1rm(current, goal, roles, increment) -> list[float]` — the
  per-week e1RM path; mirrors `_week_multipliers`' shape so the two
  progressions can't disagree about which weeks advance
- `reachable_e1rm(current, roles, increment) -> float` — the most this plan's
  length can honestly add, at one increment per build week. Both the derived
  goal and the too-ambitious warning are measured against it
- `working_weight_from_1rm(e1rm, reps, increment) -> float` —
  `compute.estimated_1rm` read backwards, rounded to loadable plates
- `plan_week_roles(plan) -> list[str]` — the week roles recovered from a stored
  plan, so retargeting sits a rewritten session at the same point on the curve
  expand_plan put it
- `session_to_build_args(session) -> tuple[str, str, dict] | None` —
  `(sport, workout_type, params)` for `planner.build_plan`; `None` for extras
- `match_completion(sessions, activities) -> list[dict]` — copies with
  `"completed"` set: same sport within ±1 day, each activity claiming at most
  one session (nearest first) so one ride can't tick off a whole week
- `group_by_week(sessions) -> list[dict]` — the row structure
  `display.render_training_plan` renders; also attaches each session's
  `"description"` line so `display.py` prints text rather than composing it
  (same split as `planner.describe_plan` → `render_plan_saved`)
- `plan_summary(plan, today) -> dict` — header figures for `fit train show`
- `describe_session(session) -> str` — one human line per session
- `retargetable(sessions, today) -> list[dict]` / `retarget_sessions(plan,
  targets, today) -> dict` — re-derive intensity for the sessions still ahead;
  see "Retargeting"
- `_test_identity(sport, template_session) -> tuple` / `_test_week_sessions(
  week_one, monday, testable) -> list[dict]` — week 0's benchmarks-only week;
  see "The test week"
- `sync_window(sessions, today, window_days) -> list[dict]` — the still-
  unscheduled, non-extra sessions inside the rolling horizon; what makes
  `fit train sync` idempotent
- `future_scheduled(sessions, today) -> list[dict]` — scheduled sessions still
  ahead of today; what `clear` unschedules and what `import` warns about

Key constants (the goal templates themselves live in `templates.py`):
`PROGRESSION_DEFAULTS` (`build_recover` 3:1, `taper_weeks` 2),
`REFERENCE_RAMP_PCT = 8` (the rate templates are calibrated against, not a plan
default — see "Plan length"), `RECOVERY_FACTOR = 0.6`, `TAPER_START = 0.75` /
`TAPER_END = 0.45`, `MIN_PLAN_WEEKS = 4`, `EXTRA_DURATIONS_S`,
`FALLBACK_TARGETS`, and the starting-volume knobs (`VOLUME_SCALE_MIN` 0.6 /
`VOLUME_SCALE_MAX` 1.25, `RECENT_VOLUME_WEEKS` 8, `VOLUME_RAMP_WARN` 2.2).

### `cli.py`
Typer app. One function per subcommand. Each function: call storage → call compute → call
display. Nothing else.

Shared coordination helpers (small, single-purpose, and mostly one level deep — a
few intentionally compose one another, noted below):
- `_load_activities() -> list[dict]` — the preamble every history-reading command
  shares: `ensure_data_dir` → `read_activities_with_warnings` → `render_warnings`
- `_recompute_and_write_pbs(activities) -> dict` — recomputes `pbs.json` and
  writes it; called after every successful import and by `_get_fresh_pbs` when
  the cache is stale
- `_get_fresh_pbs(activities) -> dict` — reads `pbs.json`, recomputing via
  `_recompute_and_write_pbs` if the cache is stale
- `_windowed_pbs(activities, start, end) -> dict` — PBs computed fresh over a
  date-filtered slice, bypassing the `pbs.json` cache entirely
- `_pbs_for_window(activities, months, today) -> dict` — picks between the two
  above for a `--months`/`pbs_window_months` value (0 = cached all-time via
  `_get_fresh_pbs`, else windowed via `_windowed_pbs`); shared by the `pbs`
  command and `_dashboard_window`
- `_write_new_baseline(value) -> dict` — builds and persists a
  `{"baseline_date", "baseline_value"}` fitness.json dict for `value` dated
  today; shared by `_get_or_init_fitness_baseline` (lazy init) and
  `fitness_reset` (explicit re-anchor)
- `_get_or_init_fitness_baseline(activities) -> dict` — lazy-cache-if-missing,
  mirroring `_get_fresh_pbs`, via `_write_new_baseline`; `fitness.json`'s
  baseline is sticky rather than auto-recomputed (see "Fitness index")
- `_fitness_snapshot(activities, today, baseline, window=None) -> dict` — the
  `{"current", "baseline_date", "weekly"}` dict `render_dashboard`/`render_fitness_index`
  consume; callers fetch `baseline` via `_get_or_init_fitness_baseline` first
- `_dashboard_window(all_activities, timerange, config_months, today) -> dict` —
  resolves the dashboard's `--timerange` / `pbs_window_months` / all-time precedence
  into `{"activities", "pbs", "window_months", "window_label", "date_window"}`,
  via `_pbs_for_window`
- `_import_and_report(new_activities) -> None` — the shared tail of every
  import path (dedupe via `storage.activity_exists`, write, print new-PB
  messages, recompute the PB cache, print the imported/skipped summary); used
  by both `import_activity` and `garmin_sync`
- `_require_training_plan() -> dict` — the active plan, or the
  render-and-exit "no active plan" path
- `_show_plan(plan, activities, weeks) -> None` — the shared `train import`/`train
  show` tail: match completion, group by week, render
- `_login_or_exit()` — the `garmin.login()` + `GarminAuthError` → exit dance
  shared by `train sync` and `train clear`
- `_prompt_params(specs: list[dict]) -> dict` — `typer.prompt`s each
  `planner.workout_params` spec (Enter accepts the shown default), re-prompting
  on `ValueError` from the spec's parser; a spec carrying a `"derive"` callable
  (see `planner.recommend_defaults`) gets its default computed at prompt time
  from the answers so far. The only interactive prompting in `cli.py` (the other
  is `garmin.login`'s credential flow)

`import_activity` is the one command with real branching beyond storage → compute
→ display: it inspects `path`'s filesystem shape (directory vs file,
`activities.csv` present, suffix) to pick which `importers.py` entry point to
call. That's classification of the raw CLI argument, not business logic, so it
stays inline rather than moving to `importers.py` (which doesn't own path-shape
decisions) or a private helper (which would just relocate the same branches).

---

## Activity data shape

```python
{
    "id": "2024-01-15T08:30:00",      # ISO 8601, used as filename key
    "type": "run",                     # "run" | "cycle" | "walk" | "hike" | "swim" | "squash" | "canoe" | "strength"
    "date": "2024-01-15",
    "distance_km": 10.2,
    "duration_seconds": 3120,
    "elevation_gain_m": 45,            # optional
    "avg_heart_rate": 152,             # optional
    "max_heart_rate": 171,             # optional
    "avg_power": 187,                  # optional, watts; TCX (lap AvgWatts) / FIT (session avg_power) only
    "best_power": {"1min": 304, "5min": 227, "20min": 141},  # optional, FIT only, see "Power windows"
    "source": "garmin",                # "garmin" | "strava"
    "splits": {"5k_seconds": 1423, "10k_seconds": 2950},  # optional, see "Split PBs"
    "hr_zones": {"zone1_seconds": 120.0, "zone2_seconds": 340.5, "zone3_seconds": 890.0,
                 "zone4_seconds": 210.0, "zone5_seconds": 40.0},  # optional, see "HR zones"
    "exercises": [                     # strength activities only, see "Strength sessions"
        {"name": "deadlift", "sets": [{"reps": 10, "weight_kg": 100.0}]},
    ],
}
```

All optional fields must be handled with `.get()` in `compute.py` and `display.py`.
Never assume a field exists on an activity dict.

---

## Personal bests cache

`pbs.json` caches the result of `compute.all_personal_bests()` to avoid scanning all
activities on every command. The cache stores a `computed_from` count alongside the
PB data. On startup, `cli.py` compares this count against the current number of activity
files. If they differ, the cache is stale and must be recomputed and rewritten.

```python
{
    "computed_from": 47,
    "run": {
        "fastest_5k_seconds": 1423,
        "fastest_5k_date": "2024-03-12",
        "longest_distance_km": 21.1,
        "longest_distance_date": "2024-02-18"
    },
    "cycle": {
        "fastest_50k_seconds": 6120,
        "longest_distance_km": 87.4
    },
    "swim": {
        "fastest_1k_seconds": 1080,
        "fastest_1k_date": "2024-05-04",
        "fastest_500m_split_seconds": 512,
        "fastest_500m_split_date": "2024-06-01"
    },
    "strength": {                       # nested per exercise — see "Strength sessions"
        "deadlift": {"heaviest_set_kg": 130.0, "heaviest_set_date": "2026-09-04",
                      "best_e1rm_kg": 143.0, "best_e1rm_date": "2026-09-04"}
    }
}
```

---

## Split PBs

Separate from the "dedicated effort" PBs above (a whole activity whose total
distance is close to a milestone), split PBs find the fastest continuous segment of
a given distance *within* any activity's track — e.g. the fastest 5k hidden inside a
10k run. This is the same thing Strava calls "Best Efforts".

There is no persisted stream of trackpoint data anywhere in this app. Instead,
`import_tcx`/`import_fit` compute split times once, at import time,
against `compute.SPLIT_DISTANCES_KM` for that activity's type, using a transient
in-memory (elapsed_seconds, distance_km) point stream that is discarded immediately
after — only the resulting numbers are stored, as the activity dict's optional
`splits` field (e.g. `{"5k_seconds": 1423}`). Strava-CSV-only (no linked file)
activities never get a `splits` field — there's no track data to
derive it from, and none is needed: `all_personal_bests`/`detect_new_pbs` already
use `.get("splits", {})` throughout, so its absence is silently correct.

`compute._split_pbs` (one of `_candidate_pbs`'s four composed helpers — see
`compute.py`'s key-functions list above) scans every activity's `splits` field per
configured distance and stores the type's best as `fastest_{label}_split_seconds`/`_date`
in `pbs.json`, distinct from the dedicated-effort `fastest_{label}_seconds`/`_date`
keys — both coexist per type without collision:

```python
{
    "run": {
        "fastest_5k_seconds": 1423,          # dedicated: a whole ~5k activity
        "fastest_5k_date": "2024-03-12",
        "fastest_5k_split_seconds": 1390,     # split: the best 5k found inside any run
        "fastest_5k_split_date": "2024-04-02"
    }
}
```

At display time, `compute.best_pb_per_label` (called from `display.render_pbs_table`)
collapses each label's dedicated/split pair down to whichever is faster, so the PBs
table shows one row per label, not two — `pbs.json` itself still stores both keys
independently, exactly as above, since `detect_new_pbs` needs to track each category
separately.

If a distance isn't in `SPLIT_DISTANCES_KM`, it is **gone**, not merely
unqueryable: fit keeps no copy of the source file, so adding a distance to the
list only affects activities imported afterwards. Recomputing it for existing
history means re-importing from wherever the originals still live — a Garmin
export, the watch, Strava. This is the cost of not keeping originals, and it is
the same going-forward-only rule `hr_zones` follows.

---

## Power windows

`best_power` stores the highest average power sustained over each of
`compute.POWER_WINDOWS_S` — 1, 5 and 20 minutes — computed once at import from
the same transient point stream splits and HR zones use, then discarded with it.
Same going-forward-only rule: an activity imported before this existed simply
has no `best_power`, and there is no backfill.

**Why it exists.** Stored `avg_power` is a whole-activity mean, so a 20-minute
effort inside a longer ride is invisible in it — on a real ride, a 141W best
20-minute window sat behind a 128W activity average, and an FTP test's warmup
and cooldown drag the mean down much harder than that. `planner.derive_ride_watts`
reads `best_power["20min"]` as its FTP proxy, at the conventional 95%, and only
falls back to `avg_power` (**unadjusted** — a long ride's average is already
sub-threshold, so discounting it again would stack two conservative estimates)
when no window was recorded.

1min and 5min come free from the same sweep and are what a rider actually reads
a ride by; only the 20min figure has a consumer today.

**FIT only.** TCX would need a namespace-tolerant `Watts` extractor plus a
change to how its stream handles position-less trackpoints, for a format
nothing in practice feeds — `garmin-sync` pulls FIT and Strava supplies CSV.

**Points without distance are kept.** The FIT record loop used to require a
`distance` field, which would have dropped every record of an indoor trainer
ride — exactly where an FTP test happens. It now keeps a record carrying either
distance or power, `distance_km` is `None` when absent, and `_compute_splits`
filters those out since only it needs distance.

---

## Strength sessions

Strength is a full activity type — logged, PB-tracked and counted in volume —
rather than the placeholder `extras: {strength: N}` a training plan schedules.
`fit train`'s side of it (periodised progression toward a target working
weight) is designed but not built; see `docs/STRENGTH_PLAN.md`.

**Shape.** A strength activity carries an `exercises` list instead of a track:
`[{"name": "deadlift", "sets": [{"reps": 10, "weight_kg": 100.0}, ...]}, ...]`.
Names are FIT's own normalised lowercase/underscore form (`shoulder_press`),
an open string like `type` itself rather than a closed enum. Weight is always
kg — fitparse's `set.weight` is scaled to kilograms at source, so
`weight_display_unit` is a display preference, not a unit to convert from.

**`distance_km` stays present and meaningless**, usually 0.0, exactly as it is
for squash — `NO_DISTANCE_TYPES` is what suppresses it everywhere it would be
displayed or ranked. Dropping the key instead would have made strength the one
type whose activity dict is missing one of the five always-present fields.

**PBs are keyed by exercise, not by distance label** — two per exercise,
tracked independently: `heaviest_set_kg` (the heaviest single set at any rep
count) and `best_e1rm_kg` (the best `compute.estimated_1rm` across all sets).
They genuinely differ — a heavy triple can be the heaviest set while a set of
ten is the better e1RM — and either can be the more recent. This is why
`_strength_pbs` sits outside `_candidate_pbs`'s four helpers rather than
becoming a fifth, and why `pbs.json`'s `"strength"` entry is nested one level
deeper than every other type's.

**Import is FIT-only**, from `set` messages rather than a point stream. Rest
sets (`set_type == "rest"`) are dropped, consecutive sets of the same exercise
group into one entry, and a category fitparse can't name is kept as
`"unknown_<int>"` rather than dropped — an unrecognised exercise still counts
toward the session's volume and load, and keeping the code distinct stops two
unmapped exercises merging into one PB line. `category_subtype` (front vs back
squat) is deliberately ignored for now; it is an undecoded uint16 indexing
per-category `<category>_exercise_name` enums, and adding it would change what
PBs are keyed on.

In the dashboard's recent-activity table, a gym session's effort column shows
**tonnage** rather than the `"—"` a pace would be. It is the closest thing
strength has to distance: two 45-minute sessions are not the same work, and
because it counts reps and sets as well as load it registers a week where the
plan grew rather than only one where the bar did.

Splits and best-power are skipped for strength (there is no distance or power
stream to sweep), but **HR zones are not**: the FIT record loop keeps records
carrying only a heart rate, which is the exact shape a gym session's records
have. Same going-forward-only rule as `hr_zones` everywhere else.

---

## HR zones

The dashboard's recent-activity table shows a per-activity breakdown of time
spent in each of 5 heart-rate training zones, as a segmented colour bar (see
`display.render_history_table`/`_format_hr_zones`), replacing the older plain
"Avg HR"/"Max HR" columns.

Zones are the standard 5-band %-of-max-heart-rate model: Z1 50-60%, Z2 60-70%,
Z3 70-80%, Z4 80-90%, Z5 90-100%+ of `max_heart_rate` (`compute.HR_ZONE_BOUNDARIES`).
`max_heart_rate` is a new config key (bpm, `0` = unset) — the user's own value,
not derived or calibrated from activity data (unlike the fitness index's
self-calibrating HR multiplier).

Like split PBs, there is no persisted stream of trackpoint data.
`import_tcx`/`import_fit` compute HR zone-seconds once, at import time, from the
same transient (elapsed_seconds, distance_km, ...) point stream used for splits,
now also carrying `hr` where the format provides it — TCX's per-trackpoint
`<HeartRateBpm>` (a new extractor,
`_tcx_trackpoint_heart_rate`, since previously only lap-level HR was read), and
FIT's per-record `heart_rate` field (already decoded by `fitparse`, previously
unread). `compute.hr_zone_seconds` attributes each gap between samples to the
earlier sample's zone and returns `{}` if `max_heart_rate` is unset or fewer than
2 points carry `hr` — the result is attached as the activity dict's optional
`hr_zones` field (raw seconds, e.g. `{"zone1_seconds": 120.0, ...}`), and the raw
point stream is discarded immediately after, same as splits.

Because `max_heart_rate` is only consulted at import time, this is
**going-forward only**: activities imported before the config key was set (or
before this feature existed at all) simply have no `hr_zones` field, and the
dashboard column shows `"—"` for them — same as Strava-CSV-only imports, which
have no track data to derive zones from regardless. There is no backfill and no
recompute-on-config-change; changing `max_heart_rate` later only affects
activities imported afterward (consistent with `fitness.json`'s baseline being
sticky rather than silently recomputed — see "Fitness index").

`compute.hr_zone_percentages` converts the stored seconds into
`{"zone1": pct, ..., "zone5": pct}` at display time — this arithmetic
deliberately lives in `compute.py`, not `display.py`.

---

## Fitness index

A single number, rescaled to a baseline of 100, tracking overall training load
over time — similar in spirit to Strava/Garmin/TrainingPeaks, but computable
entirely from what this app already stores. Deliberately coarse, not a precise
physiological model — a "don't need it to be perfect" stretch goal.

**Per-activity load** is MET-hours (`compute.met_for_activity(activity) *
duration_hours`) — the Ainsworth Compendium-of-Physical-Activities-style public-
health unit, using `compute.MET_TABLE`'s small, coarse, pace/speed-banded values
per type. This is the *only* base that works for every activity regardless of
source, since `avg_heart_rate`/`avg_power` are only populated from TCX/FIT
imports (confirmed via `importers.py`) — never bare Strava-CSV rows — while
`type`/`distance_km`/`duration_seconds` are always
present. No FTP/threshold-pace/resting-HR calibration is required from the user.

When `avg_heart_rate` **is** present, the MET-hours base is scaled by a clamped,
self-calibrating multiplier: `avg_heart_rate / (median avg_heart_rate across all
of this user's other HR-tagged activities of the same type)`, clamped to
`[0.8, 1.25]` so one anomalous reading can't swing a day's load too far
(`compute.median_hr_by_type`, `compute.activity_load`). The median is inclusive of
the activity being scored, which gives the first-ever HR-tagged activity of a type
a free, correct neutral multiplier of 1.0 (median of one value = itself).

**Known limitation**: `median_hr_by_type` is recomputed fresh from *all* current
activities on every invocation, not frozen at each activity's own point in time —
so a load value computed for an old activity can shift slightly as more HR-tagged
data of that type is imported later. Acceptable under the "coarse, not perfect"
goal, and consistent with how `pbs.json`/`all_personal_bests` already recompute
fresh from full history rather than incrementally.

**Smoothing**: daily load totals (`compute.daily_load_totals`) feed a 42-day
exponentially-weighted moving average (`compute.fitness_ewma_daily`) — Coggan's
"CTL"/"Fitness" formula from the TrainingPeaks Performance Management Chart,
computed over *every calendar day* (not just days with activity) so rest periods
correctly decay the number. Seeded as `value[first_day] = load[first_day]` (not 0)
to avoid an artificial multi-week ramp-up for anyone backfilling years of history —
the trade-off is a brief artificially-high blip on day one that settles within the
42-day window. No 7-day "Fatigue"/Form companion metrics in v1 — a single Fitness
number only.

**Baseline and rescale**: the first time the index is ever needed (lazily, mirroring
`pbs.json`'s lazy-recompute pattern — see `cli._get_or_init_fitness_baseline`), the
current EWMA value is persisted to `fitness.json` as the baseline. From then on,
`index(t) = 100 * EWMA(t) / baseline_value`. Unlike `pbs.json`, **this baseline is
sticky** — never auto-recomputed as new activities are added, only replaced by
explicit `fit fitness-reset` (which re-anchors it to today's EWMA value and prints
the old→new comparison).

**One combined index**, not per-sport — matches how Strava/Garmin/TrainingPeaks
actually work. The current headline value and the underlying EWMA calculation are
**never** filtered by `--sport`/config `sports`. On `fit dashboard`, `--timerange`
narrows which slice of the trend *sparkline* is shown (via
`compute.filter_series_by_date`, applied to the already-computed index series —
not by re-running the EWMA over a truncated activity list, which would wrongly
discard pre-window decay/carry-over) but never changes the headline value itself,
which always reflects full history as of today. This is why `fit dashboard`'s
fitness block renders *before* the empty-activities/empty-sport-match early
returns — see `display.render_dashboard`.

---

## Config defaults

`~/.fit/config` is a hand-editable plain-text file — `key = value` per line, blank
lines and `#` comments (leading or inline) ignored, unknown keys ignored, malformed
lines silently skipped rather than raising. `ensure_data_dir()` writes a default,
fully-commented copy on first run so the file is discoverable via `cat ~/.fit/config`
without a setup step. Edits take effect on the *next* command invocation — nothing
is cached across runs.

`read_config()` merges the parsed file over `DEFAULTS` (`storage.py`):

```python
DEFAULTS = {
    "sports": [],               # empty = all types shown
    "pbs_window_months": 0,     # 0 = all-time PBs
    "history_count": 5,         # rows in the dashboard's embedded history table
    "dashboard_weeks": 12,      # weeks shown in dashboard volume/fitness sparklines (0 = all)
    "max_heart_rate": 0,        # bpm, 0 = unset (HR zone breakdown column shows "—" until set)
    "train_sync_window_days": 14,  # how far ahead `fit train sync` schedules sessions
    "show_sparkline": True,     # weekly volume (hours) sparkline block
    "show_pbs": True,           # personal bests block
    "show_sports_summary": True,  # sports summary block (all types, count + time + distance)
    "show_fitness_index": True,   # fitness index (EWMA training load rescaled to a baseline of 100) block
    "show_calendar": True,        # calendar block (active days over the last 2 months)
}
```

These are deliberately *functional*, not cosmetic — every key changes what a command
actually shows, not how it's formatted (no units/week_start/hr_zones/name knobs;
those were unused dead config in an earlier draft and were dropped as
out-of-scope). Each key's parsed type is inferred from its `DEFAULTS` value (bool,
list, or plain string/int) — see `storage._parse_config_text`/`_serialize_config_text`.

Consumers:
- `sports` — `compute.filter_by_types` restricts which activity types `fit dashboard`
  shows (history table, sparkline, and which type-rows appear in the PBs table). It
  never changes what the all-time `pbs.json` cache computes over — filtering is a
  display-time concern only, so `computed_from`'s activity count stays meaningful
  regardless of the configured sport filter. `fit dashboard --sport X` overrides it
  for a single invocation — single sport only, unlike the config file's
  comma-separated list, and it does not persist back to config.
- `pbs_window_months` — default for "PBs in the last N months" on both
  `fit dashboard` and `fit pbs`; `fit pbs --months N` overrides it per-invocation
  (`--months 0` forces the all-time cached path). A non-zero window bypasses
  `pbs.json` entirely: `cli._windowed_pbs(activities, start, end)` calls
  `compute.all_personal_bests` fresh over `compute.filter_by_date(activities,
  start, end)` — no cache read or write, so the windowed view can never corrupt
  the all-time cache. `_windowed_pbs` itself just takes an explicit `(start, end)`;
  callers derive it via `compute.months_ago` (this key / `--months`) or
  `compute.parse_timerange` (`fit dashboard --timerange`, below) — both mechanisms
  share the same cache-bypass helper. `fit dashboard --timerange X` takes priority
  over this config key for that invocation (it windows PBs, and everything else on
  the dashboard, via the same date range) — `pbs_window_months` is ignored
  entirely whenever `--timerange` is passed.
- `history_count` — row count for the dashboard's embedded recent-activity table
  (the standalone `fit history N` command keeps its own explicit `N` argument,
  unaffected by this key).
- `dashboard_weeks` — how many recent weeks the dashboard's weekly-volume and
  fitness-trend sparklines show (0 = all weeks). Applied as a final display-time
  slice of the (already sorted) weekly series in `display.render_dashboard` only
  — the standalone `fit fitness` deep-dive view stays full-history.
  There is deliberately no CLI flag for this. `--timerange` takes priority: when
  it's driving the dashboard window, this cap is skipped so the explicit flag's
  range wins (same precedence as `pbs_window_months`).
- `max_heart_rate` — used only at import time (`importers.import_tcx`/
  `import_fit`, threaded down from `cli.py`) to compute each
  newly-imported activity's `hr_zones` breakdown (see "HR zones"). Changing it
  later has no effect on already-imported activities — no backfill, no silent
  recompute, same "explicit only" precedent as `fitness.json`'s baseline
  (`fit fitness-reset`).
- `train_sync_window_days` — how many days ahead `fit train sync` pushes and
  schedules training-plan sessions (the rolling window that makes re-running it
  idempotent). `fit train sync --days N` overrides it per-invocation. Purely
  behavioural: it changes how much of the plan reaches the Garmin calendar.
- `show_sparkline` / `show_pbs` — toggle those two dashboard blocks off entirely.
- `show_sports_summary` — toggles the dashboard's sports-summary block
  (`display.render_sports_summary`). Unlike `sports`, this block is never
  restricted by `--sport`/config `sports` — it always covers every activity type
  present. Like the history table and sparkline (and unlike the PBs block), it is
  *not* restricted by `pbs_window_months` — only `--timerange` narrows it, since
  that flag date-filters the whole dashboard's activity list before any block
  renders, whereas `pbs_window_months` only ever windows the PBs computation.
- `show_fitness_index` — toggles the dashboard's fitness-index block (see
  "Fitness index"). Like `show_sports_summary`, never restricted by `--sport`. Its
  headline value is *never* restricted by `--timerange`/`pbs_window_months`
  either (always full-history/as-of-today) — only its trend sparkline narrows with
  `--timerange`.
- `show_calendar` — toggles the dashboard's calendar block
  (`display.render_calendar` + `compute.activity_calendar`, same view as the
  standalone `fit calendar` command). Unlike `show_sports_summary`/
  `show_fitness_index`, it *is* restricted by `--sport`/config `sports` — it
  shares the same sport-filtered activity list as the history table
  immediately above it. `--minimal`/`fit dash` does not force this toggle
  off (only `show_pbs`/`show_sports_summary` are forced off), so the
  calendar block stays visible in minimal mode, same as the sparkline and
  fitness-index blocks.

---

## Stretch features (not yet implemented)

- Further goal templates — pure data entries in `templates.GOAL_TEMPLATES`; the
  engine around them is already goal-agnostic, so a new goal is a change to
  that one file. See "Training plans".
- Adaptive reflow when sessions are missed — `fit train show` reports adherence
  but never rewrites the plan. Deliberately out of scope for v1.
- Garmin multisport/brick single-workout files — bricks are currently two
  same-day sessions instead (that schema is unverified).
- Multiple concurrent training plans (`train/plan.json` holds exactly one).
- USB workout delivery — encode a workout .FIT file (needs a FIT *encoder*
  dependency, e.g. `fit-tool`; fitparse only reads) and copy it to a mounted
  watch's `GARMIN/NEWFILES/`, for pushing without a Garmin Connect login.

Do not design current modules around these features.

---

## Garmin integration

`garmin.py` is implemented — live API sync via the unofficial `garminconnect`
library, gated behind the optional `garmin` extra (`pip install -e '.[garmin]'`).
It is the **only** module that talks to the Garmin Connect network API. Like
`importers.py`, it never touches the data dir or builds fit's activity dict shape
directly — it returns raw FIT bytes and raw Garmin activity summaries, and
`cli.py`'s `garmin-sync` command feeds those bytes through
`importers.import_fit` -> `storage.write_activity`, the same path every other
import takes.

The optional `garminconnect` dependency is imported lazily inside functions (same
pattern as `importers.import_fit`'s lazy `fitparse` import), so every other
command works without it installed — `garmin.GarminAuthError` with
`garmin.INSTALL_HINT` is raised instead if it's missing.

Key functions:
- `login()` — resumes a saved session from `TOKEN_STORE` if present; otherwise
  prompts for email/password (and MFA if needed) via `typer`, then saves a
  session token for next time. The session token lives at the library-default
  `~/.garminconnect`, deliberately outside `~/.fit` so a zipped backup of the
  data dir never carries a login.
- `list_recent_activities(client, start_date, end_date) -> list[dict]` — raw
  Garmin Connect activity summaries in range, not yet in fit's activity shape.
- `download_activity_fit(client, garmin_activity_id) -> bytes` — raw FIT bytes
  for one activity; unwraps Garmin's zip-wrapped "original" export format.
- `push_workout(client, workout_payload: dict) -> dict` — uploads one
  workout-service payload (built by `planner.py` — this module never shapes
  workout dicts itself) and returns the raw response dict (contains
  `"workoutId"`). Sends rather than fetches; used by `fit plan` (see "Workout
  planner").
- `get_workout(client, workout_id) -> dict` — fetches one workout back as its
  stored workout-service dict; counterpart to `push_workout`, used by
  `scripts/diff_workout.py` to verify `planner.py`'s payload schema against a
  live round-trip (see "Workout planner" / that module's docstring).
- `schedule_workout(client, workout_id, date_str) -> dict` — places an
  already-pushed workout onto a single `YYYY-MM-DD` date in the Garmin
  calendar. Deliberately atomic (one workout, one date) so a future multi-week
  planner schedules a whole progression by calling it once per session rather
  than needing a batch-shaped entry point. `date_str` is pre-validated by
  `planner.parse_schedule_date` — garmin.py stays a thin API boundary and does
  no date logic. Used by `fit plan --schedule DATE` (see "Workout planner").

- `get_scheduled_workouts(client, year, month) -> dict` — raw scheduled-workout
  entries for one calendar month; lets a caller see what is actually on the
  Garmin calendar rather than trusting local state alone.
- `unschedule_workout(client, scheduled_workout_id)` — removes one scheduled
  workout from the calendar. Takes the *schedule* id returned by
  `schedule_workout`, not the workout id (the same workout can sit on several
  dates). Atomic for the same reason `schedule_workout` is: `fit train clear`
  calls it once per session.

`fit garmin-sync --days N` (default 14, `cli.py`) logs in, lists recent
activities, downloads each as FIT bytes to a temp file, imports it via
`importers.import_fit`, and hands the results to the same `_import_and_report`
tail every other import path shares (dedupe, write, save original, print new
PBs, recompute PB cache). Temp files are cleaned up in a `finally` block.

---

## Workout planner

`fit plan --sport run --type intervals [--no-push] [--schedule DATE]` generates a
structured workout interactively (typer prompts, Enter accepts each default),
saves it to `~/.fit/plans/<created-timestamp>.json`, and pushes it to Garmin
Connect via `garmin.push_workout` — the workout appears under Training >
Workouts on the watch's next sync. Delivery is Connect-only: no route data, no
USB path (deliberately deferred — see "Stretch features").

`--schedule DATE` (`YYYY-MM-DD`) additionally places the pushed workout on that
date in the Garmin calendar via `garmin.schedule_workout`, writing the date back
into the plan file as `scheduled_date`. The date is validated up front by
`planner.parse_schedule_date` — before the interactive prompts and the push — so
a typo fails fast and offline. Scheduling needs a pushed workout to attach to,
so `--schedule` with `--no-push` is rejected rather than silently ignored. This
is single-workout scheduling only; the full multi-week periodised planner that
would schedule a whole progression stays deferred (see "Stretch features"), but
the atomic `garmin.schedule_workout` wrapper and the pure
`planner.parse_schedule_date` primitive are shaped so that planner composes on
top of them rather than replacing them.

**Steady vs quality types**: the quality types below are all warmup → main →
cooldown. The four *steady* types (`planner.STEADY_TYPES` — run `easy`/`long`,
cycle `endurance`/`long`, swim `continuous`) are instead a **single block**
carrying a wide target band (`EASY_PACE_TOLERANCE_S_PER_KM` /
`ENDURANCE_POWER_TOLERANCE_W`), because a warmup inside an easy run is just
more easy running. They exist chiefly so `fit train` can build a whole week out
of real workouts; `fit plan --sport run --type easy` reaches them too.

**Strength is shaped differently from every cardio combo**, in three ways
that are all forced by Garmin's own schema (dumped from a real workout — see
`docs/STRENGTH_PLAN.md` Findings, and `scripts/dump_workout.py` to re-read it):

- **Weight is not a target.** `targetType` stays `no.target`; the load rides on
  the step as `weightValue` (kilograms) + `weightUnit`. Reps are the
  `endCondition` (`"reps"`, id 10) with the count in `endConditionValue`.
- **One `RepeatGroupDTO` per exercise**, siblings in the segment, numbered
  globally — the same repeat machinery run intervals already use, several
  times over. This is why a gym session of several exercises needed no new
  payload concept.
- **`params` carries a list**, `{"exercises": [{"exercise", "sets", "reps",
  "target_weight_kg"}, ...]}`, so `fit plan` prompts in a loop until the
  exercise is left blank (`planner.repeated_param_specs` →
  `cli._prompt_repeated_params`). Every other combo answers one value per
  prompt.

The exercise vocabulary is shared with the import side: `importers.py` stores
FIT's lowercase `exercise_category` name and the payload upper-cases it, so
`deadlift` and `DEADLIFT` are the same lift by construction. `exerciseName`
(the variant, e.g. `BARBELL_BACK_SQUAT`) is deliberately left unset — each of
the four lifts is a category in its own right.

`straight_sets` optionally opens with a `CARDIO`-tagged warmup and rests
between sets either on a timer or on the lap button (blank rest), matching how
Connect's own UI builds them. `strength`/`baseline` is the e1RM re-test: a
single untargeted top set at a fixed rep count, no warmup step and no
prescribed load — the ramp of lighter sets *is* the warmup, and
`compute._strength_pbs` reads only the best set, so the ramp costs nothing.

**Sport/type matrix** (`planner.WORKOUT_TYPES`): run has `intervals` (N × distance
reps at a pace target with timed recoveries), `tempo` (sustained block at pace),
`hills` (N × timed uphill efforts — deliberately *no* pace target, since gradient
makes pace meaningless), and `baseline` (Runna-style benchmark: best-effort fixed
distance, open target, used to re-measure current pace). swim has `intervals`
(distance reps with timed rest steps, optional pace-per-100m target). cycle has
`intervals` (timed blocks at a power target, 2:1 work:rest defaults), `hills`, and
`baseline` (FTP-test shape: sustained best effort, open target). run also has
the steady `easy` (timed block at easy pace) and `long` (distance at easy
pace); cycle has `endurance` (timed) and `long` (distance), both at ~70% of the
threshold estimate; swim has `continuous` (distance, optional pace target).

**History-derived defaults**: `planner.recommend_defaults` derives prompt defaults
from the last `RECENT_MONTHS` (6) of activities where possible, each with a `why`
provenance string the CLI prints before prompting. The derivations (standard
public-training-plan practice — Daniels/McMillan paces from a recent race, swim
CSS, bike FTP):
- run interval pace = 5k race pace, from the best recent 5k —
  `min(fastest_5k_seconds, fastest_5k_split_seconds)` over the windowed
  activities via `compute.all_personal_bests`; falls back to the fastest average
  pace of any recent ≥3 km run. Reps of `SHORT_REP_MAX_M` (1000) or less get a
  `SHORT_REP_FACTOR` (0.97, ~3% faster) discount: the reference is a *training*
  best, not a race result, so it understates race ability, and the discount puts
  short reps back inside Daniels' I-pace bracket (3k–5k race pace) instead of
  below its slow end. Because the rep distance isn't known until mid-prompt, this
  rec uses `"derive"` (a callable taking the params answered so far, returning
  the default string) instead of a static `"default"` — `cli._prompt_params`
  resolves it at prompt time, and `render_plan_recommendations` prints just its
  `why` line (the concrete value appears in the prompt once rep distance is known)
- run tempo pace = 5k pace × `TEMPO_FACTOR` (1.07 — Daniels "T" ≈ 7–8% slower
  than "I"/5k pace), rounded to 5 s
- swim interval pace = CSS per 100m: two-point critical-speed model
  `(T_1k − T_500m) / 5` over the best recent 500m/1k times (same math as the
  classic 400/200 CSS test, using the split distances fit already stores); falls
  back to best recent 1k pace, then median recent swim pace
- cycle interval watts = max `avg_power` among recent ≥20-min rides (coarse FTP
  proxy — only session averages are stored; "coarse, not perfect", like the
  fitness index), rounded to 5 W
- easy/long run pace = 5k pace × `EASY_FACTOR` (1.3 — Daniels "E" is 59–74%
  vVO2max against "I" at 95–100%), via `easy_pace_from_5k`
- endurance/long ride watts = the FTP proxy × `ENDURANCE_FTP_FACTOR` (0.70, the
  middle of the conventional 65–75% "zone 2" band), via `endurance_watts_from_ftp`
- reps = one more than the last saved plan of the same sport+type (capped at
  `REPS_CAP` = 10) — the standard build-by-one progression; this is the only
  consumer of `storage.read_plans()`

**Payload schema**: `planner.py` builds the Garmin Connect workout-service JSON
as plain dicts, replicating the schema of the reference models in garminconnect
0.3.6's `workout.py` rather than importing that module (it needs the optional
pydantic extra, which fit deliberately doesn't carry — "no Pydantic models"
convention). Sport ids: running=1/cycling=2/swimming=4; step types
warmup=1/cooldown=2/interval=3/recovery=4/rest=5/repeat=6; end conditions
time=2 (seconds)/distance=3 (meters)/iterations=7; targets no.target=1/
power.zone=2 (watts)/pace.zone=6 (m/s, low bound first as
`targetValueOne`/`targetValueTwo`). Repeats are `RepeatGroupDTO` steps with
nested `workoutSteps`; absent fields are omitted, not null. **`strength`/`straight_sets` verified against a live upload on 2026-09-05**:
a two-exercise push round-tripped through `garmin.get_workout` with every
field fit sent coming back unchanged — including a 62.5kg load returned as
62.5 under `unitKey: "kilogram"`, which settles that `weightValue` is read as
kilograms inbound (not the grams its unit `factor` hints at) and that a
half-kilo plate survives. The same push confirmed the `reps` end condition, a
*timed* rest step (Connect's own UI writes `lap.button`), a time-ended
`CARDIO` warmup, and a bare `category` with `exerciseName` unset.
**`strength`/`baseline` and `cycle`/`baseline` verified on 2026-09-05**, by
`fit train sync` pushing a test week: both round-tripped clean. The lifts came
back as a single `interval` step, `endCondition` `reps` = 3, `targetType`
`no.target`, `category` `SQUAT`/`BENCH_PRESS` with `exerciseName` unset and
**no `weightValue` at all** — which confirms an untargeted, unloaded top set
survives as one, rather than Garmin filling in a default. The ride came back as
warmup 1200s / interval 1200s / cooldown 600s, every step `no.target`,
confirming a time-ended block with an open target.
**Verified against a live upload on 2026-08-24**: a real `run`/`intervals` push round-tripped through
`client.get_workout_by_id()` unchanged — `targetValueOne`/`targetValueTwo` came
back as the low/high m/s bounds in that order, with step numbering intact,
confirming the target-value and step-numbering machinery every combo shares. The
other sport/type combos reuse the same builders but haven't each been
round-tripped individually; `scripts/diff_workout.py` performs the check (a
one-directional payload-vs-stored diff via `garmin.get_workout`), so re-run it
after first pushing an as-yet-unverified combo and update this note. It reads
either a `fit plan` file's stored `payload` or, with `--session YYYY-MM-DD`
(`--sport` to disambiguate a brick's two sessions), a training-plan session —
those store `params` rather than a payload, so it rebuilds one with the same
`planner.build_plan` call `fit train sync` makes at push time. **The four
steady types are still unverified against a live push** — they are the first
single-step workouts fit builds, so run the diff on one of them (e.g. a
`run`/`long`) before trusting a whole `fit train sync`.

**Save-before-push is deliberate**: the plan file is written before `garmin.login`
is attempted, so a failed push (no `garminconnect` installed, bad credentials,
network down) never loses the generated workout. On success the Garmin
`workoutId` is written back into the plan file as `garmin_workout_id`, and (if
`--schedule DATE` was passed) the calendar date as `scheduled_date` after the
scheduling call succeeds — so the plan file, not Garmin, stays the
reconstructable record of what fit put on the calendar.

Plan file shape (`plans/<id>.json`):

```python
{
    "id": "2026-07-03T09:15:02",       # creation timestamp, filename key
    "sport": "run",
    "workout_type": "intervals",
    "params": {"warmup_minutes": 10, "reps": 6, "rep_distance_m": 800,
                "target_pace": 268, "recovery": 120, "cooldown_minutes": 10},
    "workout_name": "Run intervals 6x800m @ 4:28/km",
    "payload": {...},                   # full Garmin workout-service dict
    "garmin_workout_id": 123456789,     # present only after a successful push
    "scheduled_date": "2026-08-28"      # present only after a successful --schedule
}
```

---

## Training plans

`fit train` expands a compact YAML **plan description** into a full multi-week
periodised schedule: a real Garmin workout per session, intensities derived from
the user's own history, rolled onto the Garmin calendar a rolling window at a
time. The intended workflow is that the user talks a goal through with an
external AI bot, the bot emits the description, and **fit owns all the
periodisation** — the description stays deliberately thin.

Built *on top of* the atomic scheduling seam, never replacing it: each session
is pushed with `planner.build_plan` → `garmin.push_workout` and placed with
`garmin.schedule_workout` one date at a time.

```
fit train import plan.yaml   # expand + save; --weeks N limits what's printed
fit train show [--weeks N]   # the plan with planned/done marks
fit train sync [--days N]    # push + schedule everything due in the window
fit train clear              # unschedule future sessions
```

### The standard form (YAML description)

The contract between the bot and fit. Only `goal` and `event_date` are required;
everything else defaults from the goal template. See `examples/training-plan.yaml`.

```yaml
goal: sprint_triathlon        # one of the goal ids in templates.GOAL_TEMPLATES
event_date: 2026-11-15        # anchor; the plan counts back from here
start_date: 2026-08-24        # optional; default = event_date - template length
days_per_week: 6              # optional; or [2, 4] to build frequency
                              # across the plan
rest_day: Mon                 # optional preferred rest day
extras: {strength: 2, yoga: 1}   # optional weekly counts; fit auto-places, never pushes
targets:                      # optional per-sport overrides (else history-derived)
  run_5k: "24:00"
  bike_ftp: 250
  swim_css_100m: "1:45"
  squat_goal_kg: 140          # per-lift goal e1RM; else derived from plan length
progression:                  # optional overrides of template defaults
  build_recover: [3, 1]
  weekly_ramp_pct: 8          # optional; else solved from the plan's length
  taper_weeks: 2
volume: 70                    # optional; % of the template's opening week
                              # (else measured from recent training)
test_week: true               # optional; open with week 0, a testing week
```

**PyYAML is YAML 1.1, and `parse_plan_spec` defends against its coercions**
rather than trusting `yaml.safe_load`'s output shape:
- an unquoted `2026-11-15` resolves to a `datetime.date`, not a string, so
  `_as_date_string` accepts both and routes the text through
  `planner.parse_schedule_date` (the single date-validation seam)
- an unquoted `24:00` resolves to the base-60 integer 1440 — which lands on
  exactly the seconds the field means, so `_as_seconds` accepts a bare int as
  already-seconds and quoted/unquoted forms normalise identically
- an unquoted `rest_day: no` would be `False`, so `_as_weekday`/`_as_int`
  reject bools explicitly (`isinstance(True, int)` is `True` in Python)

### Goal templates

### Plan length

The template's `weeks` is only a default. `start_date` sets the real length —
whole ISO weeks from its Monday through the event's week — with `MIN_PLAN_WEEKS`
(4) as the floor and no ceiling.

Phases reapportion proportionally to fill it (`_assign_phases`), but the volume
ramp needs more care: compounding a fixed rate over a longer block does not
produce a longer climb, it produces a *higher* one, which pins every scaled
session at its clamp and makes the extra weeks worthless. So the ramp is
**solved from the plan's actual length** rather than fixed:

- `intended_peak(template)` — the multiplier the goal reaches at *its own*
  default length under `REFERENCE_RAMP_PCT` (8). Derived from the template
  rather than declared beside it, so a plan at its default length behaves
  exactly as it did before this existed.
- `derive_weekly_ramp(...)` — solves `peak ** (1 / (build_weeks - 1))` so any
  length arrives at that same peak by the last build week, then takes
  `min(solved, REFERENCE_RAMP_PCT)`. The cap matters: a plan *shorter* than its
  template should reach a lower peak, which is what a short run-up honestly
  buys — chasing the full peak over four weeks would demand a ~70%/week ramp.
- `_week_roles(...)` — `'build' | 'recover' | 'taper'` per week, read by both
  the multiplier curve and the ramp derivation so the two can never disagree
  about which weeks actually ramp.

`progression.weekly_ramp_pct` in the description is now an explicit **override**
rather than a default (which is why it is absent from `PROGRESSION_DEFAULTS`);
the resolved value and whether it was derived are recorded in
`plan["progression"]`.

Net effect for `cycle_100k_sportive`: 4–12 weeks unchanged (8%, peaking lower
the shorter it is), 16 weeks at 5.5%, 26 at 3.2%, 40 at 1.9% — every one of them
peaking at the same 103km, with nothing pinned at a clamp. Note this fixes
length variance only; it does **not** change a template's calibration at its own
length, so `standard_triathlon`'s hand-lowered bases are still doing their own
separate job.

### Goal templates

`templates.GOAL_TEMPLATES` is the training-domain content: plan length, phase
structure, and the weekly session mix. It lives in its own module so a goal can
be added or recalibrated without touching the engine — see "`templates.py`".
All ten goals are implemented:

| Goal | Weeks | Days/wk | Phases (base/build/peak) | Shape |
|---|---|---|---|---|
| `run_5k` | 8 | 4 | 3/2/1 | long, 800m intervals, tempo, easy |
| `run_10k` | 10 | 5 | 4/3/1 | long, 1km intervals, tempo, 2 × easy |
| `run_half` | 12 | 5 | 5/3/2 | long, tempo (outranks intervals), 1km intervals, 2 × easy |
| `cycle_25k_tt` | 8 | 4 | 3/2/1 | long, 5min threshold reps, lengthening blocks, endurance |
| `cycle_40k_tt` | 10 | 5 | 4/3/1 | long, 6min reps, lengthening blocks, 2 × endurance |
| `cycle_100k_sportive` | 12 | 5 | 5/3/2 | long + back-to-back endurance, threshold reps, hills |
| `cycle_strength` | 12 | 5 | 5/5/2 | long ride, 2 × threshold reps, 2 × barbell straight sets |
| `sprint_triathlon` | 12 | 6 | 4/4/2 | long ride + brick run, long run, swim/run/cycle quality, easy swim |
| `standard_triathlon` | 16 | 6 | 6/5/3 | as sprint, longer, with tighter clamps |
| `strength_program` | 12 | 3 | 4/4/2 | A/B/A barbell linear progression, no endurance work |

Both triathlon goals also carry two supplementary `straight_sets` sessions,
sharing the days their hard endurance work already occupies and ranked last so
a trimmed week loses the gym before it loses a discipline.

Phase lengths sum to `weeks - taper_weeks`. Two shapes are goal-specific rather
than uniform: `run_half` ranks the tempo block *above* the interval session
(half pace sits close to threshold), and `cycle_100k_sportive` pairs a long ride
with a same-weekend endurance ride (riding tired is the sportive adaptation) and
carries the only `hills` session, which deliberately has no target.

**Each session scales along exactly one axis** — the `scale["param"]`. Which
axis is a training decision, not a mechanical one: distance for long sessions,
duration for steady ones, rep *count* for most interval work, but rep
*duration* for the TT plans' sustained-block day, where a 2–5 rep count is too
coarse to express a progression at all and the event-specific adaptation is
extending the block toward race duration anyway. Intensity targets, warmups,
cooldowns and recoveries never scale; nor do extras.

A clamp that binds tells you the axis or the base is wrong, but read it the
right way round: hitting the **floor** on a recovery or taper week is the floor
working (race week *should* be the smallest session in the block), while sitting
at the **ceiling** flattens the peak. `test_scaled_params_keep_headroom_inside_
their_clamps` holds every goal to ≤10% at-max, and asserts in the same pass
that no scaled value escaped its clamp at all. When measuring this, match each
session to its own template entry by weekday as well as type — two sessions of
the same type on different days carry different scales, and matching on type
alone reads one against the other's clamps (which is how `cycle_25k_tt` once
appeared to pin 35% of its sessions when the true figure was 10%).

Each weekly session is built by the `_session(...)` / `_scale(...)` helpers and
carries `day` (0=Mon, in the template's own week — the whole week rotates if the
description names a different `rest_day`), `priority`, `key`, an optional `brick`
flag, its fixed `planner.build_plan` params, and a `scale` naming the single
param that grows with the week's volume multiplier.

**The clamps in `scale` are load-bearing, not decoration.** The weekly
multiplier compounds, so peak volume depends on plan length: an 8-week plan
reaches ~1.36× its base, a 12-week ~1.59×, and `standard_triathlon` at 16 weeks
~2.2×. Its bases are therefore set lower relative to its clamps than the shorter
plans' — without that, a 40km/10km event would get a 108km long ride and a 22km
long run. A clamp that binds for more than a week or two at the *max* flattens
the peak and taper for that session, which is the signal a base needs lowering;
binding at the *min* on recovery weeks is correct, and small integer rep counts
pinning at their ceiling for two or three peak weeks is deliberate (you cap
reps, you don't add them forever).

**Priorities interleave the sports** rather than ranking every long session
first, so trimming a week for a lower `days_per_week` never strips a whole
discipline out — a triathlon plan with the swimming cut out of it is not a
triathlon plan. (Below three training days there aren't enough slots for that to
hold and the lowest-priority sports do drop out.)

### Periodisation

`expand_plan` lays whole ISO weeks from `start_date`'s Monday through the
event's week (`compute.week_start` + `timedelta(weeks=…)`), then:

1. **Phases** — the taper takes the final `taper_weeks`; the template's
   remaining phases share what's left by largest-remainder apportionment, so a
   plan started earlier or later than the template's own length still gets a
   sensible base/build/peak split.
2. **Volume multiplier per week** — build weeks ramp by the resolved
   `weekly_ramp_pct` (derived from the plan's length unless the description
   pins one — see "Plan length");
   each recovery week in the `build_recover` cycle dips to `RECOVERY_FACTOR`
   (0.6) of the level reached *without advancing it*; taper weeks step linearly
   from `TAPER_START` (0.75) to `TAPER_END` (0.45) of the peak.
3. **Session sizing** — `value = clamp(round(base × multiplier))`. Deliberately
   **one mechanism**: the design sketched a separate per-week growth increment
   *on top of* the multiplier, which compounds into nonsense over 12 weeks. The
   multiplier alone produces a textbook curve (a 30 km long ride reaching ~51 km
   at peak and shedding to 23 km on race week), and every scaled param carries a
   min/max clamp.
4. **Extras** — placed on the least-loaded non-key days, so rest days fill
   before easy days. Never built, never pushed.

Sessions falling before `start_date` or on/after the event are dropped: race day
is not a training day.

### Intensity targets

`derive_targets` resolves only the targets the goal's sports actually need
(`training.template_sports` / `_SPORT_TARGETS`), so a `run_5k` plan never
derives or displays a swim CSS. A `targets:` entry for a sport the goal doesn't
train is a `ValueError` rather than a silent no-op. Each is resolved once per
plan, in precedence order: description `targets` → the same
`planner.derive_*` helpers `fit plan` uses over the same recent window →
`FALLBACK_TARGETS`. Each carries a `why` provenance string, surfaced in `fit
train show`'s header — so a target derived from thin history (e.g. no swims in
six months) is visible rather than silently wrong, and the user knows to
override it. `_apply_target` then fills the one intensity param each session
type needs.

### Building frequency

`days_per_week` takes either a fixed count or `[start, end]`. A range builds the
week from `start` to `end` by the last build week and then **holds through the
taper** (`_days_for_week`, mirroring `_volume_scale_for_week`'s schedule) — a
taper cuts volume, not frequency; dropping a session in race week would lose the
sharpening it exists for. A falling range is rejected rather than honoured.

Sessions arrive in the template's own priority order, so a two-ride week is
still the two rides that matter most and later weeks only *add* — nothing is
swapped out. This is why `expand_plan` rotates the whole template week first and
trims per week afterwards (selection only counts distinct days, which rotation
preserves), and why `derive_volume_scale` is measured against **week 1's own**
session list rather than the full template week — otherwise a plan that opens at
two rides would look like the user was training far below a week they were never
going to be asked to do.

Frequency and volume are separate axes and compose: `days_per_week: [2, 4]`
changes how many sessions there are, `volume:` changes how big each one is.

### Starting volume

A goal template's opening week assumes a base the user may not have, and
`days_per_week` only removes *sessions* — it never shrinks the ones that remain,
so week 1's long ride was fixed at the template's figure however the plan was
configured. `derive_volume_scale` closes that: the description's `volume:`
(percent) wins, otherwise the user's **mean** weekly training in the goal's own
sports over the last `RECENT_VOLUME_WEEKS` (8) is measured against the
template's opening week, clamped to `[VOLUME_SCALE_MIN, VOLUME_SCALE_MAX]`
(0.6–1.25).

Three details that are easy to get wrong and are deliberate:
- **Mean, not median.** With sparse training the median collapses to zero the
  moment more than half the weeks are empty, which would hand somebody barely
  riding the *full* template volume — exactly backwards. Empty weeks are real
  information and weigh in as zeroes, which is why `weekly_volumes`' zero-filling
  matters here.
- **The current week is dropped** before measuring (`compute.is_current_week`) —
  a week that is one day old would otherwise count as a near-empty week and
  understate current form, the same reason the dashboard dims that bar.
- **No history at all in the goal's sports means "unknown", not "untrained"** —
  the user may simply not have imported any yet, so the template stands.

**The scale converges rather than scaling the plan uniformly**
(`_volume_scale_for_week`): it applies in full in week 1 and interpolates back
to the template's own level by the last build week, so the effective multiplier
is `week_multiplier × volume_scale(week)`. Uniform scaling would start the user
in the right place but leave them under-prepared for a fixed-distance event —
converging keeps the peak the goal actually demands. The cost is a steeper
week-1-to-peak ramp, so when that growth exceeds `VOLUME_RAMP_WARN` (2.2×)
`expand_plan` appends a warning to `plan["warnings"]` telling the user their
recent training is well below where the goal needs to start. Warnings are
returned, never printed — `display.render_training_plan` surfaces them, the same
split `read_activities_with_warnings` uses.

### Strength: the one intensity that is projected forward

Every other sport in a plan holds intensity fixed and ramps volume, for the
reason set out under "Benchmarks" below — prescribing a pace you might not
have by week 10 prescribes work you cannot complete. **Strength inverts both
axes**: volume is fixed (3x10 stays 3x10) and the *load* ramps, because that
is what linear progression is, and because a weight that turns out to be too
heavy is a failed rep rather than a failed session.

That inversion is why strength sessions carry **no `scale`** (`_session`'s
`scale` is `None` for them). Ramping sets or reps as well would be two
progressions at once. It follows that:

- `_week_seconds` counts only scaling sessions, and `volume_sports` excludes
  strength — measuring the volume scale against training the multiplier can
  never move would only bias it.
- The clamp tests exempt unscaled sessions, the same way they exempt
  benchmarks.

**Targets are per lift, not per sport**, so strength stays out of
`_SPORT_TARGETS`, `planner.PLAUSIBLE_TARGETS` and `planner.derive_target` —
forcing it in would mean giving `derive_target` a float path and making the
bounds per-exercise, changes to code all three cardio sports depend on. It
lives under `targets["strength"]` instead:

```python
"strength": {"squat": {"current_e1rm_kg": 105.0, "goal_e1rm_kg": 122.5,
                        "goal_from": "one 2.5kg step per build week — ...",
                        "by_week": [105.0, 107.5, ..., 98.0]}}
```

- **`current` is measured** (`derive_lift_target`, else `FALLBACK_LIFT_E1RM_KG`).
- **`goal` cannot be measured** — it is a decision about the future. A
  description's `<lift>_goal_kg` wins; otherwise it defaults to
  `reachable_e1rm`, i.e. what this plan's length delivers at one increment a
  build week. Deriving it keeps strength inside the derive-or-fall-back rule
  every other sport follows, so a plan expands with no `targets:` at all. An
  explicit goal further off than the plan is long is **capped, and warned
  about** in `plan["warnings"]` rather than quietly prescribed.
- **`by_week` is the whole point**: intensity depends on the week, but
  `_apply_target` is stateless by design, so the week structure is baked into a
  table once (`_attach_weekly_lifts`) and `_apply_target` only indexes it with
  the `week` the session already carries. `retarget_sessions` rebuilds the
  table via `plan_week_roles`, since it is a function of the plan's length as
  well as of the targets.

`LIFT_INCREMENT_KG` serves two purposes deliberately: it is the plate step a
working weight rounds to *and* the most a week may add — "add one increment a
week" is the method. Deloads use `STRENGTH_DELOAD_FACTOR` (0.85), not
`RECOVERY_FACTOR` (0.6): that is a volume cut, and 60% of a working weight
stops being training.

**Retargeting rewrites load, never sets or reps.** For a barbell, load *is*
intensity — so this is consistent with "intensity only, never volume", not an
exception to it. `_intensity_snapshot` extends the old `_INTENSITY_PARAMS`
tuple comparison down one level into the exercises list, since a strength
session's intensity is a list rather than a single param.

**Per-session params must be deep-copied** (`_build_session`): a strength
session's params hold a list of exercise dicts shared with the template, and a
shallow copy would have week 12 writing its load into week 1.

### Benchmarks and why pace does not drift

Intensity is measured, never projected. `derive_targets` runs once per plan and
every session is built against that figure, so a long run grows 6km → 13km but
stays at the same pace throughout. This is deliberate: assuming an athlete will
be x% faster by week 12 prescribes work they may not be able to complete, which
is worse than prescribing work that is slightly easy. Volume and intensity are
separate axes (see "Building frequency").

A plan therefore earns a faster pace by **re-measuring**. `BENCHMARK_SESSIONS`
defines a re-test per sport, and each one's *shape* follows a single rule:

> **The recorded activity must BE the test, unless the sport's split machinery
> can isolate the test from within it.**

- **run: a 5km best effort, warmup and cooldown included.**
  `compute.fastest_split` is a sliding-window minimum over the whole track, so
  the fastest 5k window is the test itself however much jogging surrounds it.
  5km rather than 3km because 3km appears in neither `SPLIT_DISTANCES_KM` nor
  `MILESTONES_KM` — a 3km effort was measurable *nowhere*, and worse, the best
  5k window containing it necessarily dragged in 2km of warmup, and that branch
  takes precedence over the fallback. The old 3km test made the estimate worse
  than not testing.
- **cycle: a normal 20-minute test with warmup and cooldown.** It was briefly
  bare, because stored `avg_power` is a whole-activity mean and the wrapping
  diluted it — `compute.best_power_window` now recovers the 20-minute effort
  from wherever it sits in the recording, so the constraint is gone. See
  "Power windows".
- **strength: a heavy triple**, on whichever lift that week's session leads
  with — which lift to test is a property of the session, not of the sport, so
  `_build_benchmark` takes the session being replaced. A 3RM rather than a
  true single: `compute.estimated_1rm` reads a triple perfectly well, and a
  plan should not be sending anybody to a genuine one-rep max alone in a gym
  every few weeks.
- **swim: a bare 1km test.** A whole swim of 1.000–1.060km lands in the existing
  `fastest_1k` milestone, which `derive_swim_css` already reads — so this needed
  no `compute.py` change at all. Milestones are computed from `distance_km` and
  `duration_seconds` on every read, which matters because pool swims frequently
  carry no cumulative-distance stream for `splits` to be derived from.

Only the swim test is bare now, and a bare test instructs no warmup with nothing
in the payload able to carry that — `build_plan` hard-codes `description`. So
`planner._bare_suffix` puts it in the **workout name** ("Swim baseline 1km test
(warm up first)"), which is what the athlete actually reads on the watch.

**`fit plan --type baseline` produces the same test.** The interactive prompt
defaults in `_PARAM_SPECS` mirror `BENCHMARK_SESSIONS` exactly, so planning a
baseline by hand gives a measurable test too — cycle and swim default to a blank
warmup/cooldown (`_optional_int`, blank → 0 → the step is omitted), and run keeps
its 10-minute wrap. The two drifted once already, when the plan's run benchmark
moved 3km → 5km and the prompt default stayed at 3km; `test_planning_a_baseline_
by_hand_gives_the_same_test_the_plan_schedules` now holds them together.

`expand_plan` then places them:

- **On recovery weeks only**, never in the taper — you test rested, which is
  what makes one test comparable with the next.
- **Replacing** that sport's session for the week rather than adding to it:
  a quality session (`intervals`, then `tempo`) if the week has one, otherwise
  the `long` session, which at low frequencies is the only slot a sport has.
- **Taking turns** between the sports a goal trains, picking whichever has gone
  longest without a test. `benchmark_sports` iterates `BENCHMARK_SESSIONS`, so
  that dict's order is the rotation order. A week where no testable sport has a suitable session
  is skipped *without* consuming a turn, so a low-frequency opening block cannot
  silently eat every run's test.
- **Unscaled and untargeted** — a 3km test is only a benchmark if it is the same
  3km every time, run at an open effort rather than a prescribed pace. This is
  why benchmark sessions carry no `scale` and are exempt from the clamp tests.

`benchmarks: false` in the description turns them off. Weeks carrying one are
recorded in `plan["benchmark_weeks"]` and surfaced by `render_training_plan`.

### The test week

`test_week: true` opens the plan with **week 0**, carrying benchmarks and
nothing else. It exists because a plan's targets are derived once, at import,
from whatever history happens to exist — and an unmeasured target (a
hand-written `bike_ftp:`, or a `FALLBACK_TARGETS` entry standing in for a sport
with no recent history) silently sets the intensity of every session until the
first recovery-week re-test, which is week 4 at the earliest.

Four decisions in it:

- **It takes a week out of the plan, not off the front.** `start_date` and
  `event_date` are the user's; shifting either to make room would answer a
  different question than the one they asked. So `_plan_weeks` raises its floor
  to `MIN_PLAN_WEEKS + 1`, the periodised block runs from the *second* Monday
  (`first_monday`), and `plan["weeks"]` stays the **periodised** count — which
  is what keeps `plan_week_roles`, `_attach_weekly_lifts`' `by_week` table and
  `_apply_target`'s `week - 1` indexing aligned. Only `_default_start_date`
  compensates, reaching one week further back so a description that names no
  `start_date` still gets the template's own length of actual training.
- **Tests alone, no training around them.** This is what separates it from an
  in-plan re-test, which stands in for one session of one sport and leaves the
  rest of that week intact. A test week's job is clean measurements, so you turn
  up rested, test, and go home.
- **One benchmark per distinct test, deduplicated by `_test_identity`** — per
  sport for run/cycle/swim (one FTP test in a week is plenty), but per *lift*
  for strength, since `_build_benchmark` tests whichever lift the session leads
  with. Sessions are drawn from week 1's own selection in template priority
  order, so each test lands on a day that sport already owns. **Only the lead
  lift of each strength session is tested** — a goal whose Wednesday session is
  squat-then-deadlift tests the squat and derives the deadlift from history as
  before.
- **Independent of `benchmarks:`.** "Measure once before we start" and
  "re-measure as we go" are separate decisions, and wanting the first is no
  reason to be forced into the second.

Week 0 sessions carry `week: 0` and `phase: "test"`, which `group_by_week`
sorts first and `render_training_plan` renders with no special case. The plan
records `test_week: true`, and the renderer prints the follow-up the week is
pointless without: the targets below it were still derived at import, so do the
tests, `fit garmin-sync`, then `fit train retarget`.

**Applying a re-test**: do the test → `fit garmin-sync` → **`fit train
retarget`**, which re-derives every target from the updated history and rewrites
the sessions still ahead of you, in place. No Garmin login, no calendar change.
`fit train import` remains the way to change a plan's *shape* and still refuses
to replace a plan with future scheduled sessions.

### Retargeting

`training.retargetable(sessions, today)` selects what may be rewritten, and
`training.retarget_sessions(plan, targets, today)` does it, returning
`{"old_targets", "new_targets", "retargeted", "unchanged", "frozen", "past",
"changed"}`. Four things it deliberately will not touch:

- **Extras** — they have no `params` key at all; touching one is a `KeyError`.
- **Benchmarks** — a test at a prescribed pace is not a test.
- **Sessions already scheduled on Garmin.** A pushed workout is a frozen copy on
  the account and `garmin.py` has no endpoint to update or delete it, so
  rewriting one locally would only desynchronise the plan from the watch. They
  are counted as `frozen` and the renderer says so.
- **Volume.** Intensity only — and this is *structurally forced*, not just
  chosen: the template's `scale` dict is never persisted, so a stored session
  knows its own size but not the rule that produced it. `_INTENSITY_PARAMS`
  names the three params `_apply_target` writes, and
  `test_retarget_never_changes_volume` holds the line.

`workout_name` is regenerated whenever params change — it is derived from them,
and a stale name would disagree with the payload that eventually gets pushed.
Like `sync`, this mutates `plan["sessions"]` in place ("pure" here means no I/O,
not no mutation) and `cli.py` writes the plan afterwards. `--dry-run` writes
nothing, which matters more here than in `sync`: there is no backup of
`plan.json` and this rewrites every future session at once.

### Sync, idempotency, and completion

`train sync` first prints the batch it is about to create
(`display.render_training_sync_preview`) and asks for confirmation before
`garmin.login()` is even called — `--yes` bypasses the prompt, `--dry-run`
stops after the list. This guard exists because `login()` resumes a saved
session silently and one `sync` creates a workout *and* a calendar entry per
session, so a whole batch can otherwise reach the account with no visible step
in between (exactly the mistake that prompted it). `fit plan`'s single push
stays unguarded.

`train sync` schedules only sessions with `status == "planned"` dated inside
`[today, today + train_sync_window_days]`, so **re-running it is safe** — it
simply finds fewer. The plan file is rewritten after *every* session (not once
at the end), so a crash mid-sync can never leave it claiming less than what is
actually on the Garmin calendar. One session's failure is collected as a warning
rather than aborting the rest.

`train clear` unschedules future sessions via the stored `scheduled_workout_id`
and resets their sync state. `train import` refuses to replace a plan that still
has future scheduled sessions, since clearing needs Garmin and importing may
well be offline.

`match_completion` marks a non-extra session done when an activity of the same
sport falls within ±1 day, **each activity claiming at most one session**
(nearest first) so a single ride can't tick off a whole week. Extras are never
matched — fit has no strength/yoga activity type.

**Extras are local-only** because the Garmin API has no calendar-note or generic
non-workout scheduling endpoint (`schedule_workout` only takes a `workout_id`).
Bricks are represented as **two same-day sessions** (the ride, then a short run
tagged `brick`), not a Garmin multisport workout — that schema is unverified.

### Plan file shape (`~/.fit/train/plan.json`)

```python
{
  "goal": "sprint_triathlon",
  "event_date": "2026-11-15",
  "start_date": "2026-08-24",
  "weeks": 12,
  "test_week": false,             # true = week 0 is a testing week
  "created": "2026-08-24",
  "spec": {...},                  # the normalised description, for re-expansion
  "targets": {"run_5k_seconds": 1440, "bike_ftp": 250, "swim_css_100m": 105,
              "why": {...}},      # provenance per target
  "sessions": [
    {"date": "2026-08-25", "week": 1, "phase": "base", "sport": "run",
     "session_type": "intervals",
     "params": {...},             # ready for planner.build_plan
     "workout_name": "Run intervals 5x800m @ 4:51/km",
     "is_key": true, "is_brick": false, "is_extra": false,
     "garmin_workout_id": null, "scheduled_workout_id": null,
     "scheduled_date": null, "status": "planned"},
    {"date": "2026-08-24", "week": 1, "phase": "base", "sport": "strength",
     "session_type": "strength", "duration_s": 2700,
     "workout_name": "Strength 45min", "is_extra": true, "status": "planned"}
  ]
}
```

Sessions store `params`, not payloads — a payload per session would bloat the
file ~80×, and `planner.build_plan` rebuilds it at sync time from the same
params. `workout_name` is stored (via `planner.workout_name`) so `fit train
show` reads well without rebuilding anything.

---

## Conventions

- Functions over classes, always.
- British English in comments, docstrings, and user-facing text (e.g. "summarise",
  "colour"); title case for table/section titles in `display.py` output.
- `storage.py` is the filesystem boundary. Nothing outside it opens files.
- `compute.py` is pure. No I/O, no side effects.
- Use `.get()` for all activity dict field access in case older activities lack the field.
- Paths stored in dicts are relative to the data dir, never absolute.
- Atomic writes (`os.replace`) for any full-file overwrite.
- `FIT_DATA_DIR` env var overrides the data directory — useful for tests and alternate locations.
- `tests/` holds a small, deliberately non-exhaustive pytest suite covering the
  subtlest pure functions (splits, PBs, fitness EWMA, config parsing, importers,
  workout payloads/recommendations, plan-description parsing and periodisation
  math). Run `.venv/bin/pytest` after any change to `compute.py`, `storage.py`,
  `importers.py`, `planner.py`, or `training.py`.

  **The suite is kept small on purpose, and was trimmed once already** (192
  test functions to 154). Three things do not earn a test, and adding them
  back is how it regrows:
  - **Restating a constant or a one-line definition.** `EASY_FACTOR > 1` and
    `"strength" not in volume_sports(...)` are the definitions, not
    behaviour. Where the *direction* of a factor genuinely matters, assert it
    through the pipeline that consumes it — the easy-pace direction is
    checked in `test_targets_reach_the_session_params`, against a real
    session's params, not against the constant.
  - **Asserting what the code guarantees by construction.** A clamped value
    lies inside its clamp; `workout_name` and `build_plan` return the same
    name because they call the same `_BUILDERS` entry. Such a test can only
    fail if the test itself is wrong.
  - **Running goal-agnostic code once per goal.** `@pytest.mark.parametrize
    ("goal", ALL_GOALS)` is for properties of the *template data* —
    that every goal expands and stays off its clamps' ceilings. Phase
    apportionment and `days_per_week` trimming are engine code and are
    covered once.

  The three surviving `ALL_GOALS` loops are deliberate and should stay:
  `test_every_goal_expands_into_buildable_sessions`, `test_scaled_params_
  keep_headroom_inside_their_clamps`, and `test_every_goal_schedules_a_re_
  test`. Each asserts a property of the template data — that a goal expands
  at all, that its bases sit low enough to leave headroom under its clamps,
  and that its session mix leaves a benchmark somewhere to land. They are the
  only tests that catch mistakes in `GOAL_TEMPLATES`, which is where a plan
  actually goes wrong.

  Prefer folding a new assertion into a loop that already exists over adding
  a function beside it — the clamp check and the stored-vs-rebuilt
  `workout_name` check both ride along in the two loops above, covering every
  sport/type combo in every template for one line each.

  **Before deleting a test, mutate the behaviour it names and confirm
  something still fails.** Four of the cuts in that trim looked tautological
  and were not; the mutation caught it, and the assertions were folded back
  in. It also surfaced a kept test whose fixture dated two swims four years
  apart, so `CSS_PAIR_MAX_DAYS` rejected the pair and the ratio guard the
  test is named for never ran.
- Everything committed under `examples/` and `tests/data/` is synthetic —
  generated fake data, no real recordings. Real personal data never enters the
  repo: keep it outside the project dir (e.g. `~/fit-dev-data/`) and point
  `FIT_DATA_DIR` at it when needed. `examples/data/` is a demo data dir
  (six months of fake activities); `examples/strava-export/` is a miniature
  fake Strava bulk export; `tests/data/test_run.fit` is a synthetic
  constant-pace FIT file and `tests/data/test_strength.fit` a synthetic gym
  session (both written by throwaway generators kept outside the repo); `examples/training-plan.yaml` is a commented sample
  `fit train` plan description. The generation tooling is deliberately not part of
  the repo — fit displays fitness data, it doesn't generate it.
