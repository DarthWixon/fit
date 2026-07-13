"""Pure functions over activity dicts. No file I/O, no side effects.

Every activity field access uses .get() since older activities may lack
fields added later (e.g. avg_heart_rate, elevation_gain_m).
"""

import calendar
import re
import statistics
from datetime import date, timedelta

MILESTONES_KM = {
    "run": [(5.0, "5k"), (10.0, "10k"), (21.1, "half"), (42.2, "marathon")],
    "cycle": [(20.0, "20k"), (50.0, "50k"), (100.0, "100k")],
    "walk": [(5.0, "5k"), (10.0, "10k")],
    "hike": [],
    "swim": [(0.5, "500m"), (1.0, "1k"), (1.5, "1.5k"), (2.0, "2k")],
}

# An activity within [D, D*MILESTONE_TOLERANCE] counts as a "D-distance effort".
# This is the "dedicated effort" PB: a whole activity whose total distance was
# approximately D. Kept separate from SPLIT_DISTANCES_KM below, which finds the
# fastest D-distance segment hidden inside any activity, however long.
MILESTONE_TOLERANCE = 1.06

# Distances-of-interest for "best split" extraction: the fastest continuous
# segment of this length found anywhere within an activity's track, regardless
# of the activity's total distance (e.g. the fastest 5k inside a 10k run).
# Only activities imported from GPX/TCX/FIT carry the per-point stream needed
# to compute this (see importers.py) — CSV-only activities never contribute
# here.
SPLIT_DISTANCES_KM = {
    "run": [(5.0, "5k"), (10.0, "10k")],
    "cycle": [
        (25.0, "25k"),
        (50.0, "50k"),
        (75.0, "75k"),
        (100.0, "100k"),
        (160.0, "160k"),
    ],
    "swim": [(0.5, "500m"), (1.0, "1k"), (1.5, "1.5k"), (2.0, "2k")],
}

# Coarse, Compendium-of-Physical-Activities-style approximate MET values for
# the fitness index (see "Fitness index" in CLAUDE.md) — not pinned to a
# specific citation, just directionally consistent with commonly-published
# MET-vs-speed tables. run/walk: banded by pace in seconds/km, ascending
# threshold (smaller = faster, matched first). swim: banded by seconds/100m,
# same shape. cycle: banded by speed in km/h, descending threshold (larger =
# faster, matched first) to match calc_pace's existing km/h convention for
# cycle. hike and squash are single flat values, not banded by pace/speed —
# hike because trail pace is a poor intensity signal given terrain/elevation
# variance; squash because it has no meaningful distance/pace at all (an
# indoor court sport with no GPS track — see NO_DISTANCE_TYPES below). Any
# activity_type whose MET_TABLE value is a plain int/float rather than a list
# of bands is treated as flat by met_for_activity. squash's 12.0 approximates
# the Compendium of Physical Activities' "squash, general" figure — one of
# the highest commonly-cited MET values among recreational sports, matching
# its reputation as an explosive, high-intensity, stop-start court sport.
MET_TABLE = {
    "run": [(240, 13.0), (300, 11.0), (390, 9.4), (float("inf"), 7.5)],
    "walk": [(560, 5.4), (650, 4.3), (float("inf"), 3.3)],
    "swim": [(105, 10.0), (135, 8.3), (float("inf"), 5.8)],
    "cycle": [(32, 14.0), (23, 9.0), (16, 6.8), (0, 4.0)],
    "hike": 6.0,
    "squash": 12.0,
}

# Used when duration_seconds is present but distance_km is 0/missing (e.g. an
# indoor trainer ride, a pool session logged by time only) — there's no
# pace/speed to band on, so fall back to each type's "moderate" MET as a
# neutral guess. The avg_heart_rate multiplier (activity_load, below) is what
# actually differentiates a hard session from an easy one in this case.
_MET_FALLBACK_ZERO_DISTANCE = {
    "run": 9.4,
    "walk": 4.3,
    "swim": 8.3,
    "cycle": 6.8,
    "hike": 6.0,
}
_MET_DEFAULT_UNKNOWN_TYPE = 6.0

