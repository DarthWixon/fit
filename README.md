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
fit import ./run.fit
fit dashboard
fit plan --sport run --type intervals   # generate a workout, push to your watch
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

The four `baseline` types are the benchmark tests, and you can plan one on its
own — `fit plan --sport swim --type baseline` — as well as letting a training
block schedule them, and either way you get the same test. Run and cycle tests
keep a warmup and cooldown inside the workout, because fit can isolate the
effort from what surrounds it. The swim one defaults to neither, and its name
says "warm up first": the measurement spans the whole activity.

Five of these are *steady* sessions — run `easy`/`long`, cycle
`endurance`/`long`, swim `continuous` — a single block at a wide target band,
with no warmup or cooldown split (a warmup inside an easy run is just more easy
running). They exist mainly so `fit train` can build a whole week out of real
workouts.

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
goal: standard_triathlon       # see the goal table below
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

Four goals are available — one per discipline, since `start_date` already
re-lengthens any of them:

| Goal | Weeks | Days/wk | Trains |
|---|---|---|---|
| `run_10k` | 10 | 5 | run |
| `cycle_strength` | 12 | 5 | cycle, strength |
| `standard_triathlon` | 16 | 6 | swim, cycle, run, strength |
| `strength_program` | 12 | 3 | strength |

Each sets its own length, weekly session mix and progression; `days_per_week`,
`rest_day` and `progression` in the description override the defaults. Trimming
a multi-sport plan to fewer days never drops a whole discipline.

### Changing the length

Each goal has a default length, but `start_date` sets the real one — anything
from four weeks up:

```yaml
goal: cycle_strength
event_date: 2026-11-15
start_date: 2026-09-21     # 8 weeks instead of the template's 12
```

The phases reapportion to fit, and the weekly ramp is solved from the length you
chose: a longer block climbs more gently to the same peak rather than trying to
climb higher. For `cycle_strength` that is 8%/week over its own 12 weeks, 5.5%
over 16, and 1.9% over 40 — all of them peaking at the same 68km long ride. A
block shorter than the template simply peaks lower (54km over 8 weeks), which is
what a short run-up buys you.

Set `progression.weekly_ramp_pct` if you would rather pin the rate yourself, and
`progression.taper_weeks` to change how long the taper runs.

### Getting faster, not just fitter

Paces and power targets are always *measured* from your history, never
projected forward — the sessions get longer, not faster. A plan that assumed you
would improve on schedule would start prescribing work you cannot finish. A plan
earns a faster pace by re-measuring.

So every plan schedules **re-tests** on its recovery weeks, when you are
rested: a 5km best effort for running, a 20-minute FTP test for cycling, a 1km
time trial for swimming, a heavy triple for strength, taking turns between
whichever the goal trains. Each one replaces that week's quality session rather
than adding to it.

Run and cycle tests are normal workouts — warm up, test, cool down, all in one
recording — because fit finds the effort inside it: the fastest 5km anywhere in
the track, and the best 20 minutes of power anywhere in the ride. The swim test
is bare, and its name says "warm up first", because there is no equivalent way
to isolate a swim effort from the rest of a session. The strength test is a
triple rather than a true 1RM: a plan shouldn't send you to a maximal single
alone in a gym every few weeks, and an estimated 1RM reads a 3RM fine.

```
Re-test weeks: 4, 8, 12 — do the test and `fit garmin-sync`; the plan
re-derives from it automatically.
```

There is no step in between, and no `fit train retarget` — the plan is derived,
not stored. `~/.fit/train/plan.json` keeps only your description, the starting
volume pinned at import, and a ledger of what has been pushed; the schedule
itself is rebuilt from scratch on every `fit train` command. So the targets you
see are always the ones your *current* history supports, and importing a faster
5k is what applies it.

Two things deliberately don't move. **Starting volume** is pinned when the plan
is imported — it is a decision about where you were when you began, and
re-measuring it weekly would rewrite your session sizes as you train.
**Sessions already pushed to your watch** render from the ledger rather than
from a fresh derivation, because Garmin has no edit endpoint and the watch is
holding the copy that was sent. `fit train show` marks those `on watch`, or
`on watch*` once the live derivation has moved away from them:

```
3 session(s) marked * were pushed at an earlier target and can't be updated —
Garmin has no edit endpoint. `fit train clear` removes them so they re-push at
your current fitness.
```

Volume is never touched by a re-test: the sessions get your new paces, not new
distances. Add `benchmarks: false` to skip the tests entirely, or `test_week:
true` to open the plan with a benchmarks-only week 0 — worth it when your
history is thin, since otherwise every target is derived from whatever happens
to be on disk.

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
