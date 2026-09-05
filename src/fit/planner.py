"""Structured workout generation: pure functions building Garmin Connect
workout-service payloads as plain dicts. No file I/O, no network, no
garminconnect import — cli.py wires prompts, storage, and the push around
this module (see garmin.push_workout).

The payload schema (sport/step/condition/target ids, DTO shapes) is
replicated from the reference models in garminconnect 0.3.6's workout.py
rather than imported from it, which would drag in the optional pydantic
extra fit doesn't carry. The target-value field names (targetValueOne /
targetValueTwo, low m/s bound first) and global step numbering follow the
workout-service JSON as served by Garmin Connect.

Verified against a live upload on 2026-08-24: a real `run`/`intervals` push
round-tripped through get_workout_by_id() with every field fit sent coming
back unchanged — targetValueOne/Two stored as the low/high m/s bounds in
that order under those exact names, endCondition/step numbering intact (see
scripts/diff_workout.py). This confirms the target-value and step-numbering
machinery every sport/type combo shares; the other combos reuse the same
builders but have not each been individually round-tripped, so re-run the
diff after first pushing an unverified combo. Two shapes are newer than that
verification and worth the diff first: the steady types (easy/long/endurance/
continuous), which are fit's first single-step workouts, and the bare
baselines (cycle/swim), which are the first to omit warmup and cooldown steps
entirely and renumber what remains.

The strength combos are a different case again, and were built from a real
workout rather than from garminconnect's models, which carry nothing
strength-specific at all (see docs/STRENGTH_PLAN.md Findings).
`strength`/`straight_sets` is **verified against a live upload on 2026-09-05**:
a two-exercise push round-tripped through get_workout() with every field
unchanged, including a 62.5kg load that came back as 62.5 under unitKey
"kilogram" — so weightValue is read as kilograms on the way in, not as the
grams its unit factor might suggest, and a half-kilo plate is not rounded
away. Also confirmed by that push: a reps end condition, a timed rest step
(Connect's own UI writes a lap.button one), a time-ended CARDIO warmup, and a
bare `category` with exerciseName left unset. `strength`/`baseline` shares
those builders but has not been pushed on its own.
"""

import statistics
from datetime import date

from fit import compute

SPORT_TYPES = {
    "run": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycle": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "swim": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3},
    # Copied verbatim from a live strength workout's dump (see
    # docs/STRENGTH_PLAN.md Findings), not inferred.
    "strength": {
        "sportTypeId": 5,
        "sportTypeKey": "strength_training",
        "displayOrder": 4,
    },
}

WORKOUT_TYPES = {
    "run": ["intervals", "tempo", "hills", "baseline", "easy", "long"],
    "swim": ["intervals", "continuous", "baseline"],
    "cycle": ["intervals", "hills", "baseline", "endurance", "long"],
    "strength": ["straight_sets", "baseline"],
}

# The four barbell lifts fit plans. Deliberately not Garmin's whole exercise
# catalogue: an unknown category is silently blanked on the watch rather than
# rejected, so a typo would otherwise become a nameless step nobody can read.
# These are the FIT exercise_category names importers.py already stores, and
# the payload wants them uppercased -- one vocabulary, both directions. Extend
# from a real session, the same rule FIT_SPORT_CODE_MAP follows.
STRENGTH_CATEGORIES = ("deadlift", "squat", "bench_press", "shoulder_press")

# Quality workouts (intervals/tempo/hills/baseline) are warmup -> main ->
# cooldown. The steady types above (easy/long/endurance/continuous) are a
# single block instead: a warmup inside an easy run is just more easy running,
# and splitting it only makes the watch beep for no reason.
STEADY_TYPES = {"easy", "long", "endurance", "continuous"}

# Target zones are a band around the single value the user gives, so the
# watch has a realistic corridor to beep against rather than a knife edge.
PACE_TOLERANCE_S_PER_KM = 10
SWIM_PACE_TOLERANCE_S_PER_100M = 5
POWER_TOLERANCE_W = 10

# Steady sessions get a wider corridor than quality work: the point of an easy
# run is to stay comfortable over a range, not to hold one pace to the second.
EASY_PACE_TOLERANCE_S_PER_KM = 20
ENDURANCE_POWER_TOLERANCE_W = 25

# History-derived defaults (recommend_defaults): how far back "recent
# performance" looks, how much slower tempo pace is than 5k race pace
# (Daniels: T is 88-92% vVO2max vs I at 95-100%, roughly 7-8% slower), and
# the cap on the build-by-one rep progression.
RECENT_MONTHS = 6
TEMPO_FACTOR = 1.07
REPS_CAP = 10

# Easy and long runs are both run at Daniels' "E" pace, ~30% slower than 5k
# race pace (E is 59-74% vVO2max against I's 95-100%). Endurance riding sits
# at ~70% of FTP, the middle of the conventional 65-75% "zone 2" band.
EASY_FACTOR = 1.3
ENDURANCE_FTP_FACTOR = 0.70

# Run intervals of SHORT_REP_MAX_M or less default to 5k pace * SHORT_REP_FACTOR
# (~3% faster). The reference 5k is a *training* best, not a race result, so it
# understates race ability — the discount puts short reps back inside Daniels'
# I-pace bracket (3k-5k race pace at 97-100% VO2max) instead of below its slow
# end. Longer reps stay at plain 5k pace, the bracket's safe end.
SHORT_REP_MAX_M = 1000
SHORT_REP_FACTOR = 0.97

# Derived targets are sanity-checked before they become prescriptions. A
# derivation built from mismatched efforts can produce a number no human
# produces, and a plan built on it prescribes work nobody can complete.
# Generous on purpose: these reject the impossible, not the merely unusual.
# An explicit `targets:` in a plan description is the user's own word and is
# NOT bounded by these.
PLAUSIBLE_TARGETS = {
    "run": (750, 3600),  # 5k seconds: 2:30/km world record .. 12:00/km walking
    "swim": (60, 240),  # seconds per 100m: elite .. very slow
    "cycle": (60, 500),  # watts: not a training target .. world class
}

# Guards on the two-point critical-speed model (derive_swim_css). The shorter
# effort must actually be faster per 100m — if it is not, the two are not
# comparable maximal efforts and the model extrapolates the wrong way, which
# is how a 2021 drill 500m and a 2025 1k once produced a 35s/100m CSS. The
# floor bounds the extrapolation the other way, so a sprint measured against a
# steady swim cannot project far past either.
CSS_PAIR_MIN_RATIO = 0.75
CSS_PAIR_MAX_DAYS = 90

# A 20-minute best effort is the standard field FTP test, and the conventional
# correction is 95% of it. Only worth applying now that the plan's cycle
# benchmark is a bare test whose avg_power really is that effort.
FTP_FROM_20MIN = 0.95
# Just under 20 minutes: a watch stopped on the beep can record 1199s, and
# disqualifying a test that was actually done is the worst failure available —
# `fit train retarget` would report no change and never say why.
RIDE_POWER_MIN_SECONDS = 1140

# Speeds assumed for estimatedDurationInSecs when a distance step has no
# pace target to average (6:00/km run, 2:00/100m swim).
_FALLBACK_SPEED_MPS = {"run": 1000 / 360, "swim": 100 / 120, "cycle": 25 / 3.6}

