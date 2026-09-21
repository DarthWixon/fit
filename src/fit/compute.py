"""Pure functions over activity dicts. No I/O, no side effects.

Always .get() activity fields: older activities lack later additions.
"""

import calendar
import re
from datetime import date, timedelta

MILESTONES_KM = {
    "run": [(5.0, "5k"), (10.0, "10k"), (21.1, "half"), (42.2, "marathon")],
    "cycle": [(20.0, "20k"), (50.0, "50k"), (100.0, "100k")],
    "walk": [(5.0, "5k"), (10.0, "10k")],
    "hike": [],
    "swim": [(0.5, "500m"), (1.0, "1k"), (1.5, "1.5k"), (2.0, "2k")],
    "canoe": [(5.0, "5k"), (10.0, "10k")],
}

# "Dedicated effort" PB: a whole activity of [D, D*TOLERANCE]. Distinct from
# SPLIT_DISTANCES_KM, which finds a D-segment hidden inside any activity.
MILESTONE_TOLERANCE = 1.06

# Time-axis sibling of SPLIT_DISTANCES_KM. Only 20min has a consumer
# (planner.derive_ride_watts, as the FTP proxy); the rest come free.
POWER_WINDOWS_S = [(60, "1min"), (300, "5min"), (1200, "20min")]

# Fastest continuous segment of this length anywhere in a track (the 5k inside
# a 10k). Needs a point stream, so TCX/FIT only — never bare Strava CSV.
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
    "canoe": [(1.0, "1k"), (5.0, "5k")],
}

# distance_km is present but never meaningful: squash reports ~0.07km of
# accelerometer noise, strength reports 0.0. Checked by calc_pace and
# _longest_distance_pb so neither fabricates a figure from it.
NO_DISTANCE_TYPES = {"squash", "strength"}

# Read as km/h rather than min/km — the natural sense of progress for each.
SPEED_TYPES = {"cycle", "hike", "canoe"}