# One anomalous avg_heart_rate reading can't move a single day's load by more
# than +/-25% (0.8 and 1.25 are reciprocal, i.e. symmetric in log-space).
_HR_MULTIPLIER_MIN = 0.8
_HR_MULTIPLIER_MAX = 1.25

_EWMA_WINDOW_DAYS = 42  # Coggan's CTL/"Fitness" smoothing window

# Activity types whose distance_km, even when nonzero, doesn't represent real
# movement worth ranking or pacing on -- e.g. squash's FIT export reports a
# tiny nonzero total_distance (~0.07km) that's accelerometer noise from
# movement on an indoor court with no GPS track, not a meaningful travel
# distance. calc_pace and _candidate_pbs (_longest_distance_pb specifically)
# both check this set so a pace string / "longest distance" PB is never
# fabricated from that noise. Extend for any future indoor/court-sport type
# with the same characteristic.
NO_DISTANCE_TYPES = {"squash"}


def _seconds_per_km(distance_km: float, duration_seconds: float) -> float:
    return duration_seconds / distance_km


def _seconds_per_100m(distance_km: float, duration_seconds: float) -> float:
    return duration_seconds / (distance_km * 10)


def _speed_kmh(distance_km: float, duration_seconds: float) -> float:
    return distance_km / (duration_seconds / 3600)


def calc_pace(
    distance_km: float, duration_seconds: int, activity_type: str = ""
) -> str:
    if not distance_km or activity_type in NO_DISTANCE_TYPES:
        return "—"
    if activity_type == "swim":
        minutes, seconds = divmod(
            round(_seconds_per_100m(distance_km, duration_seconds)), 60
        )
        return f"{minutes}:{seconds:02d}/100m"
    if activity_type == "cycle":
        if not duration_seconds:
            return "—"
        return f"{_speed_kmh(distance_km, duration_seconds):.1f}km/h"
    minutes, seconds = divmod(round(_seconds_per_km(distance_km, duration_seconds)), 60)
    return f"{minutes}:{seconds:02d}/km"


def filter_by_type(activities: list[dict], type: str) -> list[dict]:
    return [a for a in activities if a.get("type") == type]


def filter_by_date(activities: list[dict], start: str, end: str) -> list[dict]:
    return [a for a in activities if start <= a.get("date", "") <= end]


def filter_by_types(activities: list[dict], types: list[str]) -> list[dict]:
    """types: e.g. ["run", "cycle"]. Empty/falsy -> no filtering."""
    if not types:
        return activities
    return [a for a in activities if a.get("type") in types]


def stats_date_range(window: str | None, reference: date) -> tuple[str, str] | None:
    """window: 'week' | 'month' | 'year' | None. Returns (start_iso, end_iso)
    inclusive, ending at reference, or None if window is None (no filtering)."""
    if window is None:
        return None
    if window == "week":
        start = reference - timedelta(days=reference.weekday())
    elif window == "month":
        start = reference.replace(day=1)
    else:
        start = reference.replace(month=1, day=1)
    return start.isoformat(), reference.isoformat()


def months_ago(reference: date, n: int) -> str:
    """ISO date n calendar months before reference, day clamped to the target
    month's last valid day (e.g. Mar 31 minus 1 month -> Feb 28/29, not a
    ValueError)."""
    total_months = reference.year * 12 + (reference.month - 1) - n
    year, month = divmod(total_months, 12)
    month += 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


_TIMERANGE_PATTERN = re.compile(r"^(\d+)([dwmy])$")