# The same role on the strength side: a reps step and a press-lap-when-ready
# rest carry no duration, but training.py sizes a planned week by summing
# estimate_seconds, so a strength session that estimated zero would look free.
# Both are coarse by design -- a rep is a rep whatever is on the bar.
_FALLBACK_SECONDS_PER_REP = 4
_FALLBACK_REST_SECONDS = 90

_STEP_TYPES = {
    "warmup": 1,
    "cooldown": 2,
    "interval": 3,
    "recovery": 4,
    "rest": 5,
    "repeat": 6,
}
_END_CONDITIONS = {
    "lap.button": 1,
    "time": 2,
    "distance": 3,
    "iterations": 7,
    "reps": 10,
}

# A set's load is not a target: the live dump has targetType "no.target" on a
# step carrying weightValue/weightUnit (see docs/STRENGTH_PLAN.md Findings).
# weightValue is in kilograms -- the unit's factor is metadata, not something
# to pre-apply.
_WEIGHT_UNIT_KG = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}

# A lap.button step ends when the athlete presses lap, so its endConditionValue
# is inert -- but Connect writes 10.0 there rather than 0, on both the warmup
# and the rests in the dumped workout. Sent as observed: a value Garmin itself
# stores is safer than one it has never been given.
_LAP_BUTTON_END_VALUE = 10.0
_TARGET_TYPES = {"no.target": 1, "power.zone": 2, "pace.zone": 6}


def parse_pace(text: str) -> int:
    """'4:30' -> 270 seconds (per km or per 100m — caller's unit). Strict
    m:ss form; raises ValueError otherwise."""
    minutes, _, seconds = text.strip().partition(":")
    if not minutes.isdigit() or not seconds.isdigit() or len(seconds) != 2:
        raise ValueError(f"invalid pace '{text}': expected m:ss, e.g. '4:30'")
    total = int(minutes) * 60 + int(seconds)
    if int(seconds) > 59 or total == 0:
        raise ValueError(f"invalid pace '{text}': expected m:ss, e.g. '4:30'")
    return total


def parse_duration(text: str) -> int:
    """'90' -> 90, '2:00' -> 120 seconds. Raises ValueError on anything else."""
    text = text.strip()
    if ":" in text:
        return parse_pace(text)
    if not text.isdigit() or int(text) == 0:
        raise ValueError(
            f"invalid duration '{text}': expected seconds or m:ss, e.g. '90' or '1:30'"
        )
    return int(text)


def parse_schedule_date(text: str) -> str:
    """Validate a calendar date for scheduling a workout and return it as a
    normalized ISO 'YYYY-MM-DD' string. Raises ValueError on anything that
    isn't a real date in that format. The single scheduling primitive today;
    a future multi-week planner will compute a run of session dates itself
    (that day-picking logic belongs here, pure, alongside this) and each
    still passes through here before reaching garmin.schedule_workout. No
    past/future policy is imposed — Garmin accepts any real date, and a
    scheduler may legitimately (re)place a session in the past."""
    text = text.strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(
            f"invalid schedule date '{text}': expected YYYY-MM-DD, e.g. '2026-08-28'"
        ) from exc


def parse_distance_km(text: str) -> int:
    """'16' or '16.5' -> 16000/16500 metres. Prompts for steady sessions ask in
    km (nobody thinks of a long run as '16000'), but every payload endCondition
    is in metres, so the parse converts. Raises ValueError on anything that
    isn't a positive number."""
    try:
        km = float(str(text).strip())
    except ValueError as exc:
        raise ValueError(
            f"invalid distance '{text}': expected km, e.g. '16' or '16.5'"
        ) from exc
    if km <= 0:
        raise ValueError(f"invalid distance '{text}': must be greater than zero")
    return round(km * 1000)


def _positive_int(text: str) -> int:
    value = int(str(text).strip())
    if value <= 0:
        raise ValueError("must be greater than zero")
    return value


def _optional_int(text: str) -> int:
    """Blank -> 0, meaning "omit this step entirely" rather than emit a
    zero-length one (see _baseline_steps). Used for a baseline's warmup and
    cooldown, which have to be absent for the sports whose measurement spans
    the whole recording."""
    text = str(text).strip()
    return _positive_int(text) if text else 0


def _optional_pace(text: str) -> int | None:
    return parse_pace(text) if str(text).strip() else None


def parse_exercise(text: str) -> str:
    """'Back Squat' / 'squat' -> 'squat', validated against
    STRENGTH_CATEGORIES. Raises ValueError naming the valid lifts otherwise --
    Garmin blanks an exercise it doesn't recognise rather than rejecting it,
    which would put a nameless step on the watch."""
    name = str(text).strip().lower().replace(" ", "_").replace("-", "_")
    if name not in STRENGTH_CATEGORIES:
        raise ValueError(
            f"unknown exercise '{text}': expected one of "
            f"{', '.join(STRENGTH_CATEGORIES)}"
        )
    return name


def parse_weight_kg(text: str) -> float | None:
    """'60' / '62.5' -> kilograms; blank -> None, meaning the step carries no
    prescribed load (bodyweight, or 'work up to something today'). Raises
    ValueError on anything that isn't a positive number."""
    text = str(text).strip()
    if not text:
        return None
    try:
        kg = float(text)
    except ValueError as exc:
        raise ValueError(
            f"invalid weight '{text}': expected kilograms, e.g. '60' or '62.5'"
        ) from exc
    if kg <= 0:
        raise ValueError(f"invalid weight '{text}': must be greater than zero")
    return round(kg, 2)


def _optional_rest(text: str) -> int:
    """Blank -> 0, meaning a press-lap-when-ready rest between sets rather
    than a timed one (which is how Connect's own UI builds them)."""
    text = str(text).strip()
    return parse_duration(text) if text else 0


def pace_zone_mps(
    seconds_per_km: int, tolerance_s: int = PACE_TOLERANCE_S_PER_KM
) -> tuple[float, float]:
    """(low, high) speed bounds in m/s for a pace +/- tolerance band."""
    return 1000 / (seconds_per_km + tolerance_s), 1000 / max(
        seconds_per_km - tolerance_s, 1
    )


def swim_pace_zone_mps(
    seconds_per_100m: int, tolerance_s: int = SWIM_PACE_TOLERANCE_S_PER_100M
) -> tuple[float, float]:
    return 100 / (seconds_per_100m + tolerance_s), 100 / max(
        seconds_per_100m - tolerance_s, 1
    )


# --- prompt specs ---------------------------------------------------------

