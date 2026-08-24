# fit

Local-first terminal workout tracker. All data lives in `~/.fit/` — no accounts,
no cloud. See `CLAUDE.md` for the full design notes.

## Requirements

- Python 3.11 or newer. The pinned version for this project is in
  `.python-version` (currently 3.14).
- [uv](https://docs.astral.sh/uv/) is the recommended way to set up the
  environment — it reads `.python-version` and downloads a matching interpreter
  if the machine doesn't have one.

## Setup on a new machine

```bash
git clone <repo-url> fit && cd fit
uv venv                        # creates .venv with the pinned Python version
uv pip install -e '.[dev]'     # the app plus pytest, black, isort
git config core.hooksPath hooks  # enable the pre-commit format check
.venv/bin/fit usage            # smoke test
```

The `core.hooksPath` line is needed once per clone — git does not pick up
`hooks/` on its own (see [Formatting](#formatting) below).

Without uv, any Python ≥3.11 works:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Add the Garmin Connect dependency only if you use `fit garmin-sync` or push
workouts to your watch with `fit plan`:

```bash
uv pip install -e '.[garmin]'
```

Add PyYAML only if you use `fit train import` (see
[Training plans](#training-plans)):

```bash
uv pip install -e '.[train]'
```

## Running

```bash
fit usage        # command cheat sheet
fit import ./run.gpx
fit dashboard
fit plan --sport run --type intervals   # generate a workout, push to your watch
```

`fit plan` prompts for the workout's parameters (reps, target pace, recovery, …)
with defaults derived from your recent training where possible, saves the workout
to `~/.fit/plans/`, and uploads it to Garmin Connect so it appears under
Training > Workouts on the watch's next sync. Use `--no-push` to generate and
save without a Garmin login (works without the `garmin` extra installed).

Supported `--sport` / `--type` combinations:

- **run** — `intervals`, `tempo`, `hills`, `baseline` (a best-effort benchmark
  test to re-measure your pace), `easy`, `long`
- **swim** — `intervals`, `continuous`
- **cycle** — `intervals`, `hills`, `baseline` (an FTP-test shape), `endurance`,
  `long`

The last four are steady sessions: a single block at a wide target band, with no
warmup or cooldown split (a warmup inside an easy run is just more easy running).
They exist mainly so `fit train` can build a whole week out of real workouts.

Prompt defaults are calculated from your last six months of activities where
possible — each one is just a suggestion; press Enter to accept or type your own:

- run interval pace: your best recent 5k (a whole run or the fastest 5k split
  inside a longer one), read as 5k race pace — 3% faster when reps are 1km or
  shorter
- run tempo pace: ~7% slower than that 5k pace
- swim interval pace: critical swim speed estimated from your best recent 500m
  and 1k times (falling back to 1k pace, then your median swim pace)
- cycle interval power: the highest average power among your recent rides of 20+
  minutes
- easy/long run pace: ~30% slower than that 5k pace
- endurance/long ride power: ~70% of that threshold estimate
- reps: one more than your last plan of the same type, capped at 10

With no matching history, sensible static defaults are shown instead.

## Training plans

`fit train` turns a goal into a full multi-week periodised plan: a real Garmin
workout for every session, paces and power derived from your own history, rolled
onto the calendar a fortnight at a time.

The input is a short YAML description — the idea is that you talk a goal through
with an AI assistant and it writes this file; `fit` owns all the periodisation, so
the description stays thin:

```yaml
goal: sprint_triathlon         # see the goal table below
event_date: 2026-11-15
days_per_week: 6
rest_day: Mon
extras: {strength: 2, yoga: 1}
targets:                  # optional; otherwise derived from your history
  run_5k: "24:00"
  bike_ftp: 250
  swim_css_100m: "1:45"
```

```bash
uv pip install -e '.[train]'   # only `train import` needs this (PyYAML)

fit train import plan.yaml     # expand it into a dated schedule
fit train show --weeks 2       # what's coming, and what you've already done
fit train sync --dry-run       # exactly what would be pushed, without pushing
fit train sync                 # push + schedule the next 14 days on Garmin
fit train clear                # take future sessions back off the calendar
```

`sync` lists what it is about to create and asks before touching your Garmin
account (`--yes` skips the prompt, `--dry-run` stops after the list). It is a
rolling window — re-run it whenever you like and it schedules only what is newly
due, so it is safe to repeat. `show` marks a session done when an
activity of the same sport lands within a day of it. Yoga and strength "extras"
are placed on your easier days and tracked locally only: the Garmin calendar has
no endpoint for anything that isn't a workout, so they are never pushed.

Eight goals are available:

| Goal | Weeks | Days/wk |
|---|---|---|
| `run_5k` | 8 | 4 |
| `run_10k` | 10 | 5 |
| `run_half` | 12 | 5 |
| `cycle_25k_tt` | 8 | 4 |
| `cycle_40k_tt` | 10 | 5 |
| `cycle_100k_sportive` | 12 | 5 |
| `sprint_triathlon` | 12 | 6 |
| `standard_triathlon` | 16 | 6 |

Each sets its own length, weekly session mix and progression; `days_per_week`,
`rest_day` and `progression` in the description override the defaults. Trimming
a multi-sport plan to fewer days never drops a whole discipline.

### Changing the length

Each goal has a default length, but `start_date` sets the real one — anything
from four weeks up:

```yaml
goal: cycle_100k_sportive
event_date: 2026-11-15
start_date: 2026-09-21     # 8 weeks instead of the template's 12
```

The phases reapportion to fit, and the weekly ramp is solved from the length you
chose: a longer block climbs more gently to the same peak rather than trying to
climb higher. For the sportive that is 8%/week over 12 weeks, 5.5% over 16, and
1.9% over 40 — all of them peaking at the same 103km long ride. A block shorter
than the template simply peaks lower, which is what a short run-up buys you.

Set `progression.weekly_ramp_pct` if you would rather pin the rate yourself, and
`progression.taper_weeks` to change how long the taper runs.

### Getting faster, not just fitter

Paces and power targets are measured from your history when the plan is built,
and they stay put — the sessions get longer, not faster. A plan that assumed you
would improve on schedule would start prescribing work you cannot finish.

Instead every plan schedules **re-tests** on its recovery weeks, when you are
rested: a 3km best effort for running, a 20-minute FTP test for cycling, taking
turns if the goal trains both. Each one replaces that week's quality session
rather than adding to it.

```
Re-test weeks: 4, 8, 12 — do the test, sync it back, then re-import to
rebuild the rest at your new fitness.
```

So the cycle is: do the test, `fit garmin-sync`, then `fit train clear` and
`fit train import` again. The remaining weeks come back rebuilt at whatever you
just demonstrated. Add `benchmarks: false` to skip them.

### Easing in

`days_per_week` also takes a range, to build frequency across the plan rather
than training the same number of days from week one:

```yaml
days_per_week: [2, 4]      # two rides a week, working up to four
```

Sessions arrive in priority order, so the first weeks hold the ones that matter
most and later weeks add to them. The final count is held through the taper —
a taper cuts volume, not frequency.

### Starting where you actually are

Templates assume a base you may not have, so `fit` measures your average weekly
training in the goal's sports over the last eight weeks and sizes the opening
week to match — then converges back to the template's own level by the last build
week, so you still arrive at a volume the event demands. `fit train show` reports
what it measured and what it did with it:

```
Starting volume: 60% — your recent 0.5h/week against the template's 5.0h opening week
note: this plan grows 2.9x from week 1 to its peak — your recent training is well
below where the goal needs to start. An earlier start_date, or a shorter goal,
would be a gentler way in.
```

Set `volume: 70` in the description to override the measurement, or
`days_per_week` to train fewer days. They do different things — `volume` shrinks
each session, `days_per_week` removes whole sessions — and they combine.

Data lives in `~/.fit/`. Point the app somewhere else with the `FIT_DATA_DIR`
environment variable — useful for development so you never touch real data:

```bash
FIT_DATA_DIR=./examples/data fit dashboard
```

## Example data

Everything under `examples/` and `tests/data/` is synthetic — generated fake
activities, not recordings of a real person. `examples/data/` is a ready-made
data directory with six months of fake training history, and
`examples/strava-export/` is a miniature Strava bulk export:

```bash
FIT_DATA_DIR=./examples/data fit dashboard
FIT_DATA_DIR=./examples/data fit pbs
FIT_DATA_DIR=./examples/data fit import examples/strava-export
```

Running commands against `examples/data` generates a config file and caches
there (`config`, `pbs.json`, `fitness.json`); those are gitignored. The import
demo also adds the five imported activities to `examples/data/activities/` —
put the example dir back with:

```bash
git restore examples/data && git clean -fdq examples/data
```

## Tests

```bash
.venv/bin/pytest
```

## Formatting

Code is formatted with black (default settings) and isort (black profile).
`hooks/pre-commit` checks staged `.py` files and aborts the commit if either
would change them — it never reformats behind your back, so a commit contains
exactly what you staged. Enable it once per clone:

```bash
git config core.hooksPath hooks
```

To fix what it flags:

```bash
.venv/bin/isort src tests && .venv/bin/black src tests
```

Without the `dev` extra installed the hook skips itself with a hint rather than
blocking the commit.
