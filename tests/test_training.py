"""Tests for training.py: plan-description parsing (including the YAML 1.1
coercion traps), the periodisation date/volume math, and completion matching.
Deliberately narrow — the engine's arithmetic, not the CLI around it."""

from datetime import date, timedelta

import pytest

from fit import planner, training

pytest.importorskip("yaml", reason="parse_plan_spec needs the optional [train] extra")

REFERENCE = date(2026, 8, 24)
MINIMAL = "goal: sprint_triathlon\nevent_date: 2026-11-15\n"


def _plan(text=MINIMAL, activities=None):
    spec = training.parse_plan_spec(text)
    return training.expand_plan(spec, activities or [], REFERENCE)


# --- description parsing -------------------------------------------------------


def test_parse_plan_spec_defaults_from_the_goal_template():
    spec = training.parse_plan_spec(MINIMAL)
    template = training.GOAL_TEMPLATES["sprint_triathlon"]
    assert spec["days_per_week"] == template["days_per_week"]
    assert spec["rest_day"] == template["rest_day"]
    assert spec["progression"] == training.PROGRESSION_DEFAULTS
    # start_date defaults to the template's length back from the event's week
    assert spec["start_date"] == "2026-08-24"


@pytest.mark.parametrize(
    "text",
    [
        "event_date: 2026-11-15\n",  # no goal
        "goal: run_marathon\nevent_date: 2026-11-15\n",  # unknown goal
        "goal: sprint_triathlon\n",  # no event_date
        "goal: sprint_triathlon\nevent_date: 2026-11-15\nintensity: hard\n",
        "goal: sprint_triathlon\nevent_date: 2026-11-15\nrest_day: nonesuch\n",
        "goal: sprint_triathlon\nevent_date: 2026-11-15\ndays_per_week: 9\n",
        "goal: sprint_triathlon\nevent_date: 2026-11-15\ntargets: {run_10k: '44:00'}\n",
        "goal: sprint_triathlon\nevent_date: 2026-11-15\nstart_date: 2026-12-01\n",
        "- a list\n",
        "goal: [unclosed\n",
    ],
)
def test_parse_plan_spec_rejects(text):
    with pytest.raises(ValueError):
        training.parse_plan_spec(text)


def test_parse_plan_spec_survives_yaml_coercion():
    """PyYAML is YAML 1.1: an unquoted date resolves to datetime.date and an
    unquoted 24:00 to the base-60 integer 1440. Both must land on the same
    normalised values as their quoted forms."""
    unquoted = training.parse_plan_spec(
        "goal: sprint_triathlon\nevent_date: 2026-11-15\n"
        "targets: {run_5k: 24:00, swim_css_100m: 1:45}\n"
    )
    quoted = training.parse_plan_spec(
        "goal: sprint_triathlon\nevent_date: '2026-11-15'\n"
        "targets: {run_5k: '24:00', swim_css_100m: '1:45'}\n"
    )
    assert unquoted == quoted
    assert unquoted["event_date"] == "2026-11-15"
    assert unquoted["targets"] == {"run_5k": 1440, "swim_css_100m": 105}


def test_parse_plan_spec_rejects_a_yaml_boolean_rest_day():
    # `rest_day: no` is False under YAML 1.1, not the string "no".
    with pytest.raises(ValueError):
        training.parse_plan_spec(
            "goal: sprint_triathlon\nevent_date: 2026-11-15\nrest_day: no\n"
        )


def test_plan_needs_a_minimum_number_of_weeks():
    with pytest.raises(ValueError):
        _plan(
            "goal: sprint_triathlon\nevent_date: 2026-11-15\nstart_date: 2026-11-02\n"
        )


# --- periodisation -------------------------------------------------------------


def test_expand_plan_covers_every_week_and_stops_before_the_event():
    plan = _plan()
    weeks = training.group_by_week(plan["sessions"])
    assert plan["weeks"] == 12
    assert [w["week"] for w in weeks] == list(range(1, 13))
    # Race day itself is not a training day.
    assert all(s["date"] < plan["event_date"] for s in plan["sessions"])


def test_phases_end_with_the_taper():
    phases = [w["phase"] for w in training.group_by_week(_plan()["sessions"])]
    assert phases[-2:] == ["taper", "taper"]
    assert phases[0] == "base"
    # Every phase runs as one contiguous block, never interleaved.
    assert [p for i, p in enumerate(phases) if i == 0 or phases[i - 1] != p] == [
        "base",
        "build",
        "peak",
        "taper",
    ]


def _long_ride_km_by_week(plan):
    by_week = {}
    for session in plan["sessions"]:
        if session["sport"] == "cycle" and session["session_type"] == "long":
            by_week[session["week"]] = session["params"]["distance_m"]
    return by_week


def test_build_weeks_ramp_and_every_fourth_week_recovers():
    rides = _long_ride_km_by_week(_plan())
    assert rides[1] < rides[2] < rides[3]  # 3-week build block ramps
    assert rides[4] < rides[1]  # then a recovery week drops below its start
    assert rides[5] > rides[3]  # and the ramp resumes above the block's peak
    assert rides[8] < rides[7]  # the next recovery week dips again


def test_the_taper_sheds_volume_into_the_event():
    rides = _long_ride_km_by_week(_plan())
    peak = max(rides.values())
    assert rides[11] < peak
    assert rides[12] < rides[11]


def test_progression_overrides_change_the_cycle():
    plan = _plan(MINIMAL + "progression:\n  build_recover: [2, 1]\n  taper_weeks: 1\n")
    phases = [w["phase"] for w in training.group_by_week(plan["sessions"])]
    assert phases.count("taper") == 1
    rides = _long_ride_km_by_week(plan)
    assert rides[3] < rides[2]  # recovery now lands on every third week