# One spec per prompt: key (params dict key), label (shown by typer.prompt),
# default (int or string, always prompted as text), parse (str -> value,
# raising ValueError to re-prompt).
_PARAM_SPECS = {
    ("run", "intervals"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
        {
            "key": "reps",
            "label": "Number of reps",
            "default": 6,
            "parse": _positive_int,
        },
        {
            "key": "rep_distance_m",
            "label": "Rep distance (m)",
            "default": 800,
            "parse": _positive_int,
        },
        {
            "key": "target_pace",
            "label": "Target pace (min:sec per km)",
            "default": "4:30",
            "parse": parse_pace,
        },
        {
            "key": "recovery",
            "label": "Recovery (min:sec)",
            "default": "2:00",
            "parse": parse_duration,
        },
        {
            "key": "cooldown_minutes",
            "label": "Cooldown (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
    ],
    ("run", "tempo"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
        {
            "key": "tempo_minutes",
            "label": "Tempo block (minutes)",
            "default": 20,
            "parse": _positive_int,
        },
        {
            "key": "target_pace",
            "label": "Target pace (min:sec per km)",
            "default": "5:00",
            "parse": parse_pace,
        },
        {
            "key": "cooldown_minutes",
            "label": "Cooldown (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
    ],
    ("run", "hills"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
        {
            "key": "reps",
            "label": "Number of hill reps",
            "default": 8,
            "parse": _positive_int,
        },
        {
            "key": "effort",
            "label": "Uphill effort (min:sec)",
            "default": "0:45",
            "parse": parse_duration,
        },
        {
            "key": "recovery",
            "label": "Recovery (min:sec)",
            "default": "2:00",
            "parse": parse_duration,
        },
        {
            "key": "cooldown_minutes",
            "label": "Cooldown (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
    ],
    # Keeps its warmup and cooldown: compute.fastest_split isolates the fastest
    # 5k window from whatever surrounds it. 5000 because 3km appears in neither
    # SPLIT_DISTANCES_KM nor MILESTONES_KM, so a 3km effort is measurable
    # nowhere — see training.BENCHMARK_SESSIONS.
    ("run", "baseline"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes)",
            "default": 10,
            "parse": _optional_int,
        },
        {
            "key": "test_distance_m",
            "label": "Test distance (m)",
            "default": 5000,
            "parse": _positive_int,
        },
        {
            "key": "cooldown_minutes",
            "label": "Cooldown (minutes)",
            "default": 10,
            "parse": _optional_int,
        },
    ],
    ("swim", "intervals"): [
        {
            "key": "warmup_m",
            "label": "Warmup distance (m)",
            "default": 200,
            "parse": _positive_int,
        },
        {
            "key": "reps",
            "label": "Number of reps",
            "default": 10,
            "parse": _positive_int,
        },
        {
            "key": "rep_distance_m",
            "label": "Rep distance (m)",
            "default": 100,
            "parse": _positive_int,
        },
        {
            "key": "target_pace_100m",
            "label": "Target pace per 100m (min:sec, blank for none)",
            "default": "",
            "parse": _optional_pace,
        },
        {
            "key": "rest",
            "label": "Rest between reps (min:sec)",
            "default": "0:20",
            "parse": parse_duration,
        },
        {
            "key": "cooldown_m",
            "label": "Cooldown distance (m)",
            "default": 200,
            "parse": _positive_int,
        },
    ],
    ("cycle", "intervals"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes)",
            "default": 15,
            "parse": _positive_int,
        },
        {
            "key": "reps",
            "label": "Number of reps",
            "default": 4,
            "parse": _positive_int,
        },
        {
            "key": "work",
            "label": "Work block (min:sec)",
            "default": "8:00",
            "parse": parse_duration,
        },
        {
            "key": "target_watts",
            "label": "Target power (watts)",
            "default": 200,
            "parse": _positive_int,
        },
        {
            "key": "recovery",
            "label": "Recovery (min:sec)",
            "default": "4:00",
            "parse": parse_duration,
        },
        {
            "key": "cooldown_minutes",
            "label": "Cooldown (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
    ],
    ("cycle", "hills"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes)",
            "default": 15,
            "parse": _positive_int,
        },
        {
            "key": "reps",
            "label": "Number of hill reps",
            "default": 6,
            "parse": _positive_int,
        },
        {
            "key": "effort",
            "label": "Climb effort (min:sec)",
            "default": "1:00",
            "parse": parse_duration,
        },
        {
            "key": "recovery",
            "label": "Recovery (min:sec)",
            "default": "3:00",
            "parse": parse_duration,
        },
        {
            "key": "cooldown_minutes",
            "label": "Cooldown (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
    ],
    # compute.best_power_window finds the 20-minute effort wherever it sits in
    # the recording, so this is a normal warmup/test/cooldown workout. Blank
    # still means "no step", for anyone who prefers to record the test alone.
    ("cycle", "baseline"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes, blank for none)",
            "default": 20,
            "parse": _optional_int,
        },
        {
            "key": "test_minutes",
            "label": "Best-effort test (minutes)",
            "default": 20,
            "parse": _positive_int,
        },
        {
            "key": "cooldown_minutes",
            "label": "Cooldown (minutes, blank for none)",
            "default": 10,
            "parse": _optional_int,
        },
    ],
    ("run", "easy"): [
        {
            "key": "duration_minutes",
            "label": "Duration (minutes)",
            "default": 40,
            "parse": _positive_int,
        },
        {
            "key": "target_pace",
            "label": "Easy pace (min:sec per km)",
            "default": "6:00",
            "parse": parse_pace,
        },
    ],
    ("run", "long"): [
        {
            "key": "distance_m",
            "label": "Distance (km)",
            "default": 16,
            "parse": parse_distance_km,
        },
        {
            "key": "target_pace",
            "label": "Long-run pace (min:sec per km)",
            "default": "5:45",
            "parse": parse_pace,
        },
    ],
    ("cycle", "endurance"): [
        {
            "key": "duration_minutes",
            "label": "Duration (minutes)",
            "default": 90,
            "parse": _positive_int,
        },
        {
            "key": "target_watts",
            "label": "Target power (watts)",
            "default": 150,
            "parse": _positive_int,
        },
    ],
    ("cycle", "long"): [
        {
            "key": "distance_m",
            "label": "Distance (km)",
            "default": 60,
            "parse": parse_distance_km,
        },
        {
            "key": "target_watts",
            "label": "Target power (watts)",
            "default": 150,
            "parse": _positive_int,
        },
    ],
    # Still bare, unlike cycle: there is no power-window equivalent for
    # swimming, so the whole-swim milestone is the measurement, and pool swims
    # often carry no distance stream for splits to come from either.
    ("swim", "baseline"): [
        {
            "key": "warmup_m",
            "label": "Warmup distance (m, blank for none)",
            "default": "",
            "parse": _optional_int,
        },
        {
            "key": "test_distance_m",
            "label": "Test distance (m)",
            "default": 1000,
            "parse": _positive_int,
        },
        {
            "key": "cooldown_m",
            "label": "Cooldown distance (m, blank for none)",
            "default": "",
            "parse": _optional_int,
        },
    ],
    ("swim", "continuous"): [
        {
            "key": "distance_m",
            "label": "Distance (m)",
            "default": 1500,
            "parse": _positive_int,
        },
        {
            "key": "target_pace_100m",
            "label": "Target pace per 100m (min:sec, blank for none)",
            "default": "",
            "parse": _optional_pace,
        },
    ],
    # Straight sets are the only combo whose params aren't one flat answer per
    # prompt: a gym session is several exercises, each with its own sets, reps
    # and load. The per-exercise half lives in EXERCISE_PARAM_SPECS below and
    # is prompted in a loop -- see repeated_param_specs.
    ("strength", "straight_sets"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes, blank for none)",
            "default": 10,
            "parse": _optional_int,
        },
        {
            "key": "rest",
            "label": "Rest between sets (min:sec, blank for press-lap)",
            "default": "1:30",
            "parse": _optional_rest,
        },
    ],
    # The e1RM re-test. No warmup step and no prescribed load: the ramp of
    # lighter sets before the top set is the warmup, and prescribing the
    # weight would make it a workout rather than a measurement. Phase 1's PB
    # tracking already reads only the best set, so the ramp costs nothing.
    ("strength", "baseline"): [
        {
            "key": "exercise",
            "label": f"Exercise ({' | '.join(STRENGTH_CATEGORIES)})",
            "default": "deadlift",
            "parse": parse_exercise,
        },
        {
            "key": "reps",
            "label": "Reps in the top set",
            "default": 3,
            "parse": _positive_int,
        },
    ],
}

