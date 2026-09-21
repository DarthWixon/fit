"""Pure functions building Garmin workout-service payloads as plain dicts.
No I/O, no network, no garminconnect import — cli.py wires prompts and the
push around this.

Schema replicated from garminconnect 0.3.6's workout.py models rather than
imported (that needs the pydantic extra fit doesn't carry).

Verified by live round-trip (scripts/diff_workout.py):
  run/intervals          2026-08-24  targetValueOne/Two = low/high m/s, in
                                     that order; step numbering intact
  strength/straight_sets 2026-09-05  weightValue is kilograms (62.5 came back
                                     as 62.5), reps end condition, timed rest,
                                     bare category
  strength|cycle/baseline 2026-09-05 untargeted top set / open-target block
  cycle/long             2026-09-11  power.zone id 2 correct; targetValueOne/
                                     Two = low/high watts on a top-level step;
                                     one-step workout with a target accepted
Still unverified: run easy/long, swim continuous, the untargeted easy rides
(cycle endurance/long), and the strength warmup ramp. The run/swim steady
combos pair a nested-proven pace.zone with a power-proven top-level position.
The easy rides are one untargeted step, the shape strength/baseline proved.
The ramp sends a lift category and a weightValue on a *warmup* step: the
2026-09-05 dump proved each half alone (category + weight on an interval step,
CARDIO on a warmup step) but never together, so Connect may blank one of them.
Re-run the diff after first pushing any unverified combo, and update this.
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
    "run": ["intervals", "tempo", "baseline", "easy", "long"],
    "swim": ["intervals", "continuous", "baseline"],
    "cycle": ["intervals", "baseline", "endurance", "long"],
    "strength": ["straight_sets", "baseline"],
}

# Not Garmin's whole catalogue: an unknown category is silently blanked on the
# watch, so a typo would become a nameless step. Same vocabulary importers.py
# stores, uppercased for the payload.
STRENGTH_CATEGORIES = ("deadlift", "squat", "bench_press", "shoulder_press")

# Quality workouts (intervals/tempo/baseline) are warmup -> main ->
# cooldown. The steady types above (easy/long/endurance/continuous) are a
# single block instead: a warmup inside an easy run is just more easy running,
# and splitting it only makes the watch beep for no reason. Each has its own
# entry in _BUILDERS, so nothing branches on a set of their names.

# A band around the given value, so the watch beeps against a corridor.
PACE_TOLERANCE_S_PER_KM = 10
SWIM_PACE_TOLERANCE_S_PER_100M = 5
POWER_TOLERANCE_W = 10

# Wider for steady work: an easy run holds a range, not a pace to the second.
# Easy rides carry no target at all — a power band only makes the watch beep.
EASY_PACE_TOLERANCE_S_PER_KM = 20

# recommend_defaults knobs. TEMPO_FACTOR: Daniels' T is ~7-8% slower than
# I/5k pace. REPS_CAP bounds the build-by-one rep progression.
RECENT_MONTHS = 6
TEMPO_FACTOR = 1.07
REPS_CAP = 10

# Daniels' E pace is ~30% slower than 5k.
EASY_FACTOR = 1.3

# Short reps get a ~3% discount: the reference 5k is a *training* best, so it
# understates race ability and would otherwise sit below Daniels' I bracket.
SHORT_REP_MAX_M = 1000
SHORT_REP_FACTOR = 0.97

# Reject the impossible, not the unusual: a derivation from mismatched efforts
# can prescribe work nobody can complete. An explicit `targets:` is the user's
# own word and is NOT bounded by these.
PLAUSIBLE_TARGETS = {
    "run": (750, 3600),  # 5k seconds: 2:30/km world record .. 12:00/km walking
    "swim": (60, 240),  # seconds per 100m: elite .. very slow
    "cycle": (60, 500),  # watts: not a training target .. world class
}

# derive_swim_css guards: the shorter effort must actually be faster per 100m
# and be recent enough to be comparable. A 2021 drill 500m against a 2025 1k
# once produced a 35s/100m CSS.
CSS_PAIR_MIN_RATIO = 0.75
CSS_PAIR_MAX_DAYS = 90

FTP_FROM_20MIN = 0.95  # conventional correction on a 20-min best effort
# Just under 20 min: a watch stopped on the beep records 1199s, and
# disqualifying a test that was done is the worst failure available.
RIDE_POWER_MIN_SECONDS = 1140

# Assumed for estimatedDurationInSecs when a distance step has no pace target.
_FALLBACK_SPEED_MPS = {"run": 1000 / 360, "swim": 100 / 120, "cycle": 25 / 3.6}

# Strength equivalents: reps and lap-button rests carry no duration, and
# training.py sums estimate_seconds, so a gym session would otherwise be free.
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

# Load is not a target: the live dump has "no.target" on a step carrying
# weightValue/weightUnit. weightValue is kg — the factor is metadata.
_WEIGHT_UNIT_KG = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}

# Inert (the step ends on the lap press), but Connect writes 10.0 rather than
# 0. Sent as observed.
_LAP_BUTTON_END_VALUE = 10.0

# A lifting warmup is a ramp to the working weight, not cardio: the empty bar,
# then percentages of the work set at descending reps. Rounded to 2.5kg rather
# than training's whole-kg-or-2.5kg rule — a warmup set does not need 1kg
# precision, and planner may not import training.
_BAR_WEIGHT_KG = 20.0
_WARMUP_ROUNDING_KG = 2.5
_WARMUP_RAMP = ((0.55, 5), (0.70, 3), (0.85, 2))
_BAR_SET_REPS = 5


def parse_pace(text: str) -> int:
    """'4:30' -> 270. Strict m:ss; unit is the caller's."""
    minutes, _, seconds = text.strip().partition(":")
    if not minutes.isdigit() or not seconds.isdigit() or len(seconds) != 2:
        raise ValueError(f"invalid pace '{text}': expected m:ss, e.g. '4:30'")
    total = int(minutes) * 60 + int(seconds)
    if int(seconds) > 59 or total == 0:
        raise ValueError(f"invalid pace '{text}': expected m:ss, e.g. '4:30'")
    return total