def test_session_sizes_stay_inside_the_template_clamps():
    plan = _plan()
    scale = next(
        s["scale"]
        for s in training.GOAL_TEMPLATES["sprint_triathlon"]["weekly_sessions"]
        if s["sport"] == "cycle" and s["session_type"] == "long"
    )
    for distance in _long_ride_km_by_week(plan).values():
        assert scale["min"] <= distance <= scale["max"]


# --- every goal template -------------------------------------------------------

ALL_GOALS = sorted(training.GOAL_TEMPLATES)


@pytest.mark.parametrize("goal", ALL_GOALS)
def test_every_goal_expands_into_buildable_sessions(goal):
    """The pipeline is goal-agnostic, so each template must expand and every
    non-extra session must hand straight to planner.build_plan."""
    plan = _plan(f"goal: {goal}\nevent_date: 2027-03-14\n")
    assert plan["weeks"] == training.GOAL_TEMPLATES[goal]["weeks"]
    assert plan["sessions"]
    for session in plan["sessions"]:
        args = training.session_to_build_args(session)
        if args is not None:
            planner.build_plan(*args, session["date"])


@pytest.mark.parametrize("goal", ALL_GOALS)
def test_every_goal_phases_run_base_to_taper(goal):
    plan = _plan(f"goal: {goal}\nevent_date: 2027-03-14\n")
    phases = [w["phase"] for w in training.group_by_week(plan["sessions"])]
    blocks = [p for i, p in enumerate(phases) if i == 0 or phases[i - 1] != p]
    assert blocks == ["base", "build", "peak", "taper"]


@pytest.mark.parametrize("goal", ALL_GOALS)
def test_every_goal_respects_its_own_days_per_week(goal):
    template = training.GOAL_TEMPLATES[goal]
    plan = _plan(f"goal: {goal}\nevent_date: 2027-03-14\nextras: {{}}\n")
    week_two = [s for s in plan["sessions"] if s["week"] == 2]
    assert len({s["date"] for s in week_two}) <= template["days_per_week"]


@pytest.mark.parametrize("goal", ALL_GOALS)
def test_every_goal_keeps_its_scaled_params_inside_the_clamps(goal):
    """The clamps are what stop a long plan's compounding ramp running away —
    a 16-week plan reaches ~2.2x, far more than an 8-week one."""
    template = training.GOAL_TEMPLATES[goal]
    plan = _plan(f"goal: {goal}\nevent_date: 2027-03-14\n")
    for session in plan["sessions"]:
        # Benchmarks are deliberately unscaled — a 3km test is only a benchmark
        # if it is the same 3km every time — so they have no clamp to check.
        if session["is_extra"] or session.get("is_benchmark"):
            continue
        # A template may carry two sessions of the same type with different
        # scales (the TT plans' short and long interval days, say), so the
        # value need only satisfy one of the matching clamps.
        candidates = [
            s["scale"]
            for s in template["weekly_sessions"]
            if (s["sport"], s["session_type"])
            == (session["sport"], session["session_type"])
            and s["scale"]
        ]
        # Strength sessions carry no scale at all: their progression is the
        # weight on the bar, so there is no size to clamp.
        if not candidates:
            assert session["sport"] == "strength"
            continue
        assert any(
            scale["min"] <= session["params"][scale["param"]] <= scale["max"]
            for scale in candidates
        )


@pytest.mark.parametrize("goal", ALL_GOALS)
def test_targets_cover_exactly_the_sports_the_goal_trains(goal):
    plan = _plan(f"goal: {goal}\nevent_date: 2027-03-14\n")
    expected = {
        training._SPORT_TARGETS[sport][0]
        for sport in training.template_sports(goal)
        if sport in training._SPORT_TARGETS
    }
    # Strength sits under its own key: one figure per lift, not one per sport.
    if training.template_lifts(goal):
        expected.add("strength")
        assert set(plan["targets"]["strength"]) == set(training.template_lifts(goal))
    assert set(plan["targets"]) - {"why"} == expected


def test_a_target_for_an_untrained_sport_is_rejected():
    """Silently ignoring it would read as fit disagreeing, not as a no-op."""
    with pytest.raises(ValueError):
        training.parse_plan_spec(
            "goal: run_5k\nevent_date: 2027-03-14\ntargets: {bike_ftp: 250}\n"
        )


# --- weekly layout -------------------------------------------------------------


def test_rest_day_rotates_the_whole_week():
    plan = _plan(MINIMAL + "rest_day: Wed\nextras: {}\n")
    weekdays = {
        date.fromisoformat(s["date"]).weekday()
        for s in plan["sessions"]
        if s["week"] == 2  # a whole week, clear of the start-date truncation
    }
    assert 2 not in weekdays  # Wednesday is free


@pytest.mark.parametrize("goal", ["sprint_triathlon", "standard_triathlon"])
@pytest.mark.parametrize("days_per_week", [6, 5, 4, 3])
def test_trimming_the_week_keeps_every_discipline(goal, days_per_week):
    """A triathlon plan with the swimming cut out of it is not a triathlon
    plan — the template's priorities interleave the sports for this reason."""
    plan = _plan(
        f"goal: {goal}\nevent_date: 2027-03-14\n"
        f"days_per_week: {days_per_week}\nextras: {{}}\n"
    )
    week_two = [s for s in plan["sessions"] if s["week"] == 2]
    # Every endurance discipline survives the trim. Strength may or may not:
    # it is ranked last precisely so the gym goes before a discipline does.
    assert {"run", "cycle", "swim"} <= {s["sport"] for s in week_two}
    assert {s["sport"] for s in week_two} <= {"run", "cycle", "swim", "strength"}
    training_days = {s["date"] for s in week_two}
    assert len(training_days) <= days_per_week


def test_extras_avoid_key_sessions_and_are_never_pushed():
    plan = _plan(MINIMAL + "extras: {strength: 2, yoga: 1}\n")
    week_two = [s for s in plan["sessions"] if s["week"] == 2]
    extras = [s for s in week_two if s["is_extra"]]
    assert len(extras) == 3
    key_dates = {s["date"] for s in week_two if s["is_key"]}
    assert not key_dates & {s["date"] for s in extras}
    assert all(training.session_to_build_args(s) is None for s in extras)


