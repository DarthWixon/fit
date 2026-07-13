"""Short characterization tests for the subtlest compute.py functions."""

from datetime import date

import pytest

from fit import compute

# --- calc_pace -----------------------------------------------------------------


def test_calc_pace_run_per_km():
    assert compute.calc_pace(10.0, 3000, "run") == "5:00/km"


def test_calc_pace_swim_per_100m():
    assert compute.calc_pace(1.0, 1080, "swim") == "1:48/100m"


def test_calc_pace_cycle_speed():
    assert compute.calc_pace(30.0, 3600, "cycle") == "30.0km/h"


def test_calc_pace_no_distance_type_and_zero_distance():
    assert compute.calc_pace(0.07, 3600, "squash") == "—"
    assert compute.calc_pace(0, 3000, "run") == "—"


# --- date windows --------------------------------------------------------------


def test_months_ago_clamps_day_to_month_end():
    assert compute.months_ago(date(2024, 3, 31), 1) == "2024-02-29"


def test_parse_timerange_days_and_months():
    assert compute.parse_timerange("10d", date(2026, 7, 2)) == (
        "2026-06-22",
        "2026-07-02",
    )
    assert compute.parse_timerange("3m", date(2026, 7, 2)) == (
        "2026-04-02",
        "2026-07-02",
    )


def test_parse_timerange_rejects_bad_input():
    with pytest.raises(ValueError):
        compute.parse_timerange("3x", date(2026, 7, 2))
    with pytest.raises(ValueError):
        compute.parse_timerange("0d", date(2026, 7, 2))


def test_activity_calendar_marks_days_and_spans_year_boundary():
    activities = [
        {"date": "2025-12-31", "type": "run"},
        {"date": "2026-01-05", "type": "cycle"},
        {"date": "2026-01-05", "type": "run"},  # same day, deduped
        {"date": "2025-11-30", "type": "run"},  # outside the 2-month window
    ]
    months = compute.activity_calendar(activities, date(2026, 1, 15))
    assert [m["label"] for m in months] == ["December 2025", "January 2026"]
    assert months[0]["active_days"] == [31]
    assert months[1]["active_days"] == [5]


def test_activity_calendar_weeks_are_monday_first_with_padding():
    months = compute.activity_calendar([], date(2026, 7, 3), months=1)
    weeks = months[0]["weeks"]
    # July 2026 starts on a Wednesday: two Monday/Tuesday padding cells.
    assert weeks[0] == [0, 0, 1, 2, 3, 4, 5]
    assert all(len(week) == 7 for week in weeks)
    assert months[0]["active_days"] == []


# --- weekly_volumes ------------------------------------------------------------


def test_weekly_volumes_zero_fills_and_spans_year_boundary():
    activities = [
        {"date": "2025-12-20", "type": "run", "duration_seconds": 3600},
        {"date": "2026-01-10", "type": "run", "duration_seconds": 1800},
    ]
    weekly = compute.weekly_volumes(activities)
    # Rest weeks are troughs, not missing bars — and W52 rolls into W01 cleanly.
    assert [w["week"] for w in weekly] == [
        "2025-W51",
        "2025-W52",
        "2026-W01",
        "2026-W02",
    ]
    assert [w["duration_seconds"] for w in weekly] == [3600, 0, 0, 1800]


def test_weekly_volumes_through_extends_to_the_current_week():
    activities = [{"date": "2026-01-10", "type": "run", "duration_seconds": 1800}]
    weekly = compute.weekly_volumes(activities, through=date(2026, 1, 26))
    assert [w["week"] for w in weekly] == [
        "2026-W02",
        "2026-W03",
        "2026-W04",
        "2026-W05",
    ]
    assert compute.is_current_week(weekly[-1]["week"], date(2026, 1, 26))


# --- fastest_split -------------------------------------------------------------


def _stream(pairs):
    return [{"elapsed_seconds": t, "distance_km": d} for t, d in pairs]


def test_fastest_split_constant_speed():
    points = _stream([(i * 240, float(i)) for i in range(7)])  # 4:00/km for 6km
    assert compute.fastest_split(points, 5.0) == {"duration_seconds": 1200}


def test_fastest_split_interpolates_window_end():
    # 3km in 900s then 3km in 600s: the 5k from the start crosses 5km
    # two-thirds of the way through the second segment.
    points = _stream([(0, 0.0), (900, 3.0), (1500, 6.0)])
    assert compute.fastest_split(points, 5.0) == {"duration_seconds": 1300}


def test_fastest_split_track_too_short():
    points = _stream([(0, 0.0), (600, 3.0)])
    assert compute.fastest_split(points, 5.0) is None


def test_fastest_split_dedupes_equal_timestamps():
    # The second sample replaces the first (same timestamp), so the window
    # runs 0.1km -> 5.1km: exactly 5k in 1200s.
    points = _stream([(0, 0.0), (0, 0.1), (1200, 5.1)])
    assert compute.fastest_split(points, 5.0) == {"duration_seconds": 1200}


