"""Parses TCX, FIT and Strava exports into fit's activity dict shape.

stdlib ElementTree for TCX (no lxml); fitparse for FIT. Never dedupes and
never imports storage — cli._import_and_report owns on-disk state.
"""

import csv
import gzip
import math
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path

from fit import compute

# TCX's Sport is only Running/Biking/Other — no Swimming. Everything else
# falls through to "run" below: a known mislabelling, not worth working around.
TCX_SPORT_MAP = {
    "running": "run",
    "biking": "cycle",
}

FIT_SPORT_MAP = {
    "running": "run",
    "cycling": "cycle",
    "walking": "walk",
    "hiking": "hike",
    "swimming": "swim",
    # No canoe-specific FIT sport. Folded to "canoe" for this canoe-only setup.
    "paddling": "canoe",
    "kayaking": "canoe",
    "canoeing": "canoe",
}

# fitparse's profile doesn't decode every sport enum (it jumps 48 -> 254), so
# codes added in between arrive as bare ints. Anything unlisted defaults to
# "run", as an unrecognised string sport already does.
FIT_SPORT_CODE_MAP = {
    64: "squash",
    19: "canoe",
    41: "canoe",
}

# A gym session is sport 10 ("training") + sub_sport 20. Keyed on the sub_sport
# because sport 10 alone also covers cardio. The raw int is accepted too, so a
# profile that stops decoding it can't reclassify a gym session as a run.
FIT_STRENGTH_SUB_SPORTS = {"strength_training", 20}

# fitparse decodes a *scalar* `category` but not array elements — and a real
# watch array-encodes it, so every lift arrives as [28] rather than ["squat"].
# This is the FIT exercise_category enum (0-32), kept explicit rather than
# reaching into fitparse.profile. 65534 ("not classified") is left out on
# purpose, so an unclassified set reads as unclassified.
FIT_EXERCISE_CATEGORY_MAP = {
    0: "bench_press",
    1: "calf_raise",
    2: "cardio",
    3: "carry",
    4: "chop",
    5: "core",
    6: "crunch",
    7: "curl",
    8: "deadlift",
    9: "flye",
    10: "hip_raise",
    11: "hip_stability",
    12: "hip_swing",
    13: "hyperextension",
    14: "lateral_raise",
    15: "leg_curl",
    16: "leg_raise",
    17: "lunge",
    18: "olympic_lift",
    19: "plank",
    20: "plyo",
    21: "pull_up",
    22: "push_up",
    23: "row",
    24: "shoulder_press",
    25: "shoulder_stability",
    26: "shrug",
    27: "sit_up",
    28: "squat",
    29: "total_body",
    30: "triceps_extension",
    31: "warm_up",
    32: "run",
}

# `category_subtype` (front vs back squat) is ignored: PBs key on the category,
# so both share a line. Adding it would change what PBs are keyed on.

# No "squash" entry: no fixture confirms Strava's raw type string, and guessing
# risks mismapping real data. Unmapped types drop with a warning.
STRAVA_TYPE_MAP = {
    "run": "run",
    "ride": "cycle",
    "walk": "walk",
    "hike": "hike",
    "swim": "swim",
    # Kayaking/Rowing/SUP are left unmapped: canoe-only setup.
    "canoeing": "canoe",
}

DISTANCE_COLUMNS = ["Distance"]
DURATION_COLUMNS = ["Elapsed Time", "Moving Time"]
TYPE_COLUMNS = ["Activity Type"]
DATE_COLUMNS = ["Activity Date"]
FILENAME_COLUMNS = ["Filename"]


# --- namespace-tolerant XML helpers ------------------------------------------------
# Some exporters emit no namespace, so every lookup tries "{uri}tag" then "tag".


def _parse_xml_root(path: str):
    """Root element, tolerating the leading whitespace Strava's TCX exports
    carry (ElementTree otherwise rejects them). Bytes, so the file's own
    encoding declaration stays authoritative."""
    with open(path, "rb") as f:
        return ET.fromstring(f.read().lstrip())


def _namespace(root_tag: str) -> str:
    return root_tag.split("}")[0].strip("{") if root_tag.startswith("{") else ""


def _find(elem, tag, ns_uri):
    if ns_uri:
        found = elem.find(f"{{{ns_uri}}}{tag}")
        if found is not None:
            return found
    return elem.find(tag)


def _findall(elem, tag, ns_uri):
    if ns_uri:
        found = elem.findall(f"{{{ns_uri}}}{tag}")
        if found:
            return found
    return elem.findall(tag)


