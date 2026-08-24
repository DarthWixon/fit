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


def test_parse_schedule_date():
    assert planner.parse_schedule_date("2026-08-28") == "2026-08-28"
    assert planner.parse_schedule_date(" 2026-08-28 ") == "2026-08-28"


@pytest.mark.parametrize(
    "bad", ["", "2026-8-28", "28-08-2026", "2026/08/28", "2026-13-01", "2026-02-30"]
)
def test_parse_schedule_date_rejects(bad):
    with pytest.raises(ValueError):
        planner.parse_schedule_date(bad)


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
    assert [s["type"] for s in steps] == [
        "ExecutableStepDTO",
        "RepeatGroupDTO",
        "ExecutableStepDTO",
    ]
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
    paced_interval = paced["payload"]["workoutSegments"][0]["workoutSteps"][1][
        "workoutSteps"
    ][0]
    assert paced_interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert paced["workout_name"] == "Swim intervals 10x100m @ 1:50/100m"


def test_cycle_intervals_power_zone():
    plan = planner.build_plan("cycle", "intervals", _params("cycle", "intervals"), "t")
    interval = plan["payload"]["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"][
        0
    ]
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
    return {
        "type": "run",
        "date": date_iso,
        "distance_km": distance_km,
        "duration_seconds": duration_seconds,
        **extra,
    }


def test_recommended_interval_pace_scales_by_rep_length():
    # 1300s 5k -> 260 s/km; short reps get the 0.97 discount -> 252
    assert planner.recommended_interval_pace(1300, 800) == 252
    assert planner.recommended_interval_pace(1300, 1000) == 252
    assert planner.recommended_interval_pace(1300, 1600) == 260


def test_run_interval_pace_from_best_recent_5k():
    activities = [
        _run("2026-05-14", 5.05, 1330),  # dedicated 5k, 22:10
        _run("2026-06-01", 10.0, 3000, splits={"5k_seconds": 1300}),  # faster split 5k
        _run("2025-01-01", 5.0, 1100),  # outside 6-month window
    ]
    recs = planner.recommend_defaults("run", "intervals", activities, [], REFERENCE)
    # split (1300s) beats dedicated (1330s); the default is rep-length
    # dependent, so it's a derive callable rather than a static value
    rec = recs["target_pace"]
    assert "default" not in rec
    assert rec["derive"]({"rep_distance_m": 800}) == "4:12"  # 260 * 0.97 = 252
    assert rec["derive"]({"rep_distance_m": 1600}) == "4:20"  # plain 5k pace
    assert rec["derive"]({}) == "4:12"  # falls back to the 800m default
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
    activities = [
        {
            "type": "swim",
            "date": "2026-06-10",
            "distance_km": 2.0,
            "duration_seconds": 2900,
            "splits": {"500m_seconds": 480, "1k_seconds": 1000},
        }
    ]
    recs = planner.recommend_defaults("swim", "intervals", activities, [], REFERENCE)
    # CSS = (1000 - 480) / 5 = 104 -> 1:44
    assert recs["target_pace_100m"]["default"] == "1:44"
    assert "CSS" in recs["target_pace_100m"]["why"]


def test_swim_fallback_to_1k_pace_then_median():
    only_1k = [
        {
            "type": "swim",
            "date": "2026-06-10",
            "distance_km": 1.0,
            "duration_seconds": 1080,
        }
    ]
    recs = planner.recommend_defaults("swim", "intervals", only_1k, [], REFERENCE)
    assert recs["target_pace_100m"]["default"] == "1:48"

    no_milestones = [
        {
            "type": "swim",
            "date": "2026-06-10",
            "distance_km": 0.8,
            "duration_seconds": 960,
        }
    ]
    recs = planner.recommend_defaults("swim", "intervals", no_milestones, [], REFERENCE)
    assert recs["target_pace_100m"]["default"] == "2:00"
    assert "median" in recs["target_pace_100m"]["why"]


