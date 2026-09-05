"""The training content behind `fit train`: one template per goal.

Pure data and the two constructors that build it — plan length, phase
structure and the weekly session mix for each goal fit knows how to train
for. No engine logic: training.py reads these and does the periodising, so
adding or recalibrating a goal never means touching the code that expands one.

Imports nothing from fit. `_scale`'s clamps and `_session`'s fields are
consumed by training._scaled and training._build_session respectively; the
docstrings here describe what a template author needs to know, and training.py
documents what the engine does with it.
"""

# One profile per goal: plan length, phase structure (the taper comes from the
# progression settings, so it is not listed here), and the weekly session mix.
# Standard endurance-coaching shapes; every number here is a tunable constant.
#
# Phase lengths sum to weeks - training.PROGRESSION_DEFAULTS["taper_weeks"]. A
# description that moves start_date reapportions them (see
# training._assign_phases), so they need not sum exactly — but keeping them
# tidy makes the intent readable.


def _scale(param: str, base: int, low: int, high: int, step: int) -> dict:
    """The single session param that grows with the week's volume multiplier,
    with the clamp it may never escape (see training._scaled)."""
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

    day       0=Mon .. 6=Sun, in the template's own week; the whole week is
              rotated if the description names a different rest_day
    priority  1 = drop last, used to trim the week when days_per_week is lower
              than the template's default. Multi-sport goals interleave the
              sports here rather than ranking every long session first, so a
              trimmed week keeps one session of each discipline: a triathlon
              plan with the swimming cut out of it is not a triathlon plan.
              (Below three training days there are not enough slots for that
              to hold, and the lowest-priority sports do drop out.)
    key       a hard session (quality or long); extras are never placed on
              these days
    brick     runs straight off the session sharing its day (triathlon only)
    scale     the single param that grows with the week's volume multiplier,
              or None for a session that does not scale. Strength is the only
              one: its progression is the weight on the bar, and ramping sets
              or reps as well would be two progressions at once — you hold
              3x10 and add load, which is the whole method
    params    the fixed planner.build_plan params for the session type; the
              intensity one is filled in later by training._apply_target
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
    "run_5k": {
        "label": "5k",
        "description": "5km race",
        "weeks": 8,
        "days_per_week": 4,
        "rest_day": 0,  # Mon
        "phases": [("base", 3), ("build", 2), ("peak", 1)],
        "weekly_sessions": [
            _session(
                "run",
                "long",
                day=5,
                priority=1,
                key=True,
                scale=_scale("distance_m", 8000, 6000, 14000, 250),
            ),
            _session(
                "run",
                "intervals",
                day=1,
                priority=2,
                key=True,
                scale=_scale("reps", 5, 4, 10, 1),
                warmup_minutes=10,
                rep_distance_m=800,
                recovery=120,
                cooldown_minutes=10,
            ),
            _session(
                "run",
                "tempo",
                day=3,
                priority=3,
                key=True,
                scale=_scale("tempo_minutes", 20, 10, 40, 5),
                warmup_minutes=10,
                cooldown_minutes=10,
            ),
            _session(
                "run",
                "easy",
                day=6,
                priority=4,
                scale=_scale("duration_minutes", 30, 20, 60, 5),
            ),
        ],
    },
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
    "run_half": {
        "label": "Half marathon",
        "description": "21.1km race",
        "weeks": 12,
        "days_per_week": 5,
        "rest_day": 0,
        "phases": [("base", 5), ("build", 3), ("peak", 2)],
        "weekly_sessions": [
            _session(
                "run",
                "long",
                day=5,
                priority=1,
                key=True,
                scale=_scale("distance_m", 14000, 10000, 24000, 500),
            ),
            # Half-marathon pace sits close to threshold, so the tempo block
            # outranks the interval session here (it does not in the 5k/10k).
            _session(
                "run",
                "tempo",
                day=3,
                priority=2,
                key=True,
                scale=_scale("tempo_minutes", 30, 20, 60, 5),
                warmup_minutes=10,
                cooldown_minutes=10,
            ),
            _session(
                "run",
                "intervals",
                day=1,
                priority=3,
                key=True,
                scale=_scale("reps", 5, 3, 8, 1),
                warmup_minutes=10,
                rep_distance_m=1000,
                recovery=150,
                cooldown_minutes=10,
            ),
            _session(
                "run",
                "easy",
                day=6,
                priority=4,
                scale=_scale("duration_minutes", 40, 30, 75, 5),
            ),
            _session(
                "run",
                "easy",
                day=2,
                priority=5,
                scale=_scale("duration_minutes", 40, 25, 70, 5),
            ),
        ],
    },
    # --- cycling ---------------------------------------------------------
    "cycle_25k_tt": {
        "label": "25km time trial",
        "description": "25km time trial (~35-45min effort)",
        "weeks": 8,
        "days_per_week": 4,
        "rest_day": 0,
        "phases": [("base", 3), ("build", 2), ("peak", 1)],
        "weekly_sessions": [
            _session(
                "cycle",
                "long",
                day=5,
                priority=1,
                key=True,
                scale=_scale("distance_m", 50000, 30000, 100000, 500),
            ),
            # Two threshold sessions a week: short sharp reps midweek, longer
            # sustained blocks at the weekend — the TT-specific bit.
            _session(
                "cycle",
                "intervals",
                day=1,
                priority=2,
                key=True,
                scale=_scale("reps", 4, 3, 8, 1),
                warmup_minutes=15,
                work=300,
                recovery=300,
                cooldown_minutes=10,
            ),
            # The TT-specific session: three sustained blocks that lengthen
            # toward race duration, rather than more short reps. Scaling the
            # block in seconds also gives the ramp somewhere to go — a 2-5 rep
            # count is too coarse to express a progression at all.
            _session(
                "cycle",
                "intervals",
                day=6,
                priority=3,
                key=True,
                scale=_scale("work", 480, 240, 900, 30),
                warmup_minutes=15,
                reps=3,
                recovery=360,
                cooldown_minutes=10,
            ),
            _session(
                "cycle",
                "endurance",
                day=3,
                priority=4,
                scale=_scale("duration_minutes", 60, 40, 120, 5),
            ),
        ],
    },
    "cycle_40k_tt": {
        "label": "40km time trial",
        "description": "40km time trial (~55-70min effort)",
        "weeks": 10,
        "days_per_week": 5,
        "rest_day": 0,
        "phases": [("base", 4), ("build", 3), ("peak", 1)],
        "weekly_sessions": [
            _session(
                "cycle",
                "long",
                day=5,
                priority=1,
                key=True,
                scale=_scale("distance_m", 60000, 40000, 130000, 500),
            ),
            _session(
                "cycle",
                "intervals",
                day=1,
                priority=2,
                key=True,
                scale=_scale("reps", 4, 3, 8, 1),
                warmup_minutes=15,
                work=360,
                recovery=300,
                cooldown_minutes=10,
            ),
            # As cycle_25k_tt's block day, scaled for a longer event.
            _session(
                "cycle",
                "intervals",
                day=3,
                priority=3,
                key=True,
                scale=_scale("work", 600, 300, 1500, 30),
                warmup_minutes=15,
                reps=3,
                recovery=420,
                cooldown_minutes=10,
            ),
            _session(
                "cycle",
                "endurance",
                day=6,
                priority=4,
                scale=_scale("duration_minutes", 90, 60, 180, 5),
            ),
            _session(
                "cycle",
                "endurance",
                day=2,
                priority=5,
                scale=_scale("duration_minutes", 60, 40, 120, 5),
            ),
        ],
    },
    "cycle_100k_sportive": {
        "label": "100km sportive",
        "description": "100km sportive, climbing included",
        "weeks": 12,
        "days_per_week": 5,
        "rest_day": 0,
        "phases": [("base", 5), ("build", 3), ("peak", 2)],
        "weekly_sessions": [
            _session(
                "cycle",
                "long",
                day=5,
                priority=1,
                key=True,
                scale=_scale("distance_m", 60000, 40000, 160000, 500),
            ),
            # Back-to-back weekend riding: the sportive-specific adaptation is
            # riding tired, not riding hard.
            _session(
                "cycle",
                "endurance",
                day=6,
                priority=2,
                scale=_scale("duration_minutes", 90, 60, 210, 5),
            ),
            _session(
                "cycle",
                "intervals",
                day=1,
                priority=3,
                key=True,
                scale=_scale("reps", 4, 3, 8, 1),
                warmup_minutes=15,
                work=300,
                recovery=300,
                cooldown_minutes=10,
            ),
            # Hills carry no target — gradient makes power/pace meaningless
            # (see planner._hills).
            _session(
                "cycle",
                "hills",
                day=3,
                priority=4,
                key=True,
                scale=_scale("reps", 6, 4, 12, 1),
                warmup_minutes=15,
                effort=180,
                recovery=180,
                cooldown_minutes=10,
            ),
            _session(
                "cycle",
                "endurance",
                day=2,
                priority=5,
                scale=_scale("duration_minutes", 60, 40, 120, 5),
            ),
        ],
    },
    "cycle_strength": {
        "label": "Cycling + strength",
        "description": "40km bike time, supported by squat/deadlift/bench/press",
        "weeks": 12,
        "days_per_week": 5,
        "rest_day": 0,  # Mon
        "phases": [("base", 5), ("build", 5), ("peak", 2)],
        "weekly_sessions": [
            _session(
                "cycle",
                "long",
                day=6,
                priority=1,
                key=True,
                scale=_scale("distance_m", 40000, 25000, 90000, 500),
            ),
            _session(
                "cycle",
                "intervals",
                day=1,
                priority=2,
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
                day=3,
                priority=3,
                key=True,
                scale=_scale("work", 600, 300, 1500, 30),
                warmup_minutes=15,
                reps=3,
                recovery=420,
                cooldown_minutes=10,
            ),
            _session(
                "strength",
                "straight_sets",
                day=2,
                priority=4,
                warmup_minutes=10,
                rest=180,
                exercises=[
                    {"exercise": "squat", "sets": 4, "reps": 6},
                    {"exercise": "deadlift", "sets": 3, "reps": 5},
                ],
            ),
            _session(
                "strength",
                "straight_sets",
                day=4,
                priority=5,
                warmup_minutes=10,
                rest=90,
                exercises=[
                    {"exercise": "bench_press", "sets": 3, "reps": 10},
                    {"exercise": "shoulder_press", "sets": 3, "reps": 10},
                ],
            ),
        ],
    },
    # --- triathlon -------------------------------------------------------
    "sprint_triathlon": {
        "label": "Sprint triathlon",
        "description": "750m swim / 20km bike / 5km run",
        "weeks": 12,
        "days_per_week": 6,
        "rest_day": 0,
        "phases": [("base", 4), ("build", 4), ("peak", 2)],
        "weekly_sessions": [
            _session(
                "cycle",
                "long",
                day=5,
                priority=1,
                key=True,
                scale=_scale("distance_m", 30000, 20000, 70000, 500),
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
                scale=_scale("reps", 8, 6, 16, 1),
                warmup_m=200,
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
                scale=_scale("reps", 5, 4, 10, 1),
                warmup_minutes=10,
                rep_distance_m=800,
                recovery=120,
                cooldown_minutes=10,
            ),
            _session(
                "cycle",
                "intervals",
                day=3,
                priority=4,
                key=True,
                scale=_scale("reps", 4, 3, 8, 1),
                warmup_minutes=15,
                work=240,
                recovery=180,
                cooldown_minutes=10,
            ),
            _session(
                "run",
                "easy",
                day=5,
                priority=7,
                brick=True,
                scale=_scale("duration_minutes", 15, 10, 30, 5),
            ),
            _session(
                "swim",
                "continuous",
                day=4,
                priority=5,
                scale=_scale("distance_m", 1000, 800, 2000, 100),
            ),
            # Supplementary strength: two sessions, sharing the days the hard
            # endurance work already occupies rather than claiming days of
            # their own, and ranked last so a trimmed week loses the gym
            # before it loses a discipline. They carry no scale — the load
            # ramps instead (see _session), which is why they don't move the
            # volume measurement either.
            _session(
                "strength",
                "straight_sets",
                day=1,
                priority=8,
                warmup_minutes=10,
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
                warmup_minutes=10,
                rest=150,
                exercises=[
                    {"exercise": "deadlift", "sets": 3, "reps": 8},
                    {"exercise": "shoulder_press", "sets": 3, "reps": 8},
                ],
            ),
        ],
    },
    "standard_triathlon": {
        "label": "Standard triathlon",
        "description": "1.5km swim / 40km bike / 10km run",
        "weeks": 16,
        "days_per_week": 6,
        "rest_day": 0,
        "phases": [("base", 6), ("build", 5), ("peak", 3)],
        "weekly_sessions": [
            # The clamps matter more here than anywhere else: at 16 weeks the
            # 8%/week ramp compounds to ~2.2x, which would otherwise put a
            # 108km ride and a 22km run in an Olympic-distance plan. These caps
            # hold the long sessions at roughly 2.2x the bike leg and 1.6x the
            # run leg, which is what the distance actually calls for.
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
                warmup_minutes=10,
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
                warmup_minutes=10,
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
    # The one goal with no endurance event behind it, and the only one whose
    # sessions are all unscaled: nothing here grows in size, because the whole
    # progression is the number on the bar (see _session's `scale`). The
    # phases and taper still apply — they are what decides which weeks deload
    # and which back off before the day you re-test — so `event_date` is the
    # day you plan to find out, rather than a race.
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
                warmup_minutes=10,
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
                warmup_minutes=10,
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
                warmup_minutes=10,
                rest=180,
                exercises=[
                    {"exercise": "squat", "sets": 3, "reps": 5},
                    {"exercise": "bench_press", "sets": 3, "reps": 5},
                ],
            ),
        ],
    },
}
