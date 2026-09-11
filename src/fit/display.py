"""Rich terminal rendering. No I/O, no arithmetic — any maths a view needs is
delegated to compute.py.
"""

from datetime import date

from rich.console import Console
from rich.table import Table
from rich.text import Text

from fit import compute

console = Console()

_SPARK_CHARS = "▁▂▃▄▅▆▇█"

# Z1..Z5, cool -> hot.
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
        "fit fitness-reset [--as-of DATE]            re-anchor the fitness baseline\n"
        "fit import <path>                           TCX/FIT file or Strava export\n"
        "fit garmin-sync [--days N]                  pull recent Garmin activities\n"
        "fit gs                                      = garmin-sync --days 7\n"
        "fit plan --sport S --type T [--no-push]     build a workout, push to Garmin\n"
        "fit plan ... --schedule YYYY-MM-DD          also put it on the Garmin calendar\n"
        "fit train import <plan.yaml>                expand a goal into a full plan\n"
        "fit train show [--weeks N]                  the plan, with what's done\n"
        "fit train sync [--days N] [--dry-run]       push + schedule the next sessions\n"
        "fit train clear                             unschedule future sessions\n"
        "fit train refresh [--days N]                clear + sync at current fitness\n"
        "fit history [N]                             last N activities (default 10)\n"
        "fit calendar                                active days, last 2 months\n"
        "fit usage                                   this screen\n"
        "\n"
        "Data dir: ~/.fit  (override: FIT_DATA_DIR=path fit ...)\n"
        "Config:   ~/.fit/config  (hand-editable, see comments in the file)"
    )


def render_sparkline(data: list[float], label: str, partial_last: bool = False) -> None:
    """partial_last dims the final bar: a week that is only low because it is
    not over yet."""
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
    """Tonnage for a gym session, avg power for a ride that has it, else pace —
    whatever says how hard the session was in that sport's own terms."""
    if activity.get("type") == "strength":
        tonnage = compute.total_weight_lifted(activity)
        return f"{tonnage:,.0f}kg" if tonnage else "—"
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
    """Segmented colour bar, width proportional to each zone's share. "—" when
    the activity has no hr_zones (pre-feature, CSV-only, or max HR unset)."""
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
    # "Effort", not "Pace": pace, power or tonnage, per sport.
    table.add_column("Effort", justify="right")
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
        if activity_type in ("computed_from", "strength"):
            continue
        if sports and activity_type not in sports:
            continue
        collapsed = compute.best_pb_per_label(type_pbs)
        for key, value in collapsed.items():
            if key.endswith("_date"):
                continue
            pb_date = collapsed.get(_date_key_for(key), "")
            label, formatted = _format_pb_metric(key, value)
            table.add_row(activity_type, label, formatted, pb_date)

    console.print(table)

    # Its own table: strength PBs are keyed by exercise, not distance label, so
    # one row per exercise carries both metrics side by side.
    if pbs.get("strength") and not (sports and "strength" not in sports):
        _render_strength_pbs_table(pbs["strength"], f"Strength {title}")


def _humanise_exercise(name: str) -> str:
    """ "shoulder_press" -> "Shoulder press"."""
    return name.replace("_", " ").capitalize()


def _format_kg_with_date(value, pb_date: str | None) -> str:
    """ "120kg (2026-08-01)". Both metrics carry their own date, so a shared
    Date column would have to pick one."""
    if value is None:
        return "—"
    weight = f"{value:g}kg"
    return f"{weight} [dim]({pb_date})[/dim]" if pb_date else weight


