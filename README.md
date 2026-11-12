# fit

Local-first terminal workout tracker. All data lives in `~/.fit/` — no accounts,
no cloud, no sync. See `CLAUDE.md` for the full design notes.

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

`./install.sh` does the `.venv` steps above with the `train` extra too, and
puts `fit` on your PATH as an editable `uv tool install`, so source changes
apply immediately. `./install.sh --garmin` adds the Garmin extra to both.

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
fit import ./run.fit
fit dashboard
fit plan --sport run --type intervals   # generate a workout, push to your watch
```

Data lives in `~/.fit/`. Point the app somewhere else with the `FIT_DATA_DIR`
environment variable — useful for development so you never touch real data:

```bash
FIT_DATA_DIR=./examples/data fit dashboard
```

`fit plan` prompts for the workout's parameters (reps, target pace, recovery, …)
with defaults derived from your recent training where possible, saves the workout
to `~/.fit/plans/`, and uploads it to Garmin Connect so it appears under
Training > Workouts on the watch's next sync. Use `--no-push` to generate and
save without a Garmin login (works without the `garmin` extra installed).

Supported `--sport` / `--type` combinations:

- **run** — `intervals`, `tempo`, `baseline` (a best-effort benchmark test to
  re-measure your pace), `easy`, `long`
- **cycle** — `intervals`, `baseline` (an FTP-test shape), `endurance`, `long`
- **swim** — `intervals`, `continuous`, `baseline` (a 1km time trial)
- **strength** — `straight_sets`, `baseline` (a heavy triple on one lift, read as
  an estimated 1RM)

The four `baseline` types are benchmark tests — `fit plan --sport swim --type
baseline` plans one. Run and cycle tests keep a warmup and cooldown inside the
workout, because fit can isolate the effort from what surrounds it. The swim
and strength tests are bare, and their names say "warm up first": a swim
measurement spans the whole activity, and a strength test prescribes no load
to ramp a warmup to.

Five of these are *steady* sessions — run `easy`/`long`, cycle
`endurance`/`long`, swim `continuous` — a single block at a wide target band
(none at all for the rides, which are just marked easy), with no warmup or
cooldown split (a warmup inside an easy run is just more easy
running).

Prompt defaults are calculated from your last six months of activities where
possible — each one is just a suggestion; press Enter to accept or type your own:

- run interval pace: your best recent 5k (a whole run or the fastest 5k split
  inside a longer one, else your fastest recent run of 3km or more), read as 5k
  race pace — 3% faster when reps are 1km or shorter
- run tempo pace: ~7% slower than that 5k pace
- swim interval pace: critical swim speed estimated from your best recent 500m
  and 1k times (falling back to 1k pace, then your median swim pace)
- cycle interval power: 95% of your best recent 20-minute power, as an FTP
  estimate (without one, the best whole-ride average of a recent ride of 19+
  minutes)
- easy/long run pace: ~30% slower than that 5k pace
- reps: one more than your last plan of the same type, capped at 10

With no matching history, sensible static defaults are shown instead. A
measurement outside plausible bounds is thrown out, and the prompt says so.

## Training plans

`fit train` runs one plan: **StrongLifts 5x5** on Monday, Wednesday and Friday,
with easy riding around it — a ride on Thursday and Sunday, and a second weekend
ride on Saturday. Every session is a real Garmin workout, rolled onto the
calendar a fortnight at a time.

A short YAML description dates it:

```yaml
start_date: 2026-11-04
end_date: 2027-01-31
days_per_week: [5, 6]      # optional: open with Thu/Sun rides, add Saturday later
volume: 70                 # optional: % of the opening week's riding
targets:                   # optional: e1RM goal per lift
  squat_goal_kg: 105
```

```bash
uv pip install -e '.[train]'   # only `train import` needs this (PyYAML)

fit train import plan.yaml     # expand it into a dated schedule
fit train show --weeks 2       # what's coming, and what you've already done
fit train sync --dry-run       # exactly what would be pushed, without pushing
fit train sync                 # push + schedule the next 14 days on Garmin
fit train clear                # take future sessions off the calendar and library
fit train refresh              # clear + sync: re-push at your current loads
```

`sync` lists what it is about to create and asks before touching your Garmin
account (`--yes` skips the prompt, `--dry-run` stops after the list). It is a
rolling window — re-run it whenever you like and it schedules only what is newly
due, so it is safe to repeat. `show` marks a session done when an activity of
the same sport lands in the same week.

### Lifting

Workouts A (squat, bench, row) and B (squat, overhead press, deadlift 1x5, then
jumping pull-ups) alternate every time you lift: A/B/A one week, B/A/B the next.
Loads follow what you actually lifted. Hit every rep of every set and the next
session adds 2.5kg (5kg for the deadlift); miss reps and the weight stays; miss three sessions running
at one weight and it drops 10%. Each lift starts at the weight your history says
you could lift ten times — StrongLifts' start for anyone who has lifted before.
There are no deload weeks and no tests. Pull-ups add a rep instead of weight, up
to three sets of eight.

A lift with a goal stops at it; without one, `fit train show` says where the
plan will get to.

### Riding

Every ride is easy and untargeted, so the watch never beeps. They grow 4% a
week. `fit` measures your average weekly riding over the last eight weeks and
sizes the opening week to match, then converges back to the plan's own level by
the end:

```
Starting volume: 60% — your recent 1.3h/week against the plan's 2.4h opening week
```

Set `volume: 70` to override the measurement, or `days_per_week` to train fewer
days. A range — `[5, 6]` — starts with fewer and adds the rest later.

### Nothing to re-run

The plan is derived, not stored. `~/.fit/train/plan.json` keeps only your
description, the starting volume pinned at import, and a ledger of what has
been pushed; the schedule itself is rebuilt on every `fit train` command. So
importing a session with `fit garmin-sync` is what moves the next load.

**Sessions already pushed to your watch** render from the ledger rather than
from a fresh derivation, because Garmin has no edit endpoint and the watch is
holding the copy that was sent. `fit train show` marks those `on watch`, or
`on watch*` once the live derivation has moved away from them; `fit train
refresh` re-pushes them at current loads.

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
there (`config`, `pbs.json`); those are gitignored. The import demo adds five
activities to `examples/data/activities/` — two of them from the CSV row, since
fit reads TCX and FIT but not the GPX files Strava exports — and skips one
`Workout` row it has no type for. Put the example dir back with:

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
