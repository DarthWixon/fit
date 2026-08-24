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
diff after first pushing an unverified combo.
"""

import statistics
from datetime import date

from fit import compute

SPORT_TYPES = {
    "run": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "cycle": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "swim": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3},
}

WORKOUT_TYPES = {
    "run": ["intervals", "tempo", "hills", "baseline"],
    "swim": ["intervals"],
    "cycle": ["intervals", "hills", "baseline"],
}

# Target zones are a band around the single value the user gives, so the
# watch has a realistic corridor to beep against rather than a knife edge.
PACE_TOLERANCE_S_PER_KM = 10
SWIM_PACE_TOLERANCE_S_PER_100M = 5
POWER_TOLERANCE_W = 10

# History-derived defaults (recommend_defaults): how far back "recent
# performance" looks, how much slower tempo pace is than 5k race pace
# (Daniels: T is 88-92% vVO2max vs I at 95-100%, roughly 7-8% slower), and
# the cap on the build-by-one rep progression.
RECENT_MONTHS = 6
TEMPO_FACTOR = 1.07
REPS_CAP = 10

# Run intervals of SHORT_REP_MAX_M or less default to 5k pace * SHORT_REP_FACTOR
# (~3% faster). The reference 5k is a *training* best, not a race result, so it
# understates race ability — the discount puts short reps back inside Daniels'
# I-pace bracket (3k-5k race pace at 97-100% VO2max) instead of below its slow
# end. Longer reps stay at plain 5k pace, the bracket's safe end.
SHORT_REP_MAX_M = 1000
SHORT_REP_FACTOR = 0.97

# Speeds assumed for estimatedDurationInSecs when a distance step has no
# pace target to average (6:00/km run, 2:00/100m swim).
_FALLBACK_SPEED_MPS = {"run": 1000 / 360, "swim": 100 / 120, "cycle": 25 / 3.6}

_STEP_TYPES = {
    "warmup": 1,
    "cooldown": 2,
    "interval": 3,
    "recovery": 4,
    "rest": 5,
    "repeat": 6,
}
_END_CONDITIONS = {"lap.button": 1, "time": 2, "distance": 3, "iterations": 7}
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


def _positive_int(text: str) -> int:
    value = int(str(text).strip())
    if value <= 0:
        raise ValueError("must be greater than zero")
    return value


def _optional_pace(text: str) -> int | None:
    return parse_pace(text) if str(text).strip() else None


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
    ("run", "baseline"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
        {
            "key": "test_distance_m",
            "label": "Test distance (m)",
            "default": 3000,
            "parse": _positive_int,
        },
        {
            "key": "cooldown_minutes",
            "label": "Cooldown (minutes)",
            "default": 10,
            "parse": _positive_int,
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
    ("cycle", "baseline"): [
        {
            "key": "warmup_minutes",
            "label": "Warmup (minutes)",
            "default": 20,
            "parse": _positive_int,
        },
        {
            "key": "test_minutes",
            "label": "Best-effort test (minutes)",
            "default": 20,
            "parse": _positive_int,
        },
        {
            "key": "cooldown_minutes",
            "label": "Cooldown (minutes)",
            "default": 10,
            "parse": _positive_int,
        },
    ],
}


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


def _run_baseline(params: dict) -> tuple[str, list[dict]]:
    # Runna-style benchmark: a best-effort fixed distance with an open
    # target, used to (re)measure current pace baseline.
    steps = [
        _step(1, "warmup", "time", params["warmup_minutes"] * 60, _no_target()),
        _step(2, "interval", "distance", params["test_distance_m"], _no_target()),
        _step(3, "cooldown", "time", params["cooldown_minutes"] * 60, _no_target()),
    ]
    return f"Run baseline {_format_meters(params['test_distance_m'])} test", steps


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
    steps = [
        _step(1, "warmup", "time", params["warmup_minutes"] * 60, _no_target()),
        _step(2, "interval", "time", params["test_minutes"] * 60, _no_target()),
        _step(3, "cooldown", "time", params["cooldown_minutes"] * 60, _no_target()),
    ]
    return f"Cycle baseline {params['test_minutes']}min test", steps


_BUILDERS = {
    ("run", "intervals"): _run_intervals,
    ("run", "tempo"): _run_tempo,
    ("run", "hills"): lambda params: _hills("Run", params),
    ("run", "baseline"): _run_baseline,
    ("swim", "intervals"): _swim_intervals,
    ("cycle", "intervals"): _cycle_intervals,
    ("cycle", "hills"): lambda params: _hills("Cycle", params),
    ("cycle", "baseline"): _cycle_baseline,
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
        else:  # distance: estimate via the pace target's midpoint if present
            if step.get("targetType", {}).get("workoutTargetTypeKey") == "pace.zone":
                speed = (step["targetValueOne"] + step["targetValueTwo"]) / 2
            else:
                speed = _FALLBACK_SPEED_MPS[sport]
            total += step["endConditionValue"] / speed
    return total


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
    if step["endCondition"]["conditionTypeKey"] == "time":
        return _format_mmss(step["endConditionValue"])
    return _format_meters(step["endConditionValue"])


def _describe_step(step: dict, sport: str) -> str:
    kind = step["stepType"]["stepTypeKey"]
    body = _describe_extent(step) + _describe_target(step, sport)
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


def _recent_run_5k(recent: list[dict]) -> tuple[int, str] | None:
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


def _recent_swim_css(recent: list[dict]) -> tuple[int, str] | None:
    """(seconds per 100m, why) — two-point critical-speed model over the
    best recent 500m and 1k times (same math as the classic 400/200 CSS
    test), falling back to 1k pace, then to the median pace of recent
    swims."""
    pbs = compute.all_personal_bests(recent).get("swim", {})
    t500 = _best_of_keys(pbs, ["fastest_500m_seconds", "fastest_500m_split_seconds"])
    t1k = _best_of_keys(pbs, ["fastest_1k_seconds", "fastest_1k_split_seconds"])
    if t500 and t1k and t1k[0] > t500[0]:
        css = round((t1k[0] - t500[0]) / 5)
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


def _recent_ride_watts(recent: list[dict]) -> tuple[int, str] | None:
    """(watts, why) — max avg_power over recent rides of >=20 minutes, a
    coarse FTP proxy (only session averages are stored)."""
    rides = [
        a
        for a in recent
        if a.get("type") == "cycle"
        and a.get("avg_power")
        and (a.get("duration_seconds") or 0) >= 1200
    ]
    if not rides:
        return None
    best = max(rides, key=lambda a: a["avg_power"])
    watts = _round_to(best["avg_power"], 5)
    return (
        watts,
        f"best avg power of recent 20min+ rides ({best['avg_power']}W, {best.get('date')})",
    )


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
    recent = compute.filter_by_date(
        activities, compute.months_ago(reference, RECENT_MONTHS), reference.isoformat()
    )
    recs: dict = {}

    if sport == "run" and workout_type in ("intervals", "tempo"):
        run_ref = _recent_run_5k(recent)
        if run_ref:
            five_k_seconds, why = run_ref
            if workout_type == "intervals":
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
                tempo = _round_to(five_k_seconds / 5 * TEMPO_FACTOR, 5)
                recs["target_pace"] = {
                    "default": _format_mmss(tempo),
                    "why": f"~7% slower than 5k race pace — {why}",
                }
    elif sport == "swim" and workout_type == "intervals":
        swim_ref = _recent_swim_css(recent)
        if swim_ref:
            css, why = swim_ref
            recs["target_pace_100m"] = {"default": _format_mmss(css), "why": why}
    elif sport == "cycle" and workout_type == "intervals":
        ride_ref = _recent_ride_watts(recent)
        if ride_ref:
            watts, why = ride_ref
            recs["target_watts"] = {"default": watts, "why": why}

    same_type = [
        p
        for p in previous_plans
        if p.get("sport") == sport
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