def _find_path(elem, path, ns_uri):
    current = elem
    for tag in path.split("/"):
        current = _find(current, tag, ns_uri)
        if current is None:
            return None
    return current


def _parse_iso_time(text: str) -> datetime:
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    earth_radius_km = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


def _compute_splits(activity_type: str, points: list[dict]) -> dict:
    """Split times from the transient point stream; {} if no distance reached.

    Points without distance are dropped here only — an indoor ride's records
    carry power and HR that the other consumers still need."""
    located = [p for p in points if p.get("distance_km") is not None]
    splits = {}
    for target_km, label in compute.SPLIT_DISTANCES_KM.get(activity_type, []):
        result = compute.fastest_split(located, target_km)
        if result is not None:
            splits[f"{label}_seconds"] = result["duration_seconds"]
    return splits


def _base_activity(
    start_time,
    activity_type: str,
    distance_km: float,
    duration_seconds: float,
    source: str,
) -> dict:
    """The five always-present fields. Optional ones are each importer's own
    concern — their presence conditions differ by format."""
    return {
        "id": start_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": activity_type,
        "date": start_time.strftime("%Y-%m-%d"),
        "distance_km": round(distance_km, 2),
        "duration_seconds": round(duration_seconds),
        "source": source,
    }


def _attach_splits(activity: dict, points: list[dict]) -> dict:
    """Attach splits; the key is omitted entirely, never left empty."""
    splits = _compute_splits(activity["type"], points)
    if splits:
        activity["splits"] = splits
    return activity


def _attach_best_power(activity: dict, points: list[dict]) -> dict:
    """Attach the power-duration curve; key omitted when there is none. Stored
    because the point stream is discarded at import and cannot be recovered."""
    best = {}
    for window_seconds, label in compute.POWER_WINDOWS_S:
        watts = compute.best_power_window(points, window_seconds)
        if watts is not None:
            best[label] = watts
    if best:
        activity["best_power"] = best
    return activity


def _attach_hr_zones(activity: dict, points: list[dict], max_heart_rate: int) -> dict:
    """Attach HR zone-seconds; key omitted when max_heart_rate is unset or the
    file carries no per-point hr."""
    hr_zones = compute.hr_zone_seconds(points, max_heart_rate)
    if hr_zones:
        activity["hr_zones"] = hr_zones
    return activity


def _tcx_start_time(activity_elem, laps, ns_uri):
    """<Id>, else the first lap's StartTime, else None."""
    id_elem = _find(activity_elem, "Id", ns_uri)
    if id_elem is not None:
        return _parse_iso_time(id_elem.text)
    for lap in laps:
        start_time_attr = lap.get("StartTime")
        if start_time_attr:
            return _parse_iso_time(start_time_attr)
    return None


def _tcx_lap_totals(lap, ns_uri) -> tuple[float, float]:
    """(distance_m, time_s) for one lap; 0.0 if absent."""
    dist_elem = _find(lap, "DistanceMeters", ns_uri)
    time_elem = _find(lap, "TotalTimeSeconds", ns_uri)
    distance_m = float(dist_elem.text) if dist_elem is not None else 0.0
    time_s = float(time_elem.text) if time_elem is not None else 0.0
    return distance_m, time_s


def _tcx_lap_heart_rate(lap, ns_uri) -> tuple[float | None, float | None]:
    avg_hr_elem = _find_path(lap, "AverageHeartRateBpm/Value", ns_uri)
    max_hr_elem = _find_path(lap, "MaximumHeartRateBpm/Value", ns_uri)
    avg_hr = float(avg_hr_elem.text) if avg_hr_elem is not None else None
    max_hr = float(max_hr_elem.text) if max_hr_elem is not None else None
    return avg_hr, max_hr


def _tcx_lap_power(lap, ns_uri) -> float | None:
    """Lap watts from <Extensions><LX><AvgWatts>. That block sits in a different
    namespace, so it is matched by tag suffix rather than by ns_uri."""
    extensions = _find(lap, "Extensions", ns_uri)
    if extensions is None:
        return None
    for lx in extensions:
        if not lx.tag.endswith("LX"):
            continue
        for child in lx:
            if child.tag.endswith("AvgWatts"):
                return float(child.text)
    return None


def _tcx_trackpoints(lap, ns_uri):
    track_elem = _find(lap, "Track", ns_uri)
    container = track_elem if track_elem is not None else lap
    return _findall(container, "Trackpoint", ns_uri)


