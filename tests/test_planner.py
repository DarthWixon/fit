"""Tests for planner.py: input parsing, target-zone math, Garmin payload
structure, and the history-derived recommended defaults."""

from datetime import date

import pytest

from fit import planner


# --- parsing -------------------------------------------------------------------

def test_parse_pace():
    assert planner.parse_pace("4:30") == 270
    assert planner.parse_pace(" 10:05 ") == 605


@pytest.mark.parametrize("bad", ["430", "4:5", "4:xx", "-4:30", "4:75", "0:00", ""])
def test_parse_pace_rejects(bad):
    with pytest.raises(ValueError):
        planner.parse_pace(bad)


def test_parse_duration():
    assert planner.parse_duration("90") == 90
    assert planner.parse_duration("2:00") == 120


@pytest.mark.parametrize("bad", ["", "0", "-90", "1.5", "abc"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        planner.parse_duration(bad)


def test_pace_zone_mps_bounds_ordered():
    low, high = planner.pace_zone_mps(270, tolerance_s=10)
    assert low == 1000 / 280
    assert high == 1000 / 260
    assert low < high

    low, high = planner.swim_pace_zone_mps(110, tolerance_s=5)
    assert (low, high) == (100 / 115, 100 / 105)


# --- workout_params ------------------------------------------------------------

def test_workout_params_invalid_combos_raise():
    with pytest.raises(ValueError):
        planner.workout_params("swim", "tempo")
    with pytest.raises(ValueError):
        planner.workout_params("squash", "intervals")


def test_workout_params_returns_copies():
    specs = planner.workout_params("run", "intervals")
    specs[0]["default"] = 99
    assert planner.workout_params("run", "intervals")[0]["default"] != 99


# --- payload structure ---------------------------------------------------------

def _params(sport, workout_type, **overrides):
    params = {
        spec["key"]: spec["parse"](str(spec["default"]))
        for spec in planner.workout_params(sport, workout_type)
        if str(spec["default"])
    }
    params.update(overrides)
    return params


def test_run_intervals_payload():
    plan = planner.build_plan(
        "run", "intervals", _params("run", "intervals"), "2026-07-03T09:00:00"
    )
    payload = plan["payload"]
    assert payload["sportType"]["sportTypeId"] == 1
    assert payload["estimatedDurationInSecs"] > 0

    assert len(payload["workoutSegments"]) == 1
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert [s["type"] for s in steps] == ["ExecutableStepDTO", "RepeatGroupDTO", "ExecutableStepDTO"]
    assert [s["stepOrder"] for s in steps] == [1, 2, 5]

    warmup, repeat, cooldown = steps
    assert warmup["stepType"]["stepTypeKey"] == "warmup"
    assert warmup["endCondition"]["conditionTypeKey"] == "time"
    assert warmup["endConditionValue"] == 600.0
    assert cooldown["stepType"]["stepTypeKey"] == "cooldown"

    assert repeat["numberOfIterations"] == 6
    assert repeat["endConditionValue"] == 6.0
    interval, recovery = repeat["workoutSteps"]
    assert interval["endCondition"]["conditionTypeKey"] == "distance"
    assert interval["endConditionValue"] == 800.0
    assert interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert interval["targetValueOne"] < interval["targetValueTwo"]
    assert recovery["stepType"]["stepTypeKey"] == "recovery"
    assert recovery["endCondition"]["conditionTypeKey"] == "time"
    assert recovery["targetType"]["workoutTargetTypeKey"] == "no.target"

    assert plan["workout_name"] == "Run intervals 6x800m @ 4:30/km"


def test_hills_and_baseline_efforts_have_no_target():
    hills = planner.build_plan("run", "hills", _params("run", "hills"), "t")
    repeat = hills["payload"]["workoutSegments"][0]["workoutSteps"][1]
    effort = repeat["workoutSteps"][0]
    assert effort["targetType"]["workoutTargetTypeKey"] == "no.target"

    baseline = planner.build_plan("run", "baseline", _params("run", "baseline"), "t")
    test_step = baseline["payload"]["workoutSegments"][0]["workoutSteps"][1]
    assert test_step["endCondition"]["conditionTypeKey"] == "distance"
    assert test_step["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_swim_intervals_payload():
    plan = planner.build_plan("swim", "intervals", _params("swim", "intervals"), "t")
    steps = plan["payload"]["workoutSegments"][0]["workoutSteps"]
    assert plan["payload"]["sportType"]["sportTypeId"] == 4

    warmup, repeat, cooldown = steps
    assert warmup["endCondition"]["conditionTypeKey"] == "distance"
    assert cooldown["endCondition"]["conditionTypeKey"] == "distance"

    interval, rest = repeat["workoutSteps"]
    assert rest["stepType"]["stepTypeId"] == 5
    # blank pace default -> open target
    assert interval["targetType"]["workoutTargetTypeKey"] == "no.target"

    paced = planner.build_plan(
        "swim", "intervals", _params("swim", "intervals", target_pace_100m=110), "t"
    )
    paced_interval = paced["payload"]["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"][0]
    assert paced_interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert paced["workout_name"] == "Swim intervals 10x100m @ 1:50/100m"


def test_cycle_intervals_power_zone():
    plan = planner.build_plan("cycle", "intervals", _params("cycle", "intervals"), "t")
    interval = plan["payload"]["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"][0]
    assert interval["targetType"]["workoutTargetTypeKey"] == "power.zone"
    assert interval["targetValueOne"] == 190
    assert interval["targetValueTwo"] == 210
    assert plan["payload"]["sportType"]["sportTypeId"] == 2


def test_build_plan_invalid_combo_raises():
    with pytest.raises(ValueError):
        planner.build_plan("swim", "hills", {}, "t")


def test_describe_plan_run_intervals():
    plan = planner.build_plan(
        "run", "intervals", _params("run", "intervals"), "2026-07-03T09:00:00"
    )
    lines = planner.describe_plan(plan)
    assert lines == [
        "Warmup 10:00",
        "6 x 800m @ 4:20–4:40/km, 2:00 recovery",
        "Cooldown 10:00",
    ]


# --- recommend_defaults --------------------------------------------------------

REFERENCE = date(2026, 7, 3)


def _run(date_iso, distance_km, duration_seconds, **extra):
    return {"type": "run", "date": date_iso, "distance_km": distance_km,
            "duration_seconds": duration_seconds, **extra}


def test_recommended_interval_pace_scales_by_rep_length():
    # 1300s 5k -> 260 s/km; short reps get the 0.97 discount -> 252
    assert planner.recommended_interval_pace(1300, 800) == 252
    assert planner.recommended_interval_pace(1300, 1000) == 252
    assert planner.recommended_interval_pace(1300, 1600) == 260


def test_run_interval_pace_from_best_recent_5k():
    activities = [
        _run("2026-05-14", 5.05, 1330),                                  # dedicated 5k, 22:10
        _run("2026-06-01", 10.0, 3000, splits={"5k_seconds": 1300}),     # faster split 5k
        _run("2025-01-01", 5.0, 1100),                                   # outside 6-month window
    ]
    recs = planner.recommend_defaults("run", "intervals", activities, [], REFERENCE)
    # split (1300s) beats dedicated (1330s); the default is rep-length
    # dependent, so it's a derive callable rather than a static value
    rec = recs["target_pace"]
    assert "default" not in rec
    assert rec["derive"]({"rep_distance_m": 800}) == "4:12"   # 260 * 0.97 = 252
    assert rec["derive"]({"rep_distance_m": 1600}) == "4:20"  # plain 5k pace
    assert rec["derive"]({}) == "4:12"                        # falls back to the 800m default
    assert "5k" in rec["why"]


def test_run_tempo_pace_is_slower_and_rounded():
    activities = [_run("2026-05-14", 5.0, 1300)]
    recs = planner.recommend_defaults("run", "tempo", activities, [], REFERENCE)
    # 260 * 1.07 = 278.2 -> rounded to 280 = 4:40
    assert recs["target_pace"]["default"] == "4:40"


def test_run_pace_fallback_to_fastest_recent_run():
    activities = [_run("2026-06-20", 4.0, 1120)]  # no ~5k activity; 4:40/km
    recs = planner.recommend_defaults("run", "intervals", activities, [], REFERENCE)
    assert recs["target_pace"]["derive"]({"rep_distance_m": 1600}) == "4:40"
    assert "fastest recent run pace" in recs["target_pace"]["why"]


def test_run_no_history_returns_empty():
    assert planner.recommend_defaults("run", "intervals", [], [], REFERENCE) == {}


def test_swim_css_from_500m_and_1k():
    activities = [{
        "type": "swim", "date": "2026-06-10", "distance_km": 2.0, "duration_seconds": 2900,
        "splits": {"500m_seconds": 480, "1k_seconds": 1000},
    }]
    recs = planner.recommend_defaults("swim", "intervals", activities, [], REFERENCE)
    # CSS = (1000 - 480) / 5 = 104 -> 1:44
    assert recs["target_pace_100m"]["default"] == "1:44"
    assert "CSS" in recs["target_pace_100m"]["why"]


def test_swim_fallback_to_1k_pace_then_median():
    only_1k = [{"type": "swim", "date": "2026-06-10", "distance_km": 1.0, "duration_seconds": 1080}]
    recs = planner.recommend_defaults("swim", "intervals", only_1k, [], REFERENCE)
    assert recs["target_pace_100m"]["default"] == "1:48"

    no_milestones = [{"type": "swim", "date": "2026-06-10", "distance_km": 0.8, "duration_seconds": 960}]
    recs = planner.recommend_defaults("swim", "intervals", no_milestones, [], REFERENCE)
    assert recs["target_pace_100m"]["default"] == "2:00"
    assert "median" in recs["target_pace_100m"]["why"]


def test_cycle_watts_from_recent_rides():
    activities = [
        {"type": "cycle", "date": "2026-06-01", "distance_km": 30.0,
         "duration_seconds": 3600, "avg_power": 187},
        {"type": "cycle", "date": "2026-06-05", "distance_km": 10.0,
         "duration_seconds": 900, "avg_power": 320},   # < 20 min: ignored
        {"type": "cycle", "date": "2026-06-08", "distance_km": 40.0,
         "duration_seconds": 5000},                     # no power: ignored
    ]
    recs = planner.recommend_defaults("cycle", "intervals", activities, [], REFERENCE)
    assert recs["target_watts"]["default"] == 185  # 187 rounded to nearest 5


def test_rep_progression_from_previous_plans():
    plans = [
        {"id": "2026-06-01T08:00:00", "sport": "run", "workout_type": "intervals",
         "params": {"reps": 6}},
        {"id": "2026-06-15T08:00:00", "sport": "run", "workout_type": "intervals",
         "params": {"reps": 7}},
        {"id": "2026-06-20T08:00:00", "sport": "cycle", "workout_type": "intervals",
         "params": {"reps": 3}},  # other sport: ignored
    ]
    recs = planner.recommend_defaults("run", "intervals", [], plans, REFERENCE)
    assert recs["reps"]["default"] == 8

    capped = [{"id": "2026-06-15T08:00:00", "sport": "run", "workout_type": "intervals",
               "params": {"reps": 10}}]
    recs = planner.recommend_defaults("run", "intervals", [], capped, REFERENCE)
    assert "reps" not in recs
