"""Renders compute.py outputs (and raw activity lists) as Rich terminal output.
No file I/O. Any math needed to build a composite view (e.g. weekly volumes for
the dashboard) is delegated to compute.py, never done inline here.
"""

from datetime import date

from rich.console import Console
from rich.table import Table
from rich.text import Text

from fit import compute

console = Console()

_SPARK_CHARS = "▁▂▃▄▅▆▇█"

# Z1..Z5, cool -> hot, matching the standard training-zone colour convention.
_HR_ZONE_COLORS = ["blue", "green", "yellow", "dark_orange", "red"]
_HR_ZONE_BAR_WIDTH = 10


def render_warnings(messages: list[str]) -> None:
    for message in messages:
        console.print(f"warning: {message}")


def render_usage() -> None:
    console.print(
        "fit dashboard [--sport S] [--timerange 3m]  summary, sparkline, PBs, fitness\n"
        "fit dash [--sport S] [--timerange 3m]       = dashboard --minimal (no PBs)\n"
        "fit pbs [--months N]                        personal bests table\n"
        "fit stats [--week|--month|--year]           totals + breakdown by type\n"
        "fit fitness                                 current fitness index + trend\n"
        "fit fitness-reset                           re-anchor fitness baseline to today\n"
        "fit import <path>                           GPX/TCX/FIT or Strava export\n"
        "fit garmin-sync [--days N]                  pull recent Garmin activities\n"
        "fit plan --sport S --type T [--no-push]     build a workout, push to Garmin\n"
        "fit history [N]                             last N activities (default 10)\n"
        "fit calendar                                active days, last 2 months\n"
        "fit usage                                   this screen\n"
        "\n"
        "Data dir: ~/.fit  (override: FIT_DATA_DIR=path fit ...)\n"
        "Config:   ~/.fit/config  (hand-editable, see comments in the file)"
    )