def _tcx_trackpoint_elevation(
    trackpoint, ns_uri, prev_ele: float | None
) -> tuple[float, float | None]:
    """(gain_delta, new_prev_ele); prev_ele passes through if no altitude."""
    alt_elem = _find(trackpoint, "AltitudeMeters", ns_uri)
    if alt_elem is None:
        return 0.0, prev_ele
    ele = float(alt_elem.text)
    gain = ele - prev_ele if prev_ele is not None and ele > prev_ele else 0.0
    return gain, ele


def _tcx_trackpoint_position(trackpoint, ns_uri):
    """(lat, lon, time), else None. Trackpoint DistanceMeters is cumulative in
    some exporters and lap-relative in others with no way to tell, so the split
    stream comes from GPS position via haversine instead."""
    position_elem = _find(trackpoint, "Position", ns_uri)
    time_elem = _find(trackpoint, "Time", ns_uri)
    if position_elem is None or time_elem is None:
        return None
    lat_elem = _find(position_elem, "LatitudeDegrees", ns_uri)
    lon_elem = _find(position_elem, "LongitudeDegrees", ns_uri)
    if lat_elem is None or lon_elem is None:
        return None
    return float(lat_elem.text), float(lon_elem.text), _parse_iso_time(time_elem.text)


def _tcx_trackpoint_heart_rate(trackpoint, ns_uri) -> int | None:
    """Per-trackpoint HR, or None. HR zones need a per-sample series, which the
    lap-level average (_tcx_lap_heart_rate) can't give."""
    hr_elem = _find_path(trackpoint, "HeartRateBpm/Value", ns_uri)
    return round(float(hr_elem.text)) if hr_elem is not None else None


def _tcx_totals(laps, ns_uri) -> tuple[float, float]:
    """(distance_m, time_s) over all laps."""
    total_distance_m = 0.0
    total_time_s = 0.0
    for lap in laps:
        lap_distance_m, lap_time_s = _tcx_lap_totals(lap, ns_uri)
        total_distance_m += lap_distance_m
        total_time_s += lap_time_s
    return total_distance_m, total_time_s


def _tcx_heart_rate_stats(laps, ns_uri) -> tuple[int | None, int | None]:
    """(mean of lap averages, max of lap maxima); None if absent."""
    hr_avgs = []
    hr_maxes = []
    for lap in laps:
        avg_hr, max_hr = _tcx_lap_heart_rate(lap, ns_uri)
        if avg_hr is not None:
            hr_avgs.append(avg_hr)
        if max_hr is not None:
            hr_maxes.append(max_hr)
    avg = round(sum(hr_avgs) / len(hr_avgs)) if hr_avgs else None
    peak = round(max(hr_maxes)) if hr_maxes else None
    return avg, peak


def _tcx_power_avg(laps, ns_uri) -> int | None:
    """Mean of lap AvgWatts, or None."""
    power_avgs = [
        p for p in (_tcx_lap_power(lap, ns_uri) for lap in laps) if p is not None
    ]
    return round(sum(power_avgs) / len(power_avgs)) if power_avgs else None


def _tcx_elevation_gain_m(laps, ns_uri) -> float:
    """Summed positive altitude deltas, reset at each lap boundary."""
    gain = 0.0
    for lap in laps:
        prev_ele = None
        for trackpoint in _tcx_trackpoints(lap, ns_uri):
            delta, prev_ele = _tcx_trackpoint_elevation(trackpoint, ns_uri, prev_ele)
            gain += delta
    return gain


def _tcx_point_stream(laps, ns_uri, start_time) -> list[dict]:
    """Cumulative (elapsed_seconds, distance_km, hr) stream from GPS via
    haversine, carried across laps."""
    points = []
    distance_km = 0.0
    prev_point = None
    for lap in laps:
        for trackpoint in _tcx_trackpoints(lap, ns_uri):
            position = _tcx_trackpoint_position(trackpoint, ns_uri)
            if position is None:
                continue
            lat, lon, trackpoint_time = position
            if prev_point is not None:
                distance_km += _haversine_km(prev_point[0], prev_point[1], lat, lon)
            prev_point = (lat, lon)
            points.append(
                {
                    "elapsed_seconds": (trackpoint_time - start_time).total_seconds(),
                    "distance_km": distance_km,
                    "hr": _tcx_trackpoint_heart_rate(trackpoint, ns_uri),
                }
            )
    return points