def parse_duration(text: str) -> int:
    """'90' -> 90, '2:00' -> 120 seconds."""
    text = text.strip()
    if ":" in text:
        return parse_pace(text)
    if not text.isdigit() or int(text) == 0:
        raise ValueError(
            f"invalid duration '{text}': expected seconds or m:ss, e.g. '90' or '1:30'"
        )
    return int(text)


def parse_schedule_date(text: str) -> str:
    """Validate and normalise a YYYY-MM-DD date. The single seam every
    scheduled date passes through before garmin.schedule_workout. No
    past/future policy: a scheduler may legitimately place one in the past."""
    text = text.strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(
            f"invalid schedule date '{text}': expected YYYY-MM-DD, e.g. '2026-08-28'"
        ) from exc


def parse_distance_km(text: str) -> int:
    """'16'/'16.5' -> metres. Steady prompts ask in km; payloads want metres."""
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
    """Blank -> 0, meaning "omit the step" rather than a zero-length one."""
    text = str(text).strip()
    return _positive_int(text) if text else 0


def _optional_pace(text: str) -> int | None:
    return parse_pace(text) if str(text).strip() else None


def parse_exercise(text: str) -> str:
    """'Back Squat' -> 'squat', validated against STRENGTH_CATEGORIES. Garmin
    blanks an unknown exercise rather than rejecting it, so a typo would reach
    the watch as a nameless step."""
    name = str(text).strip().lower().replace(" ", "_").replace("-", "_")
    if name not in STRENGTH_CATEGORIES:
        raise ValueError(
            f"unknown exercise '{text}': expected one of "
            f"{', '.join(STRENGTH_CATEGORIES)}"
        )
    return name


def parse_weight_kg(text: str) -> float | None:
    """Kilograms; blank -> None, meaning no prescribed load."""
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
    """Blank -> 0, meaning a press-lap rest rather than a timed one."""
    text = str(text).strip()
    return parse_duration(text) if text else 0


def pace_zone_mps(
    seconds_per_km: int, tolerance_s: int = PACE_TOLERANCE_S_PER_KM
) -> tuple[float, float]:
    """(low, high) m/s for a pace ± tolerance band."""
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

