"""The training content behind `fit train`: one template per goal — plan
length, phase structure and the weekly session mix, plus the two constructors
that build them.

Imports nothing from fit, which is the point: adding or recalibrating a goal
never touches the engine that expands one. These docstrings say what a
template *author* needs; training.py says what the engine does with it.
"""

# Phase lengths sum to weeks - PROGRESSION_DEFAULTS["taper_weeks"] (the taper
# is not listed here). A moved start_date reapportions them, so they need not
# sum exactly — but keeping them tidy makes the intent readable.


def _scale(param: str, base: int, low: int, high: int, step: int) -> dict:
    """The one param that grows with the week's multiplier, and its clamp."""
    return {"param": param, "base": base, "min": low, "max": high, "step": step}


def _session(
    sport: str,
    session_type: str,
    day: int,
    priority: int,
    scale: dict | None = None,
    key: bool = False,
    brick: bool = False,
    **params,
) -> dict:
    """One weekly session template.

    day       0=Mon, in the template's own week; rotated as a whole if the
              description names a different rest_day
    priority  1 = drop last, when days_per_week trims the week. Multi-sport
              goals interleave the sports rather than ranking every long
              session first, so a trimmed week keeps one of each discipline
    key       a hard session; extras are never placed on these days
    brick     runs straight off the session sharing its day
    scale     the one param that grows, or None. Only strength is None: its
              progression is load, and ramping sets too would be two
              progressions at once
    params    fixed build_plan params; the intensity one is filled in by
              training._apply_target
    """
    return {
        "sport": sport,
        "session_type": session_type,
        "day": day,
        "priority": priority,
        "key": key,
        "brick": brick,
        "params": params,
        "scale": scale,
    }


