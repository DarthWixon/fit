"""Typer app. One function per subcommand. Each function: call storage -> call
compute -> call display. Nothing else.
"""

import tempfile
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path

import typer

from fit import compute, display, garmin, importers, planner, storage, training

app = typer.Typer()


def _load_activities() -> list[dict]:
    """Shared preamble for every command that reads history: make sure the
    data dir exists, read all activities, surface corrupt-file warnings."""
    storage.ensure_data_dir()
    activities, warnings = storage.read_activities_with_warnings()
    display.render_warnings(warnings)
    return activities


def _recompute_and_write_pbs(activities: list[dict]) -> dict:
    pbs = compute.all_personal_bests(activities)
    pbs["computed_from"] = len(activities)
    storage.write_pbs(pbs)
    return pbs


def _get_fresh_pbs(activities: list[dict]) -> dict:
    pbs = storage.read_pbs()
    if not compute.pbs_cache_is_valid(pbs, len(activities)):
        return _recompute_and_write_pbs(activities)
    return pbs


def _windowed_pbs(activities: list[dict], start: str, end: str) -> dict:
    windowed = compute.filter_by_date(activities, start, end)
    return compute.all_personal_bests(windowed)


def _pbs_for_window(activities: list[dict], months: int, today: date_cls) -> dict:
    """PBs for a --months/pbs_window_months value: cached all-time when 0,
    windowed fresh-compute otherwise. Shared by `pbs` and `_dashboard_window`."""
    if not months:
        return _get_fresh_pbs(activities)
    start = compute.months_ago(today, months)
    return _windowed_pbs(activities, start, today.isoformat())


def _write_new_baseline(value: float) -> dict:
    """Builds {"baseline_date", "baseline_value"} for `value` (dated today) and
    persists it — shared by _get_or_init_fitness_baseline (lazy init) and
    fitness_reset (explicit re-anchor)."""
    baseline = {"baseline_date": date_cls.today().isoformat(), "baseline_value": value}
    storage.write_fitness_baseline(baseline)
    return baseline


def _get_or_init_fitness_baseline(activities: list[dict]) -> dict:
    """Lazy-cache-if-missing, mirroring _get_fresh_pbs — but unlike pbs.json,
    fitness.json's baseline is sticky: never auto-recomputed once set, only
    replaced by explicit `fit fitness-reset`."""
    stored = storage.read_fitness_baseline()
    if stored:
        return stored
    baseline_value = compute.compute_baseline_value(activities, date_cls.today())
    if not baseline_value:
        return {}
    return _write_new_baseline(baseline_value)


_EMPTY_FITNESS_SNAPSHOT = {"current": None, "baseline_date": None, "weekly": []}


def _fitness_snapshot(
    activities: list[dict],
    today: date_cls,
    baseline: dict,
    window: tuple[str, str] | None = None,
) -> dict:
    """activities must always be the full, unfiltered list — never narrowed by
    --sport/--timerange — so the index stays "one combined index" and the
    headline stays "as of today" regardless of dashboard filters. baseline is
    the _get_or_init_fitness_baseline() dict ({} = no data yet). window, if
    given, only narrows the trend series returned for display, not the
    headline value."""
    if not baseline:
        return _EMPTY_FITNESS_SNAPSHOT

    raw_series = compute.fitness_ewma_daily(activities, today)
    index_series = compute.rescale_to_index(raw_series, baseline["baseline_value"])
    current = index_series[-1]["index"] if index_series else None
    display_series = (
        compute.filter_series_by_date(index_series, *window) if window else index_series
    )
    weekly = compute.weekly_fitness_index(display_series)
    return {
        "current": current,
        "baseline_date": baseline["baseline_date"],
        "weekly": weekly,
    }


def _dashboard_window(
    all_activities: list[dict],
    timerange: str | None,
    config_months: int,
    today: date_cls,
) -> dict:
    """Resolve the dashboard's window precedence: --timerange beats
    pbs_window_months beats all-time. Returns the resolved activity list, the
    PBs to show, and the window labelling render_dashboard needs:
    {"activities", "pbs", "window_months", "window_label", "date_window"}.
    Raises ValueError on a malformed timerange."""
    if timerange is not None:
        start, end = compute.parse_timerange(timerange, today)
        activities = compute.filter_by_date(all_activities, start, end)
        return {
            "activities": activities,
            "pbs": _windowed_pbs(activities, start, end),
            "window_months": 0,
            "window_label": f"last {timerange.strip().lower()}",
            "date_window": (start, end),
        }
    return {
        "activities": all_activities,
        "pbs": _pbs_for_window(all_activities, config_months, today),
        "window_months": config_months,
        "window_label": None,
        "date_window": None,
    }


