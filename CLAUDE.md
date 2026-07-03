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
- Standard library `xml.etree.ElementTree` for GPX/TCX parsing (no lxml)
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

---

## CLI design

Commands are subcommand-based. Each subcommand produces a single one-shot output and
exits. No persistent TUI.

```
fit dashboard                # summary: recent activity, weekly volume, trend sparklines
fit dashboard --sport cycle  # dashboard filtered to one sport for this run (overrides config)
fit dashboard --timerange 3m # dashboard windowed to a rolling 3 months (history, sparkline, sports summary, PBs)
fit dashboard --minimal      # dashboard with just the sparklines and recent activities for this run (hides PBs, sports summary)
fit pbs                      # personal bests table, grouped by activity type
fit pbs --months 3           # personal bests over just the last N months (overrides config)
fit stats                    # breakdown, accepts --week / --month / --year
fit fitness                  # current fitness index (baseline 100) + trend sparkline
fit fitness-reset            # re-anchor the fitness index baseline to today
fit import ./run.gpx         # parse and store a GPX or TCX file
fit garmin-sync --days 14    # pull recent activities from Garmin Connect (see "Garmin integration")
fit plan --sport run --type intervals  # generate a workout interactively, push to the watch (see "Workout planner")
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
└── gpx/                     ← original imported GPX/TCX files kept for reference
    └── 2024-01-17T07:15:00.gpx
```

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

Paths stored inside activity dicts (e.g. `gpx_file`) are relative to the data directory,
not absolute, so the folder stays portable across machines.

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
- `activities_dir() -> Path`, `config_path() -> Path`, `pbs_path() -> Path`, `fitness_path() -> Path`, `gpx_dir() -> Path`, `plans_dir() -> Path`
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
- `save_gpx_file(source_path: str, activity_id: str) -> Path`
- `write_plan(plan: dict) -> None` — writes single plan file atomically, named by
  the plan's `id` (mirrors `write_activity`)
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
  per-km except `"swim"` (per 100m) and `"cycle"` (speed in km/h, e.g. `"24.3km/h"`,
  since bike effort is conventionally read as speed rather than pace). Always
  returns `"—"` for any type in `NO_DISTANCE_TYPES` (currently `"squash"`),
  regardless of `distance_km` — its nonzero distance is never a real pace signal
- `weekly_volumes(activities: list[dict]) -> list[dict]`
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
  `[{"type": ..., "count": ..., "distance_km": ...}, ...]`. Shared by
  `display.render_stats`'s "By type" table and the dashboard's sports-summary block
  (`display.render_sports_summary`) — the single source of truth for "by type"
  aggregation
- `activity_calendar(activities: list[dict], reference: date, months: int = 2) -> list[dict]` —
  month grids for the `months` calendar months ending with `reference`'s, oldest
  first: `[{"label": "June 2026", "weeks": [[0, 1, ...], ...], "active_days":
  [3, 5, 12]}, ...]`. `weeks` comes from stdlib `calendar.monthcalendar`
  (Monday-first rows, 0 = padding cell); grid layout lives here so
  `display.render_calendar` stays computation-free. Powers `fit calendar`
- `fastest_split(points: list[dict], target_distance_km: float) -> dict | None` — best
  continuous segment of a given distance within one activity's (elapsed_seconds,
  distance_km) point stream; see "Split PBs" below
- `all_personal_bests(activities: list[dict]) -> dict` — groups activities by type and
  calls the private `_candidate_pbs` per type, which itself composes four
  single-purpose helpers: `_longest_distance_pb`, `_milestone_pbs`, `_split_pbs`,
  `_elevation_pb` — each returns its own slice of the PB dict, merged by `_candidate_pbs`
