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

Add the Garmin Connect sync dependency only if you use `fit garmin-sync`:

```bash
uv pip install -e '.[garmin]'
```

To run `fit` without the `.venv/bin/` prefix, activate the venv
(`source .venv/bin/activate`) or add an alias.

### Do not copy `.venv` between machines

A virtualenv hard-codes the absolute path and interpreter of the machine it was
created on, and macOS quarantines executables that arrive by file copy, so a
copied `.venv` fails in confusing ways (silent kills, Gatekeeper popups).
When moving to a new machine, bring only the code (via git) and your data
folder (`~/.fit/`, which is plain JSON and portable), then recreate the venv
with the steps above. `.venv/` is gitignored for this reason.

## Running

```bash
fit usage        # command cheat sheet
fit import ./run.gpx
fit dashboard
```

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
