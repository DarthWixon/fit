"""Tests for training.py: plan-description parsing (including the YAML 1.1
coercion traps), the weekly date/volume math, StrongLifts progression, and
completion matching. Deliberately narrow — the engine's arithmetic, not the
CLI around it."""

from datetime import date, timedelta

import pytest

from fit import compute, planner, templates, training

pytest.importorskip("yaml", reason="parse_plan_spec needs the optional [train] extra")

REFERENCE = date(2026, 8, 24)  # a Monday
PLAN = "start_date: 2026-08-24\nend_date: 2026-11-15\n"  # 12 whole weeks


def _plan(text=PLAN, activities=None, volume=None):
    spec = training.parse_plan_spec(text)
    return training.expand_plan(spec, activities or [], REFERENCE, volume=volume)


# --- description parsing -------------------------------------------------------


def test_parse_plan_spec_defaults():
    spec = training.parse_plan_spec(PLAN)
    assert spec["days_per_week"] == templates.DAYS_PER_WEEK
    assert spec["targets"] == {}
    assert "volume" not in spec


@pytest.mark.parametrize(
    "text",
    [
        "end_date: 2026-11-15\n",  # no start_date
        "start_date: 2026-08-24\n",  # no end_date
        "start_date: 2026-11-15\nend_date: 2026-08-24\n",  # backwards
        PLAN + "goal: cycle_strength\n",  # the old description's keys are gone
        PLAN + "days_per_week: 9\n",
        PLAN + "days_per_week: [6, 5]\n",  # frequency only builds
        PLAN + "days_per_week: true\n",
        PLAN + "targets: {curl_goal_kg: 40}\n",
        PLAN + "targets: {squat_goal_kg: 900}\n",
        "- a list\n",
        "start_date: [unclosed\n",
    ],
)
def test_parse_plan_spec_rejects(text):
    with pytest.raises(ValueError):
        training.parse_plan_spec(text)


def test_parse_plan_spec_survives_yaml_coercion():
    """PyYAML is YAML 1.1: an unquoted date resolves to datetime.date. It must
    land on the same normalised value as its quoted form."""
    unquoted = training.parse_plan_spec(PLAN + "targets: {squat_goal_kg: 120}\n")
    quoted = training.parse_plan_spec(
        "start_date: '2026-08-24'\nend_date: '2026-11-15'\n"
        "targets: {squat_goal_kg: 120.0}\n"
    )
    assert unquoted == quoted
    assert unquoted["start_date"] == "2026-08-24"


# --- the weekly shape ----------------------------------------------------------


def test_expand_plan_covers_every_week_within_its_dates():
    plan = _plan()
    weeks = training.group_by_week(plan["sessions"])
    assert plan["weeks"] == 12
    assert [w["week"] for w in weeks] == list(range(1, 13))
    assert all(
        plan["start_date"] <= s["date"] <= plan["end_date"] for s in plan["sessions"]
    )


def _ride_km_by_week(plan, weekday):
    return {
        s["week"]: s["params"]["distance_m"]
        for s in plan["sessions"]
        if s["session_type"] == "long"
        and date.fromisoformat(s["date"]).weekday() == weekday
    }


def test_rides_grow_every_week_with_no_deload():
    """No event, so no recovery weeks or taper: the rides only climb."""
    sundays = list(_ride_km_by_week(_plan(PLAN + "volume: 100\n"), 6).values())
    assert sundays == sorted(sundays)
    assert sundays[-1] > sundays[0]


def test_the_plan_is_plain_stronglifts():
    """A/B/A one week, B/A/B the next — so each workout comes round three times
    a fortnight, and squats every session. No deload weeks and no tests: the
    bar only comes down by StrongLifts' own failure rule."""
    plan = _plan()
    squats = [
        e["target_weight_kg"]
        for s in plan["sessions"]
        if s["sport"] == "strength"
        for e in s["params"]["exercises"]
        if e["exercise"] == "squat"
    ]
    assert squats == sorted(squats)

    def workouts(week):
        return [
            (
                "A"
                if "bench_press" in [e["exercise"] for e in s["params"]["exercises"]]
                else "B"
            )
            for s in plan["sessions"]
            if s["week"] == week and s["sport"] == "strength"
        ]

    assert workouts(1) == ["A", "B", "A"]
    assert workouts(2) == ["B", "A", "B"]