@app.command()
def dashboard(
    sport: str = typer.Option(
        None,
        "--sport",
        help="Only show this sport for this run: run, cycle, walk, hike, swim, "
        "squash, canoe (overrides the sports config)",
    ),
    timerange: str = typer.Option(
        None,
        "--timerange",
        help="Rolling window ending today, e.g. 10d, 2w, 3m, 1y (overrides pbs_window_months for this run)",
    ),
    minimal: bool = typer.Option(
        False,
        "--minimal",
        help="Show only the sparklines and recent activities for this run",
    ),
) -> None:
    all_activities = _load_activities()
    config = storage.read_config()
    if minimal:
        config = {**config, "show_pbs": False, "show_sports_summary": False}

    today = date_cls.today()
    try:
        window = _dashboard_window(
            all_activities, timerange, config["pbs_window_months"], today
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    if config["show_fitness_index"]:
        baseline = _get_or_init_fitness_baseline(all_activities)
        fitness = _fitness_snapshot(
            all_activities, today, baseline, window["date_window"]
        )
    else:
        fitness = _EMPTY_FITNESS_SNAPSHOT

    sports = [sport] if sport is not None else (config["sports"] or None)
    display.render_dashboard(
        window["activities"],
        window["pbs"],
        config,
        fitness,
        today,
        sports=sports,
        window_months=window["window_months"],
        window_label=window["window_label"],
    )


@app.command()
def dash(
    sport: str = typer.Option(
        None,
        "--sport",
        help="Only show this sport for this run: run, cycle, walk, hike, swim, "
        "squash, canoe (overrides the sports config)",
    ),
    timerange: str = typer.Option(
        None,
        "--timerange",
        help="Rolling window ending today, e.g. 10d, 2w, 3m, 1y (overrides pbs_window_months for this run)",
    ),
) -> None:
    """Shorthand for `fit dashboard --minimal`."""
    dashboard(sport=sport, timerange=timerange, minimal=True)


@app.command()
def pbs(
    months: int = typer.Option(
        None,
        "--months",
        help="Only consider activities from the last N months (0 = all-time)",
    ),
) -> None:
    activities = _load_activities()
    config = storage.read_config()
    window = months if months is not None else config["pbs_window_months"]
    current_pbs = _pbs_for_window(activities, window, date_cls.today())
    display.render_pbs_table(
        current_pbs, sports=config["sports"] or None, window_months=window
    )


@app.command()
def fitness() -> None:
    activities = _load_activities()
    baseline = _get_or_init_fitness_baseline(activities)
    snapshot = _fitness_snapshot(activities, date_cls.today(), baseline)
    display.render_fitness_index(
        snapshot["current"], snapshot["baseline_date"], snapshot["weekly"]
    )


@app.command(name="fitness-reset")
def fitness_reset() -> None:
    activities = _load_activities()

    old_baseline = storage.read_fitness_baseline()
    new_value = compute.compute_baseline_value(activities, date_cls.today())
    if not new_value:
        typer.echo("Not enough activity data to set a fitness baseline yet.", err=True)
        raise typer.Exit(code=1)

    new_baseline = _write_new_baseline(new_value)
    display.render_fitness_reset(old_baseline, new_baseline)


@app.command()
def stats(week: bool = False, month: bool = False, year: bool = False) -> None:
    activities = _load_activities()

    today = date_cls.today()
    window = "week" if week else "month" if month else "year" if year else None
    date_range = compute.stats_date_range(window, today)
    if date_range is not None:
        activities = compute.filter_by_date(activities, *date_range)

    display.render_stats(activities, today)


def _import_and_report(new_activities: list[dict]) -> None:
    """Shared tail of every import path: dedupe, write, print new-PB messages,
    recompute the PB cache, and print the imported/skipped summary."""
    pbs_before_import = storage.read_pbs()

    imported, skipped = 0, 0
    for activity in new_activities:
        if storage.activity_exists(activity["id"]):
            skipped += 1
            continue
        storage.write_activity(activity)
        imported += 1
        display.render_new_pb_messages(
            compute.detect_new_pbs(activity, pbs_before_import)
        )

    if imported:
        _recompute_and_write_pbs(_load_activities())

    typer.echo(f"Imported {imported} new activities, skipped {skipped} duplicates")


@app.command(name="import")
def import_activity(path: str) -> None:
    storage.ensure_data_dir()
    max_hr = storage.read_config()["max_heart_rate"]
    source = Path(path)

    import_warnings: list[str] = []
    if source.is_dir():
        if (source / "activities.csv").exists():
            new_activities, import_warnings = importers.import_strava_export(
                str(source), max_hr
            )
        else:
            new_activities = importers.import_directory(str(source), max_hr)
    else:
        suffix = source.suffix.lower()
        if suffix == ".csv":
            new_activities, import_warnings = importers.import_strava_csv(str(source))
        elif suffix in (".tcx", ".fit"):
            new_activities = [
                importers.import_by_extension(str(source), suffix, max_hr)
            ]
        else:
            typer.echo(f"Unsupported file type: {suffix}", err=True)
            raise typer.Exit(code=1)

    display.render_warnings(import_warnings)
    _import_and_report(new_activities)


@app.command()
def gs() -> None:
    """Shorthand for `garmin-sync --days = 7`."""
    garmin_sync(days=7)


@app.command(name="garmin-sync")
def garmin_sync(
    days: int = typer.Option(
        14, "--days", help="Look back this many days for new activities"
    ),
) -> None:
    storage.ensure_data_dir()
    max_hr = storage.read_config()["max_heart_rate"]

    try:
        client = garmin.login()
    except garmin.GarminAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    end = date_cls.today()
    start = end - timedelta(days=days)
    summaries = garmin.list_recent_activities(client, start, end)
    typer.echo(
        f"Found {len(summaries)} activities on Garmin Connect since {start.isoformat()}"
    )

    new_activities, tmp_paths = [], []
    try:
        for summary in summaries:
            fit_bytes = garmin.download_activity_fit(client, summary["activityId"])
            with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as tmp:
                tmp.write(fit_bytes)
                tmp_paths.append(tmp.name)
            new_activities.append(importers.import_fit(tmp_paths[-1], max_hr))

        _import_and_report(new_activities)
    finally:
        for tmp_path in tmp_paths:
            Path(tmp_path).unlink(missing_ok=True)


def _prompt_params(specs: list[dict]) -> dict:
    """typer.prompt each planner spec (Enter accepts the shown default),
    re-prompting on a ValueError from the spec's parser. A spec with a
    "derive" callable (see planner.recommend_defaults) gets its default
    computed at prompt time from the answers collected so far."""
    params = {}
    for spec in specs:
        default = spec["derive"](params) if "derive" in spec else spec["default"]
        while True:
            raw = typer.prompt(spec["label"], default=str(default))
            try:
                params[spec["key"]] = spec["parse"](str(raw))
                break
            except ValueError as exc:
                typer.echo(f"invalid value: {exc}", err=True)
    return params


def _prompt_optional_value(spec: dict, default: str):
    """One prompt that may be answered blank, returning None when it is —
    which is how a repeated group knows the user has finished adding."""
    while True:
        raw = str(typer.prompt(spec["label"], default=default)).strip()
        if not raw:
            return None
        try:
            return spec["parse"](raw)
        except ValueError as exc:
            typer.echo(f"invalid value: {exc}", err=True)


def _prompt_repeated_params(specs: list[dict]) -> list[dict]:
    """Prompt one whole group of specs at a time, appending each answered
    group to a list, until the first field is left blank. `fit plan --sport
    strength` is the only caller — a gym session is several exercises, not one
    flat answer per prompt — and planner.repeated_param_specs is what says so,
    so this never names the combo itself."""
    first, rest = specs[0], specs[1:]
    entries: list[dict] = []
    while True:
        value = _prompt_optional_value(
            (
                first
                if not entries
                else {**first, "label": f"{first['label']}, or blank to finish"}
            ),
            str(first["default"]) if not entries else "",
        )
        if value is None:
            if entries:
                return entries
            typer.echo("at least one is needed", err=True)
            continue
        entries.append({first["key"]: value, **_prompt_params(rest)})


@app.command()
def plan(
    sport: str = typer.Option(..., "--sport", help="run | swim | cycle | strength"),
    type: str = typer.Option(
        ...,
        "--type",
        help=(
            "intervals | tempo | hills | baseline | straight_sets "
            "(availability varies by sport)"
        ),
    ),
    push: bool = typer.Option(
        True, "--push/--no-push", help="Push to Garmin Connect after saving locally"
    ),
    schedule: str = typer.Option(
        None,
        "--schedule",
        metavar="DATE",
        help="Also place the workout on this date (YYYY-MM-DD) in the Garmin calendar",
    ),
) -> None:
    try:
        specs = planner.workout_params(sport, type)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    # Validate --schedule up front: fail fast and offline, before the interactive
    # prompts and the Garmin push, so a typo'd date never wastes either. Scheduling
    # needs a pushed workout to attach to, so --schedule with --no-push is a
    # contradiction rather than a silent no-op.
    schedule_date = None
    if schedule is not None:
        if not push:
            typer.echo("--schedule cannot be combined with --no-push", err=True)
            raise typer.Exit(code=1)
        try:
            schedule_date = planner.parse_schedule_date(schedule)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1)

    activities = _load_activities()
    recs = planner.recommend_defaults(
        sport, type, activities, storage.read_plans(), date_cls.today()
    )
    for spec in specs:
        rec = recs.get(spec["key"])
        if rec and "derive" in rec:
            spec["derive"] = rec["derive"]
        elif rec:
            spec["default"] = rec["default"]
    display.render_plan_recommendations(recs)

    params = _prompt_params(specs)
    repeated_key, repeated_specs = planner.repeated_param_specs(sport, type)
    if repeated_key:
        params[repeated_key] = _prompt_repeated_params(repeated_specs)
    created = datetime.now().isoformat(timespec="seconds")
    plan_dict = planner.build_plan(sport, type, params, created)
    storage.write_plan(plan_dict)
    display.render_plan_saved(plan_dict, planner.describe_plan(plan_dict))

    if not push:
        return
    try:
        client = garmin.login()
    except garmin.GarminAuthError as exc:
        typer.echo(
            f"{exc}\nThe plan is saved locally — re-run with --no-push to skip Garmin.",
            err=True,
        )
        raise typer.Exit(code=1)
    response = garmin.push_workout(client, plan_dict["payload"])
    plan_dict["garmin_workout_id"] = response.get("workoutId")
    storage.write_plan(plan_dict)
    display.render_plan_pushed(plan_dict)

    if schedule_date is None:
        return
    workout_id = plan_dict["garmin_workout_id"]
    if workout_id is None:
        typer.echo(
            "Pushed, but Garmin returned no workout id, so it can't be scheduled.",
            err=True,
        )
        raise typer.Exit(code=1)
    garmin.schedule_workout(client, workout_id, schedule_date)
    plan_dict["scheduled_date"] = schedule_date
    storage.write_plan(plan_dict)
    display.render_plan_scheduled(plan_dict)