def parse_timerange(text: str, reference: date) -> tuple[str, str]:
    """Rolling window ending at reference (inclusive). text: e.g. '10d', '2w',
    '3m', '1y' (days/weeks/months/years). Unlike stats_date_range (calendar-
    aligned to the start of the current week/month/year), this always counts
    back exactly N units from reference. 'm' and 'y' delegate to months_ago for
    its day-of-month clamping (year = 12 months, for free). Raises ValueError
    on malformed input (bad unit, non-numeric/zero/negative N, or a window so
    large it overflows date's supported range)."""
    match = _TIMERANGE_PATTERN.match(text.strip().lower())
    if not match:
        raise ValueError(
            f"invalid --timerange '{text}': expected a positive whole number "
            "followed by d (days), w (weeks), m (months), or y (years), "
            "e.g. '10d', '2w', '3m', '1y'"
        )
    n, unit = int(match.group(1)), match.group(2)
    if n == 0:
        raise ValueError(
            f"invalid --timerange '{text}': number must be greater than zero"
        )

    try:
        if unit == "d":
            start = reference - timedelta(days=n)
        elif unit == "w":
            start = reference - timedelta(weeks=n)
        elif unit == "m":
            start = date.fromisoformat(months_ago(reference, n))
        else:
            start = date.fromisoformat(months_ago(reference, n * 12))
    except OverflowError:
        raise ValueError(f"invalid --timerange '{text}': window is too large")

    return start.isoformat(), reference.isoformat()


def _iso_week_key(date_iso: str) -> str:
    """'2026-W24'-style bucket key for an ISO date."""
    year, week, _ = date.fromisoformat(date_iso).isocalendar()
    return f"{year}-W{week:02d}"


def _week_start(date_iso: str) -> date:
    """Monday of the ISO week containing date_iso."""
    day = date.fromisoformat(date_iso)
    return day - timedelta(days=day.weekday())


def _empty_week(key: str) -> dict:
    return {"week": key, "distance_km": 0.0, "duration_seconds": 0, "count": 0}


def is_current_week(week_key: str, reference: date) -> bool:
    """True if week_key ('2026-W28') is the ISO week containing reference — i.e.
    that week is still filling up, so its volume is not yet comparable to the
    complete weeks beside it."""
    return week_key == _iso_week_key(reference.isoformat())


def weekly_volumes(activities: list[dict], through: date | None = None) -> list[dict]:
    """One bucket per ISO week, oldest first, spanning every calendar week from
    the first activity's through the last — weeks with no activity zero-fill
    rather than collapsing, so the series reads as a timeline (a rest week is a
    trough, not a missing bar). `through`, when given, extends the series to the
    week containing that date, so the current week always appears even before
    anything is logged in it.

    Volume is measured in time: duration_seconds is what the sparklines plot.
    distance_km rides along as a secondary figure."""
    weeks: dict[str, dict] = {}
    dates = []
    for activity in activities:
        activity_date = activity.get("date")
        if not activity_date:
            continue
        dates.append(activity_date)
        key = _iso_week_key(activity_date)
        bucket = weeks.setdefault(key, _empty_week(key))
        bucket["distance_km"] += activity.get("distance_km", 0) or 0
        bucket["duration_seconds"] += activity.get("duration_seconds", 0) or 0
        bucket["count"] += 1

    if not dates:
        return []

    monday = _week_start(min(dates))
    last_monday = _week_start(max(dates))
    if through is not None:
        last_monday = max(last_monday, _week_start(through.isoformat()))

    series = []
    while monday <= last_monday:
        key = _iso_week_key(monday.isoformat())
        series.append(weeks.get(key, _empty_week(key)))
        monday += timedelta(days=7)
    return series


def summarize_by_type(activities: list[dict]) -> list[dict]:
    """One row per distinct activity type present, sorted alphabetically.
    Returns [{"type": ..., "count": ..., "duration_seconds": ..., "distance_km": ...}, ...].
    """
    types = sorted({a.get("type", "unknown") for a in activities})
    summary = []
    for activity_type in types:
        type_activities = filter_by_type(activities, activity_type)
        distance = sum(a.get("distance_km", 0) or 0 for a in type_activities)
        duration = sum(a.get("duration_seconds", 0) or 0 for a in type_activities)
        summary.append(
            {
                "type": activity_type,
                "count": len(type_activities),
                "duration_seconds": duration,
                "distance_km": distance,
            }
        )
    return summary