def test_real_sessions_carry_planner_build_args():
    plan = _plan()
    session = next(s for s in plan["sessions"] if not s["is_extra"])
    sport, workout_type, params = training.session_to_build_args(session)
    assert workout_type in planner.WORKOUT_TYPES[sport]
    assert params  # enough to hand straight to planner.build_plan


# --- intensity targets ---------------------------------------------------------


def test_description_targets_win_over_history():
    activities = [
        {
            "id": "2026-08-01T07:00:00",
            "type": "run",
            "date": "2026-08-01",
            "distance_km": 5.0,
            "duration_seconds": 1500,
        }
    ]
    plan = _plan(MINIMAL + 'targets:\n  run_5k: "24:00"\n', activities)
    assert plan["targets"]["run_5k_seconds"] == 1440
    assert plan["targets"]["why"]["run_5k_seconds"] == "set in the plan description"


def test_targets_fall_back_when_there_is_no_history():
    plan = _plan()
    assert (
        plan["targets"]["run_5k_seconds"] == training.FALLBACK_TARGETS["run_5k_seconds"]
    )
    assert "no recent history" in plan["targets"]["why"]["run_5k_seconds"]


def test_targets_reach_the_session_params():
    plan = _plan(MINIMAL + "targets:\n  bike_ftp: 250\n")
    intervals = next(
        s
        for s in plan["sessions"]
        if s["sport"] == "cycle" and s["session_type"] == "intervals"
    )
    long_ride = next(
        s
        for s in plan["sessions"]
        if s["sport"] == "cycle" and s["session_type"] == "long"
    )
    assert intervals["params"]["target_watts"] == 250
    # Steady riding sits below threshold, never at it.
    assert long_ride["params"]["target_watts"] < 250


# --- completion ----------------------------------------------------------------


def test_match_completion_marks_sessions_within_a_day():
    sessions = [
        {"date": "2026-09-01", "sport": "run", "is_extra": False},
        {"date": "2026-09-05", "sport": "run", "is_extra": False},
    ]
    activities = [{"type": "run", "date": "2026-09-02"}]
    matched = training.match_completion(sessions, activities)
    assert [s["completed"] for s in matched] == [True, False]


def test_one_activity_cannot_complete_two_sessions():
    sessions = [
        {"date": "2026-09-01", "sport": "cycle", "is_extra": False},
        {"date": "2026-09-02", "sport": "cycle", "is_extra": False},
    ]
    activities = [{"type": "cycle", "date": "2026-09-01"}]
    matched = training.match_completion(sessions, activities)
    assert [s["completed"] for s in matched] == [True, False]


def test_extras_and_wrong_sports_never_match():
    sessions = [
        {"date": "2026-09-01", "sport": "strength", "is_extra": True},
        {"date": "2026-09-01", "sport": "swim", "is_extra": False},
    ]
    activities = [{"type": "run", "date": "2026-09-01"}]
    matched = training.match_completion(sessions, activities)
    assert [s["completed"] for s in matched] == [False, False]


# --- sync windows --------------------------------------------------------------


def test_sync_window_is_idempotent_once_scheduled():
    sessions = [
        {"date": "2026-08-26", "is_extra": False, "status": "planned"},
        {"date": "2026-08-27", "is_extra": False, "status": "scheduled"},
        {"date": "2026-09-30", "is_extra": False, "status": "planned"},  # beyond it
        {"date": "2026-08-26", "is_extra": True, "status": "planned"},  # never pushed
    ]
    due = training.sync_window(sessions, REFERENCE, 14)
    assert [s["date"] for s in due] == ["2026-08-26"]


def test_future_scheduled_ignores_the_past():
    sessions = [
        {"date": "2026-08-01", "status": "scheduled"},
        {"date": "2026-09-01", "status": "scheduled"},
        {"date": "2026-09-02", "status": "planned"},
    ]
    assert [s["date"] for s in training.future_scheduled(sessions, REFERENCE)] == [
        "2026-09-01"
    ]


# --- starting volume -----------------------------------------------------------


def _rides(weeks_back: int, per_week: int, seconds: int) -> list[dict]:
    """Synthetic cycling history: `per_week` rides a week for `weeks_back`
    complete weeks ending the week before REFERENCE. REFERENCE is a Monday, so
    subtracting whole weeks lands on a Monday and the rides stay inside their
    own ISO week — which matters, since the measurement is per-week."""
    out = []
    for week in range(1, weeks_back + 1):
        monday = REFERENCE - timedelta(weeks=week)
        for n in range(per_week):
            day = monday + timedelta(days=n)
            out.append(
                {
                    "id": f"{day.isoformat()}T07:0{n}:00",
                    "type": "cycle",
                    "date": day.isoformat(),
                    "distance_km": seconds / 120,
                    "duration_seconds": seconds,
                }
            )
    return out


def _week_hours(plan: dict) -> dict:
    hours: dict = {}
    for session in plan["sessions"]:
        args = training.session_to_build_args(session)
        if args:
            hours[session["week"]] = (
                hours.get(session["week"], 0) + planner.estimate_seconds(*args) / 3600
            )
    return hours


SPORTIVE = "goal: cycle_100k_sportive\nevent_date: 2027-03-14\n"


def test_starting_volume_scales_down_for_a_rider_barely_training():
    plan = _plan(SPORTIVE, _rides(8, 1, 1800))  # ~0.5h/week
    assert plan["volume"]["start_scale"] == training.VOLUME_SCALE_MIN
    assert "0.5h/week" in plan["volume"]["why"]


def test_starting_volume_leaves_a_well_trained_rider_alone():
    plan = _plan(SPORTIVE, _rides(8, 4, 5400))  # ~6h/week, near the template
    assert plan["volume"]["start_scale"] > 0.8