@app.command()
def history(n: int = typer.Argument(10)) -> None:
    display.render_history_table(_load_activities(), n)


@app.command()
def calendar() -> None:
    activities = _load_activities()
    display.render_calendar(compute.activity_calendar(activities, date_cls.today()))


@app.command()
def usage() -> None:
    display.render_usage()


# --- fit train: multi-week periodised plans -------------------------------

train_app = typer.Typer(help="Multi-week periodised training plans")
app.add_typer(train_app, name="train")


def _require_training_plan() -> dict:
    """The active plan, or exit with the how-to-create-one message."""
    storage.ensure_data_dir()
    plan = storage.read_training_plan()
    if not plan:
        display.render_training_missing()
        raise typer.Exit(code=1)
    return plan


def _show_plan(plan: dict, activities: list[dict], weeks: int | None) -> None:
    """The shared `train import`/`train show` tail: match completion against
    history, then render."""
    sessions = training.match_completion(plan["sessions"], activities)
    grouped = training.group_by_week(sessions)
    if weeks:
        grouped = grouped[:weeks]
    display.render_training_plan(
        training.plan_summary({**plan, "sessions": sessions}, date_cls.today()),
        grouped,
    )


def _login_or_exit():
    try:
        return garmin.login()
    except garmin.GarminAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@train_app.command(name="import")