# One exercise's worth of prompts, asked repeatedly by `fit plan --sport
# strength --type straight_sets` until the exercise is left blank. Kept out of
# _PARAM_SPECS because its answers become one entry in a list, not one key in
# the params dict.
EXERCISE_PARAM_SPECS = [
    {
        "key": "exercise",
        "label": f"Exercise ({' | '.join(STRENGTH_CATEGORIES)})",
        "default": "deadlift",
        "parse": parse_exercise,
    },
    {"key": "sets", "label": "Sets", "default": 3, "parse": _positive_int},
    {"key": "reps", "label": "Reps per set", "default": 10, "parse": _positive_int},
    {
        "key": "target_weight_kg",
        "label": "Weight (kg, blank for none)",
        "default": "",
        "parse": parse_weight_kg,
    },
]


def repeated_param_specs(sport: str, workout_type: str) -> tuple[str, list[dict]]:
    """(params key, specs) for a combo whose prompts repeat into a list, or
    ("", []) when the combo's params are one flat answer per prompt like every
    other one.

    This exists so cli.py can ask whether a combo needs its loop rather than
    hard-coding which combo that is — the knowledge of what a workout's params
    look like belongs here with the rest of the spec."""
    if (sport, workout_type) == ("strength", "straight_sets"):
        return "exercises", [dict(spec) for spec in EXERCISE_PARAM_SPECS]
    return "", []


def workout_params(sport: str, workout_type: str) -> list[dict]:
    """Prompt specs for one sport/type combo (see _PARAM_SPECS' shape).
    Raises ValueError, listing the valid combos, when there is no such
    workout — the single validation point for --sport/--type."""
    if (sport, workout_type) not in _PARAM_SPECS:
        valid = "; ".join(
            f"{s}: {', '.join(types)}" for s, types in WORKOUT_TYPES.items()
        )
        raise ValueError(
            f"no '{workout_type}' workout for sport '{sport}' — available: {valid}"
        )
    return [dict(spec) for spec in _PARAM_SPECS[(sport, workout_type)]]


# --- payload building -----------------------------------------------------


def _step_type(key: str) -> dict:
    return {
        "stepTypeId": _STEP_TYPES[key],
        "stepTypeKey": key,
        "displayOrder": _STEP_TYPES[key],
    }


def _end_condition(key: str) -> dict:
    return {
        "conditionTypeId": _END_CONDITIONS[key],
        "conditionTypeKey": key,
        "displayOrder": _END_CONDITIONS[key],
        "displayable": key != "iterations",
    }


def _no_target() -> dict:
    return {
        "targetType": {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1,
        }
    }


def _pace_target(low_mps: float, high_mps: float) -> dict:
    return {
        "targetType": {
            "workoutTargetTypeId": 6,
            "workoutTargetTypeKey": "pace.zone",
            "displayOrder": 6,
        },
        "targetValueOne": low_mps,
        "targetValueTwo": high_mps,
    }


def _power_target(watts: int, tolerance_w: int = POWER_TOLERANCE_W) -> dict:
    return {
        "targetType": {
            "workoutTargetTypeId": 2,
            "workoutTargetTypeKey": "power.zone",
            "displayOrder": 2,
        },
        "targetValueOne": watts - tolerance_w,
        "targetValueTwo": watts + tolerance_w,
    }


def _step(
    order: int, step_key: str, end_key: str, end_value: float, target: dict
) -> dict:
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _step_type(step_key),
        "endCondition": _end_condition(end_key),
        "endConditionValue": float(end_value),
        **target,
    }


def _repeat(order: int, iterations: int, steps: list[dict]) -> dict:
    return {
        "type": "RepeatGroupDTO",
        "stepOrder": order,
        "stepType": _step_type("repeat"),
        "numberOfIterations": iterations,
        "smartRepeat": False,
        "endCondition": _end_condition("iterations"),
        "endConditionValue": float(iterations),
        "workoutSteps": steps,
    }


def _format_mmss(seconds: int) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _format_meters(meters: float) -> str:
    km = meters / 1000
    return f"{km:g}km" if meters >= 1000 and km == round(km, 2) else f"{round(meters)}m"


def _run_intervals(params: dict) -> tuple[str, list[dict]]:
    steps = [
        _step(1, "warmup", "time", params["warmup_minutes"] * 60, _no_target()),
        _repeat(
            2,
            params["reps"],
            [
                _step(
                    3,
                    "interval",
                    "distance",
                    params["rep_distance_m"],
                    _pace_target(*pace_zone_mps(params["target_pace"])),
                ),
                _step(4, "recovery", "time", params["recovery"], _no_target()),
            ],
        ),
        _step(5, "cooldown", "time", params["cooldown_minutes"] * 60, _no_target()),
    ]
    name = (
        f"Run intervals {params['reps']}x{_format_meters(params['rep_distance_m'])}"
        f" @ {_format_mmss(params['target_pace'])}/km"
    )
    return name, steps


def _run_tempo(params: dict) -> tuple[str, list[dict]]:
    steps = [
        _step(1, "warmup", "time", params["warmup_minutes"] * 60, _no_target()),
        _step(
            2,
            "interval",
            "time",
            params["tempo_minutes"] * 60,
            _pace_target(*pace_zone_mps(params["target_pace"])),
        ),
        _step(3, "cooldown", "time", params["cooldown_minutes"] * 60, _no_target()),
    ]
    name = f"Run tempo {params['tempo_minutes']}min @ {_format_mmss(params['target_pace'])}/km"
    return name, steps


def _hills(sport_word: str, params: dict) -> tuple[str, list[dict]]:
    # No pace/power target on the efforts: gradient makes pace meaningless
    # and effort is the point — the watch just times the reps.
    steps = [
        _step(1, "warmup", "time", params["warmup_minutes"] * 60, _no_target()),
        _repeat(
            2,
            params["reps"],
            [
                _step(3, "interval", "time", params["effort"], _no_target()),
                _step(4, "recovery", "time", params["recovery"], _no_target()),
            ],
        ),
        _step(5, "cooldown", "time", params["cooldown_minutes"] * 60, _no_target()),
    ]
    name = f"{sport_word} hills {params['reps']}x{_format_mmss(params['effort'])}"
    return name, steps


