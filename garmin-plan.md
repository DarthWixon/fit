# Direct Garmin watch import (no Strava)

**Status: implemented 2026-07-02** (both parts), with one deviation: the
Garmin session token lives at the library-default `~/.garminconnect`, outside
the data dir, so `~/.fit` stays credential-free — the `storage.garmin_token_dir()`
accessor described below was dropped. Remaining: a live `fit garmin-sync` test
against the real account (needs the user's credentials), and confirming the
watch mounts as USB storage. Original plan below, written 2026-07-01.

## Context

`fit import` today only ingests: a single `.gpx`/`.tcx`/`.fit` file, a bare
Strava `activities.csv`, or a full Strava bulk-export directory
(`importers.import_strava_export`). There is no way to get data from a
Garmin watch into `fit` without first exporting from Strava. The user wants
two independent paths, both bypassing Strava:

1. **USB cable / direct file copy** (primary) — plug the watch in, copy FIT
   files straight off it. No account, no credentials, no cloud — matches
   this app's "no accounts, no cloud dependency" design goal.
2. **Garmin Connect API** (secondary/fallback) — for when a cable isn't
   handy, sync over WiFi/Bluetooth via Garmin's cloud (still not Strava)
   using the unofficial `garminconnect` PyPI package.

Both are one-shot manual commands, run whenever the user wants — no
background daemon, no auto-detection of a plugged-in device, consistent with
the app's one-shot CLI design (no persistent TUI). Scope is new activities
only — no backfill/reconciliation against already-imported Strava history is
needed. `storage.activity_exists(activity["id"])` (existing) already dedupes
by ISO-timestamp ID, so repeated/overlapping syncs are safe for free without
any new dedup logic.

The user wasn't sure whether their watch exposes a USB mass-storage volume —
Part 1 explains how to check. If it turns out not to, Part 2 is the only
path; no design change either way, it just decides which part gets used
first.

## Part 1 — USB / direct file copy (extends `fit import`)

Every Garmin watch with a physical charge/data cable (USB-C on newer
Forerunner/Fenix/Vivoactive models, the older proprietary clip on things
like the Forerunner 235/45) exposes a `GARMIN/ACTIVITY/` folder of `.FIT`
files as a plain USB mass-storage device when plugged in — same mechanism as
a card reader, no drivers needed on macOS or Linux. (A small number of newer
watches without a physical port are Bluetooth-only and can't do this — Part
2 is the only option for those.) **To check:** plug the watch in and look
for a new volume — `ls /Volumes` on macOS, `lsblk` / `~/media/$USER/` on
Linux — then look inside for `GARMIN/ACTIVITY/*.FIT`.

`fit` already has a working FIT parser (`importers.import_fit`, added for
the squash fixture) — the only missing piece is "import every loose file in
a folder", since `fit import <path>` currently only recognizes two
directory shapes: a Strava bulk export (`activities.csv` present), or
nothing else.

### `src/fit/importers.py` — new function, next to `import_strava_export`

```python
def import_directory(dir_path: str) -> list[dict]:
    """A loose folder of .gpx/.tcx/.fit files - e.g. a Garmin watch's mounted
    GARMIN/ACTIVITY folder - NOT a Strava bulk export (see import_strava_export
    for that; cli.py tells the two apart by checking for activities.csv).
    Each returned dict carries a transient "_source_path" key (the absolute
    path of the file it came from) that only cli.py reads, to decide what to
    copy into gpx/ - never persisted to the activity JSON."""
    activities = []
    for file_path in sorted(Path(dir_path).iterdir()):
        suffix = file_path.suffix.lower()
        if suffix not in (".gpx", ".tcx", ".fit"):
            continue
        activity = import_by_extension(str(file_path), suffix)
        activity["_source_path"] = str(file_path)
        activities.append(activity)
    return activities
```

Reuses `import_by_extension` (existing dispatcher) — no new parsing code.
Non-recursive (`iterdir()`, not `rglob()`) since `GARMIN/ACTIVITY/` is flat;
revisit only if a real device turns out to nest subfolders.

### `src/fit/cli.py` — `import_activity`'s directory branch grows a second case

```python
if source.is_dir():
    if (source / "activities.csv").exists():
        new_activities = importers.import_strava_export(str(source))
        save_original = False
    else:
        new_activities = importers.import_directory(str(source))
        save_original = True
```

Per-activity loop pops the transient key to find what to copy:

```python
for activity in new_activities:
    source_path = activity.pop("_source_path", None)
    if storage.activity_exists(activity["id"]):
        skipped += 1
        continue
    storage.write_activity(activity)
    if save_original:
        storage.save_gpx_file(source_path or str(source), activity["id"])
    ...
```

That's the entire USB path. Once merged: `fit import
/Volumes/GARMIN/GARMIN/ACTIVITY` (macOS) or `fit import
/media/$USER/GARMIN/GARMIN/ACTIVITY` (Linux) imports every FIT file on the
watch, deduping and copying originals into `~/.fit/gpx/` exactly like a
single-file import does today. Re-running it later only imports what's new.

## Part 2 — Garmin Connect API sync (`fit garmin-sync`)

For when the cable isn't handy. Uses
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect) (PyPI:
`garminconnect`), an actively-maintained unofficial wrapper around Garmin
Connect's private mobile API. The following was confirmed from a live read
of `master` on 2026-07-01 — cite for whoever implements this, but **reconfirm
against the actually-installed version before writing code**, since this is
a fast-moving, unofficial library:

- `Garmin(email=None, password=None, prompt_mfa=None, return_on_mfa=False,
  ...)` — constructor.
- `garmin.login(tokenstore: str | None)` — logs in; on first call with
  `email`/`password` set, authenticates and **saves a session token** to
  `tokenstore` (library default `~/.garminconnect/garmin_tokens.json`,
  overridable with any path). On later calls with no `email`/`password`,
  resumes the saved session — **no password ever stored on disk**, only a
  session token.
- MFA: pass `prompt_mfa=lambda: input("MFA code: ").strip()` (or a
  Typer-prompt equivalent) to the constructor — the library calls it
  interactively if the account has MFA enabled. Build this in from the
  start since it's unknown whether this account has MFA on.
- Listing activities: `garmin.get_activities(start=0, limit=20,
  activitytype=None)` is confirmed in current source. A date-ranged variant
  (commonly `get_activities_by_date(startdate, enddate, activitytype=None)`
  in past releases) needs reconfirming against the installed version —
  check the installed package's `__init__.py` directly (`pip show
  garminconnect` to find it) rather than trusting memory or this doc.
- Downloading: the library exposes FIT download via a
  `download-service/files/activity` endpoint (confirmed in current source),
  historically wrapped as `garmin.download_activity(activity_id,
  dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)`. **Also reconfirm**: (a)
  exact current method/enum names, (b) whether the response bytes are a raw
  `.fit` file or a `.zip` wrapping one (Garmin's "original" export has, at
  various points, returned a zip even for a single activity — if so, unwrap
  via `zipfile`/`io.BytesIO` before parsing, the same "unwrap then dispatch
  to `import_by_extension`" shape `importers._import_strava_linked_file`
  already uses for gzipped Strava-export files). A 2023 GitHub issue (#155)
  reported `download_activity` broken after a garth migration; the library
  has since moved to a different native auth engine (v0.3+), so this is
  very likely stale — confirm with one live call against a real account
  before trusting it.

### New module: `src/fit/garmin.py`

Parallel role to `importers.py` (external-format parsing boundary) and
`storage.py` (filesystem boundary) — `garmin.py` is **the only module that
talks to the Garmin Connect network API**. It returns raw FIT bytes; it
never builds the activity JSON shape itself and never touches
`~/.fit/activities/` — that stays `cli.py` → `importers.import_fit` →
`storage.write_activity`, identical to every other import path.

```python
def login(token_dir: Path) -> "Garmin":
    """Resumes a saved session from token_dir if present; otherwise prompts
    for email/password (and an MFA code, if the account needs it) via typer,
    then saves a session token to token_dir for next time. Raises
    GarminAuthError on failure."""

def list_recent_activities(client, start_date: date, end_date: date) -> list[dict]:
    """Raw Garmin Connect activity summaries (id, activityType, startTimeLocal,
    ...) in [start_date, end_date] - NOT yet in fit's activity dict shape."""

def download_activity_fit(client, garmin_activity_id) -> bytes:
    """Raw FIT bytes for one activity, unzipped if Garmin wraps it."""
```

`import garminconnect` happens lazily inside these functions (same pattern
`importers.import_fit` already uses for `from fitparse import FitFile`) so
the rest of `fit` keeps working if the optional dependency isn't installed —
only `fit garmin-sync` needs it.

### `src/fit/storage.py` — one new path accessor

```python
def garmin_token_dir() -> Path:
    return resolve_data_dir() / "garmin" / "token"
```

Follows the existing one-accessor-per-special-path convention. The
`garminconnect` library manages the files inside this directory itself
(writes/reads its own token JSON) — `fit`'s own code never opens files there
directly, so this isn't really an exception to "`storage.py` is the only
module that touches the filesystem", it's `storage.py` handing an opaque
directory to a library that manages it, similar to how `fitparse` reads FIT
bytes itself once handed a path.

Trade-off worth flagging: this lives inside `~/.fit/`, the same folder the
"single folder that can be zipped and moved to a new machine" goal covers
(CLAUDE.md's opening section). That's convenient (no re-login after moving
machines) but means a zipped copy of `~/.fit/` now contains an auth token,
not just workout data — treat the zip as sensitive if that's unwanted.
(Alternative: use the library's own default, `~/.garminconnect`, outside the
data dir entirely — a decision point, not decided here.)

### `src/fit/cli.py` — new command

```python
@app.command(name="garmin-sync")
def garmin_sync(
    days: int = typer.Option(14, "--days", help="Look back this many days for new activities"),
) -> None:
```

Loop shape: login → list activities in `[today - days, today]` → per
activity, download FIT bytes → write to a
`tempfile.NamedTemporaryFile(suffix=".fit")` → `importers.import_fit(tmp_path)`
→ same dedupe/write/`save_gpx_file`/new-PB-message/PB-recompute sequence
`import_activity` already has, then delete the temp file.
`import_activity`'s loop body (dedupe check, `storage.write_activity`,
`storage.save_gpx_file`, `display.render_new_pb_messages`, final
`_recompute_and_write_pbs`) is close to identical between the two commands —
worth factoring into a small shared helper (e.g. `_import_and_report`)
during implementation rather than duplicating it.

### `pyproject.toml` — optional dependency

```toml
[project.optional-dependencies]
garmin = ["garminconnect>=0.2,<1.0"]
```

Kept optional rather than a hard dependency — CLAUDE.md is explicit about
minimizing the dependency footprint, and `garminconnect` is a much heavier,
faster-moving, unofficial-reverse-engineered-API dependency than `fitparse`.
Every other command keeps working with a plain `pip install -e .`; only
`fit garmin-sync` needs `pip install -e '.[garmin]'`, and should fail with
that exact suggestion in its error message if the import fails.

## Non-changes

- No change to `import_fit`/`import_gpx`/`import_tcx` or any existing
  parsing logic — both new paths feed the exact same `importers.import_fit`.
- No backfill/reconciliation logic — out of scope, only new activities going
  forward are needed.
- No auto-detection of a mounted watch, no background/scheduled sync — both
  paths are explicit, manually-invoked commands.
- No new `~/.fit/config` keys — `--days` on `garmin-sync` covers the one
  tunable knob; no cosmetic/functional config need identified.

## Open items to verify before/during implementation

1. Confirm the installed `garminconnect` version's exact activity-listing
   method (date-range params) and `download_activity`/
   `ActivityDownloadFormat` — don't trust this document's names blindly,
   they're best-effort from a live read of `master` on 2026-07-01 and the
   package moves fast.
2. Confirm whether `download_activity(..., dl_fmt=ORIGINAL)` returns a raw
   `.fit` or a `.zip` — unwrap if the latter.
3. Confirm this Garmin Connect account's MFA status (the `prompt_mfa`
   callback should handle it either way, but worth testing the actual login
   flow once).
4. Confirm the user's specific watch model mounts as USB mass storage (see
   Part 1's "how to check").

## Verification (manual, no test suite — this project has none by design)

Using `FIT_DATA_DIR` pointed at a scratch dir (never `~/.fit` or the local
real-data folder `~/fit-dev-data/` without asking first):

**Part 1:** copy a couple of real `.FIT` files (e.g. reuse
`~/fit-dev-data/gpx/Afternoon_Squash.fit`) into a scratch folder with no
`activities.csv`, run `fit import <folder>`, confirm both import, dedupe
correctly on a second run, and land in `gpx/` under their activity IDs.
`.venv/bin/python -m py_compile src/fit/importers.py src/fit/cli.py` first.

**Part 2:** requires a real Garmin Connect login to test end-to-end —
`.venv/bin/python -m py_compile` everything first, then a live
`fit garmin-sync --days 7` against the real account (with the user present,
since it needs their credentials and possibly an MFA code) to confirm
login, token persistence (second run shouldn't re-prompt), listing,
download, unzip-if-needed, and import all work.