def activity_calendar(
    activities: list[dict], reference: date, months: int = 2
) -> list[dict]:
    """Month grids for the `months` calendar months ending with reference's,
    oldest first. Returns [{"label": "June 2026", "weeks": [[0, 1, ...], ...],
    "active_days": [3, 5, 12]}, ...] — weeks are Monday-first rows from
    calendar.monthdayscalendar (0 = padding cell outside the month), and
    active_days lists the days-of-month with at least one activity. Grid
    layout lives here so display.py stays computation-free."""
    active_by_month: dict[tuple[int, int], set[int]] = {}
    for activity in activities:
        activity_date = activity.get("date")
        if not activity_date:
            continue
        parsed = date.fromisoformat(activity_date)
        active_by_month.setdefault((parsed.year, parsed.month), set()).add(parsed.day)

    grids = []
    total_months = reference.year * 12 + (reference.month - 1)
    for offset in range(months - 1, -1, -1):
        year, month = divmod(total_months - offset, 12)
        month += 1
        grids.append(
            {
                "label": f"{calendar.month_name[month]} {year}",
                "weeks": calendar.monthcalendar(year, month),
                "active_days": sorted(active_by_month.get((year, month), set())),
            }
        )
    return grids


def _dedupe_by_time(points: list[dict]) -> list[dict]:
    """Collapse consecutive samples sharing an elapsed_seconds value, keeping
    the last one."""
    deduped = []
    for point in points:
        if deduped and deduped[-1]["elapsed_seconds"] == point["elapsed_seconds"]:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped


def _crossing_time(points: list[dict], j: int, needed_distance: float) -> float:
    """Elapsed time at which the track first reaches needed_distance, linearly
    interpolated between samples j-1 and j (which bracket the crossing)."""
    d_before, d_after = points[j - 1]["distance_km"], points[j]["distance_km"]
    t_before, t_after = points[j - 1]["elapsed_seconds"], points[j]["elapsed_seconds"]
    if d_after <= d_before:
        return t_after
    frac = (needed_distance - d_before) / (d_after - d_before)
    return t_before + frac * (t_after - t_before)


def fastest_split(points: list[dict], target_distance_km: float) -> dict | None:
    """Fastest continuous segment of target_distance_km within a single activity's
    track. points: [{"elapsed_seconds": float, "distance_km": float}, ...], with
    distance_km cumulative and non-decreasing. Returns {"duration_seconds": float}
    or None if the track never covers target_distance_km.

    O(n) two-pointer sliding window: cumulative distance is monotonic, so as the
    start index advances the required end index only ever moves forward too. Only
    raw sample points are considered valid window starts (no start-interpolation) —
    a standard simplification at realistic sampling rates that can only make the
    reported time equal-or-slower than reality, never faster.
    """
    deduped = _dedupe_by_time(points)
    n = len(deduped)
    if n < 2:
        return None
    if deduped[-1]["distance_km"] - deduped[0]["distance_km"] < target_distance_km:
        return None

    best_duration = None
    j = 0
    for i in range(n):
        j = max(j, i)
        while (
            j < n
            and deduped[j]["distance_km"] - deduped[i]["distance_km"]
            < target_distance_km
        ):
            j += 1
        if j >= n:
            break

        crossing = _crossing_time(
            deduped, j, deduped[i]["distance_km"] + target_distance_km
        )
        duration = crossing - deduped[i]["elapsed_seconds"]
        if best_duration is None or duration < best_duration:
            best_duration = duration

    return (
        {"duration_seconds": round(best_duration, 1)}
        if best_duration is not None
        else None
    )


def _longest_distance_pb(activities: list[dict]) -> dict:
    with_distance = [a for a in activities if a.get("distance_km") is not None]
    if not with_distance:
        return {}
    longest = max(with_distance, key=lambda a: a["distance_km"])
    return {
        "longest_distance_km": longest["distance_km"],
        "longest_distance_date": longest.get("date"),
    }