def _baseline_steps(
    warmup: float,
    main_key: str,
    main_value: float,
    cooldown: float,
    wrap_key: str = "time",
) -> list[dict]:
    """warmup -> best effort -> cooldown, with a zero-or-absent warmup or
    cooldown omitted entirely rather than emitted as a zero-length step, and
    the remaining steps left contiguously numbered.

    Whether a benchmark can afford the wrapping depends on the sport, not on
    taste: the recorded activity must BE the test unless the sport's split
    machinery can isolate it from within. compute.fastest_split finds the
    fastest window anywhere in a run's track, so a run test survives being
    wrapped. avg_power is a whole-activity mean with no split analogue, and a
    pool swim often carries no distance stream at all, so those must be bare."""
    steps, order = [], 1
    if warmup:
        steps.append(_step(order, "warmup", wrap_key, warmup, _no_target()))
        order += 1
    steps.append(_step(order, "interval", main_key, main_value, _no_target()))
    order += 1
    if cooldown:
        steps.append(_step(order, "cooldown", wrap_key, cooldown, _no_target()))
    return steps


def _bare_suffix(warmup: float) -> str:
    """The instruction that has to travel with a bare test. The workout name is
    what the athlete reads on the watch, and nothing else in the payload can
    carry it — description is fixed at build_plan."""
    return "" if warmup else " (warm up first)"


def _run_baseline(params: dict) -> tuple[str, list[dict]]:
    # Runna-style benchmark: a best-effort fixed distance with an open
    # target, used to (re)measure current pace baseline. Keeps its warmup and
    # cooldown — see _baseline_steps.
    steps = _baseline_steps(
        params.get("warmup_minutes", 0) * 60,
        "distance",
        params["test_distance_m"],
        params.get("cooldown_minutes", 0) * 60,
    )
    name = f"Run baseline {_format_meters(params['test_distance_m'])} test"
    return name + _bare_suffix(params.get("warmup_minutes", 0)), steps


def _swim_intervals(params: dict) -> tuple[str, list[dict]]:
    pace = params.get("target_pace_100m")
    target = _pace_target(*swim_pace_zone_mps(pace)) if pace else _no_target()
    steps = [
        _step(1, "warmup", "distance", params["warmup_m"], _no_target()),
        _repeat(
            2,
            params["reps"],
            [
                _step(3, "interval", "distance", params["rep_distance_m"], target),
                _step(4, "rest", "time", params["rest"], _no_target()),
            ],
        ),
        _step(5, "cooldown", "distance", params["cooldown_m"], _no_target()),
    ]
    name = f"Swim intervals {params['reps']}x{_format_meters(params['rep_distance_m'])}"
    if pace:
        name += f" @ {_format_mmss(pace)}/100m"
    return name, steps


def _cycle_intervals(params: dict) -> tuple[str, list[dict]]:
    steps = [
        _step(1, "warmup", "time", params["warmup_minutes"] * 60, _no_target()),
        _repeat(
            2,
            params["reps"],
            [
                _step(
                    3,
                    "interval",
                    "time",
                    params["work"],
                    _power_target(params["target_watts"]),
                ),
                _step(4, "recovery", "time", params["recovery"], _no_target()),
            ],
        ),
        _step(5, "cooldown", "time", params["cooldown_minutes"] * 60, _no_target()),
    ]
    name = f"Cycle intervals {params['reps']}x{_format_mmss(params['work'])} @ {params['target_watts']}W"
    return name, steps


def _cycle_baseline(params: dict) -> tuple[str, list[dict]]:
    # FTP-test shape: sustained best effort, open target. The plan's own
    # benchmark passes no warmup/cooldown, because stored avg_power averages
    # the whole recording — see _baseline_steps.
    steps = _baseline_steps(
        params.get("warmup_minutes", 0) * 60,
        "time",
        params["test_minutes"] * 60,
        params.get("cooldown_minutes", 0) * 60,
    )
    name = f"Cycle baseline {params['test_minutes']}min test"
    return name + _bare_suffix(params.get("warmup_minutes", 0)), steps


def _swim_baseline(params: dict) -> tuple[str, list[dict]]:
    # Pool time trial, open target. Bare when the plan schedules it: a pool
    # swim often carries no cumulative-distance stream, so the whole-activity
    # milestone is the only reliable measurement — see _baseline_steps.
    steps = _baseline_steps(
        params.get("warmup_m", 0),
        "distance",
        params["test_distance_m"],
        params.get("cooldown_m", 0),
        wrap_key="distance",
    )
    name = f"Swim baseline {_format_meters(params['test_distance_m'])} test"
    return name + _bare_suffix(params.get("warmup_m", 0)), steps


# --- steady sessions (single block, wide target band) ---------------------


def _run_easy(params: dict) -> tuple[str, list[dict]]:
    steps = [
        _step(
            1,
            "interval",
            "time",
            params["duration_minutes"] * 60,
            _pace_target(
                *pace_zone_mps(params["target_pace"], EASY_PACE_TOLERANCE_S_PER_KM)
            ),
        )
    ]
    name = f"Run easy {params['duration_minutes']}min @ {_format_mmss(params['target_pace'])}/km"
    return name, steps


def _run_long(params: dict) -> tuple[str, list[dict]]:
    steps = [
        _step(
            1,
            "interval",
            "distance",
            params["distance_m"],
            _pace_target(
                *pace_zone_mps(params["target_pace"], EASY_PACE_TOLERANCE_S_PER_KM)
            ),
        )
    ]
    name = (
        f"Run long {_format_meters(params['distance_m'])}"
        f" @ {_format_mmss(params['target_pace'])}/km"
    )
    return name, steps


def _cycle_endurance(params: dict) -> tuple[str, list[dict]]:
    steps = [
        _step(
            1,
            "interval",
            "time",
            params["duration_minutes"] * 60,
            _power_target(params["target_watts"], ENDURANCE_POWER_TOLERANCE_W),
        )
    ]
    name = (
        f"Cycle endurance {params['duration_minutes']}min @ {params['target_watts']}W"
    )
    return name, steps


def _cycle_long(params: dict) -> tuple[str, list[dict]]:
    steps = [
        _step(
            1,
            "interval",
            "distance",
            params["distance_m"],
            _power_target(params["target_watts"], ENDURANCE_POWER_TOLERANCE_W),
        )
    ]
    name = (
        f"Cycle long {_format_meters(params['distance_m'])}"
        f" @ {params['target_watts']}W"
    )
    return name, steps


def _swim_continuous(params: dict) -> tuple[str, list[dict]]:
    pace = params.get("target_pace_100m")
    target = _pace_target(*swim_pace_zone_mps(pace)) if pace else _no_target()
    steps = [_step(1, "interval", "distance", params["distance_m"], target)]
    name = f"Swim continuous {_format_meters(params['distance_m'])}"
    if pace:
        name += f" @ {_format_mmss(pace)}/100m"
    return name, steps


def _weight_fields(weight_kg: float | None) -> dict:
    """The load half of a lifting step. Omitted entirely when there is none,
    rather than sent as zero — an unloaded step is a bodyweight or work-up set,
    not a 0kg one."""
    if not weight_kg:
        return {}
    return {"weightValue": float(weight_kg), "weightUnit": dict(_WEIGHT_UNIT_KG)}


