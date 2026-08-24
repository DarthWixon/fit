"""Parses external activity formats (GPX, TCX, FIT, Strava exports) into the
standard activity dict shape (see storage.py's module docstring).

Uses stdlib xml.etree.ElementTree for GPX/TCX (no lxml). FIT is a binary format
parsed via the fitparse library.
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

GPXTPX_NS = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"

GPX_TYPE_MAP = {
    "running": "run",
    "run": "run",
    "cycling": "cycle",
    "biking": "cycle",
    "ride": "cycle",
    "walking": "walk",
    "walk": "walk",
    "hiking": "hike",
    "hike": "hike",
    "swimming": "swim",
    "swim": "swim",
    "canoeing": "canoe",
    "canoe": "canoe",
    "kayaking": "canoe",
    "paddling": "canoe",
}

# Per the Garmin TCX schema, Sport is only ever Running/Biking/Other — there's no
# native Swimming value. Anything outside Running/Biking (walk, hike, swim) falls
# through to the "run" default below, which is a known mislabeling, not something
# worth working around here.
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
    # No canoe-specific FIT sport exists; paddling activities decode (when they
    # decode to a string at all) as one of these. Folded to "canoe" because this
    # is a canoe-only setup -- revisit if kayaking is later split out.
    "paddling": "canoe",
    "kayaking": "canoe",
    "canoeing": "canoe",
}

# Some FIT sport enum codes aren't decoded to a string name by fitparse==1.2.0's
# bundled profile -- e.g. Garmin's enum jumps straight from 48 ("floor_climbing")
# to 254 ("all"), so codes Garmin added in between (64 = squash) come back from
# fields.get("sport") as a bare, undecoded int rather than a string. import_fit
# checks this table when the sport field isn't a string. Any int not listed here
# (now, or from a future firmware/profile version) falls through to the same
# "run" default an unrecognized string sport already gets -- see import_fit.
# There is no canoe-specific FIT sport code: Garmin records paddling as 19
# (paddling) or 41 (kayaking), both folded to "canoe" here since this is a
# canoe-only setup (revisit the 41 mapping if kayaking is later split out).
FIT_SPORT_CODE_MAP = {
    64: "squash",
    19: "canoe",
    41: "canoe",
}

# Deliberately no "squash" entry: no Strava CSV/bulk-export fixture exists to
# confirm the exact raw_type string Strava uses for squash (if any), and
# guessing risks silently mismapping real data. An unmapped raw_type is
# dropped -- _parse_strava_row returns (None, raw_type) and the row is skipped
# in import_strava_csv/import_strava_export -- but the drop is now reported: a
# per-type skipped-row summary is surfaced via warnings, not silent. Any other
# currently-unmapped Strava activity type gets the same treatment, not
# squash-specific.
STRAVA_TYPE_MAP = {
    "run": "run",
    "ride": "cycle",
    "walk": "walk",
    "hike": "hike",
    "swim": "swim",
    # Strava's "Canoeing" type (lowercased by _parse_strava_row). Kayaking,
    # Rowing and Stand Up Paddling are deliberately left unmapped -- they
    # drop-with-warning, matching this setup's canoe-only scope.
    "canoeing": "canoe",
}

DISTANCE_COLUMNS = ["Distance"]
DURATION_COLUMNS = ["Elapsed Time", "Moving Time"]
TYPE_COLUMNS = ["Activity Type"]
DATE_COLUMNS = ["Activity Date"]
FILENAME_COLUMNS = ["Filename"]


# --- namespace-tolerant XML helpers ------------------------------------------------
# GPX/TCX are namespaced XML, so ElementTree tags come back as "{uri}localname".
# Some exporters (older Garmin, some phone apps) emit no namespace at all, so
# every lookup tries the namespaced tag first and falls back to the bare tag.


def _parse_xml_root(path: str):
    """Root element of a GPX/TCX file, tolerating leading whitespace before
    the <?xml?> declaration. Strava's exported TCX files are padded with
    spaces, which ElementTree otherwise rejects ('XML or text declaration not
    at start of entity'). Reading raw bytes and lstrip-ing keeps the file's
    own encoding declaration authoritative."""
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


def _gpx_heart_rate(trkpt, ns_uri):
    extensions = _find(trkpt, "extensions", ns_uri)
    if extensions is None:
        return None
    tpx = None
    for child in extensions:
        if child.tag.endswith("TrackPointExtension"):
            tpx = child
            break
    if tpx is None:
        return None
    for child in tpx:
        if child.tag.endswith("hr"):
            return int(child.text)
    return None


def _compute_splits(activity_type: str, points: list[dict]) -> dict:
    """Best-effort split times from a transient (elapsed_seconds, distance_km)
    point stream. Returns {} if none of the type's configured distances were
    reached. Pure — the caller decides whether/how to attach the result."""
    splits = {}
    for target_km, label in compute.SPLIT_DISTANCES_KM.get(activity_type, []):
        result = compute.fastest_split(points, target_km)
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
    """The five always-present activity fields (full shape in storage.py's
    module docstring). Optional fields stay each importer's own concern —
    their presence conditions deliberately differ by format."""
    return {
        "id": start_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": activity_type,
        "date": start_time.strftime("%Y-%m-%d"),
        "distance_km": round(distance_km, 2),
        "duration_seconds": round(duration_seconds),
        "source": source,
    }


def _attach_splits(activity: dict, points: list[dict]) -> dict:
    """Attach best-effort splits from the transient point stream; the key is
    omitted entirely (never an empty dict) when no split distance was reached."""
    splits = _compute_splits(activity["type"], points)
    if splits:
        activity["splits"] = splits
    return activity


def _attach_hr_zones(activity: dict, points: list[dict], max_heart_rate: int) -> dict:
    """Attach HR zone-seconds from the transient point stream; the key is
    omitted entirely (never an empty dict) when zones can't be computed (no
    max_heart_rate configured, or no per-point hr data in this format/file)."""
    hr_zones = compute.hr_zone_seconds(points, max_heart_rate)
    if hr_zones:
        activity["hr_zones"] = hr_zones
    return activity


# --- GPX ----------------------------------------------------------------------------


def _gpx_raw_points(trkpts, ns_uri) -> list[dict]:
    """One dict per trkpt: {"lat", "lon", "ele" (float | None), "time"
    (datetime | None), "hr" (int | None)} — a single XML pass that every other
    _gpx_* helper below then derives its metric from."""
    raw_points = []
    for trkpt in trkpts:
        ele_elem = _find(trkpt, "ele", ns_uri)
        time_elem = _find(trkpt, "time", ns_uri)
        raw_points.append(
            {
                "lat": float(trkpt.get("lat")),
                "lon": float(trkpt.get("lon")),
                "ele": float(ele_elem.text) if ele_elem is not None else None,
                "time": (
                    _parse_iso_time(time_elem.text) if time_elem is not None else None
                ),
                "hr": _gpx_heart_rate(trkpt, ns_uri),
            }
        )
    return raw_points


def _gpx_total_distance_km(raw_points: list[dict]) -> float:
    total = 0.0
    prev_point = None
    for point in raw_points:
        if prev_point is not None:
            total += _haversine_km(
                prev_point[0], prev_point[1], point["lat"], point["lon"]
            )
        prev_point = (point["lat"], point["lon"])
    return total


def _gpx_elevation_gain_m(raw_points: list[dict]) -> float:
    gain = 0.0
    prev_ele = None
    for point in raw_points:
        if point["ele"] is not None:
            if prev_ele is not None and point["ele"] > prev_ele:
                gain += point["ele"] - prev_ele
            prev_ele = point["ele"]
    return gain


def _gpx_heart_rate_stats(raw_points: list[dict]) -> tuple[int | None, int | None]:
    heart_rates = [point["hr"] for point in raw_points if point["hr"] is not None]
    if not heart_rates:
        return None, None
    return round(sum(heart_rates) / len(heart_rates)), max(heart_rates)


def _gpx_point_stream(raw_points: list[dict], start_time) -> list[dict]:
    """Cumulative distance stream, gated on time presence: distance accumulates
    over every point (matching _gpx_total_distance_km's accumulation exactly),
    but a stream entry is only recorded for points that carry a <time>."""
    points = []
    distance_km = 0.0
    prev_point = None
    for point in raw_points:
        if prev_point is not None:
            distance_km += _haversine_km(
                prev_point[0], prev_point[1], point["lat"], point["lon"]
            )
        prev_point = (point["lat"], point["lon"])
        if point["time"] is not None:
            points.append(
                {
                    "elapsed_seconds": (point["time"] - start_time).total_seconds(),
                    "distance_km": distance_km,
                    "hr": point["hr"],
                }
            )
    return points


def _gpx_last_time(raw_points: list[dict], start_time):
    last_time = start_time
    for point in raw_points:
        if point["time"] is not None:
            last_time = point["time"]
    return last_time


def _gpx_activity_type(trk, ns_uri) -> str:
    type_elem = _find(trk, "type", ns_uri) if trk is not None else None
    if type_elem is None:
        return "run"
    return GPX_TYPE_MAP.get((type_elem.text or "").lower(), "run")


def import_gpx(path: str, max_heart_rate: int = 0) -> dict:
    root = _parse_xml_root(path)
    ns_uri = _namespace(root.tag)

    trk = _find(root, "trk", ns_uri)
    trkpts = []
    if trk is not None:
        for trkseg in _findall(trk, "trkseg", ns_uri):
            trkpts.extend(_findall(trkseg, "trkpt", ns_uri))

    start_time_elem = _find_path(root, "metadata/time", ns_uri)
    if start_time_elem is not None:
        start_time = _parse_iso_time(start_time_elem.text)
    elif trkpts:
        first_time_elem = _find(trkpts[0], "time", ns_uri)
        start_time = _parse_iso_time(first_time_elem.text)
    else:
        raise ValueError(f"no start time found in {path}")

    raw_points = _gpx_raw_points(trkpts, ns_uri)
    distance_km = _gpx_total_distance_km(raw_points)
    elevation_gain_m = _gpx_elevation_gain_m(raw_points)
    avg_hr, max_hr = _gpx_heart_rate_stats(raw_points)
    points = _gpx_point_stream(raw_points, start_time)
    last_time = _gpx_last_time(raw_points, start_time)

    activity = _base_activity(
        start_time,
        _gpx_activity_type(trk, ns_uri),
        distance_km,
        (last_time - start_time).total_seconds(),
        "gpx",
    )
    if elevation_gain_m:
        activity["elevation_gain_m"] = round(elevation_gain_m)
    if avg_hr is not None:
        activity["avg_heart_rate"] = avg_hr
        activity["max_heart_rate"] = max_hr
    activity = _attach_splits(activity, points)
    return _attach_hr_zones(activity, points, max_heart_rate)


# --- TCX ------------------------------------------------------------------------------
# Same shape as GPX above: start_time is resolved first (<Id>, else the first
# lap's StartTime attribute), then each metric gets its own single-purpose pass
# over the laps. In the pathological case of a TCX with no <Id> whose first lap
# also lacks StartTime, trackpoints before the StartTime-carrying lap get a
# negative elapsed offset in the point stream — harmless, since fastest_split
# only ever uses elapsed-time differences.


def _tcx_start_time(activity_elem, laps, ns_uri):
    """<Id> if present, else the first lap's StartTime attribute, else None."""
    id_elem = _find(activity_elem, "Id", ns_uri)
    if id_elem is not None:
        return _parse_iso_time(id_elem.text)
    for lap in laps:
        start_time_attr = lap.get("StartTime")
        if start_time_attr:
            return _parse_iso_time(start_time_attr)
    return None


def _tcx_lap_totals(lap, ns_uri) -> tuple[float, float]:
    """(distance_m, time_s) contributed by one lap; 0.0 for either if absent."""
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
    """Average power (watts) for one lap, from Garmin's ActivityExtension
    <Extensions><LX><AvgWatts> block. That block lives in a different XML
    namespace than the rest of the TCX doc, so (unlike _tcx_lap_totals/
    _tcx_lap_heart_rate, which use ns_uri-scoped _find_path) it's matched by
    tag suffix, the same trick _gpx_heart_rate uses for GPX extensions."""
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
    """(gain_delta, new_prev_ele) for one trackpoint; prev_ele passed through
    unchanged if the trackpoint has no AltitudeMeters."""
    alt_elem = _find(trackpoint, "AltitudeMeters", ns_uri)
    if alt_elem is None:
        return 0.0, prev_ele
    ele = float(alt_elem.text)
    gain = ele - prev_ele if prev_ele is not None and ele > prev_ele else 0.0
    return gain, ele


def _tcx_trackpoint_position(trackpoint, ns_uri):
    """(lat, lon, time) if the trackpoint has both Position and Time, else None.
    Trackpoint-level DistanceMeters varies by exporter (sometimes cumulative from
    activity start, sometimes reset per lap) with no reliable way to tell which —
    so the split stream is always derived from GPS position via haversine
    instead, exactly like GPX, carried across laps rather than reset per lap."""
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
    """Per-trackpoint HeartRateBpm/Value, or None if the trackpoint has none.
    Older/lap-summary-only exporters carry HR at the Lap level instead (see
    _tcx_lap_heart_rate) — trackpoint-level HR is what powers the HR zone
    breakdown, since that needs a per-sample time series, not a lap average."""
    hr_elem = _find_path(trackpoint, "HeartRateBpm/Value", ns_uri)
    return round(float(hr_elem.text)) if hr_elem is not None else None


def _tcx_totals(laps, ns_uri) -> tuple[float, float]:
    """(total_distance_m, total_time_s) summed over all laps."""
    total_distance_m = 0.0
    total_time_s = 0.0
    for lap in laps:
        lap_distance_m, lap_time_s = _tcx_lap_totals(lap, ns_uri)
        total_distance_m += lap_distance_m
        total_time_s += lap_time_s
    return total_distance_m, total_time_s


def _tcx_heart_rate_stats(laps, ns_uri) -> tuple[int | None, int | None]:
    """(avg, max) across laps — mean of lap averages, max of lap maxima —
    each None if no lap carries that field."""
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
    """Mean of the laps' AvgWatts values, or None if no lap has power."""
    power_avgs = [
        p for p in (_tcx_lap_power(lap, ns_uri) for lap in laps) if p is not None
    ]
    return round(sum(power_avgs) / len(power_avgs)) if power_avgs else None


def _tcx_elevation_gain_m(laps, ns_uri) -> float:
    """Summed positive altitude deltas, with the previous-altitude tracking
    reset at each lap boundary."""
    gain = 0.0
    for lap in laps:
        prev_ele = None
        for trackpoint in _tcx_trackpoints(lap, ns_uri):
            delta, prev_ele = _tcx_trackpoint_elevation(trackpoint, ns_uri, prev_ele)
            gain += delta
    return gain


def _tcx_point_stream(laps, ns_uri, start_time) -> list[dict]:
    """Cumulative (elapsed_seconds, distance_km) stream from GPS positions via
    haversine, carried across laps rather than reset per lap (see
    _tcx_trackpoint_position for why lap DistanceMeters isn't used)."""
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
    if isinstance(raw_sport, str):
        activity_type = FIT_SPORT_MAP.get(raw_sport.lower(), "run")
    else:
        # fitparse couldn't decode this enum value to a name (see
        # FIT_SPORT_CODE_MAP above) -- it comes through as a raw int, or as
        # None if the sport field is absent entirely.
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
        if record_distance_m is None or record_timestamp is None:
            continue
        points.append(
            {
                "elapsed_seconds": (record_timestamp - start_time).total_seconds(),
                "distance_km": record_distance_m / 1000,
                "hr": record_fields.get("heart_rate"),
            }
        )

    activity = _attach_splits(activity, points)
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
    """Base activity fields from one Strava CSV row (id/type/date/distance_km/
    duration_seconds/source) paired with None, or (None, skip_label) if the
    row can't be imported — where skip_label names why, for the per-type
    skipped-row summary: the raw type string for an unmapped type, or
    "(no date)" for a missing date. Exactly one side is populated. Shared by
    import_strava_csv and import_strava_export — does not resolve any linked
    file."""
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
    """One summary line naming each skipped Strava row category and its count,
    e.g. 'skipped 100 Strava rows fit can't import: Workout x99, Surfing x1'.
    Empty list when nothing was skipped."""
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
    if suffix == ".gpx":
        return import_gpx(path, max_heart_rate)
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
        activity = import_by_extension(str(file_path), suffix, max_heart_rate)
        activity["_source_path"] = str(file_path)
        activities.append(activity)
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
                    warnings.append(f"skipped {filename}: {exc}")
                    continue
                activity["id"] = base["id"]
                activity["type"] = base["type"]
                activity["date"] = base["date"]
                activity["source"] = base["source"]
            else:
                activity = dict(base)

            activities.append(activity)

    warnings.extend(_strava_skip_warnings(skipped))
    return activities, warnings