def _render_strength_pbs_table(strength_pbs: dict, title: str) -> None:
    table = Table(title=title)
    table.add_column("Exercise")
    table.add_column("Heaviest Set", justify="right")
    table.add_column("Best e1RM", justify="right")
    for name in sorted(strength_pbs):
        exercise_pbs = strength_pbs[name]
        table.add_row(
            _humanise_exercise(name),
            _format_kg_with_date(
                exercise_pbs.get("heaviest_set_kg"),
                exercise_pbs.get("heaviest_set_date"),
            ),
            _format_kg_with_date(
                exercise_pbs.get("best_e1rm_kg"), exercise_pbs.get("best_e1rm_date")
            ),
        )
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
    """Callers must not pre-filter by sport; by date is fine."""
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
    """activity_calendar's grids, side by side. Active days bold green."""
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
    drift: dict | None = None,
) -> None:
    """Headline + trend sparkline. The headline is always full-history as of
    today — callers must not pre-filter it; only weekly_series may be windowed.
    drift is warned about but never repaired: the fix is the user's to run."""
    if current_index is None:
        console.print("[dim]Fitness index: not enough data yet.[/dim]")
        return

    console.print(
        f"[cyan]Fitness Index[/cyan]: {current_index:.0f}  "
        f"[dim](Baseline 100 set {baseline_date})[/dim]"
    )
    if drift:
        moved = drift["actual"] - drift["stored"]
        change = f"{moved:+d}" if moved else "changed"
        console.print(
            f"[yellow]warning[/yellow]: history on or before {baseline_date} has "
            f"changed since the baseline was set "
            f"({drift['stored']} -> {drift['actual']} activities, {change}), so "
            f"the index is measured against a day that no longer looks the same."
        )
        console.print(
            f"[dim]  re-anchor it where it stands with: "
            f"fit fitness-reset --as-of {baseline_date}[/dim]"
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
    """Does the series end on the still-in-progress current week?"""
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
    """Blocks: fitness -> volume sparkline -> time-range banner -> history ->
    calendar -> PBs -> sports summary.

    fitness is cli._fitness_snapshot's dict, always full-history/as-of-today.
    Sports summary renders last over the *unfiltered* list, so it shows every
    type even when the sport filter matches nothing else on the page.
    Sparklines cap to config["dashboard_weeks"] unless --timerange drives it."""
    # --timerange, when given, wins over the config cap.
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
            drift=fitness.get("drift"),
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
    """mm:ss for "New PB" messages, distinct from the tables' _format_duration."""
    minutes, seconds = divmod(round(total_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _parse_pb_key(key: str) -> dict:
    """The pbs.json key-naming convention in one place ->
    {"category", "label", "date_key"}."""
    date_key = None
    for suffix in ("_seconds", "_km", "_m", "_kg"):
        if key.endswith(suffix):
            date_key = key[: -len(suffix)] + "_date"
            break

    # "{exercise}_heaviest_set_kg" — the exercise may itself contain
    # underscores, so it is whatever precedes the metric suffix.
    for suffix, category in (
        ("_heaviest_set_kg", "strength_heaviest"),
        ("_best_e1rm_kg", "strength_e1rm"),
    ):
        if key.endswith(suffix):
            return {
                "category": category,
                "label": key[: -len(suffix)],
                "date_key": date_key,
            }

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
    if parsed["category"] == "strength_heaviest":
        return f"Heaviest {parsed['label'].replace('_', ' ')}", f"{value:g}kg"
    if parsed["category"] == "strength_e1rm":
        return f"Best {parsed['label'].replace('_', ' ')} e1RM", f"{value:g}kg"
    return key, str(value)


def render_new_pb_messages(new_pbs: list[dict]) -> None:
    """detect_new_pbs' entries as "New fastest 5k: 21:40"-style lines."""
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
        elif parsed["category"] == "strength_heaviest":
            console.print(
                f"New heaviest {parsed['label'].replace('_', ' ')}: {value:g}kg"
            )
        elif parsed["category"] == "strength_e1rm":
            console.print(
                f"New estimated 1RM {parsed['label'].replace('_', ' ')}: {value:g}kg"
            )


def render_plan_recommendations(recs: dict) -> None:
    """recommend_defaults' output; prints nothing when empty."""
    if not recs:
        return
    console.print("[dim]Recommended from your history:[/dim]")
    for rec in recs.values():
        if "default" in rec:
            console.print(f"[dim]  {rec['default']} — {rec['why']}[/dim]")
        else:
            # Either a "derive" rec, whose value resolves in the prompt
            # itself, or a why-only one reporting a rejected measurement.
            # Both have only the why worth showing here.
            console.print(f"[dim]  {rec['why']}[/dim]")


def render_plan_saved(plan: dict, step_lines: list[str]) -> None:
    """step_lines come from planner.describe_plan, already formatted."""
    console.print(f"[bold]{plan['workout_name']}[/bold]")
    for line in step_lines:
        console.print(f"  {line}")
    console.print(f"Saved: plans/{plan['id']}.json")


def render_plan_pushed(plan: dict) -> None:
    console.print(
        f"Pushed to Garmin Connect (workout id {plan.get('garmin_workout_id')}) — "
        "it will appear under Training > Workouts on the watch's next sync"
    )


def render_plan_scheduled(plan: dict) -> None:
    console.print(
        f"Scheduled for {plan.get('scheduled_date')} — it will appear on that "
        "date in the Garmin Connect calendar on the watch's next sync"
    )


# --- training plans (fit train) -------------------------------------------

_TARGET_LABELS = {
    "run_5k_seconds": ("Run 5k", "time"),
    "swim_css_100m": ("Swim CSS", "pace"),
    "bike_ftp": ("Bike FTP", "watts"),
}


def _format_target_value(key: str, value) -> str:
    """'22:30', '1:45/100m', '245W'."""
    _, kind = _TARGET_LABELS[key]
    if kind == "time":
        return _format_seconds_colon(value)
    if kind == "pace":
        return f"{_format_seconds_colon(value)}/100m"
    return f"{value}W"


def _format_lift_target(lift: str, entry: dict) -> str:
    """'Squat 105 → 122.5kg': a strength target is a journey, not a figure."""
    name = lift.replace("_", " ").capitalize()
    return (
        f"{name} {entry['current_e1rm_kg']:g} → {entry['goal_e1rm_kg']:g}kg"
        if entry.get("goal_e1rm_kg") is not None
        else f"{name} {entry['current_e1rm_kg']:g}kg"
    )


def _format_targets(targets: dict) -> str:
    """'Run 5k 22:30 · Bike FTP 245W', with strength lifts appended as pairs."""
    parts = [
        f"{label} {_format_target_value(key, targets[key])}"
        for key, (label, _) in _TARGET_LABELS.items()
        if targets.get(key) is not None
    ]
    parts.extend(
        _format_lift_target(lift, entry)
        for lift, entry in (targets.get("strength") or {}).items()
    )
    return " · ".join(parts)


def render_training_plan(summary: dict, weeks: list[dict]) -> None:
    """plan_summary's dict + group_by_week's rows. All grouping and counting
    happens there; this only prints."""
    console.print(f"[bold]{summary['label']}[/bold] — {summary['description']}")

    days = summary["days_to_go"]
    when = (
        f"in {days} days" if days > 0 else "today" if days == 0 else f"{-days} days ago"
    )
    length = f"{summary['weeks']} weeks"
    if summary.get("test_week"):
        length = f"week 0 + {length}"
    console.print(
        f"Event {summary['event_date']} ({when}) · "
        f"{length} from {summary['start_date']}"
    )
    console.print(
        f"{summary['completed']}/{summary['sessions'] - summary['extras']} sessions "
        f"done · {summary['scheduled']} on the Garmin calendar · "
        f"{summary['extras']} extras (tracked locally)"
    )
    targets = _format_targets(summary.get("targets", {}))
    if targets:
        console.print(f"[dim]Targets: {targets}[/dim]")
    volume = summary.get("volume") or {}
    if volume.get("why"):
        scale = round(volume.get("start_scale", 1) * 100)
        console.print(f"[dim]Starting volume: {scale}% — {volume['why']}[/dim]")
    # Not `weeks`: that is the parameter the table below iterates, and
    # shadowing it here made this raise on every plan with a re-test.
    if summary.get("test_week"):
        console.print(
            "[dim]Week 0 is a test week: every target below was derived from "
            "your history, so do the tests and `fit garmin-sync` — the plan "
            "re-derives from them automatically.[/dim]"
        )
    benchmark_weeks = summary.get("benchmark_weeks") or []
    if benchmark_weeks:
        console.print(
            f"[dim]Re-test weeks: {', '.join(str(w) for w in benchmark_weeks)} — "
            "do the test and `fit garmin-sync`; the plan re-derives from it "
            "automatically.[/dim]"
        )
    if summary.get("stale"):
        console.print(
            f"[dim]{summary['stale']} session(s) marked * were pushed at an "
            "earlier target and can't be updated — Garmin has no edit endpoint. "
            "`fit train clear` removes them so they re-push at your current "
            "fitness.[/dim]"
        )
    for warning in summary.get("warnings", []):
        console.print(f"[yellow]note:[/yellow] {warning}")

    table = Table(title="Training Plan")
    table.add_column("Week")
    table.add_column("Date")
    table.add_column("Session")
    table.add_column("Garmin")
    table.add_column("Done", justify="center")

    for week in weeks:
        table.add_section()
        for index, session in enumerate(week["sessions"]):
            when_label = "" if index else f"{week['week']} [dim]{week['phase']}[/dim]"
            day = date.fromisoformat(session["date"])
            name = _format_session_name(session)
            table.add_row(
                when_label,
                f"{day.strftime('%a')} {session['date'][5:]}",
                name,
                _format_session_garmin(session),
                _format_session_done(session),
            )
    console.print(table)


def _format_session_name(session: dict) -> str:
    text = session.get("description", session.get("workout_name", ""))
    return f"[dim]{text}[/dim]" if session.get("is_extra") else text


def _format_session_garmin(session: dict) -> str:
    """A pushed session is a frozen copy on the account; "stale" means the live
    derivation has moved on from what the watch holds."""
    if session.get("is_extra"):
        return "[dim]—[/dim]"
    if session.get("stale"):
        return "[yellow]on watch*[/yellow]"
    if session.get("pushed"):
        return "[green]on watch[/green]"
    return "[dim]planned[/dim]"


def _format_session_done(session: dict) -> str:
    if session.get("is_extra"):
        return "[dim]—[/dim]"
    return "[green]✓[/green]" if session.get("completed") else "[dim]·[/dim]"


def render_training_sync_preview(sessions: list[dict]) -> None:
    """What sync is about to push, printed before it asks to go ahead."""
    console.print(
        f"[bold]About to push {len(sessions)} workout(s) to Garmin Connect[/bold] "
        f"({sessions[0]['date']} to {sessions[-1]['date']}):"
    )
    for session in sessions:
        console.print(f"  {session['date']}  {session.get('workout_name', '')}")


def render_training_synced(summary: dict) -> None:
    """{scheduled, already, window_days, failed}."""
    console.print(
        f"Scheduled {summary['scheduled']} session(s) on the Garmin calendar "
        f"for the next {summary['window_days']} days "
        f"({summary['already']} already scheduled)"
    )
    for failure in summary.get("failed", []):
        console.print(f"warning: {failure}")
    if summary["scheduled"]:
        console.print(
            "[dim]They will appear in the Garmin Connect calendar on the "
            "watch's next sync.[/dim]"
        )


def render_training_cleared(summary: dict) -> None:
    console.print(
        f"Unscheduled {summary['cleared']} future session(s) from the Garmin calendar"
    )
    for failure in summary.get("failed", []):
        console.print(f"warning: {failure}")


def render_training_missing() -> None:
    console.print(
        "No active training plan. Create one with:\n" "  fit train import <plan.yaml>"
    )