def _lift_step(order: int, exercise: str, reps: int, weight_kg: float | None) -> dict:
    """One working set: reps as the end condition, load on the step itself.
    `category` is the uppercase FIT exercise name (see STRENGTH_CATEGORIES);
    `exerciseName` (the variant, e.g. BARBELL_BACK_SQUAT) is deliberately left
    unset — every lift fit plans is a category in its own right, and Connect
    accepts a bare one."""
    step = _step(order, "interval", "reps", reps, _no_target())
    step["category"] = exercise.upper()
    step.update(_weight_fields(weight_kg))
    return step


def _rest_step(order: int, rest_seconds: int) -> dict:
    """Rest between sets: timed if the user gave a duration, otherwise
    press-lap-when-ready, which is how Connect's own UI builds it and what a
    gym rest usually is."""
    if rest_seconds:
        return _step(order, "rest", "time", rest_seconds, _no_target())
    return _step(order, "rest", "lap.button", _LAP_BUTTON_END_VALUE, _no_target())


def _describe_exercise(name: str) -> str:
    return name.replace("_", " ")


def _strength_straight_sets(params: dict) -> tuple[str, list[dict]]:
    """One RepeatGroupDTO per exercise, siblings in the segment — the shape a
    real Connect strength workout has (see docs/STRENGTH_PLAN.md Findings).
    Step numbering runs globally across the groups, as it does for every other
    combo here."""
    exercises = params.get("exercises") or []
    if not exercises:
        raise ValueError("a straight-sets workout needs at least one exercise")

    steps: list[dict] = []
    order = 1
    warmup = params.get("warmup_minutes", 0)
    if warmup:
        # Connect tags a strength warmup CARDIO; it is a warmup, not a lift.
        warmup_step = _step(order, "warmup", "time", warmup * 60, _no_target())
        warmup_step["category"] = "CARDIO"
        steps.append(warmup_step)
        order += 1

    rest = params.get("rest", 0)
    for exercise in exercises:
        group_order = order
        steps.append(
            _repeat(
                group_order,
                exercise["sets"],
                [
                    _lift_step(
                        group_order + 1,
                        exercise["exercise"],
                        exercise["reps"],
                        exercise.get("target_weight_kg"),
                    ),
                    _rest_step(group_order + 2, rest),
                ],
            )
        )
        order = group_order + 3

    name = "Strength " + ", ".join(
        f"{_describe_exercise(e['exercise'])} {e['sets']}x{e['reps']}"
        + (f" @ {e['target_weight_kg']:g}kg" if e.get("target_weight_kg") else "")
        for e in exercises
    )
    return name, steps


def _strength_baseline(params: dict) -> tuple[str, list[dict]]:
    # An e1RM test: one top set at a fixed rep count, no prescribed load, no
    # warmup step. The ramp of lighter sets is the warmup and belongs in the
    # name, since nothing else in the payload can carry an instruction.
    steps = [_lift_step(1, params["exercise"], params["reps"], None)]
    name = (
        f"Strength baseline {_describe_exercise(params['exercise'])} "
        f"{params['reps']}-rep test"
    )
    return name + _bare_suffix(0), steps


_BUILDERS = {
    ("run", "intervals"): _run_intervals,
    ("run", "tempo"): _run_tempo,
    ("run", "hills"): lambda params: _hills("Run", params),
    ("run", "baseline"): _run_baseline,
    ("run", "easy"): _run_easy,
    ("run", "long"): _run_long,
    ("swim", "intervals"): _swim_intervals,
    ("swim", "continuous"): _swim_continuous,
    ("cycle", "intervals"): _cycle_intervals,
    ("cycle", "hills"): lambda params: _hills("Cycle", params),
    ("cycle", "baseline"): _cycle_baseline,
    ("swim", "baseline"): _swim_baseline,
    ("cycle", "endurance"): _cycle_endurance,
    ("cycle", "long"): _cycle_long,
    ("strength", "straight_sets"): _strength_straight_sets,
    ("strength", "baseline"): _strength_baseline,
}


def _estimate_seconds(steps: list[dict], sport: str) -> float:
    total = 0.0
    for step in steps:
        if step["type"] == "RepeatGroupDTO":
            total += step["numberOfIterations"] * _estimate_seconds(
                step["workoutSteps"], sport
            )
        elif step["endCondition"]["conditionTypeKey"] == "time":
            total += step["endConditionValue"]
        elif step["endCondition"]["conditionTypeKey"] == "reps":
            total += step["endConditionValue"] * _FALLBACK_SECONDS_PER_REP
        elif step["endCondition"]["conditionTypeKey"] == "lap.button":
            # Only a strength rest ends this way today, and it has no duration
            # to read; the alternative is counting it as free.
            total += _FALLBACK_REST_SECONDS
        else:  # distance: estimate via the pace target's midpoint if present
            if step.get("targetType", {}).get("workoutTargetTypeKey") == "pace.zone":
                speed = (step["targetValueOne"] + step["targetValueTwo"]) / 2
            else:
                speed = _FALLBACK_SPEED_MPS[sport]
            total += step["endConditionValue"] / speed
    return total


def workout_name(sport: str, workout_type: str, params: dict) -> str:
    """The human workout name for a sport/type/params combo, without building
    the payload around it. training.py stores this on each expanded session so
    `fit train show` reads well without carrying ~80 full payloads in the plan
    file — the payload is rebuilt from params at sync time instead."""
    workout_params(sport, workout_type)  # reuse its ValueError on bad combos
    name, _ = _BUILDERS[(sport, workout_type)](params)
    return name


def estimate_seconds(sport: str, workout_type: str, params: dict) -> int:
    """How long one workout is expected to take, without building the payload
    around it. training.py sums these to measure a planned week against what
    the user is actually training now (see "Starting volume")."""
    workout_params(sport, workout_type)  # reuse its ValueError on bad combos
    _, steps = _BUILDERS[(sport, workout_type)](params)
    return round(_estimate_seconds(steps, sport))


def build_plan(sport: str, workout_type: str, params: dict, created: str) -> dict:
    """Build the saved-plan dict for one workout. params: the parsed values
    keyed per workout_params' specs. created: ISO-seconds timestamp, becomes
    the plan id/filename. The "payload" value is the complete Garmin Connect
    workout-service dict, ready for garmin.push_workout."""
    workout_params(sport, workout_type)  # reuse its ValueError on bad combos
    name, steps = _BUILDERS[(sport, workout_type)](params)
    payload = {
        "workoutName": name,
        "sportType": dict(SPORT_TYPES[sport]),
        "estimatedDurationInSecs": round(_estimate_seconds(steps, sport)),
        "author": {},
        "description": "generated by fit",
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": dict(SPORT_TYPES[sport]),
                "workoutSteps": steps,
            }
        ],
    }
    return {
        "id": created,
        "sport": sport,
        "workout_type": workout_type,
        "params": params,
        "workout_name": name,
        "payload": payload,
    }


# --- human-readable summary (rendering-free: display.py prints these) -----