def render_sparkline(data: list[float], label: str, partial_last: bool = False) -> None:
    """partial_last dims the final bar — for a still-in-progress week, whose
    volume is low simply because the week isn't over yet."""
    if not data:
        console.print(f"{label}: [dim](no data)[/dim]")
        return

    lo, hi = min(data), max(data)
    if hi == lo:
        indices = [len(_SPARK_CHARS) // 2] * len(data)
    else:
        indices = [round((v - lo) / (hi - lo) * (len(_SPARK_CHARS) - 1)) for v in data]
    spark = "".join(_SPARK_CHARS[i] for i in indices)
    if partial_last:
        spark = f"{spark[:-1]}[dim]{spark[-1]}[/dim]"

    console.print(f"[cyan]{label}[/cyan]: {spark}  ({lo:.1f}–{hi:.1f})")


def _format_effort(activity: dict) -> str:
    """Average power for cycle activities that have it, else pace."""
    if activity.get("type") == "cycle" and activity.get("avg_power") is not None:
        return f"{round(activity['avg_power'])}W avg"
    distance_km = activity.get("distance_km", 0) or 0
    duration_seconds = activity.get("duration_seconds", 0) or 0
    return compute.calc_pace(distance_km, duration_seconds, activity.get("type", ""))


def _format_distance(activity: dict) -> str:
    if activity.get("type") in compute.NO_DISTANCE_TYPES:
        return "—"
    distance_km = activity.get("distance_km", 0) or 0
    return f"{distance_km:.2f}km"


def _format_hr_zones(activity: dict) -> Text | str:
    """Segmented colour bar (Z1 blue -> Z5 red), each zone's block-character
    width proportional to its % of time in that zone. "—" when the activity
    has no hr_zones (predates this feature, Strava-CSV-only import, or
    max_heart_rate wasn't configured at import time)."""
    percentages = compute.hr_zone_percentages(activity.get("hr_zones"))
    if percentages is None:
        return "—"
    bar = Text()
    allocated = 0
    for i in range(1, 6):
        if i < 5:
            width = round(percentages[f"zone{i}"] / 100 * _HR_ZONE_BAR_WIDTH)
        else:
            width = _HR_ZONE_BAR_WIDTH - allocated
        width = max(width, 0)
        allocated += width
        if width:
            bar.append("█" * width, style=_HR_ZONE_COLORS[i - 1])
    return bar


def render_history_table(activities: list[dict], n: int) -> None:
    recent = sorted(activities, key=lambda a: a.get("date", ""), reverse=True)[:n]

    table = Table(title=f"Last {n} Activities")
    table.add_column("Date")
    table.add_column("Type")
    table.add_column("Distance", justify="right")
    table.add_column("Duration", justify="right")
    table.add_column("Pace", justify="right")
    table.add_column("HR Zones", justify="left")

    for activity in recent:
        duration_seconds = activity.get("duration_seconds", 0) or 0
        table.add_row(
            activity.get("date", ""),
            activity.get("type", ""),
            _format_distance(activity),
            _format_duration(duration_seconds),
            _format_effort(activity),
            _format_hr_zones(activity),
        )
    console.print(table)


def render_pbs_table(
    pbs: dict,
    sports: list[str] | None = None,
    window_months: int = 0,
    window_label: str | None = None,
) -> None:
    if window_label:
        title = f"Personal Bests ({window_label})"
    elif window_months:
        title = f"Personal Bests (Last {window_months} Months)"
    else:
        title = "Personal Bests"
    table = Table(title=title)
    table.add_column("Type")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_column("Date")

    for activity_type, type_pbs in pbs.items():
        if activity_type == "computed_from":
            continue
        if sports and activity_type not in sports:
            continue
        collapsed = compute.best_pb_per_label(type_pbs)
        for key, value in collapsed.items():
            if key.endswith("_date"):
                continue
            date = collapsed.get(_date_key_for(key), "")
            label, formatted = _format_pb_metric(key, value)
            table.add_row(activity_type, label, formatted, date)

    console.print(table)


def _render_type_summary_table(activities: list[dict], title: str) -> None:
    table = Table(title=title)
    table.add_column("Type")
    table.add_column("Count", justify="right")
    table.add_column("Time", justify="right")
    table.add_column("Distance", justify="right")
    for row in compute.summarize_by_type(activities):
        distance = (
            "—"
            if row["type"] in compute.NO_DISTANCE_TYPES
            else f"{row['distance_km']:.1f}km"
        )
        table.add_row(
            row["type"],
            str(row["count"]),
            _format_duration(row["duration_seconds"]),
            distance,
        )
    console.print(table)


def render_sports_summary(activities: list[dict]) -> None:
    """Dashboard block: always covers every activity type present in the given
    activities — callers must not pre-filter by sport (pre-filtering by date
    is fine)."""
    _render_type_summary_table(activities, title="Sports Summary")


def _render_month_block(month: dict) -> Text:
    lines = [f"[bold]{month['label']}[/bold]", "[dim]Mo Tu We Th Fr Sa Su[/dim]"]
    active = set(month["active_days"])
    for week in month["weeks"]:
        cells = []
        for day in week:
            if not day:
                cells.append("  ")
            elif day in active:
                cells.append(f"[bold green]{day:2d}[/bold green]")
            else:
                cells.append(f"{day:2d}")
        lines.append(" ".join(cells))
    return Text.from_markup("\n".join(lines))


def render_calendar(months: list[dict]) -> None:
    """months: compute.activity_calendar's output — one grid dict per month,
    oldest first, rendered side by side as columns. Active days render bold
    green; padding cells (0) blank."""
    grid = Table.grid(padding=(0, 2, 0, 0))
    for _ in months:
        grid.add_column()
    grid.add_row(*(_render_month_block(month) for month in months))
    console.print(grid)


def render_fitness_index(
    current_index: float | None,
    baseline_date: str | None,
    weekly_series: list[dict],
    window_label: str | None = None,
) -> None:
    """Headline + trend sparkline for the fitness index (see "Fitness index" in
    CLAUDE.md). current_index/baseline_date always reflect full history as of
    today — callers must not pre-filter by sport or time range. weekly_series
    ([{"week": ..., "index": ...}, ...] from compute.weekly_fitness_index) may
    be windowed by the caller (e.g. for --timerange) since only the trend
    line, not the headline, is meant to narrow."""
    if current_index is None:
        console.print("[dim]Fitness index: not enough data yet.[/dim]")
        return

    console.print(
        f"[cyan]Fitness Index[/cyan]: {current_index:.0f}  "
        f"[dim](Baseline 100 set {baseline_date})[/dim]"
    )
    label = f"Fitness trend ({window_label})" if window_label else "Fitness trend"
    render_sparkline([w["index"] for w in weekly_series], label)


def render_fitness_reset(old_baseline: dict, new_baseline: dict) -> None:
    if old_baseline:
        console.print(
            f"Baseline re-anchored: {old_baseline['baseline_value']:.2f} "
            f"(set {old_baseline['baseline_date']}) -> "
            f"{new_baseline['baseline_value']:.2f} (set {new_baseline['baseline_date']})"
        )
    else:
        console.print(
            f"Baseline set: {new_baseline['baseline_value']:.2f} "
            f"(set {new_baseline['baseline_date']})"
        )


def _last_week_partial(weekly: list[dict], today: date) -> bool:
    """Whether a weekly series ends on the still-in-progress current week."""
    return bool(weekly) and compute.is_current_week(weekly[-1]["week"], today)


def render_stats(activities: list[dict], today: date) -> None:
    if not activities:
        console.print("[dim]No activities yet.[/dim]")
        return

    summary = compute.summarize_by_type(activities)
    total_distance = sum(row["distance_km"] for row in summary)
    total_duration = sum(row["duration_seconds"] for row in summary)

    console.print(
        f"[bold]{len(activities)}[/bold] activities, "
        f"[bold]{_format_duration(total_duration)}[/bold] total time, "
        f"[bold]{total_distance:.1f}km[/bold] total"
    )

    _render_type_summary_table(activities, title="By type")

    weekly = compute.weekly_volumes(activities, through=today)
    render_sparkline(
        [w["duration_seconds"] / 3600 for w in weekly],
        "Weekly volume (hours)",
        partial_last=_last_week_partial(weekly, today),
    )


def render_dashboard(
    activities: list[dict],
    pbs: dict,
    config: dict,
    fitness: dict,
    today: date,
    sports: list[str] | None = None,
    window_months: int = 0,
    window_label: str | None = None,
) -> None:
    """config is the storage.read_config() dict (history_count, dashboard_weeks,
    show_* toggles). fitness is cli's snapshot dict {"current", "baseline_date",
    "weekly"} — always full-history/as-of-today, never narrowed by sports or
    window (see "Fitness index" in CLAUDE.md). The volume and fitness-trend
    sparklines are capped to the last config["dashboard_weeks"] weeks (0 = all),
    unless --timerange is already driving the window. today anchors the volume
    series' final week (see compute.weekly_volumes) and marks it as partial.

    Block order: fitness index -> weekly volume sparkline -> time range banner
    -> history table -> calendar -> personal bests -> sports summary. Sports
    summary always renders last and is never restricted by --sport/config
    sports — even when the sport filter matches nothing elsewhere on the
    page, it still shows every type present."""
    # Cap the volume/fitness sparklines to a recent window, unless --timerange
    # is already driving the window (a truthy window_label), in which case the
    # explicit flag wins and nothing is further truncated.
    weeks_cap = 0 if window_label else config["dashboard_weeks"]

    if config["show_fitness_index"]:
        trend_series = fitness["weekly"]
        trend_label = window_label
        if weeks_cap:
            trend_series = trend_series[-weeks_cap:]
            trend_label = f"last {weeks_cap} wks"
        render_fitness_index(
            fitness["current"],
            fitness["baseline_date"],
            trend_series,
            window_label=trend_label,
        )
        console.print()

    if not activities:
        if window_label:
            console.print(f"[dim]No activities in {window_label}.[/dim]")
        else:
            console.print(
                "[dim]No activities logged yet. Use `fit import <path>` to add one.[/dim]"
            )
        return

    filtered = compute.filter_by_types(activities, sports) if sports else activities
    if not filtered:
        console.print("[dim]No activities match the configured sport filter.[/dim]")
        console.print()
    else:
        if config["show_sparkline"]:
            weekly = compute.weekly_volumes(filtered, through=today)
            volume_label = "Weekly volume (hours)"
            if weeks_cap:
                weekly = weekly[-weeks_cap:]
                volume_label += f" (last {weeks_cap} wks)"
            render_sparkline(
                [w["duration_seconds"] / 3600 for w in weekly],
                volume_label,
                partial_last=_last_week_partial(weekly, today),
            )
            console.print()

        if window_label:
            console.print(f"[dim]Time range: {window_label}[/dim]")
            console.print()

        render_history_table(filtered, config["history_count"])
        console.print()

        if config["show_calendar"]:
            render_calendar(compute.activity_calendar(filtered, today))
            console.print()

        if config["show_pbs"]:
            render_pbs_table(
                pbs,
                sports=sports,
                window_months=window_months,
                window_label=window_label,
            )
            console.print()

    if config["show_sports_summary"]:
        render_sports_summary(activities)


def _format_duration(total_seconds: int) -> str:
    minutes, seconds = divmod(round(total_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{seconds:02d}s"


def _format_seconds_colon(total_seconds) -> str:
    """mm:ss / h:mm:ss style used only for "New PB" messages, distinct from
    _format_duration's "1h02m"/"5m30s" style used in tables."""
    minutes, seconds = divmod(round(total_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _parse_pb_key(key: str) -> dict:
    """Single source of truth for the pbs.json key-naming convention:
    fastest_{label}_seconds / fastest_{label}_split_seconds / longest_distance_km
    / most_elevation_gain_m. Returns {"category", "label", "date_key"}."""
    date_key = None
    for suffix in ("_seconds", "_km", "_m"):
        if key.endswith(suffix):
            date_key = key[: -len(suffix)] + "_date"
            break

    if key.startswith("fastest_") and key.endswith("_seconds"):
        label = key[len("fastest_") : -len("_seconds")]
        if label.endswith("_split"):
            category, label = "split", label[: -len("_split")]
        else:
            category = "milestone"
    elif key == "longest_distance_km":
        category, label = "longest_distance", None
    elif key == "most_elevation_gain_m":
        category, label = "elevation", None
    else:
        category, label = "unknown", None

    return {"category": category, "label": label, "date_key": date_key}


def _date_key_for(key: str) -> str | None:
    return _parse_pb_key(key)["date_key"]


def _format_pb_metric(key: str, value) -> tuple[str, str]:
    parsed = _parse_pb_key(key)
    if parsed["category"] == "milestone":
        return f"Fastest {parsed['label']}", _format_duration(value)
    if parsed["category"] == "longest_distance":
        return "Longest distance", f"{value:.1f}km"
    if parsed["category"] == "elevation":
        return "Most elevation gain", f"{value:.0f}m"
    return key, str(value)


def render_new_pb_messages(new_pbs: list[dict]) -> None:
    """new_pbs: [{"key": ..., "value": ...}, ...] as returned by
    compute.detect_new_pbs. Message text/format must stay byte-identical to the
    prior inline f-strings — this is display.py's only formatting concern for
    "New PB" announcements, kept distinct from the table's duration style."""
    for pb in new_pbs:
        parsed = _parse_pb_key(pb["key"])
        value = pb["value"]
        if parsed["category"] == "longest_distance":
            console.print(f"New longest distance: {value:.1f}km")
        elif parsed["category"] == "milestone":
            console.print(
                f"New fastest {parsed['label']}: {_format_seconds_colon(value)}"
            )
        elif parsed["category"] == "split":
            console.print(
                f"New fastest {parsed['label']} split: {_format_seconds_colon(value)}"
            )
        elif parsed["category"] == "elevation":
            console.print(f"New most elevation gain: {value:.0f}m")


def render_plan_recommendations(recs: dict) -> None:
    """recs: planner.recommend_defaults' output — {key: {"default", "why"}}.
    Prints nothing when there was no history to derive from."""
    if not recs:
        return
    console.print("[dim]Recommended from your history:[/dim]")
    for rec in recs.values():
        if "default" in rec:
            console.print(f"[dim]  {rec['default']} — {rec['why']}[/dim]")
        else:  # "derive" recs resolve in the prompt itself; just show why
            console.print(f"[dim]  {rec['why']}[/dim]")


def render_plan_saved(plan: dict, step_lines: list[str]) -> None:
    """step_lines: planner.describe_plan's output — one line per top-level
    step (pace/power formatting happens there, not here)."""
    console.print(f"[bold]{plan['workout_name']}[/bold]")
    for line in step_lines:
        console.print(f"  {line}")
    console.print(f"Saved: plans/{plan['id']}.json")


def render_plan_pushed(plan: dict) -> None:
    console.print(
        f"Pushed to Garmin Connect (workout id {plan.get('garmin_workout_id')}) — "
        "it will appear under Training > Workouts on the watch's next sync"
    )