def test_no_history_in_the_goals_sports_means_unknown_not_untrained():
    """A user who simply hasn't imported anything keeps the template's own
    volume — absence of data is not evidence of being untrained."""
    plan = _plan(SPORTIVE)
    assert plan["volume"]["start_scale"] == 1.0
    assert "no recent training" in plan["volume"]["why"]


def test_volume_is_measured_by_mean_not_median():
    """Training in 2 weeks out of 8 makes the median zero, which under a median
    would fall through to "too sparse to measure" and hand somebody barely
    riding the full template volume. The mean sees it."""
    sparse = _rides(1, 3, 3600) + _rides(8, 3, 3600)  # weeks 2-7 back are empty
    plan = _plan(SPORTIVE, sparse)
    assert 0 < plan["volume"]["start_scale"] < 1.0
    assert "h/week" in plan["volume"]["why"]


def test_the_partial_current_week_does_not_drag_the_measurement_down():
    trained = _rides(8, 3, 3600)
    with_current = trained + [
        {
            "id": f"{REFERENCE.isoformat()}T07:00:00",
            "type": "cycle",
            "date": REFERENCE.isoformat(),
            "distance_km": 30,
            "duration_seconds": 3600,
        }
    ]
    # REFERENCE is a Monday, so the current week holds one ride and would
    # otherwise count as a near-empty week.
    assert (
        _plan(SPORTIVE, with_current)["volume"]["start_scale"]
        == _plan(SPORTIVE, trained)["volume"]["start_scale"]
    )


def test_description_volume_overrides_the_measurement():
    plan = _plan(SPORTIVE + "volume: 70\n", _rides(8, 4, 5400))
    assert plan["volume"]["start_scale"] == 0.7
    assert plan["volume"]["why"] == "set in the plan description"


@pytest.mark.parametrize("bad", ["volume: 20\n", "volume: 200\n", "volume: lots\n"])
def test_volume_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        training.parse_plan_spec(SPORTIVE + bad)


def test_a_scaled_plan_still_converges_to_the_goals_own_peak():
    """Scaling uniformly would start the rider in the right place but leave
    them under-prepared for a fixed-distance event."""
    scaled = _week_hours(_plan(SPORTIVE + "volume: 60\n"))
    full = _week_hours(_plan(SPORTIVE + "volume: 100\n"))
    assert scaled[1] < full[1] * 0.75  # opens materially easier
    assert max(scaled.values()) == pytest.approx(max(full.values()), rel=0.02)


def test_a_steep_ramp_is_flagged_rather_than_silently_built():
    plan = _plan(SPORTIVE + "volume: 60\n")
    assert any("grows" in w for w in plan["warnings"])
    assert not _plan(SPORTIVE + "volume: 100\n")["warnings"]


# --- plan length ---------------------------------------------------------------


def _at_length(goal: str, weeks: int, extra: str = "") -> dict:
    """The goal expanded over an explicit number of whole weeks."""
    event = date(2027, 6, 13)
    start = event - timedelta(weeks=weeks - 1)
    start -= timedelta(days=start.weekday())
    return _plan(
        f"goal: {goal}\nevent_date: {event.isoformat()}\n"
        f"start_date: {start.isoformat()}\nvolume: 100\n" + extra
    )


def _peak_long_session(plan: dict) -> int:
    """The plan's biggest long session — the summit the block climbs to."""
    return max(
        s["params"]["distance_m"]
        for s in plan["sessions"]
        if s.get("session_type") == "long" and "distance_m" in s.get("params", {})
    )


def test_start_date_sets_the_plan_length():
    assert _at_length("cycle_100k_sportive", 8)["weeks"] == 8
    assert _at_length("cycle_100k_sportive", 20)["weeks"] == 20


def test_a_plan_at_its_template_length_uses_the_reference_ramp():
    """Deriving the ramp must not change how any goal behaves by default."""
    for goal, template in training.GOAL_TEMPLATES.items():
        plan = _at_length(goal, template["weeks"])
        assert plan["progression"]["weekly_ramp_pct"] == float(
            training.REFERENCE_RAMP_PCT
        ), goal


def test_a_longer_plan_ramps_more_gently():
    short = _at_length("cycle_100k_sportive", 12)["progression"]["weekly_ramp_pct"]
    longer = _at_length("cycle_100k_sportive", 26)["progression"]["weekly_ramp_pct"]
    assert longer < short


def test_a_longer_plan_arrives_at_the_same_peak_not_a_higher_one():
    """The point of the derived ramp: extra weeks buy a gentler climb to the
    same summit, rather than compounding past it and pinning every long
    session at its clamp."""
    base = _peak_long_session(_at_length("cycle_100k_sportive", 12))
    for weeks in (16, 20, 26, 40):
        assert _peak_long_session(
            _at_length("cycle_100k_sportive", weeks)
        ) == pytest.approx(base, rel=0.02)


def test_a_shorter_plan_peaks_lower_rather_than_ramping_violently():
    """Chasing the full peak over four weeks would demand a ~70%/week ramp."""
    short = _at_length("cycle_100k_sportive", 5)
    assert short["progression"]["weekly_ramp_pct"] == float(training.REFERENCE_RAMP_PCT)
    assert _peak_long_session(short) < _peak_long_session(
        _at_length("cycle_100k_sportive", 12)
    )


@pytest.mark.parametrize("goal", ALL_GOALS)
def test_doubling_a_plans_length_does_not_pin_it_at_its_clamps(goal):
    template = training.GOAL_TEMPLATES[goal]
    plan = _at_length(goal, template["weeks"] * 2)
    pinned = total = 0
    for session in plan["sessions"]:
        if session["is_extra"]:
            continue
        scale = _scale_for(template, session)
        if scale is None:
            continue
        total += 1
        if session["params"][scale["param"]] == scale["max"]:
            pinned += 1
    if not total:
        # strength_program scales nothing — load is its whole progression.
        assert not training.volume_sports(goal)
        return
    assert pinned / total < 0.25