def _describe_target(step: dict, sport: str) -> str:
    target = step.get("targetType", {})
    key = target.get("workoutTargetTypeKey")
    if key == "pace.zone":
        unit_m, suffix = (100, "/100m") if sport == "swim" else (1000, "/km")
        fast = round(unit_m / step["targetValueTwo"])
        slow = round(unit_m / step["targetValueOne"])
        return f" @ {_format_mmss(fast)}–{_format_mmss(slow)}{suffix}"
    if key == "power.zone":
        return f" @ {round(step['targetValueOne'])}–{round(step['targetValueTwo'])}W"
    return ""


def _describe_extent(step: dict) -> str:
    condition = step["endCondition"]["conditionTypeKey"]
    if condition == "time":
        return _format_mmss(step["endConditionValue"])
    if condition == "reps":
        return f"{round(step['endConditionValue'])} reps"
    if condition == "lap.button":
        return "until lap"
    return _format_meters(step["endConditionValue"])


def _describe_load(step: dict) -> str:
    """The lifting counterpart to _describe_target: weight rides on the step
    itself rather than in a target zone (see _weight_fields)."""
    weight = step.get("weightValue")
    return f" @ {weight:g}kg" if weight else ""


def _describe_step(step: dict, sport: str) -> str:
    kind = step["stepType"]["stepTypeKey"]
    body = _describe_extent(step) + _describe_target(step, sport) + _describe_load(step)
    # A lift's step names the exercise; a strength warmup's CARDIO category is
    # the absence of one, and reads as noise next to "Warmup 10:00".
    category = step.get("category")
    if category and category != "CARDIO":
        body = f"{_describe_exercise(category.lower()).capitalize()} {body}"
    if kind in ("warmup", "cooldown"):
        return f"{kind.capitalize()} {body}"
    if kind in ("recovery", "rest"):
        return f"{body} {kind}"
    return body  # interval


def describe_plan(plan: dict) -> list[str]:
    """One line per top-level step, e.g. ['Warmup 10:00',
    '6 x 800m @ 4:20-4:40/km, 2:00 recovery', 'Cooldown 10:00']."""
    sport = plan["sport"]
    lines = []
    for step in plan["payload"]["workoutSegments"][0]["workoutSteps"]:
        if step["type"] == "RepeatGroupDTO":
            children = ", ".join(
                _describe_step(child, sport) for child in step["workoutSteps"]
            )
            lines.append(f"{step['numberOfIterations']} x {children}")
        else:
            lines.append(_describe_step(step, sport))
    return lines


# --- history-derived recommended defaults ---------------------------------


def _round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


def recent_activities(activities: list[dict], reference: date) -> list[dict]:
    """The last RECENT_MONTHS of activities — the window every derive_* helper
    expects. Exposed so training.py windows history the same way rather than
    re-deriving the same two compute calls."""
    return compute.filter_by_date(
        activities, compute.months_ago(reference, RECENT_MONTHS), reference.isoformat()
    )


def easy_pace_from_5k(five_k_seconds: int) -> int:
    """Seconds/km for easy and long running, from a 5k time (see EASY_FACTOR)."""
    return _round_to(five_k_seconds / 5 * EASY_FACTOR, 5)


def tempo_pace_from_5k(five_k_seconds: int) -> int:
    """Seconds/km for a tempo block, from a 5k time (see TEMPO_FACTOR)."""
    return _round_to(five_k_seconds / 5 * TEMPO_FACTOR, 5)


def endurance_watts_from_ftp(ftp_watts: int) -> int:
    """Target watts for steady endurance riding (see ENDURANCE_FTP_FACTOR)."""
    return _round_to(ftp_watts * ENDURANCE_FTP_FACTOR, 5)


def recommended_interval_pace(five_k_seconds: int, rep_distance_m: int) -> int:
    """Seconds/km to target for run intervals of the given rep length, from a
    recent 5k time: 5k pace, discounted by SHORT_REP_FACTOR for reps of
    SHORT_REP_MAX_M or less (see the constants' comment)."""
    pace = five_k_seconds / 5
    if rep_distance_m <= SHORT_REP_MAX_M:
        pace *= SHORT_REP_FACTOR
    return round(pace)


def _best_of_keys(pbs: dict, keys: list[str]) -> tuple[int, str] | None:
    """Fastest (seconds, date) across dedicated/split PB key pairs."""
    candidates = [
        (pbs[key], pbs.get(key.replace("_seconds", "_date"), ""))
        for key in keys
        if pbs.get(key) is not None
    ]
    return min(candidates) if candidates else None


def derive_run_5k(recent: list[dict]) -> tuple[int, str] | None:
    """(seconds, why) for the best recent 5k — dedicated or split — falling
    back to the fastest average pace of any recent >=3km run treated as 5k
    pace."""
    pbs = compute.all_personal_bests(recent).get("run", {})
    best = _best_of_keys(pbs, ["fastest_5k_seconds", "fastest_5k_split_seconds"])
    if best:
        seconds, pb_date = best
        return seconds, f"best recent 5k {_format_mmss(seconds)} ({pb_date})"
    runs = [
        a
        for a in recent
        if a.get("type") == "run"
        and (a.get("distance_km") or 0) >= 3
        and a.get("duration_seconds")
    ]
    if not runs:
        return None
    fastest = min(runs, key=lambda a: a["duration_seconds"] / a["distance_km"])
    pace = fastest["duration_seconds"] / fastest["distance_km"]
    return (
        round(pace * 5),
        f"fastest recent run pace {_format_mmss(round(pace))}/km ({fastest.get('date')})",
    )


def _two_point_css(
    short: tuple | None, long: tuple | None, short_m: int, long_m: int
) -> int | None:
    """Seconds per 100m from two best efforts, or None when the pair is not
    comparable. short/long are (seconds, date) as _best_of_keys returns.

    css = (t_long - t_short) / ((long_m - short_m) / 100). The model assumes
    both are maximal efforts from the same fitness, and says nothing sensible
    when they are not — so the pair is rejected unless the shorter effort is
    genuinely faster per 100m, by a believable margin, within
    CSS_PAIR_MAX_DAYS of the other. A missing date counts as not comparable:
    unknown provenance is the conservative reading."""
    if not short or not long:
        return None
    short_seconds, short_date = short
    long_seconds, long_date = long

    short_pace = short_seconds / (short_m / 100)
    long_pace = long_seconds / (long_m / 100)
    ratio = short_pace / long_pace
    if not CSS_PAIR_MIN_RATIO <= ratio < 1:
        return None

    if not short_date or not long_date:
        return None
    try:
        apart = abs(
            (date.fromisoformat(long_date) - date.fromisoformat(short_date)).days
        )
    except ValueError:
        return None
    if apart > CSS_PAIR_MAX_DAYS:
        return None

    return round((long_seconds - short_seconds) / ((long_m - short_m) / 100))