def test_cycle_watts_from_recent_rides():
    activities = [
        {
            "type": "cycle",
            "date": "2026-06-01",
            "distance_km": 30.0,
            "duration_seconds": 3600,
            "avg_power": 187,
        },
        {
            "type": "cycle",
            "date": "2026-06-05",
            "distance_km": 10.0,
            "duration_seconds": 900,
            "avg_power": 320,
        },  # < 20 min: ignored
        {
            "type": "cycle",
            "date": "2026-06-08",
            "distance_km": 40.0,
            "duration_seconds": 5000,
        },  # no power: ignored
    ]
    recs = planner.recommend_defaults("cycle", "intervals", activities, [], REFERENCE)
    # No best_power on these rides, so the whole-ride average stands
    # unadjusted — 187 to the nearest 5. The 95% correction belongs to a real
    # 20-minute effort, not to a ride average that is already sub-threshold.
    assert recs["target_watts"]["default"] == 185


def test_rep_progression_from_previous_plans():
    plans = [
        {
            "id": "2026-06-01T08:00:00",
            "sport": "run",
            "workout_type": "intervals",
            "params": {"reps": 6},
        },
        {
            "id": "2026-06-15T08:00:00",
            "sport": "run",
            "workout_type": "intervals",
            "params": {"reps": 7},
        },
        {
            "id": "2026-06-20T08:00:00",
            "sport": "cycle",
            "workout_type": "intervals",
            "params": {"reps": 3},
        },  # other sport: ignored
    ]
    recs = planner.recommend_defaults("run", "intervals", [], plans, REFERENCE)
    assert recs["reps"]["default"] == 8

    capped = [
        {
            "id": "2026-06-15T08:00:00",
            "sport": "run",
            "workout_type": "intervals",
            "params": {"reps": 10},
        }
    ]
    recs = planner.recommend_defaults("run", "intervals", [], capped, REFERENCE)
    assert "reps" not in recs


# --- steady sessions (easy / long / endurance / continuous) --------------------


def test_parse_distance_km():
    assert planner.parse_distance_km("16") == 16000
    assert planner.parse_distance_km(" 16.5 ") == 16500


@pytest.mark.parametrize("bad", ["", "0", "-5", "abc", "16km"])
def test_parse_distance_km_rejects(bad):
    with pytest.raises(ValueError):
        planner.parse_distance_km(bad)


@pytest.mark.parametrize(
    "sport,workout_type,params",
    [
        ("run", "easy", {"duration_minutes": 40, "target_pace": 360}),
        ("run", "long", {"distance_m": 16000, "target_pace": 345}),
        ("cycle", "endurance", {"duration_minutes": 90, "target_watts": 165}),
        ("cycle", "long", {"distance_m": 60000, "target_watts": 165}),
        ("swim", "continuous", {"distance_m": 1500, "target_pace_100m": 110}),
    ],
)
def test_steady_workouts_are_a_single_targeted_block(sport, workout_type, params):
    """Unlike the quality types, steady sessions have no warmup/cooldown — one
    step, carrying the target band the whole way."""
    plan = planner.build_plan(sport, workout_type, params, "2026-08-24T10:00:00")
    steps = plan["payload"]["workoutSegments"][0]["workoutSteps"]
    assert len(steps) == 1
    assert steps[0]["type"] == "ExecutableStepDTO"
    assert steps[0]["targetType"]["workoutTargetTypeKey"] in ("pace.zone", "power.zone")
    assert plan["payload"]["estimatedDurationInSecs"] > 0


def test_steady_targets_use_the_wider_band():
    tight = planner.build_plan(
        "run",
        "tempo",
        {
            "warmup_minutes": 10,
            "tempo_minutes": 20,
            "target_pace": 300,
            "cooldown_minutes": 10,
        },
        "x",
    )["payload"]["workoutSegments"][0]["workoutSteps"][1]
    wide = planner.build_plan(
        "run", "easy", {"duration_minutes": 40, "target_pace": 300}, "x"
    )["payload"]["workoutSegments"][0]["workoutSteps"][0]
    tight_span = tight["targetValueTwo"] - tight["targetValueOne"]
    wide_span = wide["targetValueTwo"] - wide["targetValueOne"]
    assert wide_span > tight_span