def test_an_explicit_ramp_overrides_the_derived_one():
    plan = _at_length(
        "cycle_100k_sportive", 26, "progression:\n  weekly_ramp_pct: 12\n"
    )
    assert plan["progression"]["weekly_ramp_pct"] == 12
    assert plan["progression"]["derived"] is False
    assert _at_length("cycle_100k_sportive", 26)["progression"]["derived"] is True


def test_week_roles_drive_both_the_curve_and_the_ramp():
    """Both read the same role list, so they cannot disagree about which weeks
    actually ramp."""
    roles = training._week_roles(
        ["base", "base", "base", "base", "build", "taper", "taper"], [3, 1]
    )
    assert roles == [
        "build",
        "build",
        "build",
        "recover",
        "build",
        "taper",
        "taper",
    ]


# --- scaled-param headroom -----------------------------------------------------


def _scale_for(template: dict, session: dict) -> dict | None:
    """The template entry a session came from, matched by weekday as well as
    type — a template may carry two sessions of the same type on different days
    with different scales, and matching on type alone reads one against the
    other's clamps."""
    for entry in template["weekly_sessions"]:
        if (entry["sport"], entry["session_type"]) == (
            session["sport"],
            session["session_type"],
        ) and entry["day"] == date.fromisoformat(session["date"]).weekday():
            return entry["scale"]
    return None


def test_the_tt_block_session_progresses_by_extending_the_block():
    """A 2-5 rep count is too coarse to express a progression at all; the TT
    plans lengthen the sustained block toward race duration instead."""
    for goal in ("cycle_25k_tt", "cycle_40k_tt"):
        template = training.GOAL_TEMPLATES[goal]
        plan = _at_length(goal, template["weeks"])
        entry = next(
            e for e in template["weekly_sessions"] if e["scale"]["param"] == "work"
        )
        blocks = [
            s["params"]["work"]
            for s in sorted(plan["sessions"], key=lambda x: x["week"])
            if (s["sport"], s["session_type"])
            == (entry["sport"], entry["session_type"])
            and date.fromisoformat(s["date"]).weekday() == entry["day"]
        ]
        assert len(set(blocks)) >= len(blocks) - 2, f"{goal}: barely progresses"
        assert blocks[0] < max(blocks), f"{goal}: never grows"
        assert not [
            b for b in blocks if b in (entry["scale"]["min"], entry["scale"]["max"])
        ]


@pytest.mark.parametrize("goal", ALL_GOALS)
def test_scaled_params_keep_headroom_inside_their_clamps(goal):
    """A clamp that binds often flattens the curve. Hitting the floor on a
    recovery or taper week is the floor working; sitting at the ceiling is not.
    """
    template = training.GOAL_TEMPLATES[goal]
    plan = _at_length(goal, template["weeks"])
    at_max = total = 0
    for session in plan["sessions"]:
        if session["is_extra"]:
            continue
        scale = _scale_for(template, session)
        if scale is None:
            continue
        total += 1
        if session["params"][scale["param"]] == scale["max"]:
            at_max += 1
    if not total:
        assert not training.volume_sports(goal)
        return
    assert at_max / total <= 0.10, f"{goal}: {at_max}/{total} sessions pinned at max"


# --- building frequency across the plan ----------------------------------------


RAMPED = (
    "goal: cycle_100k_sportive\nevent_date: 2026-12-13\nstart_date: 2026-09-07\n"
    "days_per_week: [2, 4]\nvolume: 100\n"
)


def _rides_per_week(plan: dict) -> list[int]:
    return [
        len({s["date"] for s in w["sessions"] if not s["is_extra"]})
        for w in training.group_by_week(plan["sessions"])
    ]


def test_days_per_week_accepts_a_range_and_builds_frequency():
    plan = _plan(RAMPED)
    counts = _rides_per_week(plan)
    assert counts[0] == 2
    assert max(counts) == 4
    assert counts == sorted(counts[:-1]) + counts[-1:]  # never drops mid-build
    assert plan["days_per_week"] == {"start": 2, "end": 4}


def test_frequency_is_held_through_the_taper():
    """A taper cuts volume, not frequency — dropping a session in race week
    would lose the sharpening the taper exists for."""
    plan = _plan(RAMPED)
    weeks = training.group_by_week(plan["sessions"])
    taper = [w for w in weeks if w["phase"] == "taper"]
    # The final week loses only whatever falls on race day itself.
    assert len({s["date"] for s in taper[0]["sessions"] if not s["is_extra"]}) == 4


def test_a_plain_days_per_week_still_means_a_fixed_week():
    counts = _rides_per_week(
        _plan(RAMPED.replace("days_per_week: [2, 4]", "days_per_week: 3"))
    )
    assert set(counts[:-1]) == {3}


def test_frequency_builds_in_priority_order():
    """The sessions that arrive later are the lower-priority ones, so a
    two-ride week is still the two rides that matter most."""
    plan = _plan(RAMPED)
    weeks = training.group_by_week(plan["sessions"])
    first = {s["session_type"] for s in weeks[0]["sessions"] if not s["is_extra"]}
    last = {s["session_type"] for s in weeks[-3]["sessions"] if not s["is_extra"]}
    assert first < last  # a strict subset: nothing is swapped out, only added
    assert "long" in first


def test_extras_keep_pace_with_the_weeks_own_sessions():
    plan = _plan(RAMPED + "extras: {strength: 1}\n")
    for week in training.group_by_week(plan["sessions"]):
        extras = [s for s in week["sessions"] if s["is_extra"]]
        assert len(extras) == 1
        key_dates = {s["date"] for s in week["sessions"] if s["is_key"]}
        assert extras[0]["date"] not in key_dates