def test_every_session_builds_and_names_itself():
    """Every session must hand straight to planner.build_plan, and the stored
    name is what `fit train show` and the Garmin calendar display — the
    payload is rebuilt from params at sync time, so the two must agree."""
    plan = _plan()
    for session in plan["sessions"]:
        args = training.session_to_build_args(session)
        built = planner.build_plan(*args, session["date"])
        assert built["workout_name"] == session["workout_name"]
        # Only a barbell lift has an e1RM to load from: a pull-up "@ 55kg"
        # would be a target invented from a fallback.
        for e in session["params"].get("exercises", []):
            assert "target_weight_kg" not in e or e["exercise"] in (
                training.LIFT_INCREMENT_KG
            )
    # next_working_weight judges a lift against one rep scheme.
    schemes = {}
    for session in templates.WEEKLY_SESSIONS:
        for e in session["params"].get("exercises", []):
            schemes.setdefault(e["exercise"], set()).add((e["sets"], e["reps"]))
    assert all(len(s) == 1 for s in schemes.values())


def test_scaled_params_keep_headroom_inside_their_clamps():
    """A clamp that binds often flattens the curve: sitting at the ceiling
    means a base needs lowering. Sessions are matched to their template entry by
    weekday, since the two long rides carry different scales."""
    plan = _plan(PLAN + "volume: 100\n")
    by_day = {s["day"]: s["scale"] for s in templates.WEEKLY_SESSIONS if s["scale"]}
    at_max = total = 0
    for session in plan["sessions"]:
        scale = by_day.get(date.fromisoformat(session["date"]).weekday())
        if scale is None or session["sport"] == "strength":
            continue
        total += 1
        value = session["params"][scale["param"]]
        assert scale["min"] <= value <= scale["max"], f"{value} escaped {scale}"
        at_max += value == scale["max"]
    assert total and at_max / total <= 0.10, f"{at_max}/{total} pinned at max"


def test_days_per_week_builds_frequency_in_priority_order():
    """Saturday ranks last, so a plan building from five days to six opens
    without it and adds it — nothing is swapped out, only added."""
    plan = _plan(PLAN + "days_per_week: [5, 6]\n")
    weeks = training.group_by_week(plan["sessions"])
    days = [
        {date.fromisoformat(s["date"]).weekday() for s in w["sessions"]} for w in weeks
    ]
    assert 5 not in days[0] and 5 in days[-1]
    assert all(a <= b for a, b in zip(days, days[1:]))
    assert plan["days_per_week"] == {"start": 5, "end": 6}


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
        if session["sport"] == "cycle":
            seconds = planner.estimate_seconds(*training.session_to_build_args(session))
            hours[session["week"]] = hours.get(session["week"], 0) + seconds / 3600
    return hours


def test_starting_volume_scales_down_for_a_rider_barely_training():
    plan = _plan(activities=_rides(8, 1, 1800))  # ~0.5h/week
    assert plan["volume"]["start_scale"] == training.VOLUME_SCALE_MIN
    assert "0.5h/week" in plan["volume"]["why"]
    # A rider already near the opening week is left alone...
    assert _plan(activities=_rides(8, 3, 5400))["volume"]["start_scale"] > 0.8
    # ...and an explicit `volume:` wins over the measurement either way.
    override = _plan(PLAN + "volume: 70\n", _rides(8, 3, 5400))
    assert override["volume"]["start_scale"] == 0.7
    assert override["volume"]["why"] == "set in the plan description"


def test_no_riding_history_means_unknown_not_untrained():
    """A user who simply hasn't imported anything keeps the plan's own volume
    — absence of data is not evidence of being untrained."""
    plan = _plan()
    assert plan["volume"]["start_scale"] == 1.0
    assert "no recent riding" in plan["volume"]["why"]


def test_volume_is_measured_by_mean_not_median():
    """Riding in 2 weeks out of 8 makes the median zero, which under a median
    would fall through to "too sparse to measure" and hand somebody barely
    riding the full volume. The mean sees it."""
    sparse = [
        r
        for r in _rides(8, 3, 10800)
        if date.fromisoformat(r["date"]) < REFERENCE - timedelta(weeks=7)
        or date.fromisoformat(r["date"]) >= REFERENCE - timedelta(weeks=1)
    ]
    plan = _plan(activities=sparse)
    assert training.VOLUME_SCALE_MIN < plan["volume"]["start_scale"] < 1.0
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
        _plan(activities=with_current)["volume"]["start_scale"]
        == _plan(activities=trained)["volume"]["start_scale"]
    )


def test_a_scaled_plan_still_converges_to_the_plans_own_level():
    """Scaling uniformly would start the rider in the right place but leave
    every later week short of what the plan meant."""
    scaled = _week_hours(_plan(PLAN + "volume: 60\n"))
    full = _week_hours(_plan(PLAN + "volume: 100\n"))
    assert scaled[1] < full[1] * 0.75  # opens materially easier
    last = max(full)
    assert scaled[last] == pytest.approx(full[last], rel=0.02)