# --- personal bests ------------------------------------------------------------

RUNS = [
    {"type": "run", "date": "2024-01-01", "distance_km": 5.1, "duration_seconds": 1500},
    {
        "type": "run",
        "date": "2024-02-01",
        "distance_km": 10.5,
        "duration_seconds": 3300,
        "elevation_gain_m": 120,
        "splits": {"5k_seconds": 1400},
    },
]


def test_all_personal_bests_milestone_split_and_longest():
    pbs = compute.all_personal_bests(RUNS)["run"]
    assert pbs["fastest_5k_seconds"] == 1500  # dedicated ~5k activity
    assert pbs["fastest_5k_split_seconds"] == 1400  # best 5k inside any run
    assert pbs["longest_distance_km"] == 10.5
    assert pbs["most_elevation_gain_m"] == 120


def test_best_pb_per_label_prefers_faster_split():
    pbs = compute.all_personal_bests(RUNS)["run"]
    collapsed = compute.best_pb_per_label(pbs)
    assert collapsed["fastest_5k_seconds"] == 1400  # split (1400) beat dedicated (1500)
    assert collapsed["fastest_5k_date"] == "2024-02-01"
    assert "fastest_5k_split_seconds" not in collapsed
    assert "fastest_5k_split_date" not in collapsed


def test_best_pb_per_label_prefers_faster_dedicated():
    type_pbs = {
        "fastest_5k_seconds": 1300,
        "fastest_5k_date": "2024-01-01",
        "fastest_5k_split_seconds": 1400,
        "fastest_5k_split_date": "2024-02-01",
    }
    collapsed = compute.best_pb_per_label(type_pbs)
    assert collapsed == {"fastest_5k_seconds": 1300, "fastest_5k_date": "2024-01-01"}


def test_best_pb_per_label_only_one_present():
    milestone_only = {"fastest_10k_seconds": 2900, "fastest_10k_date": "2024-03-01"}
    assert compute.best_pb_per_label(milestone_only) == milestone_only

    split_only = {
        "fastest_10k_split_seconds": 2800,
        "fastest_10k_split_date": "2024-04-01",
    }
    assert compute.best_pb_per_label(split_only) == {
        "fastest_10k_seconds": 2800,
        "fastest_10k_date": "2024-04-01",
    }


def test_best_pb_per_label_passes_through_non_time_keys():
    pbs = compute.all_personal_bests(RUNS)["run"]
    collapsed = compute.best_pb_per_label(pbs)
    assert collapsed["longest_distance_km"] == 10.5
    assert collapsed["longest_distance_date"] == "2024-02-01"
    assert collapsed["most_elevation_gain_m"] == 120
    assert collapsed["most_elevation_gain_date"] == "2024-02-01"


def test_all_personal_bests_no_distance_type_skips_longest():
    squash = [
        {
            "type": "squash",
            "date": "2024-01-01",
            "distance_km": 0.07,
            "duration_seconds": 3600,
        }
    ]
    assert "longest_distance_km" not in compute.all_personal_bests(squash)["squash"]


def test_detect_new_pbs_direction_of_comparison():
    current = {"run": {"fastest_5k_seconds": 1500, "longest_distance_km": 10.5}}
    faster_5k = {
        "type": "run",
        "date": "2024-03-01",
        "distance_km": 5.0,
        "duration_seconds": 1450,
    }
    broken = {pb["key"] for pb in compute.detect_new_pbs(faster_5k, current)}
    assert "fastest_5k_seconds" in broken  # lower seconds wins
    assert "longest_distance_km" not in broken  # 5.0 < 10.5

    slower_5k = dict(faster_5k, duration_seconds=1600)
    assert not compute.detect_new_pbs(slower_5k, current)


# --- fitness index -------------------------------------------------------------


def test_fitness_ewma_seeds_first_day_and_decays():
    activities = [
        {
            "type": "hike",
            "date": "2024-01-01",
            "distance_km": 5.0,
            "duration_seconds": 3600,
        }
    ]  # flat 6.0 MET * 1h = 6.0 load
    series = compute.fitness_ewma_daily(activities, date(2024, 1, 3))
    assert series[0]["value"] == 6.0  # seeded, no ramp-up
    assert series[1]["value"] == pytest.approx(6.0 * (1 - 1 / 42))
    assert series[2]["value"] < series[1]["value"]  # rest days decay


def test_activity_load_hr_multiplier_clamped():
    activity = {"type": "run", "distance_km": 10.0, "duration_seconds": 3000}
    base = compute.activity_load(activity, {})
    assert base == pytest.approx(11.0 * 3000 / 3600)  # 300s/km band, no HR

    hot = dict(activity, avg_heart_rate=200)
    cold = dict(activity, avg_heart_rate=50)
    assert compute.activity_load(hot, {"run": 100}) == pytest.approx(base * 1.25)
    assert compute.activity_load(cold, {"run": 100}) == pytest.approx(base * 0.8)