GOAL_TEMPLATES = {
    # --- running ---------------------------------------------------------
    "run_10k": {
        "label": "10k",
        "description": "10km race",
        "weeks": 10,
        "days_per_week": 5,
        "rest_day": 0,
        "phases": [("base", 4), ("build", 3), ("peak", 1)],
        "weekly_sessions": [
            _session(
                "run",
                "long",
                day=5,
                priority=1,
                key=True,
                scale=_scale("distance_m", 10000, 8000, 18000, 250),
            ),
            _session(
                "run",
                "intervals",
                day=1,
                priority=2,
                key=True,
                scale=_scale("reps", 5, 3, 8, 1),
                warmup_minutes=10,
                rep_distance_m=1000,
                recovery=150,
                cooldown_minutes=10,
            ),
            _session(
                "run",
                "tempo",
                day=3,
                priority=3,
                key=True,
                scale=_scale("tempo_minutes", 25, 15, 45, 5),
                warmup_minutes=10,
                cooldown_minutes=10,
            ),
            _session(
                "run",
                "easy",
                day=6,
                priority=4,
                scale=_scale("duration_minutes", 40, 25, 70, 5),
            ),
            _session(
                "run",
                "easy",
                day=2,
                priority=5,
                scale=_scale("duration_minutes", 35, 20, 60, 5),
            ),
        ],
    },
    # --- cycling ---------------------------------------------------------
    "cycle_strength": {
        "label": "Cycling + strength",
        "description": "40km bike time, supported by squat/deadlift/bench/press",
        "weeks": 12,
        "days_per_week": 5,
        "rest_day": 3,  # Thu; Sun is empty too, so the long ride can slide
        "phases": [("base", 5), ("build", 5), ("peak", 2)],
        "weekly_sessions": [
            _session(
                "cycle",
                "long",
                day=5,
                priority=1,
                key=True,
                scale=_scale("distance_m", 40000, 25000, 90000, 500),
            ),
            _session(
                "cycle",
                "intervals",
                day=1,
                priority=3,
                key=True,
                scale=_scale("reps", 4, 2, 6, 1),
                warmup_minutes=15,
                work=360,
                recovery=300,
                cooldown_minutes=10,
            ),
            _session(
                "cycle",
                "intervals",
                day=4,
                priority=2,
                key=True,
                scale=_scale("work", 600, 300, 1500, 30),
                warmup_minutes=15,
                reps=3,
                recovery=420,
                cooldown_minutes=10,
            ),
            # Each day pairs one heavy lower-body lift with a press, rather
            # than stacking squat and deadlift into one session.
            #
            # Mon deadlift, Tue ride, Wed squat, Thu rest, Fri ride, Sat long,
            # Sun rest. With a lift on Monday one ride must follow a lifting
            # day, so it is the hinge, not the squat, and the shorter session.
            # Squat gets Thursday's rest before Friday's longer reps, which
            # outrank Tuesday's so the benchmark replaces Friday: a test the
            # day after deadlifts reads low.
            _session(
                "strength",
                "straight_sets",
                day=2,
                priority=4,
                rest=180,
                exercises=[
                    {"exercise": "squat", "sets": 4, "reps": 6},
                    {"exercise": "bench_press", "sets": 3, "reps": 10},
                ],
            ),
            _session(
                "strength",
                "straight_sets",
                day=0,
                priority=5,
                rest=180,
                exercises=[
                    {"exercise": "deadlift", "sets": 3, "reps": 5},
                    {"exercise": "shoulder_press", "sets": 3, "reps": 10},
                ],
            ),
        ],
    },
    # --- triathlon -------------------------------------------------------
    "standard_triathlon": {
        "label": "Standard triathlon",
        "description": "1.5km swim / 40km bike / 10km run",
        "weeks": 16,
        "days_per_week": 6,
        "rest_day": 0,
        "phases": [("base", 6), ("build", 5), ("peak", 3)],
        "weekly_sessions": [
            # Clamps matter most here: at 16 weeks the ramp compounds to
            # ~2.2x, which without these caps would put a 108km ride and a
            # 22km run in an Olympic-distance plan.
            _session(
                "cycle",
                "long",
                day=5,
                priority=1,
                key=True,
                scale=_scale("distance_m", 40000, 25000, 90000, 500),
            ),
            _session(
                "run",
                "long",
                day=6,
                priority=2,
                key=True,
                scale=_scale("distance_m", 8000, 6000, 16000, 250),
            ),
            _session(
                "swim",
                "intervals",
                day=2,
                priority=3,
                key=True,
                scale=_scale("reps", 8, 6, 20, 1),
                warmup_m=300,
                rep_distance_m=100,
                rest=20,
                cooldown_m=200,
            ),
            _session(
                "run",
                "intervals",
                day=1,
                priority=6,
                key=True,
                scale=_scale("reps", 4, 3, 8, 1),
                warmup_minutes=10,
                rep_distance_m=1000,
                recovery=150,
                cooldown_minutes=10,
            ),
            _session(
                "cycle",
                "intervals",
                day=3,
                priority=4,
                key=True,
                scale=_scale("reps", 3, 3, 8, 1),
                warmup_minutes=15,
                work=300,
                recovery=240,
                cooldown_minutes=10,
            ),
            _session(
                "run",
                "easy",
                day=5,
                priority=7,
                brick=True,
                scale=_scale("duration_minutes", 15, 10, 40, 5),
            ),
            _session(
                "swim",
                "continuous",
                day=4,
                priority=5,
                scale=_scale("distance_m", 1200, 900, 2500, 100),
            ),
            # Same two sessions as the sprint plan, a rep heavier: 16 weeks is
            # long enough for the gym work to be worth something, and the
            # endurance volume it sits alongside is capped by clamps anyway.
            _session(
                "strength",
                "straight_sets",
                day=1,
                priority=8,
                rest=150,
                exercises=[
                    {"exercise": "squat", "sets": 3, "reps": 8},
                    {"exercise": "bench_press", "sets": 3, "reps": 8},
                ],
            ),
            _session(
                "strength",
                "straight_sets",
                day=3,
                priority=9,
                rest=150,
                exercises=[
                    {"exercise": "deadlift", "sets": 3, "reps": 8},
                    {"exercise": "shoulder_press", "sets": 3, "reps": 8},
                ],
            ),
        ],
    },
    # --- strength --------------------------------------------------------
    #
    # No endurance event behind it, and the only goal whose sessions are all
    # unscaled: the whole progression is the number on the bar. Phases and
    # taper still decide which weeks deload, so `event_date` is the day you
    # plan to re-test rather than a race.
    "strength_program": {
        "label": "Strength block",
        "description": "linear progression on the four barbell lifts",
        "weeks": 12,
        "days_per_week": 3,
        "rest_day": 6,  # Sun
        "phases": [("base", 4), ("build", 4), ("peak", 2)],
        "weekly_sessions": [
            # A/B/A across Mon/Wed/Fri: squat and press twice a fortnight
            # each, deadlift once a week, which is as often as most people can
            # pull heavy and keep adding to it. Fives throughout — heavy enough
            # to drive the numbers, light enough to keep the reps clean.
            _session(
                "strength",
                "straight_sets",
                day=0,
                priority=1,
                key=True,
                rest=180,
                exercises=[
                    {"exercise": "squat", "sets": 3, "reps": 5},
                    {"exercise": "bench_press", "sets": 3, "reps": 5},
                ],
            ),
            _session(
                "strength",
                "straight_sets",
                day=2,
                priority=2,
                key=True,
                rest=180,
                exercises=[
                    {"exercise": "deadlift", "sets": 3, "reps": 5},
                    {"exercise": "shoulder_press", "sets": 3, "reps": 5},
                ],
            ),
            _session(
                "strength",
                "straight_sets",
                day=4,
                priority=3,
                key=True,
                rest=180,
                exercises=[
                    {"exercise": "squat", "sets": 3, "reps": 5},
                    {"exercise": "bench_press", "sets": 3, "reps": 5},
                ],
            ),
        ],
    },
}