def test_the_opening_week_is_measured_against_its_own_smaller_session_list():
    """With frequency building, week 1 has fewer sessions than the template —
    measuring recent training against the full week would understate it."""
    # ~4h/week: enough that neither reading hits the clamp, so the comparison
    # is between the two measurements rather than between two floors.
    history = _rides(8, 4, 3600)
    ramped = _plan(RAMPED.replace("volume: 100\n", ""), history)
    fixed = _plan(
        RAMPED.replace("volume: 100\n", "").replace(
            "days_per_week: [2, 4]", "days_per_week: 4"
        ),
        history,
    )
    assert training.VOLUME_SCALE_MIN < fixed["volume"]["start_scale"]
    assert ramped["volume"]["start_scale"] > fixed["volume"]["start_scale"]


@pytest.mark.parametrize(
    "bad",
    [
        "days_per_week: [4, 2]\n",  # frequency must not fall
        "days_per_week: [2]\n",
        "days_per_week: [2, 4, 6]\n",
        "days_per_week: [0, 4]\n",
        "days_per_week: [2, 9]\n",
    ],
)
def test_days_per_week_range_rejects(bad):
    with pytest.raises(ValueError):
        training.parse_plan_spec(
            "goal: cycle_100k_sportive\nevent_date: 2026-12-13\n" + bad
        )


# --- benchmarks and pace progression -------------------------------------------


def _benchmarks(plan: dict) -> list[dict]:
    return [s for s in plan["sessions"] if s.get("is_benchmark")]


def test_intensity_targets_do_not_drift_across_the_plan():
    """Targets are measured, never projected: the same session type carries the
    same target in week 1 and at the peak. Guessing a future pace risks
    prescribing work the athlete cannot complete."""
    plan = _plan(SPORTIVE)
    longs = [
        s["params"]["target_watts"]
        for s in sorted(plan["sessions"], key=lambda x: x["week"])
        if s.get("session_type") == "long" and "target_watts" in s.get("params", {})
    ]
    assert len(set(longs)) == 1
    # ...while the volume on that same session very much does move.
    distances = {
        s["params"]["distance_m"]
        for s in plan["sessions"]
        if s.get("session_type") == "long"
    }
    assert len(distances) > 1


@pytest.mark.parametrize("goal", ALL_GOALS)
def test_every_goal_schedules_a_re_test(goal):
    """Without a benchmark the plan has no moment where it re-measures, so a
    pace can never legitimately improve."""
    plan = _at_length(goal, training.GOAL_TEMPLATES[goal]["weeks"])
    assert _benchmarks(plan), f"{goal} never re-tests"


def test_benchmarks_land_on_recovery_weeks():
    """You test rested — that is what makes one test comparable to the next."""
    plan = _at_length("standard_triathlon", 22, "days_per_week: [3, 5]\n")
    roles = training._week_roles(
        [w["phase"] for w in training.group_by_week(plan["sessions"])],
        plan["spec"]["progression"]["build_recover"],
    )
    for session in _benchmarks(plan):
        assert roles[session["week"] - 1] == "recover"
        assert session["phase"] != "taper"


def test_benchmarks_replace_a_session_rather_than_adding_one():
    with_tests = _at_length("run_half", 12)
    without = _at_length("run_half", 12, "benchmarks: false\n")
    assert len(with_tests["sessions"]) == len(without["sessions"])
    assert _benchmarks(with_tests) and not _benchmarks(without)


def test_benchmarks_take_turns_between_a_multisport_goals_disciplines():
    """Every testable discipline gets a turn, and none is tested twice running
    while another is waiting — the rotation picks whichever has gone longest."""
    plan = _at_length("standard_triathlon", 22, "days_per_week: [3, 5]\n")
    sports = [s["sport"] for s in _benchmarks(plan)]
    assert set(sports) == {"run", "cycle", "swim", "strength"}
    assert all(a != b for a, b in zip(sports, sports[1:]))


def test_a_benchmark_is_unscaled_and_untargeted():
    """A 3km test is only a benchmark if it is the same 3km every time, at an
    open effort rather than a prescribed pace."""
    plan = _at_length("standard_triathlon", 22, "days_per_week: [3, 5]\n")
    runs = [s for s in _benchmarks(plan) if s["sport"] == "run"]
    assert len({s["params"]["test_distance_m"] for s in runs}) == 1
    for session in _benchmarks(plan):
        assert "target_pace" not in session["params"]
        assert "target_watts" not in session["params"]
        sport, workout_type, params = training.session_to_build_args(session)
        planner.build_plan(sport, workout_type, params, session["date"])


def test_re_importing_after_a_test_re_derives_the_targets():
    """The whole point of the benchmark: do the test, sync it back, re-import,
    and the remaining weeks rebuild at the fitness you just demonstrated."""
    before = [
        {
            "id": "a",
            "type": "run",
            "date": "2026-08-01",
            "distance_km": 5.0,
            "duration_seconds": 1800,
        }
    ]
    after = before + [
        {
            "id": "b",
            "type": "run",
            "date": "2026-10-01",
            "distance_km": 5.0,
            "duration_seconds": 1350,
        }
    ]
    spec = training.parse_plan_spec(
        "goal: run_half\nevent_date: 2027-02-07\nstart_date: 2026-09-07\n"
    )
    first = training.expand_plan(spec, before, date(2026, 8, 24))
    second = training.expand_plan(spec, after, date(2026, 10, 15))
    assert second["targets"]["run_5k_seconds"] < first["targets"]["run_5k_seconds"]
    fast = [
        s["params"]["target_pace"]
        for s in second["sessions"]
        if s.get("session_type") == "long"
    ][0]
    slow = [
        s["params"]["target_pace"]
        for s in first["sessions"]
        if s.get("session_type") == "long"
    ][0]
    assert fast < slow


@pytest.mark.parametrize("bad", ["benchmarks: yes please\n", "benchmarks: 3\n"])
def test_benchmarks_rejects_non_boolean(bad):
    with pytest.raises(ValueError):
        training.parse_plan_spec(SPORTIVE + bad)