def _milestone_pbs(activities: list[dict], activity_type: str) -> dict:
    result = {}
    for milestone_km, label in MILESTONES_KM.get(activity_type, []):
        matching = [
            a
            for a in activities
            if a.get("distance_km") is not None
            and a.get("duration_seconds") is not None
            and milestone_km <= a["distance_km"] <= milestone_km * MILESTONE_TOLERANCE
        ]
        if matching:
            fastest = min(matching, key=lambda a: a["duration_seconds"])
            result[f"fastest_{label}_seconds"] = fastest["duration_seconds"]
            result[f"fastest_{label}_date"] = fastest.get("date")
    return result


def _split_pbs(activities: list[dict], activity_type: str) -> dict:
    result = {}
    for target_km, label in SPLIT_DISTANCES_KM.get(activity_type, []):
        key = f"{label}_seconds"
        candidates = [a for a in activities if a.get("splits", {}).get(key) is not None]
        if candidates:
            best = min(candidates, key=lambda a: a["splits"][key])
            result[f"fastest_{label}_split_seconds"] = best["splits"][key]
            result[f"fastest_{label}_split_date"] = best.get("date")
    return result


def _elevation_pb(activities: list[dict]) -> dict:
    with_elevation = [a for a in activities if a.get("elevation_gain_m") is not None]
    if not with_elevation:
        return {}
    most_climb = max(with_elevation, key=lambda a: a["elevation_gain_m"])
    return {
        "most_elevation_gain_m": most_climb["elevation_gain_m"],
        "most_elevation_gain_date": most_climb.get("date"),
    }


def _candidate_pbs(activities: list[dict], activity_type: str) -> dict:
    """Compute the best-of values for one type across a set of activities."""
    result: dict = {}
    if activity_type not in NO_DISTANCE_TYPES:
        result.update(_longest_distance_pb(activities))
    result.update(_milestone_pbs(activities, activity_type))
    result.update(_split_pbs(activities, activity_type))
    result.update(_elevation_pb(activities))
    return result


def best_pb_per_label(type_pbs: dict) -> dict:
    """Collapses dedicated (fastest_{label}_seconds) and split
    (fastest_{label}_split_seconds) PBs sharing the same distance label down
    to whichever is faster, for display purposes only -- pbs.json keeps both
    stored independently (see "Split PBs" in CLAUDE.md) so detect_new_pbs can
    still track each category. Non-time keys (longest_distance_km,
    most_elevation_gain_m) pass through unchanged."""
    labels: dict[str, dict] = {}
    passthrough: dict = {}

    for key, value in type_pbs.items():
        if key.endswith("_date"):
            continue
        if key.startswith("fastest_") and key.endswith("_split_seconds"):
            label = key[len("fastest_") : -len("_split_seconds")]
            date_key = f"fastest_{label}_split_date"
            labels.setdefault(label, {})["split"] = (value, type_pbs.get(date_key))
        elif key.startswith("fastest_") and key.endswith("_seconds"):
            label = key[len("fastest_") : -len("_seconds")]
            date_key = f"fastest_{label}_date"
            labels.setdefault(label, {})["milestone"] = (value, type_pbs.get(date_key))
        else:
            date_key = _date_key_for_passthrough(key)
            passthrough[key] = value
            if date_key and date_key in type_pbs:
                passthrough[date_key] = type_pbs[date_key]

    result = dict(passthrough)
    for label, candidates in labels.items():
        value, date = min(candidates.values(), key=lambda vd: vd[0])
        result[f"fastest_{label}_seconds"] = value
        result[f"fastest_{label}_date"] = date
    return result


def _date_key_for_passthrough(key: str) -> str | None:
    for suffix in ("_km", "_m"):
        if key.endswith(suffix):
            return key[: -len(suffix)] + "_date"
    return None


def all_personal_bests(activities: list[dict]) -> dict:
    by_type: dict[str, list[dict]] = {}
    for activity in activities:
        by_type.setdefault(activity.get("type", "unknown"), []).append(activity)

    return {
        activity_type: _candidate_pbs(type_activities, activity_type)
        for activity_type, type_activities in by_type.items()
    }


_HIGHER_IS_BETTER_KEYS = {"longest_distance_km", "most_elevation_gain_m"}