def train_import(
    path: str = typer.Argument(..., help="YAML plan description file"),
    weeks: int = typer.Option(
        0, "--weeks", help="Only show the first N weeks after importing (0 = all)"
    ),
) -> None:
    """Expand a YAML plan description into a full periodised schedule."""
    activities = _load_activities()

    existing = storage.read_training_plan()
    if existing:
        pending = training.future_scheduled(
            existing.get("sessions", []), date_cls.today()
        )
        if pending:
            typer.echo(
                f"The active plan still has {len(pending)} future session(s) on the "
                "Garmin calendar. Run `fit train clear` first, or they will be left "
                "there with nothing tracking them. To update this plan's targets "
                "without touching the calendar, use `fit train retarget`.",
                err=True,
            )
            raise typer.Exit(code=1)

    try:
        spec = training.parse_plan_spec(Path(path).read_text())
        plan = training.expand_plan(spec, activities, date_cls.today())
    except (ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)

    storage.write_training_plan(plan)
    _show_plan(plan, activities, weeks)


@train_app.command(name="show")
def train_show(
    weeks: int = typer.Option(
        0, "--weeks", help="Only show the first N weeks (0 = all)"
    ),
) -> None:
    """Show the active plan with each session marked planned or done."""
    plan = _require_training_plan()
    _show_plan(plan, _load_activities(), weeks)