def derive_swim_css(recent: list[dict]) -> tuple[int, str] | None:
    """(seconds per 100m, why) — two-point critical-speed model over the
    best recent 500m and 1k times (same math as the classic 400/200 CSS
    test), falling back to 1k pace, then to the median pace of recent
    swims."""
    pbs = compute.all_personal_bests(recent).get("swim", {})
    t500 = _best_of_keys(pbs, ["fastest_500m_seconds", "fastest_500m_split_seconds"])
    t1k = _best_of_keys(pbs, ["fastest_1k_seconds", "fastest_1k_split_seconds"])
    css = _two_point_css(t500, t1k, 500, 1000)
    if css is not None:
        return (
            css,
            f"CSS from best recent 500m ({_format_mmss(t500[0])}) and 1k ({_format_mmss(t1k[0])})",
        )
    if t1k:
        return (
            round(t1k[0] / 10),
            f"best recent 1k pace ({_format_mmss(t1k[0])}, {t1k[1]})",
        )
    swims = [
        a
        for a in recent
        if a.get("type") == "swim"
        and a.get("distance_km")
        and a.get("duration_seconds")
    ]
    if not swims:
        return None
    median = statistics.median(
        a["duration_seconds"] / (a["distance_km"] * 10) for a in swims
    )
    return round(median), "median pace of recent swims"


def derive_ride_watts(recent: list[dict]) -> tuple[int, str] | None:
    """(watts, why) — FTP, from the best 20-minute power across recent rides
    where that is available, at the conventional 95% of it.

    Falls back to whole-activity avg_power for rides imported before power
    windows were extracted, or from a format that carries no per-point power.
    That fallback is used **unadjusted**: a long ride's average already sits
    well below threshold, and discounting it again would stack two
    conservative estimates. The 95% belongs only to a genuine 20-minute
    effort."""
    windowed = [
        (a["best_power"]["20min"], a)
        for a in recent
        if a.get("type") == "cycle" and (a.get("best_power") or {}).get("20min")
    ]
    if windowed:
        watts, best = max(windowed, key=lambda pair: pair[0])
        return (
            _round_to(watts * FTP_FROM_20MIN, 5),
            f"95% of your best recent 20min power ({watts}W, {best.get('date')})",
        )

    rides = [
        a
        for a in recent
        if a.get("type") == "cycle"
        and a.get("avg_power")
        and (a.get("duration_seconds") or 0) >= RIDE_POWER_MIN_SECONDS
    ]
    if not rides:
        return None
    best = max(rides, key=lambda a: a["avg_power"])
    return (
        _round_to(best["avg_power"], 5),
        f"best whole-ride average of recent sustained rides "
        f"({best['avg_power']}W, {best.get('date')}) — no 20min power recorded",
    )


_DERIVERS = {
    "run": derive_run_5k,
    "swim": derive_swim_css,
    "cycle": derive_ride_watts,
}

# How each sport's target reads back in a rejection message.
_TARGET_UNITS = {
    "run": lambda v: _format_mmss(v),
    "swim": lambda v: f"{_format_mmss(v)}/100m",
    "cycle": lambda v: f"{v}W",
}


def derive_target(sport: str, recent: list[dict]) -> dict:
    """{"value": int | None, "why": str} — the plausibility-guarded front door
    to the three derive_* helpers, and the one callers should use.

    value is None both when there was nothing to derive from and when what was
    derived falls outside PLAUSIBLE_TARGETS; why says which, so a rejected
    measurement is never silently swapped for a default. Callers substitute
    their own documented fallback."""
    derived = _DERIVERS[sport](recent)
    if derived is None:
        return {"value": None, "why": "no recent history to derive from"}

    value, why = derived
    value = int(round(value))
    low, high = PLAUSIBLE_TARGETS[sport]
    if not low <= value <= high:
        unit = _TARGET_UNITS[sport]
        return {
            "value": None,
            "why": (
                f"{why} gave {unit(value)}, outside the plausible "
                f"{unit(low)}–{unit(high)} — ignored"
            ),
        }
    return {"value": value, "why": why}


def _guarded(derived: dict) -> tuple[int, str] | None:
    """derive_target's dict back in the (value, why) shape recommend_defaults
    reads, or None when the measurement was missing or rejected."""
    if derived["value"] is None:
        return None
    return derived["value"], derived["why"]


def recommend_defaults(
    sport: str,
    workout_type: str,
    activities: list[dict],
    previous_plans: list[dict],
    reference: date,
) -> dict:
    """{param_key: {"default": value, "why": provenance string}} for every
    prompt default derivable from the last RECENT_MONTHS of history; keys
    with nothing to derive from are simply absent (static spec defaults
    stand). Values are in the prompt's own input format (pace strings,
    ints), ready to substitute into workout_params' specs.

    Run intervals' target_pace carries "derive" (a callable taking the
    params answered so far, returning the default string) instead of a
    static "default", because its value depends on the rep distance the
    user hasn't been asked yet — cli._prompt_params resolves it at prompt
    time."""
    recent = recent_activities(activities, reference)
    recs: dict = {}

    if sport == "run" and workout_type in ("intervals", "tempo", "easy", "long"):
        run_ref = _guarded(derive_target("run", recent))
        if run_ref:
            five_k_seconds, why = run_ref
            if workout_type in ("easy", "long"):
                recs["target_pace"] = {
                    "default": _format_mmss(easy_pace_from_5k(five_k_seconds)),
                    "why": f"~30% slower than 5k race pace — {why}",
                }
            elif workout_type == "intervals":
                # Daniels "I" pace ~ 3k-5k race pace; short reps get the
                # SHORT_REP_FACTOR discount (see recommended_interval_pace)
                def _interval_pace_default(params_so_far: dict) -> str:
                    rep_m = params_so_far.get("rep_distance_m", 800)
                    return _format_mmss(
                        recommended_interval_pace(five_k_seconds, rep_m)
                    )

                recs["target_pace"] = {
                    "derive": _interval_pace_default,
                    "why": f"5k race pace (3% faster for reps ≤{SHORT_REP_MAX_M}m) — {why}",
                }
            else:
                recs["target_pace"] = {
                    "default": _format_mmss(tempo_pace_from_5k(five_k_seconds)),
                    "why": f"~7% slower than 5k race pace — {why}",
                }
    elif sport == "swim" and workout_type in ("intervals", "continuous"):
        swim_ref = _guarded(derive_target("swim", recent))
        if swim_ref:
            css, why = swim_ref
            recs["target_pace_100m"] = {"default": _format_mmss(css), "why": why}
    elif sport == "cycle" and workout_type in ("intervals", "endurance", "long"):
        ride_ref = _guarded(derive_target("cycle", recent))
        if ride_ref:
            watts, why = ride_ref
            if workout_type in ("endurance", "long"):
                recs["target_watts"] = {
                    "default": endurance_watts_from_ftp(watts),
                    "why": f"~70% of threshold — {why}",
                }
            else:
                recs["target_watts"] = {"default": watts, "why": why}

    # Build-by-one is an *interval* progression: add a rep to the set of reps.
    # Straight sets progress by load instead — 3x10 stays 3x10 and the bar gets
    # heavier — so recommending an eleventh rep there would be wrong training
    # advice, not just an inapplicable default.
    same_type = [
        p
        for p in previous_plans
        if sport != "strength"
        and p.get("sport") == sport
        and p.get("workout_type") == workout_type
        and isinstance(p.get("params", {}).get("reps"), int)
    ]
    if same_type:
        last = max(same_type, key=lambda p: p.get("id", ""))
        last_reps = last["params"]["reps"]
        if last_reps < REPS_CAP:
            recs["reps"] = {
                "default": last_reps + 1,
                "why": f"one more than your last {sport} {workout_type} plan ({last_reps} reps)",
            }

    return recs