# One spec per prompt: {key, label, default, parse}. parse raises ValueError
# to re-prompt.
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
    # Keeps warmup/cooldown: fastest_split isolates the 5k from what surrounds
    # it. 5000 because 3km is in neither SPLIT_DISTANCES_KM nor MILESTONES_KM.
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
    # best_power_window finds the effort wherever it sits, so warmup/cooldown
    # are fine here. Blank still means "no step".
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
    ],
    ("cycle", "long"): [
        {
            "key": "distance_m",
            "label": "Distance (km)",
            "default": 60,
            "parse": parse_distance_km,
        },
    ],
    # Bare, unlike cycle: no power-window equivalent, so the whole-swim
    # milestone is the measurement — and pool swims often carry no stream.
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
    # The only combo whose params aren't one flat answer per prompt: the
    # per-exercise half is in EXERCISE_PARAM_SPECS, prompted in a loop. Nothing
    # asks about the warmup — _warmup_sets derives the ramp from the load.
    ("strength", "straight_sets"): [
        {
            "key": "rest",
            "label": "Rest between sets (min:sec, blank for press-lap)",
            "default": "1:30",
            "parse": _optional_rest,
        },
    ],
    # The e1RM re-test. No load, so no ramp can be derived: the lighter sets
    # are the athlete's own, and _strength_pbs reads only the best set.
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

# Asked repeatedly until the exercise is blank. Out of _PARAM_SPECS because
# each answer becomes a list entry, not a params key.
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
    """(params key, specs) for a combo whose prompts repeat into a list, else
    ("", []). Exists so cli.py asks whether a combo loops rather than naming
    which one does."""
    if (sport, workout_type) == ("strength", "straight_sets"):
        return "exercises", [dict(spec) for spec in EXERCISE_PARAM_SPECS]
    return "", []


def workout_params(sport: str, workout_type: str) -> list[dict]:
    """Prompt specs for one combo; ValueError listing valid combos otherwise.
    The single validation point for --sport/--type."""
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


