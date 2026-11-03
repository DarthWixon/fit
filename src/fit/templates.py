"""The training content behind `fit train`: the one week the plan repeats —
StrongLifts 5x5 on Mon/Wed/Fri around easy riding — and how each session grows.

Imports nothing from fit, which is the point: changing the week never touches
the engine that expands it. training.py says what the engine does with it.
"""

TITLE = "StrongLifts 5x5 with easy riding"

# Ride sizes grow by this much a week, compounding. There is no event to peak
# for, so no recovery weeks or taper either: the bar moves only by StrongLifts'
# own rule, and the rides climb steadily from the opening week.
WEEKLY_RAMP_PCT = 4

# Training days when the description does not say; Tuesday is the one day off.
DAYS_PER_WEEK = 6


def _scale(param: str, base: int, low: int, high: int, step: int) -> dict:
    """The one param that grows with the week's multiplier, and its clamp."""
    return {"param": param, "base": base, "min": low, "max": high, "step": step}


def _session(
    sport: str,
    session_type: str,
    day: int,
    priority: int,
    scale: dict | None = None,
    week_parity: int | None = None,
    **params,
) -> dict:
    """One weekly session.

    day       0=Mon
    priority  1 = drop last, when days_per_week trims the week; a description
              building days_per_week adds sessions in this order
    scale     the one param that grows, or None. Only strength is None: its
              progression is load, and ramping sets too would be two
              progressions at once
    week_parity
              0 or 1 to run only in alternate weeks (counting week 1 as 0), so
              StrongLifts can alternate its workouts; None runs every week
    params    fixed planner.build_plan params
    """
    return {
        "sport": sport,
        "session_type": session_type,
        "day": day,
        "priority": priority,
        "week_parity": week_parity,
        "params": params,
        "scale": scale,
    }


def _alternating(day: int, priority: int, first: list, second: list) -> list[dict]:
    """One lifting day that swaps workouts week by week: `first` in week 1,
    `second` in week 2, and so on."""
    return [
        _session(
            "strength",
            "straight_sets",
            day=day,
            priority=priority,
            week_parity=parity,
            rest=180,
            exercises=exercises,
        )
        for parity, exercises in ((0, first), (1, second))
    ]


# StrongLifts 5x5: A and B alternate every time you lift, so Mon/Wed/Fri runs
# A/B/A one week and B/A/B the next. Squat, then the press, then the pull,
# which is the programme's own order. Deadlift is a single work set, as the
# programme has it: the heaviest lift, straight after five sets of squats. One
# rep scheme per lift across both workouts, which
# training.next_working_weight depends on.
_STRONGLIFTS_A = [
    {"exercise": "squat", "sets": 5, "reps": 5},
    {"exercise": "bench_press", "sets": 5, "reps": 5},
    {"exercise": "row", "sets": 5, "reps": 5},
]
# Pull-ups are B's assistance work, where the programme suggests them: A
# already pulls with the row. Jump to the top, lower over 3-5s; reps rise to the
# cap, which is the point to move on to strict ones.
_STRONGLIFTS_B = [
    {"exercise": "squat", "sets": 5, "reps": 5},
    {"exercise": "shoulder_press", "sets": 5, "reps": 5},
    {"exercise": "deadlift", "sets": 1, "reps": 5},
    {
        "exercise": "pull_up",
        "variant": "jumping_pull_ups",
        "rest": 90,
        "sets": 3,
        "reps": 3,
        "rep_cap": 8,
    },
]

# Every ride is easy and untargeted. Saturday ranks last, so a description
# building days_per_week from 5 to 6 adds it later.
WEEKLY_SESSIONS = [
    _session(
        "cycle",
        "long",
        day=6,
        priority=1,
        scale=_scale("distance_m", 40000, 25000, 90000, 500),
    ),
    *_alternating(0, 2, _STRONGLIFTS_A, _STRONGLIFTS_B),
    *_alternating(2, 3, _STRONGLIFTS_B, _STRONGLIFTS_A),
    *_alternating(4, 4, _STRONGLIFTS_A, _STRONGLIFTS_B),
    _session(
        "cycle",
        "endurance",
        day=3,
        priority=5,
        scale=_scale("duration_minutes", 50, 30, 90, 5),
    ),
    _session(
        "cycle",
        "long",
        day=5,
        priority=6,
        scale=_scale("distance_m", 30000, 20000, 70000, 500),
    ),
]