def test_the_opening_week_is_measured_against_its_own_smaller_session_list():
    """With frequency building, week 1 has fewer rides than the full week —
    measuring recent riding against the full week would understate it."""
    history = _rides(8, 2, 5400)
    ramped = _plan(PLAN + "days_per_week: [5, 6]\n", history)
    fixed = _plan(PLAN + "days_per_week: 6\n", history)
    assert training.VOLUME_SCALE_MIN < fixed["volume"]["start_scale"]
    assert ramped["volume"]["start_scale"] > fixed["volume"]["start_scale"]


def test_a_pinned_volume_survives_new_history():
    """Starting volume is a decision about where you were, taken once."""
    pinned = {"start_scale": 0.6, "why": "pinned at import"}
    plan = _plan(activities=_rides(8, 3, 5400), volume=pinned)
    assert plan["volume"] == pinned


# --- strength progression ------------------------------------------------------


def _lifted(date_iso, exercise, reps, weight_kg, sets=1):
    return {
        "type": "strength",
        "date": date_iso,
        "duration_seconds": 2700,
        "exercises": [
            {"name": exercise, "sets": [{"reps": reps, "weight_kg": weight_kg}] * sets}
        ],
    }


def _squats(plan):
    return [
        (s["date"], e["target_weight_kg"])
        for s in plan["sessions"]
        if s["sport"] == "strength"
        for e in s["params"]["exercises"]
        if e["exercise"] == "squat"
    ]


def test_a_miss_holds_the_weight_and_the_third_running_resets_it():
    def lifted(*sets):
        return {"sets": [{"reps": r, "weight_kg": w} for w, r in sets]}

    def next_squat(history):
        return training.next_working_weight(history, 5, 5, 2.5, 60.0)

    done = lifted((20, 5), *[(70, 5)] * 5)
    missed = lifted((20, 5), *[(70, 5)] * 4, (70, 3))
    assert next_squat([done]) == 72.5
    assert next_squat([missed, missed]) == 70.0
    assert next_squat([missed, missed, missed]) == 62.5  # 63, loadable


def test_lifting_in_the_plan_moves_the_next_session():
    """Each future session assumes the one before goes to plan; a session
    done short holds the weight from there on."""
    plan = _plan()
    (first_day, first), (second_day, _) = _squats(plan)[:2]
    # Today is past the first session, which was lifted short.
    later = date.fromisoformat(second_day) - timedelta(days=1)
    missed = [_lifted(first_day, "squat", 3, first, sets=5)]
    spec = training.parse_plan_spec(PLAN)
    held = dict(_squats(training.expand_plan(spec, missed, later)))
    assert held[second_day] == first


def test_bodyweight_reps_rise_on_every_set_done_and_stop_at_the_cap():
    def did(*reps):
        return {"sets": [{"reps": r} for r in reps]}

    def next_pull_ups(history):
        return training.next_rep_target(history, 3, 3, 5)

    assert next_pull_ups([did(3, 3, 3)]) == 4
    assert next_pull_ups([did(3, 3, 2)]) == 3  # a miss holds, never drops
    assert next_pull_ups([did(9, 9, 9)] * 4) == 5


def test_a_goal_further_off_than_the_plan_is_long_is_capped_and_warned():
    """Better to say the timeline doesn't reach than to prescribe a curve
    nobody could lift."""
    plan = _plan(PLAN + "targets: {deadlift_goal_kg: 300}\n")
    loads = [
        e["target_weight_kg"]
        for s in plan["sessions"]
        if s["sport"] == "strength"
        for e in s["params"]["exercises"]
        if e["exercise"] == "deadlift"
    ]
    steps = [b - a for a, b in zip(loads, loads[1:])]
    assert max(steps) <= training.lift_increment("deadlift")
    assert loads[-1] < training.working_weight_from_1rm(300, 5, 2.5)
    assert any("deadlift" in w for w in plan["warnings"])


def test_an_underived_goal_is_what_the_plans_length_can_deliver():
    plan = _plan()
    for entry in plan["targets"].values():
        assert entry["goal_e1rm_kg"] > entry["current_e1rm_kg"]
    # Derived, so it can never trip its own warning.
    assert not plan["warnings"]


