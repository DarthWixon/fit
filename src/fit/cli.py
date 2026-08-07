"""Typer app. One function per subcommand. Each function: call storage -> call
compute -> call display. Nothing else.
"""

import tempfile
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path

import typer

from fit import compute, display, garmin, importers, planner, storage

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
        help="Only show this sport for this run (overrides the sports config)",
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
        help="Only show this sport for this run (overrides the sports config)",
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


def _import_and_report(
    new_activities: list[dict], save_original: bool, fallback_source: str | None = None
) -> None:
    """Shared tail of every import path: dedupe, write, save the original
    file into gpx/, print new-PB messages, recompute the PB cache, and print
    the imported/skipped summary. Each activity's transient "_source_path"
    key (set by importers.import_directory / garmin_sync) is popped here even
    for skipped duplicates so it is never persisted; single-file imports pass
    fallback_source instead."""
    pbs_before_import = storage.read_pbs()

    imported, skipped = 0, 0
    for activity in new_activities:
        source_path = activity.pop("_source_path", None) or fallback_source
        if storage.activity_exists(activity["id"]):
            skipped += 1
            continue
        storage.write_activity(activity)
        if save_original:
            storage.save_gpx_file(source_path, activity["id"])
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
            save_original = False
        else:
            new_activities = importers.import_directory(str(source), max_hr)
            save_original = True
    else:
        suffix = source.suffix.lower()
        if suffix == ".csv":
            new_activities, import_warnings = importers.import_strava_csv(str(source))
        elif suffix in (".gpx", ".tcx", ".fit"):
            new_activities = [
                importers.import_by_extension(str(source), suffix, max_hr)
            ]
        else:
            typer.echo(f"Unsupported file type: {suffix}", err=True)
            raise typer.Exit(code=1)
        save_original = suffix in (".gpx", ".tcx", ".fit")

    display.render_warnings(import_warnings)
    _import_and_report(new_activities, save_original, fallback_source=str(source))


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
            activity = importers.import_fit(tmp_paths[-1], max_hr)
            activity["_source_path"] = tmp_paths[-1]
            new_activities.append(activity)

        _import_and_report(new_activities, save_original=True)
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


@app.command()
def plan(
    sport: str = typer.Option(..., "--sport", help="run | swim | cycle"),
    type: str = typer.Option(
        ...,
        "--type",
        help="intervals | tempo | hills | baseline (availability varies by sport)",
    ),
    push: bool = typer.Option(
        True, "--push/--no-push", help="Push to Garmin Connect after saving locally"
    ),
) -> None:
    try:
        specs = planner.workout_params(sport, type)
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