def test_swim_continuous_without_a_pace_target():
    plan = planner.build_plan("swim", "continuous", {"distance_m": 1500}, "x")
    step = plan["payload"]["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_easy_and_endurance_intensities_sit_below_threshold():
    assert planner.easy_pace_from_5k(1500) > 1500 / 5  # slower than 5k pace
    assert planner.endurance_watts_from_ftp(250) < 250


def test_workout_name_matches_build_plan():
    params = {"distance_m": 16000, "target_pace": 345}
    assert (
        planner.workout_name("run", "long", params)
        == planner.build_plan("run", "long", params, "x")["workout_name"]
    )


# --- guarded derivations and benchmark payloads --------------------------------


def test_swim_css_rejects_two_incomparable_efforts():
    """The real bug: a 500m from a 2021 drill session (3:56/100m) against a 1k
    from 2025 (2:15/100m) gave 35s/100m — faster than the world record. The
    shorter effort must actually be faster per 100m for the model to mean
    anything."""
    swims = [
        {
            "type": "swim",
            "date": "2022-05-01",
            "distance_km": 0.81,
            "duration_seconds": 2461,
            "splits": {"500m_seconds": 1179.9},
        },
        {
            "type": "swim",
            "date": "2026-08-01",
            "distance_km": 1.0,
            "duration_seconds": 1355,
        },
    ]
    css, why = planner.derive_swim_css(swims)
    assert css == 136  # falls through to 1k pace
    assert "1k pace" in why


def test_swim_css_pair_must_be_close_in_time():
    same_pair = lambda d1, d2: [
        {"type": "swim", "date": d1, "distance_km": 0.5, "duration_seconds": 480},
        {"type": "swim", "date": d2, "distance_km": 1.0, "duration_seconds": 1000},
    ]
    near = planner.derive_swim_css(same_pair("2026-08-01", "2026-08-08"))
    far = planner.derive_swim_css(same_pair("2026-01-01", "2026-08-08"))
    assert "CSS from" in near[1]
    assert "CSS from" not in far[1]


def test_derive_target_rejects_the_impossible_with_a_reason():
    run = [
        {
            "type": "run",
            "date": "2026-08-01",
            "distance_km": 5.0,
            "duration_seconds": 400,
        }
    ]
    result = planner.derive_target("run", run)
    assert result["value"] is None
    assert "outside the plausible" in result["why"]


def test_derive_target_passes_a_believable_measurement_through():
    run = [
        {
            "type": "run",
            "date": "2026-08-01",
            "distance_km": 5.0,
            "duration_seconds": 1500,
        }
    ]
    assert planner.derive_target("run", run)["value"] == 1500


def test_ftp_applies_the_twenty_minute_correction_to_a_real_window():
    """95% is the convention for a 20-minute best effort, so it applies to
    best_power's 20min figure and not to a whole-ride average."""
    windowed = [
        {
            "type": "cycle",
            "date": "2026-08-01",
            "avg_power": 150,
            "duration_seconds": 4000,
            "best_power": {"20min": 200},
        }
    ]
    watts, why = planner.derive_ride_watts(windowed)
    assert watts == 190  # 200 * 0.95
    assert "20min power" in why


def test_a_ride_average_is_used_unadjusted_when_no_window_exists():
    ride = [
        {
            "type": "cycle",
            "date": "2026-08-01",
            "avg_power": 200,
            "duration_seconds": 1200,
        }
    ]
    watts, why = planner.derive_ride_watts(ride)
    assert watts == 200
    assert "no 20min power recorded" in why


def test_a_recorded_window_beats_a_higher_ride_average():
    """A 20-minute effort is the better measurement even when another ride
    posted a higher whole-ride average."""
    activities = [
        {
            "type": "cycle",
            "date": "2026-08-01",
            "avg_power": 260,
            "duration_seconds": 4000,
        },
        {
            "type": "cycle",
            "date": "2026-08-05",
            "avg_power": 150,
            "duration_seconds": 4000,
            "best_power": {"20min": 200},
        },
    ]
    watts, why = planner.derive_ride_watts(activities)
    assert watts == 190
    assert "20min power" in why


@pytest.mark.parametrize("seconds,counts", [(1139, False), (1140, True), (1199, True)])
def test_a_bare_twenty_minute_test_is_not_disqualified_by_a_second(seconds, counts):
    """A watch stopped on the beep records 1199s, and rejecting a test that was
    actually done is the worst failure available — retarget would report no
    change and never say why."""
    ride = [
        {
            "type": "cycle",
            "date": "2026-08-01",
            "avg_power": 200,
            "duration_seconds": seconds,
        }
    ]
    assert (planner.derive_ride_watts(ride) is not None) is counts


@pytest.mark.parametrize(
    "sport,bare,wrapped",
    [
        (
            "cycle",
            {"test_minutes": 20},
            {"warmup_minutes": 20, "test_minutes": 20, "cooldown_minutes": 10},
        ),
        (
            "swim",
            {"test_distance_m": 1000},
            {"warmup_m": 200, "test_distance_m": 1000, "cooldown_m": 100},
        ),
    ],
)
def test_bare_baselines_omit_warmup_and_cooldown(sport, bare, wrapped):
    """A zero-length step is not a valid workout, so the wrapping is dropped
    entirely — and the remaining steps stay contiguously numbered."""
    bare_steps = planner.build_plan(sport, "baseline", bare, "x")["payload"][
        "workoutSegments"
    ][0]["workoutSteps"]
    assert len(bare_steps) == 1
    assert bare_steps[0]["stepOrder"] == 1
    assert bare_steps[0]["targetType"]["workoutTargetTypeKey"] == "no.target"

    full = planner.build_plan(sport, "baseline", wrapped, "x")["payload"][
        "workoutSegments"
    ][0]["workoutSteps"]
    assert [s["stepOrder"] for s in full] == [1, 2, 3]


def test_a_bare_test_carries_its_instruction_in_the_name():
    """The name is what the athlete reads on the watch, and nothing else in the
    payload can carry 'warm up before you start recording'."""
    assert "warm up first" in planner.workout_name(
        "cycle", "baseline", {"test_minutes": 20}
    )
    assert "warm up first" not in planner.workout_name(
        "cycle",
        "baseline",
        {"warmup_minutes": 20, "test_minutes": 20, "cooldown_minutes": 10},
    )


def test_optional_int_treats_blank_as_no_step():
    assert planner._optional_int("") == 0
    assert planner._optional_int("  ") == 0
    assert planner._optional_int("15") == 15
    with pytest.raises(ValueError):
        planner._optional_int("abc")


@pytest.mark.parametrize("sport", ["run", "cycle", "swim"])
def test_planning_a_baseline_by_hand_gives_the_same_test_the_plan_schedules(sport):
    """Both routes must produce a test fit can actually read back. They drifted
    once: the plan's run benchmark moved 3km -> 5km while the prompt default
    stayed at 3km, which is measurable nowhere."""
    from fit import training

    defaults = {
        spec["key"]: spec["parse"](str(spec["default"]))
        for spec in planner.workout_params(sport, "baseline")
    }
    # A blank warmup/cooldown parses to 0, which _baseline_steps omits — so the
    # comparison is against the plan's params with those absent.
    assert {k: v for k, v in defaults.items() if v} == training.BENCHMARK_SESSIONS[
        sport
    ]["params"]
    assert planner.workout_name(sport, "baseline", defaults) == planner.workout_name(
        sport, "baseline", training.BENCHMARK_SESSIONS[sport]["params"]
    )