- `detect_new_pbs(new_activity: dict, current_pbs: dict) -> list[dict]` — returns
  `[{"key": ..., "value": ...}, ...]` for each PB category broken, structured rather
  than pre-formatted; `display.render_new_pb_messages()` turns these into the
  human-readable "New fastest 5k: 21:40"-style text (formatting is a display.py
  concern, not compute.py's)
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
- `weekly_fitness_index(index_series: list[dict]) -> list[dict]` — resamples to one
  point per ISO week (the week's last value, not a sum — see "Fitness index")

Key constants:
- `MILESTONES_KM` / `MILESTONE_TOLERANCE` — "dedicated effort" distances per type
  (a whole activity within `[D, D*MILESTONE_TOLERANCE]` counts as a "D-distance effort")
- `SPLIT_DISTANCES_KM` — distances-of-interest per type for best-split extraction
- `MET_TABLE` — coarse per-type MET values, banded by pace/speed for most types, or
  a single flat value for types with no meaningful pace/speed signal (`hike`,
  `squash`); see "Fitness index"
- `NO_DISTANCE_TYPES` — activity types (currently just `squash`) whose
  `distance_km` is present but never meaningful (e.g. accelerometer noise from
  an indoor court sport with no GPS track); `calc_pace` and `_candidate_pbs`
  both check it to suppress pace strings and the "longest distance" PB for
  these types

Always use `.get()` when accessing activity fields — older activities may not have
fields added after they were logged (e.g. `avg_heart_rate`, `elevation_gain_m`).

### `display.py`
Takes outputs of compute functions and renders Rich output to stdout. No file I/O,
no computation.

Key functions:
- `render_dashboard(activities: list[dict], pbs: dict, config: dict, fitness: dict, sports: list[str] | None = None, window_months: int = 0, window_label: str | None = None) -> None` —
  `config` is the `storage.read_config()` dict (supplies `history_count`,
  `dashboard_weeks`, and the four `show_*` toggles); `fitness` is
  `cli._fitness_snapshot()`'s dict
  (`current`/`baseline_date`/`weekly`), passed through whole rather than
  unpacked into separate parameters.
  Block order: fitness index -> weekly volume sparkline -> time range banner
  -> history table -> personal bests -> sports summary. When `window_label`
  is set (from `--timerange`), prints a "Time range: ..." banner and threads
  the label into `render_pbs_table`'s title instead of the int-months
  wording. The sports-summary block always renders *last*, over the
  unfiltered `activities` list, so it always shows every type present (never
  restricted by `--sport`/config `sports`) even when the sport filter itself
  matches nothing — in that case the sparkline/history/pbs blocks are
  replaced by a single "no activities match" message, but sports-summary
  still renders afterward. The fitness-index block renders *first*, before
  even the empty-activities early return, so the headline (always
  full-history/as-of-today) never disappears just because
  `--sport`/`--timerange` matches nothing elsewhere on the page — see
  "Fitness index". Both the weekly-volume and fitness-trend sparklines are
  capped to the last `config["dashboard_weeks"]` weeks (0 = all) so long
  history doesn't wrap over multiple lines; the cap is skipped when
  `--timerange` (a truthy `window_label`) is already driving the window.
- `render_history_table(activities: list[dict], n: int) -> None` — "Pace" column
  uses `_format_effort()`, which shows `avg_power` ("187W avg") for cycle
  activities that have it, falling back to `compute.calc_pace()` otherwise.
  "Distance" uses `_format_distance()`, showing `"—"` for any
  `compute.NO_DISTANCE_TYPES` member instead of a meaningless value, same as
  "Pace" already does. "Avg HR"/"Max HR" use `_format_hr()`, showing `"—"`
  when `avg_heart_rate`/`max_heart_rate` are absent
- `render_pbs_table(pbs: dict, sports: list[str] | None = None, window_months: int = 0, window_label: str | None = None) -> None` —
  `window_label`, when set, overrides the `window_months`-based title text
  (`"Personal bests (last N months)"`) with `"Personal bests ({window_label})"`,
  for windows expressed in units other than months (e.g. `--timerange 10d`)
- `render_sports_summary(activities: list[dict]) -> None` — dashboard block; the
  passed-in `activities` must not be pre-filtered by sport (pre-filtering by date
  is fine — this is how it respects `--timerange` while ignoring `--sport`).
  Shares its table-building logic with `render_stats` via the private
  `_render_type_summary_table(activities, title)`, which wraps
  `compute.summarize_by_type`
- `render_fitness_index(current_index: float | None, baseline_date: str | None, weekly_series: list[dict], window_label: str | None = None) -> None` —
  headline (always full-history/as-of-today) + trend sparkline (windowed by
  `window_label`/`--timerange` if the caller passes an already-windowed
  `weekly_series`); prints a "not enough data yet" line instead when
  `current_index is None`
- `render_fitness_reset(old_baseline: dict, new_baseline: dict) -> None` — prints
  old→new baseline values; `old_baseline == {}` (never initialized) gets a
  simpler "baseline set" message
- `render_calendar(months: list[dict]) -> None` — `fit calendar`'s output: one
  month grid per `compute.activity_calendar` dict, active days in bold green,
  padding cells blank
- `render_stats(activities: list[dict]) -> None`
- `render_sparkline(data: list[float], label: str) -> None`
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

### `importers.py`
Parses external formats and returns a correctly-shaped activity dict. Uses stdlib
`xml.etree.ElementTree` for GPX/TCX (no external XML library) and `fitparse` for FIT.

Key functions:
- `import_gpx(path: str) -> dict`
- `import_tcx(path: str) -> dict`
- `import_fit(path: str) -> dict`
- `import_strava_csv(path: str) -> tuple[list[dict], list[str]]` — a single bare
  Strava `activities.csv`; second element is warning strings (see below)
- `import_strava_export(export_dir: str) -> tuple[list[dict], list[str]]` — a full
  Strava bulk-export archive (`activities.csv` + linked, possibly gzipped,
  GPX/TCX/FIT files); second element is warning strings (see below)
- `import_by_extension(path: str, suffix: str) -> dict` — dispatches to
  `import_gpx`/`import_tcx`/`import_fit` by suffix, raising `ValueError` for anything
  else; the single source of truth for extension dispatch, used by both `cli.py`'s
  `import` command and `_import_strava_linked_file` (for linked/gzipped files inside
  a bulk export)

Importers never check for duplicates themselves — they always return every activity
they parse. `cli.py`'s `import_activity` is the single place that calls
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

`import_gpx`/`import_tcx`/`import_fit` each also build a transient (elapsed_seconds,
distance_km) point stream while parsing, purely in memory, and use it to compute
best-effort split times (see "Split PBs" below) before attaching the result to the
returned dict's `splits` field and discarding the raw stream — it is never persisted.

**Known limitation**: the Garmin TCX schema's `Sport` attribute only supports
`Running`/`Biking`/`Other` — there is no native `Swimming` value. `import_tcx`
currently defaults anything outside Running/Biking (walk, hike, swim) to `"run"`,
which silently mislabels those activities rather than leaving them unclassified.
Not fixed as part of adding swim support — a separate, independent concern.

**Known limitation**: `fitparse`'s bundled profile (pinned via
`fitparse>=1.2,<2.0`) doesn't decode every Garmin FIT `sport` enum code to a
name — e.g. it jumps straight from 48 (`floor_climbing`) to 254 (`all`), so
codes Garmin added in between (64 = squash) come back from the FIT session
message as a raw, undecoded int rather than a string. `import_fit` checks
`FIT_SPORT_CODE_MAP` for these raw ints (currently just `{64: "squash"}`),
falling back to `"run"` for any other undecoded code — the same default an
unrecognized *string* sport already gets via `FIT_SPORT_MAP`. Extend
`FIT_SPORT_CODE_MAP` if a future fixture surfaces another undecoded code
worth naming.

### `planner.py`
Pure functions building Garmin Connect workout-service payloads as plain dicts.
No file I/O, no network, no garminconnect import, no typer — cli.py wires
prompts, storage, and the push around it. May import `compute` (pure → pure).
See "Workout planner" for the feature-level design and payload schema notes.

Key functions:
- `parse_pace(text: str) -> int` — `"4:30"` → 270 seconds (per km or per 100m,
  caller's unit); strict m:ss, raises `ValueError` otherwise
- `parse_duration(text: str) -> int` — `"90"` → 90, `"2:00"` → 120; raises `ValueError`
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
- `build_plan(sport, workout_type, params, created: str) -> dict` — the saved-plan
  dict: `{"id", "sport", "workout_type", "params", "workout_name", "payload"}`,
  where `payload` is the complete workout-service dict for `garmin.push_workout`
- `describe_plan(plan: dict) -> list[str]` — human step lines ("6 x 800m @
  4:20–4:40/km, 2:00 recovery") so `display.render_plan_saved` stays
  computation-free (same split as `detect_new_pbs` → `render_new_pb_messages`)

Key constants: `SPORT_TYPES` / `WORKOUT_TYPES` (valid combos), the target-band
tolerances (`PACE_TOLERANCE_S_PER_KM = 10`, `SWIM_PACE_TOLERANCE_S_PER_100M = 5`,
`POWER_TOLERANCE_W = 10`), and the recommendation knobs (`RECENT_MONTHS = 6`,
`TEMPO_FACTOR = 1.07`, `REPS_CAP = 10`, `SHORT_REP_MAX_M = 1000` /
`SHORT_REP_FACTOR = 0.97` — see "Workout planner" for the short-rep pace discount).

### `cli.py`
Typer app. One function per subcommand. Each function: call storage → call compute → call
display. Nothing else.

Shared coordination helpers (each one level deep — they call storage/compute/display
functions, never each other):
- `_load_activities() -> list[dict]` — the preamble every history-reading command
  shares: `ensure_data_dir` → `read_activities_with_warnings` → `render_warnings`
- `_dashboard_window(all_activities, timerange, config_months, today) -> dict` —
  resolves the dashboard's `--timerange` / `pbs_window_months` / all-time precedence
  into `{"activities", "pbs", "window_months", "window_label", "date_window"}`
- `_fitness_snapshot(activities, today, baseline, window=None) -> dict` — the
  `{"current", "baseline_date", "weekly"}` dict `render_dashboard`/`render_fitness_index`
  consume; callers fetch `baseline` via `_get_or_init_fitness_baseline` first
- `_prompt_params(specs: list[dict]) -> dict` — `typer.prompt`s each
  `planner.workout_params` spec (Enter accepts the shown default), re-prompting
  on `ValueError` from the spec's parser; a spec carrying a `"derive"` callable
  (see `planner.recommend_defaults`) gets its default computed at prompt time
  from the answers so far. The only interactive prompting in `cli.py` (the other
  is `garmin.login`'s credential flow)

---

## Activity data shape

```python
{
    "id": "2024-01-15T08:30:00",      # ISO 8601, used as filename key
    "type": "run",                     # "run" | "cycle" | "walk" | "hike" | "swim" | "squash"
    "date": "2024-01-15",
    "distance_km": 10.2,
    "duration_seconds": 3120,
    "elevation_gain_m": 45,            # optional
    "avg_heart_rate": 152,             # optional
    "max_heart_rate": 171,             # optional
    "avg_power": 187,                  # optional, watts; TCX (lap AvgWatts) / FIT (session avg_power) only
    "source": "gpx",                   # "gpx" | "garmin" | "strava"
    "gpx_file": "gpx/2024-01-15T08:30:00.gpx",  # optional, relative path
    "splits": {"5k_seconds": 1423, "10k_seconds": 2950}  # optional, see "Split PBs"
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
`import_gpx`/`import_tcx`/`import_fit` compute split times once, at import time,
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

If a distance isn't in `SPLIT_DISTANCES_KM`, it isn't instantly queryable after the
fact — recomputing it means re-importing from the original file (kept in `gpx/` for
exactly this kind of reference).

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
source, since `avg_heart_rate`/`avg_power` are only populated from GPX/TCX/FIT
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
    "show_sparkline": True,     # weekly volume sparkline block
    "show_pbs": True,           # personal bests block
    "show_sports_summary": True,  # sports summary block (all types, count + distance)
    "show_fitness_index": True,   # fitness index (EWMA training load rescaled to a baseline of 100) block
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

---

## Stretch features (not yet implemented)

- Multi-week periodised training plans built on top of `fit plan` — adaptive
  progressions across sessions, calendar scheduling (`client.schedule_workout`
  exists in the garminconnect library, unused so far).
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
  `"workoutId"`). The one garmin.py function that *sends* rather than fetches;
  used by `fit plan` (see "Workout planner").

`fit garmin-sync --days N` (default 14, `cli.py`) logs in, lists recent
activities, downloads each as FIT bytes to a temp file, imports it via
`importers.import_fit`, and hands the results to the same `_import_and_report`
tail every other import path shares (dedupe, write, save original, print new
PBs, recompute PB cache). Temp files are cleaned up in a `finally` block.

---

## Workout planner

`fit plan --sport run --type intervals [--no-push]` generates a structured
workout interactively (typer prompts, Enter accepts each default), saves it to
`~/.fit/plans/<created-timestamp>.json`, and pushes it to Garmin Connect via
`garmin.push_workout` — the workout appears under Training > Workouts on the
watch's next sync. Delivery is Connect-only: no route data, no calendar
scheduling, no USB path (all deliberately deferred — see "Stretch features").

**Sport/type matrix** (`planner.WORKOUT_TYPES`): run has `intervals` (N × distance
reps at a pace target with timed recoveries), `tempo` (sustained block at pace),
`hills` (N × timed uphill efforts — deliberately *no* pace target, since gradient
makes pace meaningless), and `baseline` (Runna-style benchmark: best-effort fixed
distance, open target, used to re-measure current pace). swim has `intervals`
(distance reps with timed rest steps, optional pace-per-100m target). cycle has
`intervals` (timed blocks at a power target, 2:1 work:rest defaults), `hills`, and
`baseline` (FTP-test shape: sustained best effort, open target). Every workout is
warmup → main → cooldown.

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
nested `workoutSteps`; absent fields are omitted, not null. **Not yet verified
against a live upload** — the target-value field names and step numbering follow
community-consensus workout-service JSON, not the installed models (which never
set target values). After the first real push, diff `client.get_workout_by_id()`
against a generated payload and update the note in `planner.py`'s docstring.

**Save-before-push is deliberate**: the plan file is written before `garmin.login`
is attempted, so a failed push (no `garminconnect` installed, bad credentials,
network down) never loses the generated workout. On success the Garmin
`workoutId` is written back into the plan file as `garmin_workout_id`.

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
    "garmin_workout_id": 123456789      # present only after a successful push
}
```

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
  workout payloads/recommendations). Run `.venv/bin/pytest` after any change to
  `compute.py`, `storage.py`, `importers.py`, or `planner.py`.
- Everything committed under `examples/` and `tests/data/` is synthetic —
  generated fake data, no real recordings. Real personal data never enters the
  repo: keep it outside the project dir (e.g. `~/fit-dev-data/`) and point
  `FIT_DATA_DIR` at it when needed. `examples/data/` is a demo data dir
  (six months of fake activities); `examples/strava-export/` is a miniature
  fake Strava bulk export; `tests/data/test_run.fit` is a synthetic
  constant-pace FIT file. The generation tooling is deliberately not part of
  the repo — fit displays fitness data, it doesn't generate it.