@train_app.command(name="retarget")
def train_retarget(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change, then stop"
    ),
) -> None:
    """Re-derive the plan's intensity targets from your latest history."""
    plan = _require_training_plan()
    activities = _load_activities()

    # A plan file predating the stored spec, or naming a goal that no longer
    # exists, cannot be re-derived — say so rather than KeyError-ing inside
    # derive_targets.
    spec = plan.get("spec")
    if not spec or spec.get("goal") not in training.GOAL_TEMPLATES:
        typer.echo(
            "This plan can't be retargeted — it predates the stored plan spec, or "
            "its goal no longer exists. Run `fit train clear` then "
            "`fit train import` instead.",
            err=True,
        )
        raise typer.Exit(code=1)

    today = date_cls.today()
    targets = training.derive_targets(spec, activities, today)
    summary = training.retarget_sessions(plan, targets, today)
    if not dry_run:
        storage.write_training_plan(plan)
    display.render_training_retargeted(summary, dry_run=dry_run)


@train_app.command(name="sync")
def train_sync(
    days: int = typer.Option(
        0,
        "--days",
        help="Schedule this many days ahead (0 = config train_sync_window_days)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be pushed, then stop"
    ),
) -> None:
    """Push and schedule the plan's next sessions onto the Garmin calendar."""
    plan = _require_training_plan()
    window_days = days or storage.read_config()["train_sync_window_days"]
    due = training.sync_window(plan["sessions"], date_cls.today(), window_days)
    already = sum(
        1
        for s in plan["sessions"]
        if not s.get("is_extra") and s.get("status") == "scheduled"
    )
    if not due:
        display.render_training_synced(
            {
                "scheduled": 0,
                "already": already,
                "window_days": window_days,
                "failed": [],
            }
        )
        return

    # Confirm before touching the account: this creates a workout and a calendar
    # entry per session, and `garmin.login()` resumes a saved session silently,
    # so without this the whole batch can go out with no visible step in between.
    display.render_training_sync_preview(due)
    if dry_run:
        return
    if not yes and not typer.confirm("Push and schedule these?", default=False):
        typer.echo("Nothing pushed.")
        return

    client = _login_or_exit()
    scheduled, failed = 0, []
    for session in due:
        args = training.session_to_build_args(session)
        if args is None:  # extras never reach here, but stay defensive
            continue
        try:
            built = planner.build_plan(*args, session["date"])
            response = garmin.push_workout(client, built["payload"])
            workout_id = response.get("workoutId")
            if workout_id is None:
                raise ValueError("Garmin returned no workout id")
            placed = garmin.schedule_workout(
                client, workout_id, planner.parse_schedule_date(session["date"])
            )
            session["garmin_workout_id"] = workout_id
            session["scheduled_workout_id"] = placed.get("workoutScheduleId")
            session["scheduled_date"] = session["date"]
            session["status"] = "scheduled"
            scheduled += 1
        except Exception as exc:  # one bad session must not lose the rest
            failed.append(f"{session['date']} {session['workout_name']}: {exc}")
        # Rewrite after every session: a crash mid-sync must never leave the
        # plan file claiming less than what is actually on the calendar.
        storage.write_training_plan(plan)

    display.render_training_synced(
        {
            "scheduled": scheduled,
            "already": already,
            "window_days": window_days,
            "failed": failed,
        }
    )


@train_app.command(name="clear")
def train_clear() -> None:
    """Remove the plan's future sessions from the Garmin calendar."""
    plan = _require_training_plan()
    pending = training.future_scheduled(plan["sessions"], date_cls.today())
    if not pending:
        display.render_training_cleared({"cleared": 0, "failed": []})
        return

    client = _login_or_exit()
    cleared, failed = 0, []
    for session in pending:
        schedule_id = session.get("scheduled_workout_id")
        try:
            if schedule_id is not None:
                garmin.unschedule_workout(client, schedule_id)
            session["garmin_workout_id"] = None
            session["scheduled_workout_id"] = None
            session["scheduled_date"] = None
            session["status"] = "planned"
            cleared += 1
        except Exception as exc:
            failed.append(f"{session['date']} {session['workout_name']}: {exc}")
        storage.write_training_plan(plan)

    display.render_training_cleared({"cleared": cleared, "failed": failed})