# --- retargeting ---------------------------------------------------------------

RETARGET_SPEC = "goal: run_half\nevent_date: 2027-02-07\nstart_date: 2026-08-24\n"
SLOW_5K = [
    {
        "id": "a",
        "type": "run",
        "date": "2026-08-01",
        "distance_km": 5.0,
        "duration_seconds": 1916,
    }
]
FAST_5K = SLOW_5K + [
    {
        "id": "b",
        "type": "run",
        "date": "2026-08-20",
        "distance_km": 5.0,
        "duration_seconds": 1374,
    }
]


def _retargeted(plan_activities=SLOW_5K, new_activities=FAST_5K):
    """A plan built on slow history, then retargeted against faster history."""
    spec = training.parse_plan_spec(RETARGET_SPEC)
    plan = training.expand_plan(spec, plan_activities, REFERENCE)
    targets = training.derive_targets(spec, new_activities, REFERENCE)
    return plan, training.retarget_sessions(plan, targets, REFERENCE)


def _volume_of(plan):
    return {
        (s["date"], s["sport"], s["session_type"]): {
            k: v for k, v in s["params"].items() if k not in training._INTENSITY_PARAMS
        }
        for s in plan["sessions"]
        if not s["is_extra"]
    }


def test_retarget_rewrites_only_future_unscheduled_sessions():
    spec = training.parse_plan_spec(RETARGET_SPEC)
    plan = training.expand_plan(spec, SLOW_5K, REFERENCE)
    future = [
        s for s in plan["sessions"] if not s["is_extra"] and s["date"] >= "2026-08-24"
    ]
    future[3]["status"] = "scheduled"
    frozen = dict(future[3]["params"])

    summary = training.retarget_sessions(
        plan, training.derive_targets(spec, FAST_5K, REFERENCE), REFERENCE
    )
    assert summary["retargeted"] > 0
    assert summary["frozen"] == 1
    # A pushed workout is a frozen copy on the Garmin account with no update
    # endpoint, so rewriting it locally would only desynchronise the two.
    assert future[3]["params"] == frozen


def test_retarget_never_changes_volume():
    """The load-bearing invariant: a session's `scale` rule is not stored, so
    volume is not recoverable from a session alone — only intensity is."""
    spec = training.parse_plan_spec(RETARGET_SPEC)
    plan = training.expand_plan(spec, SLOW_5K, REFERENCE)
    before = _volume_of(plan)
    training.retarget_sessions(
        plan, training.derive_targets(spec, FAST_5K, REFERENCE), REFERENCE
    )
    assert _volume_of(plan) == before


def test_retarget_leaves_benchmarks_and_extras_alone():
    plan, _ = _retargeted()
    for session in plan["sessions"]:
        if session.get("is_benchmark"):
            assert not any(k in session["params"] for k in training._INTENSITY_PARAMS)
        if session["is_extra"]:
            assert "params" not in session  # would KeyError if we touched one


def test_retarget_regenerates_the_workout_name():
    """A stale name would disagree with the payload that gets pushed."""
    plan, _ = _retargeted()
    for session in plan["sessions"]:
        if session["is_extra"]:
            continue
        assert session["workout_name"] == planner.workout_name(
            session["sport"], session["session_type"], session["params"]
        )


def test_retarget_matches_the_intensity_of_a_fresh_expansion():
    """The contract, made executable: retargeting gets you the same intensities
    a fresh import would. Only intensities — a fresh expansion also re-measures
    volume from newer history, which retarget deliberately does not."""
    spec = training.parse_plan_spec(RETARGET_SPEC)
    plan, _ = _retargeted()
    fresh = training.expand_plan(spec, FAST_5K, REFERENCE)
    fresh_by_key = {
        (s["date"], s["sport"], s["session_type"]): s for s in fresh["sessions"]
    }
    compared = 0
    for session in plan["sessions"]:
        other = fresh_by_key.get(
            (session["date"], session["sport"], session["session_type"])
        )
        if not other or session["is_extra"] or session.get("is_benchmark"):
            continue
        for key in training._INTENSITY_PARAMS:
            if key in session["params"] and key in other["params"]:
                assert session["params"][key] == other["params"][key]
                compared += 1
    assert compared > 0


def test_retarget_is_idempotent():
    spec = training.parse_plan_spec(RETARGET_SPEC)
    plan, _ = _retargeted()
    again = training.retarget_sessions(
        plan, training.derive_targets(spec, FAST_5K, REFERENCE), REFERENCE
    )
    assert again["retargeted"] == 0 and again["changed"] == []


def test_retarget_reports_the_old_and_new_targets():
    plan, summary = _retargeted()
    assert summary["old_targets"]["run_5k_seconds"] == 1916
    assert summary["new_targets"]["run_5k_seconds"] == 1374
    # Every eligible session is accounted for as either rewritten or already
    # on target — nothing falls between the two counts.
    eligible = training.retargetable(plan["sessions"], REFERENCE)
    assert summary["retargeted"] + summary["unchanged"] == len(eligible)


def test_an_implausible_derivation_falls_back_with_a_reason():
    """The real failure: a 500m from a 2021 drill session against a 1k from
    2025 produced a 35s/100m CSS — faster than the world record."""
    swims = [
        {
            "id": "s1",
            "type": "swim",
            "date": "2026-05-01",
            "distance_km": 0.81,
            "duration_seconds": 2461,
            "splits": {"500m_seconds": 1179.9},
        },
        {
            "id": "s2",
            "type": "swim",
            "date": "2026-08-01",
            "distance_km": 1.0,
            "duration_seconds": 1355,
        },
    ]
    spec = training.parse_plan_spec("goal: sprint_triathlon\nevent_date: 2026-11-15\n")
    targets = training.derive_targets(spec, swims, REFERENCE)
    low, high = planner.PLAUSIBLE_TARGETS["swim"]
    assert low <= targets["swim_css_100m"] <= high


# --- strength progression ------------------------------------------------------