def test_working_weight_round_trips_through_the_e1rm_formula():
    """The PB table and the plan must agree about the same set — both go
    through compute.estimated_1rm, one of them backwards."""
    for reps in (3, 5, 8, 10):
        e1rm = compute.estimated_1rm(100.0, reps)
        assert training.working_weight_from_1rm(e1rm, reps, 2.5) == 100.0
    # ...and lands on a loadable weight: 23.75kg is not one.
    press = compute.estimated_1rm(23.75, 10)
    assert training.working_weight_from_1rm(press, 10, 2.5) == 25.0


def test_each_session_gets_its_own_exercise_dicts():
    """A shallow copy of the template's params would have every week sharing
    one exercise list, and the last week written would win everywhere."""
    assert len({w for _, w in _squats(_plan())}) > 1


def test_re_deriving_strength_moves_load_but_never_sets_or_reps():
    def lifts(plan):
        return [
            (e["exercise"], e["sets"], e["reps"], e.get("target_weight_kg"))
            for s in plan["sessions"]
            if s["sport"] == "strength"
            for e in s["params"]["exercises"]
        ]

    weak = _plan()
    stronger = [_lifted("2026-08-20", lift, 3, 150.0) for lift in ("squat", "deadlift")]
    strong = _plan(activities=stronger)

    assert [x[:3] for x in lifts(strong)] == [x[:3] for x in lifts(weak)]
    assert [x[3] for x in lifts(strong)] != [x[3] for x in lifts(weak)]
    # StrongLifts' start for a returning lifter: the ten-rep weight, not the
    # five, since five sets of a 5RM fail by the second.
    e1rm = strong["targets"]["squat"]["current_e1rm_kg"]
    assert _squats(strong)[0][1] == training.working_weight_from_1rm(e1rm, 10, 2.5)


# --- completion ----------------------------------------------------------------


def test_match_completion_marks_sessions_in_the_same_week():
    sessions = [
        {"date": "2026-09-01", "sport": "cycle"},
        {"date": "2026-09-05", "sport": "cycle"},
    ]
    activities = [{"type": "cycle", "date": "2026-09-02"}]
    matched = training.match_completion(sessions, activities)
    assert [s["completed"] for s in matched] == [True, False]

    # The week is the unit, not a day window: Wednesday's session trained on
    # the Sunday is still that week's work (both ISO 2026-W37, 4 days apart).
    same_week = [{"date": "2026-09-09", "sport": "strength"}]
    matched = training.match_completion(
        same_week, [{"type": "strength", "date": "2026-09-13"}]
    )
    assert [s["completed"] for s in matched] == [True]

    # ...and the accepted mirror case: Sunday's session done on the Monday
    # after lands in W38, so it does not count.
    matched = training.match_completion(
        [{"date": "2026-09-13", "sport": "cycle"}],
        [{"type": "cycle", "date": "2026-09-14"}],
    )
    assert [s["completed"] for s in matched] == [False]

    # Closest pair first, and lifts must overlap: the one lift of the week
    # (Wednesday's squat) completes Wednesday, not Monday's deadlift day.
    def lifting(day, *lifts):
        exercises = [{"exercise": lift} for lift in lifts]
        return {"date": day, "sport": "strength", "params": {"exercises": exercises}}

    week = [lifting("2026-09-14", "deadlift"), lifting("2026-09-16", "squat")]
    squatted = {
        "type": "strength",
        "date": "2026-09-16",
        "exercises": [{"name": "squat"}],
    }
    matched = training.match_completion(week, [squatted])
    assert [s["completed"] for s in matched] == [False, True]
    matched = training.match_completion(week[:1], [squatted])
    assert [s["completed"] for s in matched] == [False]


def test_one_activity_cannot_complete_two_sessions():
    sessions = [
        {"date": "2026-09-01", "sport": "cycle"},
        {"date": "2026-09-02", "sport": "cycle"},
    ]
    activities = [{"type": "cycle", "date": "2026-09-01"}]
    matched = training.match_completion(sessions, activities)
    assert [s["completed"] for s in matched] == [True, False]


def test_a_moved_ride_warns_only_when_it_came_from_another_week():
    """A stale device prompts with last week's workout; a ride planned for
    Saturday and ridden on Sunday is the plan working."""
    pushed = [
        {"date": "2026-10-06", "sport": "cycle", "workout_id": 1},
        {"date": "2026-10-13", "sport": "cycle", "workout_id": 2},
        {"date": "2026-10-17", "sport": "cycle", "workout_id": 3},
    ]
    rides = [
        {"date": "2026-10-13", "workout_id": 1},
        {"date": "2026-10-18", "workout_id": 3},
    ]
    warned = training.followed_other_session(rides, pushed)
    assert [(w["date"], w["followed"]["workout_id"]) for w in warned] == [
        ("2026-10-13", 1)
    ]