def _power_target(watts: int) -> dict:
    return {
        "targetType": {
            "workoutTargetTypeId": 2,
            "workoutTargetTypeKey": "power.zone",
            "displayOrder": 2,
        },
        "targetValueOne": watts - POWER_TOLERANCE_W,
        "targetValueTwo": watts + POWER_TOLERANCE_W,
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


def _baseline_steps(
    warmup: float,
    main_key: str,
    main_value: float,
    cooldown: float,
    wrap_key: str = "time",
) -> list[dict]:
    """warmup -> effort -> cooldown, omitting a zero wrap entirely rather than
    emitting a zero-length step, and renumbering what remains.

    Whether a test can afford wrapping is a property of the sport: the recorded
    activity must BE the test unless the split machinery can isolate it."""
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
    """A bare test's warm-up instruction. The name is what the athlete reads;
    description is fixed at build_plan, so nothing else can carry it."""
    return "" if warmup else " (warm up first)"


def _run_baseline(params: dict) -> tuple[str, list[dict]]:
    # Best-effort fixed distance, open target. Keeps its wrap (_baseline_steps).
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
    # FTP-test shape: sustained best effort, open target.
    steps = _baseline_steps(
        params.get("warmup_minutes", 0) * 60,
        "time",
        params["test_minutes"] * 60,
        params.get("cooldown_minutes", 0) * 60,
    )
    name = f"Cycle baseline {params['test_minutes']}min test"
    return name + _bare_suffix(params.get("warmup_minutes", 0)), steps


def _swim_baseline(params: dict) -> tuple[str, list[dict]]:
    # Pool time trial, open target. Bare when the plan schedules it: the
    # whole-activity milestone is the only reliable measurement.
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
    minutes = params["duration_minutes"]
    steps = [_step(1, "interval", "time", minutes * 60, _no_target())]
    return f"Cycle endurance {minutes}min easy", steps


def _cycle_long(params: dict) -> tuple[str, list[dict]]:
    distance = params["distance_m"]
    steps = [_step(1, "interval", "distance", distance, _no_target())]
    return f"Cycle long {_format_meters(distance)} easy", steps


def _swim_continuous(params: dict) -> tuple[str, list[dict]]:
    pace = params.get("target_pace_100m")
    target = _pace_target(*swim_pace_zone_mps(pace)) if pace else _no_target()
    steps = [_step(1, "interval", "distance", params["distance_m"], target)]
    name = f"Swim continuous {_format_meters(params['distance_m'])}"
    if pace:
        name += f" @ {_format_mmss(pace)}/100m"
    return name, steps


def _weight_fields(weight_kg: float | None) -> dict:
    """Omitted entirely when there is no load — an unloaded step is a
    bodyweight or work-up set, not a 0kg one."""
    if not weight_kg:
        return {}
    return {"weightValue": float(weight_kg), "weightUnit": dict(_WEIGHT_UNIT_KG)}


def _lift_step(
    order: int,
    exercise: str,
    reps: int,
    weight_kg: float | None,
    kind: str = "interval",
) -> dict:
    """One set: reps as the end condition, load on the step itself.
    `exerciseName` (the variant) is left unset — every lift fit plans is a
    category in its own right, and Connect accepts a bare one. `kind` is
    "warmup" for a ramp set, which differs from a working set in nothing but
    the step type."""
    step = _step(order, kind, "reps", reps, _no_target())
    step["category"] = exercise.upper()
    step.update(_weight_fields(weight_kg))
    return step


def _warmup_sets(working_kg: float | None) -> list[tuple[float, int]]:
    """The ramp up to a working weight: the empty bar, then percentages of the
    work set at descending reps. A lifting warmup is lighter lifting — there is
    nothing to ramp to without a prescribed load, and warming up heavier than
    you lift is not a warmup.

    Kept sets are strictly heavier than the last and strictly lighter than the
    work set, which is the whole of the degenerate-case handling: a 35kg bench
    rounds 55% back onto the bar and simply loses that rung."""
    if not working_kg or working_kg <= _BAR_WEIGHT_KG:
        return []

    sets = [(_BAR_WEIGHT_KG, _BAR_SET_REPS)]
    for fraction, reps in _WARMUP_RAMP:
        rungs = round(working_kg * fraction / _WARMUP_ROUNDING_KG)
        weight = round(rungs * _WARMUP_ROUNDING_KG, 2)
        if sets[-1][0] < weight < working_kg:
            sets.append((weight, reps))
    return sets


def _rest_step(order: int, rest_seconds: int) -> dict:
    """Timed if given a duration, else press-lap — how Connect's UI builds it."""
    if rest_seconds:
        return _step(order, "rest", "time", rest_seconds, _no_target())
    return _step(order, "rest", "lap.button", _LAP_BUTTON_END_VALUE, _no_target())


def _describe_exercise(name: str) -> str:
    return name.replace("_", " ")


def _strength_straight_sets(params: dict) -> tuple[str, list[dict]]:
    """Each exercise ramps to its working weight, then one RepeatGroupDTO for
    the work sets — siblings in the segment, numbered globally, which is the
    shape a real Connect strength workout has.

    The ramp is per exercise because a different movement needs its own: you
    do not bench cold because you squatted first. Its rests are press-lap —
    warming up is self-paced."""
    exercises = params.get("exercises") or []
    if not exercises:
        raise ValueError("a straight-sets workout needs at least one exercise")

    steps: list[dict] = []
    order = 1
    rest = params.get("rest", 0)
    for exercise in exercises:
        weight = exercise.get("target_weight_kg")
        for ramp_weight, ramp_reps in _warmup_sets(weight):
            steps.append(
                _lift_step(
                    order, exercise["exercise"], ramp_reps, ramp_weight, "warmup"
                )
            )
            steps.append(_rest_step(order + 1, 0))
            order += 2

        steps.append(
            _repeat(
                order,
                exercise["sets"],
                [
                    _lift_step(
                        order + 1,
                        exercise["exercise"],
                        exercise["reps"],
                        weight,
                    ),
                    _rest_step(order + 2, rest),
                ],
            )
        )
        order += 3

    name = "Strength " + ", ".join(
        f"{_describe_exercise(e['exercise'])} {e['sets']}x{e['reps']}"
        + (f" @ {e['target_weight_kg']:g}kg" if e.get("target_weight_kg") else "")
        for e in exercises
    )
    return name, steps


def _strength_baseline(params: dict) -> tuple[str, list[dict]]:
    # One top set and no load — so there is no working weight for _warmup_sets
    # to ramp to, and only the name can carry the warm-up instruction.
    steps = [_lift_step(1, params["exercise"], params["reps"], None)]
    name = (
        f"Strength baseline {_describe_exercise(params['exercise'])} "
        f"{params['reps']}-rep test"
    )
    return name + _bare_suffix(0), steps


_BUILDERS = {
    ("run", "intervals"): _run_intervals,
    ("run", "tempo"): _run_tempo,
    ("run", "baseline"): _run_baseline,
    ("run", "easy"): _run_easy,
    ("run", "long"): _run_long,
    ("swim", "intervals"): _swim_intervals,
    ("swim", "continuous"): _swim_continuous,
    ("cycle", "intervals"): _cycle_intervals,
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
            # No duration to read; the alternative is counting it as free.
            total += _FALLBACK_REST_SECONDS
        else:  # distance: estimate via the pace target's midpoint if present
            if step.get("targetType", {}).get("workoutTargetTypeKey") == "pace.zone":
                speed = (step["targetValueOne"] + step["targetValueTwo"]) / 2
            else:
                speed = _FALLBACK_SPEED_MPS[sport]
            total += step["endConditionValue"] / speed
    return total


def workout_name(sport: str, workout_type: str, params: dict) -> str:
    """The workout name without building the payload. training.py stores it per
    session so a plan file needn't carry ~80 payloads."""
    workout_params(sport, workout_type)  # reuse its ValueError on bad combos
    name, _ = _BUILDERS[(sport, workout_type)](params)
    return name


def estimate_seconds(sport: str, workout_type: str, params: dict) -> int:
    """Expected duration without building the payload. training.py sums these
    to size a planned week against the user's actual volume."""
    workout_params(sport, workout_type)  # reuse its ValueError on bad combos
    _, steps = _BUILDERS[(sport, workout_type)](params)
    return round(_estimate_seconds(steps, sport))


def build_plan(sport: str, workout_type: str, params: dict, created: str) -> dict:
    """The saved-plan dict. created (ISO seconds) becomes the id/filename;
    "payload" is ready for garmin.push_workout."""
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
    """Lifting counterpart to _describe_target: weight rides on the step."""
    weight = step.get("weightValue")
    return f" @ {weight:g}kg" if weight else ""


def _step_kind(step: dict) -> str:
    return step["stepType"]["stepTypeKey"]


def _describe_step(step: dict, sport: str) -> str:
    kind = _step_kind(step)
    body = _describe_extent(step) + _describe_target(step, sport) + _describe_load(step)
    category = step.get("category")
    if category:
        body = f"{_describe_exercise(category.lower()).capitalize()} {body}"
    if kind in ("warmup", "cooldown"):
        return f"{kind.capitalize()} {body}"
    if kind in ("recovery", "rest"):
        return f"{body} {kind}"
    return body  # interval


def describe_plan(plan: dict) -> list[str]:
    """One line per top-level step, e.g. "6 x 800m @ 4:20-4:40/km, 2:00 recovery".

    A top-level rest belongs to the warmup set before it — a lifting ramp is
    read as set-and-rest, the same way a repeat group folds its children onto
    one line."""
    sport = plan["sport"]
    steps = plan["payload"]["workoutSegments"][0]["workoutSteps"]
    lines = []
    for index, step in enumerate(steps):
        if step["type"] == "RepeatGroupDTO":
            children = ", ".join(
                _describe_step(child, sport) for child in step["workoutSteps"]
            )
            lines.append(f"{step['numberOfIterations']} x {children}")
        elif (
            index > 0
            and _step_kind(step) == "rest"
            and _step_kind(steps[index - 1]) == "warmup"
        ):
            lines[-1] += f", {_describe_step(step, sport)}"
        else:
            lines.append(_describe_step(step, sport))
    return lines


# --- history-derived recommended defaults ---------------------------------


def _round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


def recent_activities(activities: list[dict], reference: date) -> list[dict]:
    """The last RECENT_MONTHS — the window every derive_* helper expects.
    Public so training.py windows history identically."""
    return compute.filter_by_date(
        activities, compute.months_ago(reference, RECENT_MONTHS), reference.isoformat()
    )


def easy_pace_from_5k(five_k_seconds: int) -> int:
    """Seconds/km for easy and long running (EASY_FACTOR)."""
    return _round_to(five_k_seconds / 5 * EASY_FACTOR, 5)


def tempo_pace_from_5k(five_k_seconds: int) -> int:
    """Seconds/km for a tempo block (TEMPO_FACTOR)."""
    return _round_to(five_k_seconds / 5 * TEMPO_FACTOR, 5)


def recommended_interval_pace(five_k_seconds: int, rep_distance_m: int) -> int:
    """5k pace, discounted by SHORT_REP_FACTOR for short reps."""
    pace = five_k_seconds / 5
    if rep_distance_m <= SHORT_REP_MAX_M:
        pace *= SHORT_REP_FACTOR
    return round(pace)


def _best_of_keys(pbs: dict, keys: list[str]) -> tuple[int, str] | None:
    """Fastest (seconds, date) across the given PB keys."""
    candidates = [
        (pbs[key], pbs.get(key.replace("_seconds", "_date"), ""))
        for key in keys
        if pbs.get(key) is not None
    ]
    return min(candidates) if candidates else None


def derive_run_5k(recent: list[dict]) -> tuple[int, str] | None:
    """(seconds, why) for the best recent 5k, dedicated or split; falls back to
    the fastest average pace of any recent >=3km run."""
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
    """Seconds/100m from two efforts, or None when the pair is not comparable.

    The model assumes both are maximal efforts from the same fitness, so the
    pair is rejected unless the shorter is genuinely faster per 100m, by a
    believable margin, within CSS_PAIR_MAX_DAYS. A missing date counts as not
    comparable."""
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
    """(seconds/100m, why) — two-point critical speed over the best recent 500m
    and 1k; falls back to 1k pace, then median recent swim pace."""
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
    """(watts, why) — FTP as 95% of the best recent 20-minute power.

    Falls back to whole-activity avg_power **unadjusted**: a long ride's
    average is already sub-threshold, and discounting it again would stack two
    conservative estimates. The 95% belongs only to a real 20-minute effort."""
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

# How each target reads back in a rejection message.
_TARGET_UNITS = {
    "run": lambda v: _format_mmss(v),
    "swim": lambda v: f"{_format_mmss(v)}/100m",
    "cycle": lambda v: f"{v}W",
}


def derive_target(sport: str, recent: list[dict]) -> dict:
    """{"value", "why", "rejected"} — the plausibility-guarded front door to
    the derive_* helpers, and the one callers should use.

    value is None both when there was nothing to measure and when what was
    measured fell outside PLAUSIBLE_TARGETS; "rejected" separates the two, so a
    thrown-out reading is never silently swapped for a default."""
    derived = _DERIVERS[sport](recent)
    if derived is None:
        return {
            "value": None,
            "why": "no recent history to derive from",
            "rejected": False,
        }

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
            "rejected": True,
        }
    return {"value": value, "why": why, "rejected": False}


def _rejection_note(derived: dict) -> dict | None:
    """A why-only rec for a measurement the guard threw out, else None. Without
    it `fit plan` fell back to the static default in silence while `fit train`
    reported the same rejection."""
    if not derived.get("rejected"):
        return None
    return {"why": derived["why"]}


def recommend_defaults(
    sport: str,
    workout_type: str,
    activities: list[dict],
    previous_plans: list[dict],
    reference: date,
) -> dict:
    """{param_key: rec} from the last RECENT_MONTHS. Absent keys leave the spec
    default. Values are in the prompt's own input format.

    Three rec shapes, so consumers must test for the key rather than assume:
      {"default", "why"}  a derived value
      {"derive", "why"}   a callable resolved at prompt time from the answers
                          so far (run intervals' pace needs the rep distance)
      {"why"}             a measurement the guard rejected (_rejection_note)"""
    recent = recent_activities(activities, reference)
    recs: dict = {}

    if sport == "run" and workout_type in ("intervals", "tempo", "easy", "long"):
        derived = derive_target("run", recent)
        if derived["value"] is None:
            note = _rejection_note(derived)
            if note:
                recs["target_pace"] = note
        else:
            five_k_seconds, why = derived["value"], derived["why"]
            if workout_type in ("easy", "long"):
                recs["target_pace"] = {
                    "default": _format_mmss(easy_pace_from_5k(five_k_seconds)),
                    "why": f"~30% slower than 5k race pace — {why}",
                }
            elif workout_type == "intervals":

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
        derived = derive_target("swim", recent)
        if derived["value"] is None:
            note = _rejection_note(derived)
            if note:
                recs["target_pace_100m"] = note
        else:
            recs["target_pace_100m"] = {
                "default": _format_mmss(derived["value"]),
                "why": derived["why"],
            }
    elif sport == "cycle" and workout_type == "intervals":
        derived = derive_target("cycle", recent)
        if derived["value"] is None:
            note = _rejection_note(derived)
            if note:
                recs["target_watts"] = note
        else:
            recs["target_watts"] = {
                "default": derived["value"],
                "why": derived["why"],
            }

    # Build-by-one is an *interval* progression. Straight sets progress by load
    # instead, so an eleventh rep there would be wrong advice, not just unhelpful.
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