def detect_new_pbs(new_activity: dict, current_pbs: dict) -> list[dict]:
    """Returns [{"key": ..., "value": ...}, ...] for each PB category
    new_activity broke, compared against current_pbs — no message formatting;
    see display.render_new_pb_messages for turning these into readable text."""
    activity_type = new_activity.get("type", "unknown")
    candidate = _candidate_pbs([new_activity], activity_type)
    existing = current_pbs.get(activity_type, {})

    broken = []
    for key, value in candidate.items():
        if key.endswith("_date"):
            continue
        if key in _HIGHER_IS_BETTER_KEYS:
            is_new_best = value > existing.get(key, 0)
        else:
            current_best = existing.get(key)
            is_new_best = current_best is None or value < current_best
        if is_new_best:
            broken.append({"key": key, "value": value})
    return broken


def pbs_cache_is_valid(pbs: dict, activity_count: int) -> bool:
    return pbs.get("computed_from") == activity_count


def _met_from_pace_bands(
    value_seconds: float, bands: list[tuple[float, float]]
) -> float:
    """bands sorted ascending by threshold; first band where value_seconds <=
    threshold wins (smaller pace-seconds = faster = higher-MET band)."""
    for threshold, met in bands:
        if value_seconds <= threshold:
            return met
    return bands[-1][1]


def _met_from_speed_bands(speed_kmh: float, bands: list[tuple[float, float]]) -> float:
    """bands sorted descending by threshold; first band where speed_kmh >=
    threshold wins."""
    for threshold, met in bands:
        if speed_kmh >= threshold:
            return met
    return bands[-1][1]


def met_for_activity(activity: dict) -> float:
    """Coarse MET value for one activity, banded by pace/speed via MET_TABLE
    -- except types whose MET_TABLE entry is a flat int/float (hike, squash)
    rather than a list of (threshold, met) bands, which are returned as-is."""
    activity_type = activity.get("type", "")
    if activity_type not in MET_TABLE:
        return _MET_DEFAULT_UNKNOWN_TYPE
    met_value = MET_TABLE[activity_type]
    if isinstance(met_value, (int, float)):
        return met_value

    distance_km = activity.get("distance_km") or 0
    duration_seconds = activity.get("duration_seconds") or 0
    if not distance_km or not duration_seconds:
        return _MET_FALLBACK_ZERO_DISTANCE[activity_type]

    if activity_type == "cycle":
        return _met_from_speed_bands(
            _speed_kmh(distance_km, duration_seconds), MET_TABLE["cycle"]
        )
    if activity_type == "swim":
        return _met_from_pace_bands(
            _seconds_per_100m(distance_km, duration_seconds), MET_TABLE["swim"]
        )
    return _met_from_pace_bands(
        _seconds_per_km(distance_km, duration_seconds), MET_TABLE[activity_type]
    )


def median_hr_by_type(activities: list[dict]) -> dict[str, float]:
    """Median avg_heart_rate across all HR-tagged activities, grouped by type.
    Only activities with avg_heart_rate present contribute (GPX/TCX/FIT
    imports only — bare Strava CSV rows never set this
    field). Inclusive of whichever activity is later being scored against it:
    for the very first HR-tagged activity of a type, its own median (of a
    1-element list) is itself, so activity_load's multiplier trivially
    resolves to 1.0 for it — the "no peers yet" case falls out for free."""
    by_type: dict[str, list[float]] = {}
    for activity in activities:
        hr = activity.get("avg_heart_rate")
        if hr is None:
            continue
        by_type.setdefault(activity.get("type", "unknown"), []).append(hr)
    return {t: statistics.median(hrs) for t, hrs in by_type.items()}


