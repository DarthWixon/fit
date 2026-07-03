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
uv venv                      # creates .venv with the pinned Python version
uv pip install -e '.[dev]'   # the app plus pytest
.venv/bin/fit usage          # smoke test
```

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
  test to re-measure your pace)
- **swim** — `intervals`
- **cycle** — `intervals`, `hills`, `baseline` (an FTP-test shape)

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
- reps: one more than your last plan of the same type, capped at 10

With no matching history, sensible static defaults are shown instead.

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