def import_tcx(path: str, max_heart_rate: int = 0) -> dict:
    root = _parse_xml_root(path)
    ns_uri = _namespace(root.tag)

    activity_elem = _find_path(root, "Activities/Activity", ns_uri)
    if activity_elem is None:
        raise ValueError(f"no Activity element found in {path}")

    sport = activity_elem.get("Sport", "")
    activity_type = TCX_SPORT_MAP.get(sport.lower(), "run")

    laps = _findall(activity_elem, "Lap", ns_uri)
    start_time = _tcx_start_time(activity_elem, laps, ns_uri)
    if start_time is None:
        raise ValueError(f"no start time found in {path}")

    total_distance_m, total_time_s = _tcx_totals(laps, ns_uri)
    avg_hr, max_hr = _tcx_heart_rate_stats(laps, ns_uri)
    avg_power = _tcx_power_avg(laps, ns_uri)
    elevation_gain_m = _tcx_elevation_gain_m(laps, ns_uri)
    points = _tcx_point_stream(laps, ns_uri, start_time)

    activity = _base_activity(
        start_time, activity_type, total_distance_m / 1000, total_time_s, "garmin"
    )
    if elevation_gain_m:
        activity["elevation_gain_m"] = round(elevation_gain_m)
    if avg_hr is not None:
        activity["avg_heart_rate"] = avg_hr
    if max_hr is not None:
        activity["max_heart_rate"] = max_hr
    if avg_power is not None:
        activity["avg_power"] = avg_power
    activity = _attach_splits(activity, points)
    return _attach_hr_zones(activity, points, max_heart_rate)


# --- FIT ------------------------------------------------------------------------------


def _fit_exercise_name(category) -> str:
    """Exercise name from a `set` message's category: unwrap the array a real
    watch writes, resolve via FIT_EXERCISE_CATEGORY_MAP, take a scalar string
    as-is. An unmapped code becomes "unknown_<int>" — kept, so the session's
    volume survives, and distinct, so two unmapped lifts don't merge."""
    if isinstance(category, (list, tuple)):
        category = category[0] if category else None
    if isinstance(category, str):
        return category.lower()
    if isinstance(category, int):
        return FIT_EXERCISE_CATEGORY_MAP.get(category, f"unknown_{category}")
    return "unknown"


def _append_set(exercises: list[dict], name: str, one_set: dict) -> None:
    """Append one set, grouping with the previous entry if same exercise. The
    single home of that rule, shared by both builders of an exercises list."""
    if exercises and exercises[-1]["name"] == name:
        exercises[-1]["sets"].append(one_set)
    else:
        exercises.append({"name": name, "sets": [one_set]})


def _parse_fit_sets(fit_file) -> list[dict]:
    """The `exercises` list, from the FIT `set` messages.

    Rest sets are dropped; consecutive sets of one exercise group together.
    A set with neither reps nor weight is skipped. fitparse scales weight to kg."""
    exercises: list[dict] = []
    for message in fit_file.get_messages("set"):
        fields = {field.name: field.value for field in message}

        set_type = fields.get("set_type")
        if set_type not in ("active", 1):
            continue

        reps = fields.get("repetitions")
        weight = fields.get("weight")
        if reps is None and weight is None:
            continue

        one_set: dict = {}
        if reps is not None:
            one_set["reps"] = int(reps)
        if weight is not None:
            one_set["weight_kg"] = round(float(weight), 2)

        _append_set(exercises, _fit_exercise_name(fields.get("category")), one_set)
    return exercises


def apply_garmin_exercise_sets(activity: dict, exercise_sets: list[dict]) -> dict:
    """Correct exercise names from Garmin's server-side record.

    The FIT fit downloads is the *original* upload, never rewritten, so a
    Connect-app correction lives only on the server. Neither source is
    complete, so each contributes what it holds: names from Garmin, weights
    from the FIT (the server returns null for many). A Garmin weight wins when
    present, and arrives in grams.

    Sets pair positionally, active only. A differing count or a disagreeing rep
    count abandons the merge — a misaligned pairing would rename sets to
    whatever sat at that index, which is worse than the wrong name."""
    flat = [
        (exercise["name"], one_set)
        for exercise in activity.get("exercises", [])
        for one_set in exercise["sets"]
    ]
    active = [s for s in exercise_sets if s.get("setType") == "ACTIVE"]
    if not flat or len(active) != len(flat):
        return activity

    merged: list[dict] = []
    for (fit_name, fit_set), entry in zip(flat, active):
        reps = entry.get("repetitionCount")
        if (
            reps is not None
            and fit_set.get("reps") is not None
            and int(reps) != fit_set["reps"]
        ):
            return activity

        one_set = dict(fit_set)
        grams = entry.get("weight")
        if grams is not None:
            one_set["weight_kg"] = round(float(grams) / 1000, 2)
        _append_set(merged, _garmin_exercise_name(entry) or fit_name, one_set)

    activity["exercises"] = merged
    return activity