STRENGTH = "goal: strength_program\nevent_date: 2027-06-13\n"


def _lifted(date_iso, exercise, reps, weight_kg):
    return {
        "type": "strength",
        "date": date_iso,
        "duration_seconds": 2700,
        "exercises": [
            {"name": exercise, "sets": [{"reps": reps, "weight_kg": weight_kg}]}
        ],
    }


def test_strength_weekly_e1rm_advances_only_on_build_weeks():
    roles = ["build", "build", "recover", "build", "taper"]
    weekly = training.strength_weekly_e1rm(100.0, 120.0, roles, 2.5)
    # First build week sits at the current figure; later ones add a step.
    assert weekly[0] == 100.0
    assert weekly[1] == 102.5
    # A deload dips without advancing: the week after resumes from 102.5.
    assert weekly[2] == pytest.approx(102.5 * training.STRENGTH_DELOAD_FACTOR)
    assert weekly[3] == 105.0
    assert weekly[4] == pytest.approx(105.0 * training.STRENGTH_TAPER_FACTOR)


def test_a_goal_further_off_than_the_plan_is_long_is_capped_and_warned():
    """Better to say the timeline doesn't reach than to prescribe a curve
    nobody could ride."""
    plan = _plan(STRENGTH + "targets: {deadlift_goal_kg: 300}\n")
    entry = plan["targets"]["strength"]["deadlift"]
    increment = training.lift_increment("deadlift")
    # Only build weeks advance, so they are the ones the cap applies to — a
    # deload week dips and the week after resumes, which is not a step.
    roles = training.plan_week_roles(plan)
    build = [v for v, role in zip(entry["by_week"], roles) if role == "build"]
    steps = [b - a for a, b in zip(build, build[1:])]
    assert max(steps) <= increment + 0.01
    assert entry["by_week"][-1] < 300
    assert any("deadlift" in w for w in plan["warnings"])


def test_an_underived_goal_is_what_the_plans_length_can_deliver():
    plan = _plan(STRENGTH)
    for lift, entry in plan["targets"]["strength"].items():
        roles = training.plan_week_roles(plan)
        assert entry["goal_e1rm_kg"] == pytest.approx(
            training.reachable_e1rm(
                entry["current_e1rm_kg"], roles, training.lift_increment(lift)
            ),
            abs=0.01,
        )
        # Derived, so it can never trip its own warning.
        assert not plan["warnings"]


def test_working_weight_round_trips_through_the_e1rm_formula():
    """The PB table and the plan must agree about the same set — both go
    through compute.estimated_1rm, one of them backwards."""
    from fit import compute

    for reps in (3, 5, 8, 10):
        e1rm = compute.estimated_1rm(100.0, reps)
        assert training.working_weight_from_1rm(e1rm, reps, 2.5) == 100.0


def test_each_week_gets_its_own_exercise_dicts():
    """A shallow copy of the template's params would have every week sharing
    one exercise list, and the last week written would win everywhere."""
    plan = _plan(STRENGTH)
    loads = {
        s["week"]: s["params"]["exercises"][0]["target_weight_kg"]
        for s in plan["sessions"]
        if s["sport"] == "strength" and not s.get("is_benchmark")
    }
    assert len(set(loads.values())) > 1


def test_retargeting_strength_moves_load_but_never_sets_or_reps():
    plan = _plan(STRENGTH)
    before = [
        (s["params"]["exercises"][0]["sets"], s["params"]["exercises"][0]["reps"])
        for s in plan["sessions"]
        if s["sport"] == "strength" and not s.get("is_benchmark")
    ]
    stronger = [_lifted("2026-08-20", lift, 3, 150.0) for lift in ("squat", "deadlift")]
    targets = training.derive_targets(plan["spec"], stronger, REFERENCE)
    summary = training.retarget_sessions(plan, targets, date(2026, 1, 1))

    assert summary["retargeted"] > 0
    after = [
        (s["params"]["exercises"][0]["sets"], s["params"]["exercises"][0]["reps"])
        for s in plan["sessions"]
        if s["sport"] == "strength" and not s.get("is_benchmark")
    ]
    assert after == before


def test_retargeting_keeps_an_explicit_goal_but_re_derives_an_implicit_one():
    """A re-test tells you where you are, not where you were going."""
    plan = _plan(STRENGTH + "targets: {deadlift_goal_kg: 200}\n")
    old_squat_goal = plan["targets"]["strength"]["squat"]["goal_e1rm_kg"]
    stronger = [_lifted("2026-08-20", "squat", 3, 150.0)]
    targets = training.derive_targets(plan["spec"], stronger, REFERENCE)
    training.retarget_sessions(plan, targets, date(2026, 1, 1))

    assert plan["targets"]["strength"]["deadlift"]["goal_e1rm_kg"] == 200
    assert plan["targets"]["strength"]["squat"]["goal_e1rm_kg"] > old_squat_goal


def test_a_strength_benchmark_tests_the_lift_its_session_leads_with():
    plan = _plan(STRENGTH)
    tests = [s for s in plan["sessions"] if s.get("is_benchmark")]
    assert tests
    for session in tests:
        assert session["sport"] == "strength"
        assert session["params"]["exercise"] in training.template_lifts(
            "strength_program"
        )
        # Untargeted: a test at a prescribed load is not a test.
        assert "target_weight_kg" not in session["params"]


def test_strength_volume_is_not_measured_against_endurance_history():
    """A strength session's size never moves, so counting it would scale a
    triathlete down for gym work the plan can't adjust anyway."""
    assert "strength" not in training.volume_sports("sprint_triathlon")
    assert training.volume_sports("strength_program") == set()


def test_a_lift_goal_for_a_goal_that_never_lifts_is_rejected():
    with pytest.raises(ValueError):
        training.parse_plan_spec(
            "goal: run_5k\nevent_date: 2027-03-14\ntargets: {squat_goal_kg: 120}\n"
        )
