"""Parses Garmin FIT files into fit's activity dict shape, via fitparse.

FIT is the only format: everything arrives from Garmin, by garmin-sync or as
a file. Never dedupes and never imports storage — cli._import_and_report owns
on-disk state.
"""

from pathlib import Path

from fit import compute

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
    start_time, activity_type: str, distance_km: float, duration_seconds: float
) -> dict:
    """The always-present fields. Activities imported before FIT became the
    only format may carry "source": "strava"; every new one is "garmin"."""
    return {
        "id": start_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": activity_type,
        "date": start_time.strftime("%Y-%m-%d"),
        "distance_km": round(distance_km, 2),
        "duration_seconds": round(duration_seconds),
        "source": "garmin",
    }


def _attach_splits(activity: dict, points: list[dict]) -> dict:
    """Attach splits; the key is omitted entirely, never left empty."""
    splits = _compute_splits(activity["type"], points)
    return {**activity, "splits": splits} if splits else activity


def _attach_best_power(activity: dict, points: list[dict]) -> dict:
    """Attach the power-duration curve; key omitted when there is none. Stored
    because the point stream is discarded at import and cannot be recovered."""
    best = {}
    for window_seconds, label in compute.POWER_WINDOWS_S:
        watts = compute.best_power_window(points, window_seconds)
        if watts is not None:
            best[label] = watts
    return {**activity, "best_power": best} if best else activity


def _attach_hr_zones(activity: dict, points: list[dict], max_heart_rate: int) -> dict:
    """Attach HR zone-seconds; key omitted when max_heart_rate is unset or the
    file carries no per-point hr."""
    hr_zones = compute.hr_zone_seconds(points, max_heart_rate)
    return {**activity, "hr_zones": hr_zones} if hr_zones else activity


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

    return {**activity, "exercises": merged}


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

    activity = _base_activity(start_time, activity_type, distance_m / 1000, duration_s)
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


def import_directory(dir_path: str, max_heart_rate: int = 0) -> list[dict]:
    """Every .fit file in a folder (a mounted watch, say); anything else in
    it is ignored."""
    return [
        import_fit(str(file_path), max_heart_rate)
        for file_path in sorted(Path(dir_path).iterdir())
        if file_path.suffix.lower() == ".fit"
    ]