# Lower bounds as a fraction of max HR, Z1..Z5. Below the first still counts
# as Z1 — there is no zone 0.
HR_ZONE_BOUNDARIES = [0.5, 0.6, 0.7, 0.8, 0.9]


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
    if activity_type in SPEED_TYPES:
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
    """(start, end) inclusive ending at reference; None means no filtering."""
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
    """n calendar months back, day clamped to the month's last valid day."""
    total_months = reference.year * 12 + (reference.month - 1) - n
    year, month = divmod(total_months, 12)
    month += 1
    day = min(reference.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


_TIMERANGE_PATTERN = re.compile(r"^(\d+)([dwmy])$")


def parse_timerange(text: str, reference: date) -> tuple[str, str]:
    """'10d'/'2w'/'3m'/'1y' -> (start, end) rolling window ending at reference.
    Unlike stats_date_range's calendar-aligned windows this counts back exactly
    N units. Raises ValueError on a bad unit/count or an overflowing window."""
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
    """'2026-W24' bucket key."""
    year, week, _ = date.fromisoformat(date_iso).isocalendar()
    return f"{year}-W{week:02d}"


def week_start(date_iso: str) -> date:
    """Monday of date_iso's ISO week. Public: training.py lays a plan's weekly
    cadence out from it."""
    day = date.fromisoformat(date_iso)
    return day - timedelta(days=day.weekday())


def _empty_week(key: str) -> dict:
    return {"week": key, "distance_km": 0.0, "duration_seconds": 0, "count": 0}


def is_current_week(week_key: str, reference: date) -> bool:
    """Is week_key still filling up? Its volume is not yet comparable."""
    return week_key == _iso_week_key(reference.isoformat())


def weekly_volumes(activities: list[dict], through: date | None = None) -> list[dict]:
    """One bucket per ISO week, oldest first. Empty weeks zero-fill rather than
    collapse, so a rest week is a trough and [-n:] really is the last n weeks.
    `through` extends to that date's week, so the current week always appears.

    Volume is time: duration_seconds is what the sparklines plot."""
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

    monday = week_start(min(dates))
    last_monday = week_start(max(dates))
    if through is not None:
        last_monday = max(last_monday, week_start(through.isoformat()))

    series = []
    while monday <= last_monday:
        key = _iso_week_key(monday.isoformat())
        series.append(weeks.get(key, _empty_week(key)))
        monday += timedelta(days=7)
    return series


def summarize_by_type(activities: list[dict]) -> list[dict]:
    """One row per type present, alphabetical: {type, count, duration_seconds,
    distance_km}."""
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
    """[{label, weeks, active_days}, ...] for the `months` months ending with
    reference's, oldest first. weeks are Monday-first rows, 0 = padding. Grid
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
    """Collapse samples sharing an elapsed_seconds, keeping the last."""
    deduped = []
    for point in points:
        if deduped and deduped[-1]["elapsed_seconds"] == point["elapsed_seconds"]:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped


def _crossing_time(points: list[dict], j: int, needed_distance: float) -> float:
    """Interpolated time at which the track reaches needed_distance."""
    d_before, d_after = points[j - 1]["distance_km"], points[j]["distance_km"]
    t_before, t_after = points[j - 1]["elapsed_seconds"], points[j]["elapsed_seconds"]
    if d_after <= d_before:
        return t_after
    frac = (needed_distance - d_before) / (d_after - d_before)
    return t_before + frac * (t_after - t_before)


def best_power_window(points: list[dict], window_seconds: int) -> int | None:
    """Highest average power over any window of that length, or None.

    Integrates over elapsed time, not per sample: FIT records are not reliably
    one per second, so sparse stretches must not weigh as much as dense ones.
    Recovers a 20-min effort that the whole-activity avg_power hides."""
    stream = [
        p
        for p in _dedupe_by_time(points)
        if p.get("power") is not None and p.get("elapsed_seconds") is not None
    ]
    if len(stream) < 2:
        return None
    if stream[-1]["elapsed_seconds"] - stream[0]["elapsed_seconds"] < window_seconds:
        return None

    best = None
    area = 0.0
    start = 0
    for end in range(1, len(stream)):
        # Each sample's power is held until the next one.
        area += stream[end - 1]["power"] * (
            stream[end]["elapsed_seconds"] - stream[end - 1]["elapsed_seconds"]
        )
        while (
            stream[end]["elapsed_seconds"] - stream[start]["elapsed_seconds"]
            > window_seconds
        ):
            area -= stream[start]["power"] * (
                stream[start + 1]["elapsed_seconds"] - stream[start]["elapsed_seconds"]
            )
            start += 1
        span = stream[end]["elapsed_seconds"] - stream[start]["elapsed_seconds"]
        # A gap in recording can leave the window short even at the far end;
        # only score it once it genuinely spans the requested time.
        if span >= window_seconds:
            average = area / span
            best = average if best is None else max(best, average)
    return round(best) if best is not None else None


def fastest_split(points: list[dict], target_distance_km: float) -> dict | None:
    """Fastest continuous segment of that distance, or None if never reached.
    points carry cumulative, non-decreasing distance_km.

    O(n) two-pointer sweep. Window starts are raw samples only (no
    start-interpolation), which can only report equal-or-slower, never faster."""
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


def _hr_zone_index(hr: float, max_heart_rate: int) -> int:
    """0-based zone index, clamped. Boundaries are lower bounds, so the count
    met is already 1-based; subtract 1 and floor at 0."""
    ratio = hr / max_heart_rate
    met = sum(1 for boundary in HR_ZONE_BOUNDARIES if ratio >= boundary)
    return max(met - 1, 0)


def hr_zone_seconds(points: list[dict], max_heart_rate: int) -> dict:
    """Seconds per HR zone. Each gap is attributed to the earlier sample's zone
    (no interpolation). {} if max_heart_rate <= 0 or fewer than 2 points have
    hr — a single sample has no duration."""
    if max_heart_rate <= 0:
        return {}
    if len([p for p in points if p.get("hr") is not None]) < 2:
        return {}

    totals = [0.0] * 5
    for i in range(len(points) - 1):
        hr = points[i].get("hr")
        if hr is None:
            continue
        dt = points[i + 1]["elapsed_seconds"] - points[i]["elapsed_seconds"]
        if dt > 0:
            totals[_hr_zone_index(hr, max_heart_rate)] += dt

    if sum(totals) == 0:
        return {}
    return {
        f"zone{i + 1}_seconds": round(seconds, 1) for i, seconds in enumerate(totals)
    }


def hr_zone_percentages(hr_zones: dict | None) -> dict | None:
    """hr_zones seconds -> {"zone1".."zone5"} percentages, or None."""
    if not hr_zones:
        return None
    total = sum(hr_zones.get(f"zone{i}_seconds", 0) for i in range(1, 6))
    if total <= 0:
        return None
    return {
        f"zone{i}": 100 * hr_zones.get(f"zone{i}_seconds", 0) / total
        for i in range(1, 6)
    }


def _longest_distance_pb(activities: list[dict]) -> dict:
    with_distance = [a for a in activities if a.get("distance_km") is not None]
    if not with_distance:
        return {}
    longest = max(with_distance, key=lambda a: a.get("distance_km"))
    return {
        "longest_distance_km": longest.get("distance_km"),
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
            and milestone_km
            <= a.get("distance_km")
            <= milestone_km * MILESTONE_TOLERANCE
        ]
        if matching:
            fastest = min(matching, key=lambda a: a.get("duration_seconds"))
            result[f"fastest_{label}_seconds"] = fastest.get("duration_seconds")
            result[f"fastest_{label}_date"] = fastest.get("date")
    return result


def _split_pbs(activities: list[dict], activity_type: str) -> dict:
    result = {}
    for target_km, label in SPLIT_DISTANCES_KM.get(activity_type, []):
        key = f"{label}_seconds"
        candidates = [a for a in activities if a.get("splits", {}).get(key) is not None]
        if candidates:
            best = min(candidates, key=lambda a: a.get("splits", {}).get(key))
            result[f"fastest_{label}_split_seconds"] = best.get("splits", {}).get(key)
            result[f"fastest_{label}_split_date"] = best.get("date")
    return result


def _elevation_pb(activities: list[dict]) -> dict:
    with_elevation = [a for a in activities if a.get("elevation_gain_m") is not None]
    if not with_elevation:
        return {}
    most_climb = max(with_elevation, key=lambda a: a.get("elevation_gain_m"))
    return {
        "most_elevation_gain_m": most_climb.get("elevation_gain_m"),
        "most_elevation_gain_date": most_climb.get("date"),
    }


def estimated_1rm(weight_kg: float, reps: int) -> float:
    """Epley e1RM for one set, to 0.1kg. The single source of truth, so a PB
    and a plan target can't disagree.

    A single rep returns itself, not Epley's 3%-above extrapolation."""
    if weight_kg <= 0 or reps <= 0:
        return 0.0
    if reps == 1:
        return round(float(weight_kg), 1)
    return round(weight_kg * (1 + reps / 30), 1)


def total_weight_lifted(activity: dict) -> float:
    """Tonnage: reps x load, summed. What distance is for a run — and unlike
    the heaviest set it moves with volume as well as load."""
    total = 0.0
    for exercise in activity.get("exercises", []) or []:
        for one_set in exercise.get("sets", []) or []:
            weight = one_set.get("weight_kg") or 0
            reps = one_set.get("reps") or 0
            if weight > 0 and reps > 0:
                total += weight * reps
    return total


def _strength_pbs(activities: list[dict]) -> dict:
    """{exercise: {heaviest_set_kg, heaviest_set_date, best_e1rm_kg,
    best_e1rm_date}}. Keyed by exercise, not distance label — which is why this
    sits outside _candidate_pbs.

    The two metrics are independent: a heavy triple and a set of ten are
    different achievements. Unweighted sets count for neither; ties keep the
    first seen."""
    result: dict[str, dict] = {}
    for activity in activities:
        activity_date = activity.get("date")
        for exercise in activity.get("exercises", []) or []:
            name = exercise.get("name")
            if not name:
                continue
            best = result.setdefault(name, {})
            for one_set in exercise.get("sets", []) or []:
                weight = one_set.get("weight_kg") or 0
                reps = one_set.get("reps") or 0
                if weight <= 0 or reps <= 0:
                    continue
                if weight > best.get("heaviest_set_kg", 0):
                    best["heaviest_set_kg"] = weight
                    best["heaviest_set_date"] = activity_date
                e1rm = estimated_1rm(weight, reps)
                if e1rm > best.get("best_e1rm_kg", 0):
                    best["best_e1rm_kg"] = e1rm
                    best["best_e1rm_date"] = activity_date
    # An exercise whose every set was unweighted leaves an empty dict behind.
    return {name: best for name, best in result.items() if best}


def _strength_new_pbs(candidate_pbs: dict, existing: dict) -> list[dict]:
    """detect_new_pbs' strength path. Flattens the nested per-exercise shape to
    "{exercise}_heaviest_set_kg" keys so entries match every other category;
    pbs.json itself stays nested."""
    broken = []
    for name, candidate in candidate_pbs.items():
        current = existing.get(name, {})
        for metric in ("heaviest_set_kg", "best_e1rm_kg"):
            value = candidate.get(metric)
            if value is not None and value > current.get(metric, 0):
                broken.append({"key": f"{name}_{metric}", "value": value})
    return broken


def _candidate_pbs(activities: list[dict], activity_type: str) -> dict:
    """Best-of values for one type."""
    result: dict = {}
    if activity_type not in NO_DISTANCE_TYPES:
        result.update(_longest_distance_pb(activities))
    result.update(_milestone_pbs(activities, activity_type))
    result.update(_split_pbs(activities, activity_type))
    result.update(_elevation_pb(activities))
    return result


def best_pb_per_label(type_pbs: dict) -> dict:
    """Collapse a label's dedicated and split PBs to whichever is faster, for
    display only — pbs.json keeps both so detect_new_pbs can track each.
    Non-time keys pass through."""
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
        value, best_date = min(candidates.values(), key=lambda vd: vd[0])
        result[f"fastest_{label}_seconds"] = value
        result[f"fastest_{label}_date"] = best_date
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
        activity_type: (
            _strength_pbs(type_activities)
            if activity_type == "strength"
            else _candidate_pbs(type_activities, activity_type)
        )
        for activity_type, type_activities in by_type.items()
    }


_HIGHER_IS_BETTER_KEYS = {"longest_distance_km", "most_elevation_gain_m"}


def detect_new_pbs(new_activities: list[dict], current_pbs: dict) -> list[dict]:
    """[{"key", "value"}] per PB category broken. Formatting is
    display.render_new_pb_messages'.

    The whole import is one batch, measured against current_pbs once: per
    activity, nine paddles of a new type announced nine "longest distance" PBs
    in file order rather than one at the batch's best."""
    broken = []
    for activity_type, candidate in all_personal_bests(new_activities).items():
        existing = current_pbs.get(activity_type, {})
        if activity_type == "strength":
            broken.extend(_strength_new_pbs(candidate, existing))
            continue
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