# --- the Garmin ledger ---------------------------------------------------------

LIGHT = [_lifted("2026-08-01", "squat", 5, 60.0)]
HEAVY = LIGHT + [_lifted("2026-08-20", "squat", 5, 100.0)]


def _pushed_plan(activities=LIGHT):
    """A plan with its first two sessions pushed to Garmin: Monday's and
    Wednesday's lifting, both of which squat."""
    spec = training.parse_plan_spec(PLAN)
    plan = training.expand_plan(spec, activities, REFERENCE)
    due = plan["sessions"][:2]
    ledger = [training.ledger_entry(s, 100 + i, 200 + i) for i, s in enumerate(due)]
    return spec, plan, ledger


def test_a_pushed_session_keeps_what_was_actually_sent():
    """Garmin has no edit endpoint, so the watch holds the old copy — the plan
    must show that, not a fresh derivation."""
    spec, _, ledger = _pushed_plan(LIGHT)
    # Re-derive against heavier history: unpushed sessions move, pushed do not.
    fresh = training.expand_plan(spec, HEAVY, REFERENCE)
    overlaid = training.apply_pushed(fresh["sessions"], ledger)

    pushed = [s for s in overlaid if s["pushed"]]
    assert len(pushed) == len(ledger)
    for session, entry in zip(pushed, ledger):
        assert session["params"] == entry["params"]
        assert session["workout_name"] == entry["workout_name"]
        assert session["garmin_workout_id"] == entry["workout_id"]
        assert session["scheduled_workout_id"] == entry["schedule_id"]
    # ...and it is flagged, since the live derivation has moved away from it.
    assert all(s["stale"] for s in pushed)

    unmoved = training.apply_pushed(
        training.expand_plan(spec, LIGHT, REFERENCE)["sessions"], ledger
    )
    assert not any(s["stale"] for s in unmoved)


def test_the_ledger_key_survives_re_derivation():
    """(date, sport) must still name the same session after re-expanding, or
    the overlay would attach a workout to the wrong day."""
    spec = training.parse_plan_spec(PLAN)
    a = training.expand_plan(spec, LIGHT, REFERENCE)
    b = training.expand_plan(spec, HEAVY, REFERENCE)
    assert [training.ledger_key(s) for s in a["sessions"]] == [
        training.ledger_key(s) for s in b["sessions"]
    ]


def test_sync_finds_only_what_is_not_already_pushed():
    _, plan, ledger = _pushed_plan()
    overlaid = training.apply_pushed(plan["sessions"], ledger)
    due = training.sync_window(overlaid, REFERENCE, 14)
    assert due, "something should still be due"
    assert not any(
        training.ledger_key(s) in {(e["date"], e["sport"]) for e in ledger} for s in due
    )
    assert all(s["date"] <= (REFERENCE + timedelta(days=14)).isoformat() for s in due)
    # Trained already: refresh re-pushed today's finished ride as a duplicate.
    done = [{**s, "completed": True} if s is due[0] else s for s in overlaid]
    assert training.ledger_key(due[0]) not in {
        training.ledger_key(s) for s in training.sync_window(done, REFERENCE, 14)
    }


def test_clear_takes_only_future_ledger_rows():
    _, plan, ledger = _pushed_plan()
    past = [{**ledger[0], "date": "2026-01-05"}]
    assert training.future_pushed(ledger + past, REFERENCE) == ledger
    trained = [
        {**s, "completed": True}
        for s in plan["sessions"]
        if training.ledger_key(s) == (ledger[0]["date"], ledger[0]["sport"])
    ]
    assert training.future_pushed(ledger, REFERENCE, trained) == ledger[1:]


def test_an_old_plan_file_is_refused_rather_than_read_as_unpushed():
    """Replacing a plan this version cannot expand would drop its ledger and
    strand its calendar entries — that happened to three real sessions on
    2026-09-11 — so the shape is checked before anything trusts it."""
    spec, _, ledger = _pushed_plan()
    current = {"spec": spec, "created": "2026-08-24", "volume": {}, "pushed": ledger}
    assert training.unreadable_plan(current) is None

    # The description before this one had a goal and an event_date.
    old = {**current, "spec": {"goal": "cycle_strength", "event_date": "2027-01-31"}}
    assert training.unreadable_plan(old) == ledger
    # Empty is still a refusal, not an all-clear: a file with no ledger says
    # nothing about what is on the calendar.
    assert training.unreadable_plan({"spec": {}, "sessions": []}) == []