def _garmin_exercise_name(entry: dict) -> str:
    """Name from one Garmin entry, or "". `category` is the uppercased form of
    the same vocabulary, so lowercasing is the whole conversion; the
    sub-category `name` is ignored, as FIT's `category_subtype` is."""
    exercises = entry.get("exercises") or []
    category = exercises[0].get("category") if exercises else None
    return category.lower() if isinstance(category, str) else ""


def import_fit(path: str, max_heart_rate: int = 0) -> dict:
    from fitparse import FitFile

    fit_file = FitFile(path)

    session = next(fit_file.get_messages("session"), None)
    if session is None:
        raise ValueError(f"no session summary message found in {path}")

    fields = {field.name: field.value for field in session}

    start_time = fields.get("start_time")
    if start_time is None:
        raise ValueError(f"no start_time found in {path}")

    raw_sport = fields.get("sport")
    if fields.get("sub_sport") in FIT_STRENGTH_SUB_SPORTS:
        # Before the sport map: a gym session's sport is "training", which that
        # map doesn't carry and would default to "run".
        activity_type = "strength"
    elif isinstance(raw_sport, str):
        activity_type = FIT_SPORT_MAP.get(raw_sport.lower(), "run")
    else:
        # An undecoded enum arrives as a raw int (or None if absent).
        activity_type = FIT_SPORT_CODE_MAP.get(raw_sport, "run")

    distance_m = fields.get("total_distance") or 0.0
    duration_s = (
        fields.get("total_timer_time") or fields.get("total_elapsed_time") or 0.0
    )

    activity = _base_activity(
        start_time, activity_type, distance_m / 1000, duration_s, "garmin"
    )
    if fields.get("total_ascent") is not None:
        activity["elevation_gain_m"] = round(fields["total_ascent"])
    if fields.get("avg_heart_rate") is not None:
        activity["avg_heart_rate"] = round(fields["avg_heart_rate"])
    if fields.get("max_heart_rate") is not None:
        activity["max_heart_rate"] = round(fields["max_heart_rate"])
    if fields.get("avg_power") is not None:
        activity["avg_power"] = round(fields["avg_power"])

    points = []
    for record in fit_file.get_messages("record"):
        record_fields = {field.name: field.value for field in record}
        record_distance_m = record_fields.get("distance")
        record_timestamp = record_fields.get("timestamp")
        record_power = record_fields.get("power")
        # Distance is not required: an indoor ride carries power and HR with
        # none, and a gym session carries only HR. Dropping either would lose
        # the FTP signal / leave hr_zones permanently empty. Each consumer
        # filters for what it needs.
        if record_timestamp is None or (
            record_distance_m is None
            and record_power is None
            and record_fields.get("heart_rate") is None
        ):
            continue
        points.append(
            {
                "elapsed_seconds": (record_timestamp - start_time).total_seconds(),
                "distance_km": (
                    None if record_distance_m is None else record_distance_m / 1000
                ),
                "hr": record_fields.get("heart_rate"),
                "power": record_power,
            }
        )

    if activity_type == "strength":
        # No splits or power curve — no distance stream to sweep. HR zones still
        # apply. distance_km stays as reported; NO_DISTANCE_TYPES hides it.
        activity["exercises"] = _parse_fit_sets(fit_file)
        return _attach_hr_zones(activity, points, max_heart_rate)

    activity = _attach_splits(activity, points)
    activity = _attach_best_power(activity, points)
    return _attach_hr_zones(activity, points, max_heart_rate)


# --- Strava CSV / bulk export -----------------------------------------------------------


def _get_first(row: dict, columns: list[str]):
    for col in columns:
        if row.get(col):
            return row[col]
    return None


def _parse_strava_date(text: str) -> datetime:
    text = text.strip()
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognized Strava date format: {text!r}")