def activity_load(activity: dict, median_hr: dict[str, float]) -> float:
    """MET-hours base (met_for_activity(activity) * duration_hours), scaled by
    a clamped avg_heart_rate/median_hr[type] ratio when avg_heart_rate is
    present and median_hr has a peer group for this type. median_hr is a
    frozen snapshot the caller computes once (see median_hr_by_type)."""
    met = met_for_activity(activity)
    duration_hours = (activity.get("duration_seconds") or 0) / 3600
    base_load = met * duration_hours

    avg_hr = activity.get("avg_heart_rate")
    peer_median = median_hr.get(activity.get("type", "unknown"))
    if avg_hr is None or not peer_median:
        return base_load

    multiplier = avg_hr / peer_median
    multiplier = max(_HR_MULTIPLIER_MIN, min(_HR_MULTIPLIER_MAX, multiplier))
    return base_load * multiplier


def daily_load_totals(activities: list[dict]) -> dict[str, float]:
    """{date_iso: summed activity_load()} across all activities on that date —
    same-day multiple activities simply add. Activities with no "date" field
    are skipped (mirrors weekly_volumes's same skip)."""
    median_hr = median_hr_by_type(activities)
    totals: dict[str, float] = {}
    for activity in activities:
        activity_date = activity.get("date")
        if not activity_date:
            continue
        totals[activity_date] = totals.get(activity_date, 0.0) + activity_load(
            activity, median_hr
        )
    return totals


def fitness_ewma_daily(activities: list[dict], as_of: date) -> list[dict]:
    """Dense [{"date": ..., "value": ...}, ...] series for every calendar day
    from the first-ever activity's date through as_of inclusive, zero-filling
    load on days without activity (this is what lets rest/off-seasons pull
    the EWMA down via decay). Seeded as value[first_day] = load[first_day]
    (rather than 0) to avoid a several-week ramp-up artifact for someone
    whose app history starts mid-career. value[t] = value[t-1] +
    (load[t] - value[t-1]) / 42 thereafter (Coggan's CTL formula). Returns
    [] if there's no data on/before as_of."""
    totals = daily_load_totals(activities)
    if not totals:
        return []
    first_day = date.fromisoformat(min(totals))
    if first_day > as_of:
        return []

    series = []
    current, value = first_day, None
    while current <= as_of:
        load_today = totals.get(current.isoformat(), 0.0)
        value = (
            load_today
            if value is None
            else value + (load_today - value) / _EWMA_WINDOW_DAYS
        )
        series.append({"date": current.isoformat(), "value": value})
        current += timedelta(days=1)
    return series


def compute_baseline_value(activities: list[dict], as_of: date) -> float | None:
    """Raw (unscaled) EWMA value as of as_of — the single source of truth used
    both to establish the initial fitness.json baseline (lazy-init) and to
    re-anchor it (fit fitness-reset). None if there's no data yet."""
    series = fitness_ewma_daily(activities, as_of)
    return series[-1]["value"] if series else None


def rescale_to_index(raw_series: list[dict], baseline_value: float) -> list[dict]:
    """[{"date": ..., "index": 100 * value / baseline_value}, ...]."""
    return [
        {"date": row["date"], "index": 100 * row["value"] / baseline_value}
        for row in raw_series
    ]


def filter_series_by_date(series: list[dict], start: str, end: str) -> list[dict]:
    """Like filter_by_date, but for a [{"date": ..., ...}, ...] series rather
    than activity dicts — used to window an already-computed index series for
    display, without re-running the EWMA over a truncated activity list
    (which would wrongly discard the pre-window decay/carry-over)."""
    return [row for row in series if start <= row["date"] <= end]


def weekly_fitness_index(index_series: list[dict]) -> list[dict]:
    """Resamples a daily [{"date": ..., "index": ...}, ...] series to one
    point per ISO week — the week's last value (an EWMA is an already-smoothed
    level, not an additive quantity, so weekly_volumes's sum-based bucketing
    doesn't apply here; we want each week's closing value, like a stock
    index chart). Returns [{"week": "2026-W24", "index": 103.4}, ...] sorted
    by week — same key shape/sort convention as weekly_volumes."""
    weeks: dict[str, dict] = {}
    for row in index_series:
        key = _iso_week_key(row["date"])
        weeks[key] = {"week": key, "index": row["index"]}
    return [weeks[key] for key in sorted(weeks)]
