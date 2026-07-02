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
fit pbs                      # personal bests table, grouped by activity type
fit pbs --months 3           # personal bests over just the last N months (overrides config)
fit stats                    # breakdown, accepts --week / --month / --year
fit fitness                  # current fitness index (baseline 100) + trend sparkline
fit fitness-reset            # re-anchor the fitness index baseline to today
fit import ./run.gpx         # parse and store a GPX or TCX file
fit history 10               # last N activities as a table (default 10)
fit trend pace                # ASCII sparkline for a given metric over time
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
└── gpx/                     ← original imported GPX/TCX files kept for reference
    └── 2024-01-17T07:15:00.gpx
```

(A `plans/` folder for generated workout plans will be added with the planner
stretch feature — see "Stretch features". It does not exist yet, and neither do
its storage helpers.)

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
- `activities_dir() -> Path`, `config_path() -> Path`, `pbs_path() -> Path`, `fitness_path() -> Path`, `gpx_dir() -> Path`
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

Storage functions are added when a caller needs them, not speculatively — an
earlier draft carried read/edit/delete/plan helpers with no callers, and they
were removed. When the planner or an edit command lands, add its helpers then.

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
- `rolling_average(activities: list[dict], metric: str, window: int) -> list[float]`
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
  `config` is the `storage.read_config()` dict (supplies `history_count` and the
  four `show_*` toggles); `fitness` is `cli._fitness_snapshot()`'s dict
  (`current`/`baseline_date`/`weekly`), passed through whole rather than
  unpacked into separate parameters.
  When `window_label` is set (from `--timerange`), prints a "Time range: ..."
  banner and threads the label into `render_pbs_table`'s title instead of the
  int-months wording. The sports-summary block renders *before* the `--sport`
  filter is applied, so it always shows every type present (never restricted by
  `--sport`/config `sports`) even when the sport filter itself matches nothing.
  The fitness-index block renders *first*, before even the empty-activities
  early return, so the headline (always full-history/as-of-today) never
  disappears just because `--sport`/`--timerange` matches nothing elsewhere on
  the page — see "Fitness index"
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
- `render_stats(activities: list[dict]) -> None`
- `render_sparkline(data: list[float], label: str) -> None`
- `render_trend(activities: list[dict], metric: str) -> None`
- `render_new_pb_messages(new_pbs: list[dict]) -> None` — formats
  `compute.detect_new_pbs()`'s structured output into "New fastest 5k: 21:40"-style
  lines; uses its own mm:ss colon formatter (`_format_seconds_colon`), deliberately
  distinct from the tables' `_format_duration` ("21m40s") style
- `render_warnings(messages: list[str]) -> None` — prints `storage.py`-sourced
  warning strings (e.g. corrupt activity files) as plain `warning: ...` lines
- `render_usage() -> None` — static one-screen command cheat sheet for `fit
  usage`; no computation, no file I/O

### `importers.py`
Parses external formats and returns a correctly-shaped activity dict. Uses stdlib
`xml.etree.ElementTree` for GPX/TCX (no external XML library) and `fitparse` for FIT.

Key functions:
- `import_gpx(path: str) -> dict`
- `import_tcx(path: str) -> dict`
- `import_fit(path: str) -> dict`
- `import_strava_csv(path: str) -> list[dict]` — a single bare Strava `activities.csv`
- `import_strava_export(export_dir: str) -> list[dict]` — a full Strava bulk-export
  archive (`activities.csv` + linked, possibly gzipped, GPX/TCX/FIT files)
- `import_by_extension(path: str, suffix: str) -> dict` — dispatches to
  `import_gpx`/`import_tcx`/`import_fit` by suffix, raising `ValueError` for anything
  else; the single source of truth for extension dispatch, used by both `cli.py`'s
  `import` command and `_import_strava_linked_file` (for linked/gzipped files inside
  a bulk export)

Importers never check for duplicates themselves — they always return every activity
they parse. `cli.py`'s `import_activity` is the single place that calls
`activity_exists()` per activity to decide what to skip; this keeps `importers.py`
fully decoupled from `storage.py`'s on-disk state (it doesn't import `storage` at all).

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

- `planner.py` — structured workout generation (intervals, tempo runs, long rides)
  following a periodised plan. Rule-based initially, adaptive later.
- `garmin.py` — auth and upload via the unofficial `python-garminconnect` library.
  Approach TBD: file-based GPX import vs live API sync.

Do not design current modules around these features. They will be added as separate
modules when the MVP is stable.

---

## Conventions

- Functions over classes, always.
- `storage.py` is the filesystem boundary. Nothing outside it opens files.
- `compute.py` is pure. No I/O, no side effects.
- Use `.get()` for all activity dict field access in case older activities lack the field.
- Paths stored in dicts are relative to the data dir, never absolute.
- Atomic writes (`os.replace`) for any full-file overwrite.
- `FIT_DATA_DIR` env var overrides the data directory — useful for tests and alternate locations.
- `tests/` holds a small, deliberately non-exhaustive pytest suite covering the
  subtlest pure functions (splits, PBs, fitness EWMA, config parsing, importers).
  Run `.venv/bin/pytest` after any change to `compute.py`, `storage.py`, or
  `importers.py`.
- Everything committed under `examples/` and `tests/data/` is synthetic —
  generated fake data, no real recordings. Real personal data never enters the
  repo: keep it outside the project dir (e.g. `~/fit-dev-data/`) and point
  `FIT_DATA_DIR` at it when needed. `examples/data/` is a demo data dir
  (six months of fake activities); `examples/strava-export/` is a miniature
  fake Strava bulk export; `tests/data/test_run.fit` is a synthetic
  constant-pace FIT file. The generation tooling is deliberately not part of
  the repo — fit displays fitness data, it doesn't generate it.