def _parse_strava_row(row: dict) -> tuple[dict | None, str | None]:
    """(base activity, None), or (None, skip_label) naming why the row can't be
    imported — the raw type, or "(no date)". Exactly one side is populated.
    Resolves no linked file."""
    raw_type = _get_first(row, TYPE_COLUMNS)
    activity_type = STRAVA_TYPE_MAP.get((raw_type or "").lower())
    if activity_type is None:
        return None, raw_type or "(no type)"

    raw_date = _get_first(row, DATE_COLUMNS)
    if not raw_date:
        return None, "(no date)"
    start_time = _parse_strava_date(raw_date)

    distance_m = float(_get_first(row, DISTANCE_COLUMNS) or 0)
    duration_s = float(_get_first(row, DURATION_COLUMNS) or 0)

    return (
        _base_activity(
            start_time, activity_type, distance_m / 1000, duration_s, "strava"
        ),
        None,
    )


def _strava_skip_warnings(skipped: Counter) -> list[str]:
    """One summary line per skipped-row tally, e.g. "skipped 100 Strava rows
    fit can't import: Workout x99, Surfing x1". [] when nothing was skipped."""
    if not skipped:
        return []
    parts = ", ".join(f"{label} x{n}" for label, n in skipped.most_common())
    return [f"skipped {sum(skipped.values())} Strava rows fit can't import: {parts}"]


def import_strava_csv(path: str) -> tuple[list[dict], list[str]]:
    activities, skipped = [], Counter()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            parsed, skip_label = _parse_strava_row(row)
            if parsed is None:
                skipped[skip_label] += 1
                continue
            activities.append(parsed)
    return activities, _strava_skip_warnings(skipped)


def import_by_extension(path: str, suffix: str, max_heart_rate: int = 0) -> dict:
    if suffix == ".tcx":
        return import_tcx(path, max_heart_rate)
    if suffix == ".fit":
        return import_fit(path, max_heart_rate)
    raise ValueError(f"unsupported activity file format: {suffix}")


def _import_strava_linked_file(file_path: Path, max_heart_rate: int = 0) -> dict:
    if file_path.suffix.lower() == ".gz":
        inner_suffix = file_path.with_suffix("").suffix.lower()
        with tempfile.NamedTemporaryFile(suffix=inner_suffix, delete=False) as tmp:
            with gzip.open(file_path, "rb") as gz:
                shutil.copyfileobj(gz, tmp)
            tmp_path = tmp.name
        try:
            return import_by_extension(tmp_path, inner_suffix, max_heart_rate)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    return import_by_extension(str(file_path), file_path.suffix.lower(), max_heart_rate)


def import_directory(dir_path: str, max_heart_rate: int = 0) -> list[dict]:
    """A loose folder of .tcx/.fit files (a mounted watch), not a Strava bulk
    export — cli.py tells them apart by activities.csv."""
    activities = []
    for file_path in sorted(Path(dir_path).iterdir()):
        suffix = file_path.suffix.lower()
        if suffix not in (".tcx", ".fit"):
            continue
        activities.append(import_by_extension(str(file_path), suffix, max_heart_rate))
    return activities


def import_strava_export(
    export_dir: str, max_heart_rate: int = 0
) -> tuple[list[dict], list[str]]:
    export_path = Path(export_dir)
    csv_path = export_path / "activities.csv"
    if not csv_path.exists():
        raise ValueError(
            f"{export_dir} does not look like a Strava export (no activities.csv)"
        )

    activities = []
    warnings: list[str] = []
    skipped: Counter = Counter()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            base, skip_label = _parse_strava_row(row)
            if base is None:
                skipped[skip_label] += 1
                continue

            filename = _get_first(row, FILENAME_COLUMNS)
            if filename:
                try:
                    activity = _import_strava_linked_file(
                        export_path / filename, max_heart_rate
                    )
                except Exception as exc:
                    # Fall back to the CSV row: it already has date, type,
                    # distance and duration, so an unreadable file costs only
                    # the track-derived extras, not the whole session.
                    warnings.append(
                        f"{filename} could not be read ({exc}) — imported from the "
                        "CSV row instead, without splits or HR zones"
                    )
                    activity = dict(base)
                else:
                    activity["id"] = base["id"]
                    activity["type"] = base["type"]
                    activity["date"] = base["date"]
                    activity["source"] = base["source"]
            else:
                activity = dict(base)

            activities.append(activity)

    warnings.extend(_strava_skip_warnings(skipped))
    return activities, warnings
