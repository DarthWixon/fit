"""The pure engine behind `fit train`: a YAML plan description becomes a full
dated schedule, with lifting loads derived from the user's history. No I/O —
cli.py reads the text and hands it in.

**Nothing derived is stored.** expand_plan re-runs on every command, so loads
always reflect what was actually lifted and nothing has to be re-run. The one
thing that cannot be derived — what was pushed to Garmin — lives in a small
ledger (see "the Garmin ledger" below), and a pushed session renders from what
was actually sent.

The description is thin by design; the training content lives in
templates.WEEKLY_SESSIONS, so changing the week never touches this file.
"""

import copy
import math
import statistics
from datetime import date, timedelta

from fit import compute, planner
from fit.templates import DAYS_PER_WEEK, TITLE, WEEKLY_RAMP_PCT, WEEKLY_SESSIONS

# --- starting volume ------------------------------------------------------
#
# The opening week assumes a base the user may not have, so it is measured
# against what they ride now (derive_volume_scale) and converges back to the
# plan's own level by the last week. `volume:` overrides.
VOLUME_SCALE_MIN = 0.6
VOLUME_SCALE_MAX = 1.25
# Long enough to survive one quiet week, short enough to be current form.
RECENT_VOLUME_WEEKS = 8

# --- strength progression -------------------------------------------------
#
# Volume stays fixed (5x5 stays 5x5) and load ramps: that is what linear
# progression is, and a weight that turns out too heavy is a failed rep, not a
# failed session.
#
# The ramp is StrongLifts': every set done at the weight earns one increment
# next session, a miss holds it, and the third miss running at one weight
# resets it 10% lower. The increment is StrongLifts' default 2.5kg on every
# lift but the deadlift, which takes 5kg: it is the heaviest lift and moves
# fastest. Each lift's loads round to its own increment, so every weight is a
# whole kg or a multiple of 2.5kg — keep new increments to those.
LIFT_INCREMENT_KG = {
    "deadlift": 5.0,
    "squat": 2.5,
    "bench_press": 2.5,
    "shoulder_press": 2.5,
    "row": 2.5,
}

# StrongLifts' start for anyone who has lifted before: a weight you could lift
# for ten. An e1RM read back at a scheme's own reps is a true 5RM, and five sets
# of a 5RM fail by the second.
STRENGTH_START_REPS = 10
STRENGTH_MISSES_BEFORE_RESET = 3
STRENGTH_RESET_FACTOR = 0.9

# Bounds on a measured e1RM: outside them the reading is thrown out rather
# than trusted.
PLAUSIBLE_LIFT_E1RM_KG = (20.0, 400.0)

# Deliberately light: starting under you wastes a week, over you is an injury.
FALLBACK_LIFT_E1RM_KG = {
    "deadlift": 80.0,
    "squat": 70.0,
    "bench_press": 50.0,
    "shoulder_press": 35.0,
    "row": 40.0,
}

# Sports whose sessions grow with the weekly multiplier — the riding. Lifting
# is left out of the volume measurement: its size is fixed, so counting it
# would put time into a ratio the multiplier can never move.
_SCALED_SPORTS = sorted({s["sport"] for s in WEEKLY_SESSIONS if s["scale"]})

# Barbell lifts the plan prescribes, in first-appearance order. Bodyweight
# work carries no load, so no e1RM target either.
PLAN_LIFTS = list(
    dict.fromkeys(
        e["exercise"]
        for s in WEEKLY_SESSIONS
        for e in s["params"].get("exercises", [])
        if e["exercise"] in LIFT_INCREMENT_KG
    )
)


# --- description parsing --------------------------------------------------

_SPEC_KEYS = {"start_date", "end_date", "days_per_week", "volume", "targets"}
_TARGET_KEYS = {f"{lift}_goal_kg" for lift in PLAN_LIFTS}

_YAML_INSTALL_HINT = (
    "fit train import needs the optional 'pyyaml' dependency.\n"
    "Install it with: pip install -e '.[train]'"
)


def _yaml():
    """Lazy: only `train import` parses YAML; show/sync/clear read stored JSON."""
    try:
        import yaml
    except ImportError as exc:
        raise ValueError(_YAML_INSTALL_HINT) from exc
    return yaml


def _as_date_string(value, field: str) -> str:
    """Normalise to 'YYYY-MM-DD'. YAML 1.1 resolves an unquoted date to a
    datetime.date, so accept both and route text through parse_schedule_date."""
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected a date like 2026-06-14, got {value!r}")
    try:
        return planner.parse_schedule_date(value)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc


def _as_int(value, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field}: expected a whole number, got {value!r}")
    if not low <= value <= high:
        raise ValueError(f"{field}: expected {low}-{high}, got {value}")
    return value


def _as_weight(value, field: str) -> float:
    """Kilograms. Floats allowed (a half-kilo is a real plate); bools rejected,
    since YAML 1.1 hands them through as 1/0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}: expected a weight in kg, got {value!r}")
    low, high = PLAUSIBLE_LIFT_E1RM_KG
    if not low <= value <= high:
        raise ValueError(f"{field}: expected {low:g}-{high:g}kg, got {value}")
    return float(value)


def parse_plan_spec(text: str) -> dict:
    """YAML description -> normalised spec. start_date and end_date are
    required; the full form is in examples/training-plan.yaml."""
    yaml = _yaml()
    try:
        raw = yaml.safe_load(text)
    except Exception as exc:  # yaml.YAMLError, but keep the import lazy-only
        raise ValueError(f"could not parse the plan description: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("plan description must be a YAML mapping of key: value")

    unknown = set(raw) - _SPEC_KEYS
    if unknown:
        raise ValueError(
            f"unknown key(s) {', '.join(sorted(unknown))} — "
            f"valid keys: {', '.join(sorted(_SPEC_KEYS))}"
        )
    for key in ("start_date", "end_date"):
        if key not in raw:
            raise ValueError(f"{key} is required, e.g. {key}: 2026-11-02")
    start_date = _as_date_string(raw["start_date"], "start_date")
    end_date = _as_date_string(raw["end_date"], "end_date")
    if end_date <= start_date:
        raise ValueError(f"end_date {end_date} must be after start_date {start_date}")

    spec = {
        "start_date": start_date,
        "end_date": end_date,
        "days_per_week": DAYS_PER_WEEK,
        "targets": {},
    }

    if "days_per_week" in raw:
        # A fixed count, or [start, end] to build frequency across the plan —
        # a separate axis from scaling volume.
        value = raw["days_per_week"]
        if isinstance(value, list):
            if len(value) != 2:
                raise ValueError(
                    "days_per_week: expected a number, or [start, end] to build "
                    "frequency across the plan, e.g. [5, 6]"
                )
            low = _as_int(value[0], "days_per_week[0]", 1, 7)
            high = _as_int(value[1], "days_per_week[1]", 1, 7)
            if high < low:
                raise ValueError(
                    f"days_per_week: {low} -> {high} would train less often as the "
                    "plan goes on; frequency only builds"
                )
            spec["days_per_week"] = [low, high]
        else:
            spec["days_per_week"] = _as_int(value, "days_per_week", 1, 7)
    if "volume" in raw:
        # Percent of the plan's opening week; else measured from history.
        spec["volume"] = _as_int(raw["volume"], "volume", 40, 150)

    if "targets" in raw:
        targets = raw["targets"]
        if not isinstance(targets, dict):
            raise ValueError("targets: expected a mapping, e.g. {squat_goal_kg: 120}")
        unknown_targets = set(targets) - _TARGET_KEYS
        if unknown_targets:
            raise ValueError(
                f"targets: unknown key(s) {', '.join(sorted(unknown_targets))} — "
                f"valid: {', '.join(sorted(_TARGET_KEYS))}"
            )
        spec["targets"] = {
            key: _as_weight(value, f"targets.{key}") for key, value in targets.items()
        }

    return spec


# --- weekly shape ---------------------------------------------------------


def _plan_weeks(start_date: str, end_date: str) -> tuple[date, int]:
    """(first Monday, whole ISO weeks through end_date's week)."""
    start_monday = compute.week_start(start_date)
    end_monday = compute.week_start(end_date)
    return start_monday, ((end_monday - start_monday).days // 7) + 1


def _select_sessions(sessions: list[dict], days_per_week: int) -> list[dict]:
    """Trim to days_per_week training days, lowest priority dropped first. A
    session on an already-taken day costs no extra day, so doubles survive."""
    selected: list[dict] = []
    days: set[int] = set()
    for session in sorted(sessions, key=lambda s: s["priority"]):
        if session["day"] in days or len(days) < days_per_week:
            selected.append(session)
            days.add(session["day"])
    return selected


def _scaled(scale: dict, multiplier: float) -> int:
    value = round(scale["base"] * multiplier / scale["step"]) * scale["step"]
    return int(max(scale["min"], min(scale["max"], value)))


def days_per_week_range(spec: dict) -> tuple[int, int]:
    """(first, final) training days; a plain number means both."""
    value = spec["days_per_week"]
    if isinstance(value, list):
        return value[0], value[1]
    return value, value


def _days_for_week(low: int, high: int, index: int, weeks: int) -> int:
    """Builds low -> high by the last week, on the same schedule as
    _volume_scale_for_week."""
    if low == high:
        return low
    last = max(weeks - 1, 1)
    return round(low + (high - low) * min(index, last) / last)


# --- lifting --------------------------------------------------------------


def lift_increment(exercise: str) -> float:
    return LIFT_INCREMENT_KG[exercise]


def _round_to_plates(value: float, increment: float) -> float:
    """A bar only holds what the plates allow. Rounding to the lift's own step
    rather than to any loadable weight keeps a ramp even: 67 + 2.5 would
    otherwise land on 69 and 70 by turns. Halves round up, not to even."""
    return round(math.floor(value / increment + 0.5) * increment, 2)


def working_weight_from_1rm(e1rm_kg: float, reps: int, increment: float) -> float:
    """Bar weight for `reps` reps: compute.estimated_1rm read backwards, so a
    PB and a target derived from it can't disagree. Inverting rather than
    applying a flat 75% keeps it honest across rep schemes."""
    if e1rm_kg <= 0 or reps <= 0:
        return 0.0
    return _round_to_plates(e1rm_kg / (1 + reps / 30), increment)


def next_working_weight(
    history: list[dict], sets: int, reps: int, increment: float, start_kg: float
) -> float:
    """The load the next session of one lift prescribes, from that lift's
    sessions oldest first ({"sets": [...]}).

    A session is judged at its heaviest weight, on what was actually lifted
    rather than what was prescribed."""
    level, misses, missed_at = start_kg, 0, None
    for session in history:
        loaded = [s for s in session["sets"] if s.get("weight_kg")]
        if not loaded:
            continue
        top = max(s["weight_kg"] for s in loaded)
        hit = sum(
            1 for s in loaded if s["weight_kg"] >= top and s.get("reps", 0) >= reps
        )
        if hit >= sets:
            level, misses = _round_to_plates(top + increment, increment), 0
            continue
        misses = misses + 1 if top == missed_at else 1
        level, missed_at = top, top
        if misses >= STRENGTH_MISSES_BEFORE_RESET:
            level, misses = _round_to_plates(top * STRENGTH_RESET_FACTOR, increment), 0
    return level


def next_rep_target(history: list[dict], sets: int, start_reps: int, cap: int) -> int:
    """next_working_weight for a bodyweight exercise: reps rise instead of
    load. Every set reaching the target earns one more rep, up to `cap`; a
    miss holds it. No reset — there is no lighter bodyweight to drop to."""
    level = start_reps
    for session in history:
        done = [s.get("reps") or 0 for s in session["sets"]]
        if sum(1 for reps in done if reps >= level) >= sets:
            level = min(level + 1, cap)
    return level


def derive_lift_target(recent: list[dict], exercise: str) -> dict:
    """{"value", "why"}: the best e1RM in recent training, or None with the
    reason. A reading outside PLAUSIBLE_LIFT_E1RM_KG is thrown out rather than
    silently swapped for a default."""
    strength = [a for a in recent if a.get("type") == "strength"]
    lift = compute.all_personal_bests(strength).get("strength", {}).get(exercise, {})
    value = lift.get("best_e1rm_kg")
    if not value:
        return {"value": None, "why": "no recent history to derive from"}
    seen = lift.get("best_e1rm_date", "")
    where = f" ({seen})" if seen else ""
    why = f"best {exercise.replace('_', ' ')} e1RM in training{where}"
    low, high = PLAUSIBLE_LIFT_E1RM_KG
    if not low <= value <= high:
        return {
            "value": None,
            "why": f"{why} gave {value:g}kg, outside the plausible "
            f"{low:g}–{high:g}kg — ignored",
        }
    return {"value": value, "why": why}


def derive_targets(spec: dict, activities: list[dict], reference: date) -> dict:
    """{lift: {current_e1rm_kg, why, goal_e1rm_kg, goal_from}} — where each
    lift starts and where the plan aims it.

    `current` is measured from recent history, else FALLBACK_LIFT_E1RM_KG.
    `goal` cannot be measured — it is a decision about the future — so the
    description wins, else _progress_lifts fills in where the progression gets
    to."""
    recent = planner.recent_activities(activities, reference)
    goals = spec.get("targets", {})
    targets = {}
    for lift in PLAN_LIFTS:
        derived = derive_lift_target(recent, lift)
        if derived["value"] is not None:
            entry = {
                "current_e1rm_kg": round(derived["value"], 2),
                "why": derived["why"],
            }
        else:
            entry = {
                "current_e1rm_kg": FALLBACK_LIFT_E1RM_KG[lift],
                "why": f"default — {derived['why']}",
            }
        if f"{lift}_goal_kg" in goals:
            entry["goal_e1rm_kg"] = goals[f"{lift}_goal_kg"]
            entry["goal_from"] = "set in the plan description"
        targets[lift] = entry
    return targets


def _iso_week(iso_date: str) -> tuple:
    return date.fromisoformat(iso_date).isocalendar()[:2]


def _lift_history(
    lift: str, lifting: list[dict], start_date: str, today: str
) -> list[dict]:
    """One exercise's sessions within the plan so far, oldest first."""
    return [
        {"date": activity["date"], "sets": e.get("sets") or []}
        for activity in lifting
        if start_date <= activity["date"] <= today
        for e in activity.get("exercises") or []
        if e.get("name") == lift
    ]


def _progress_lifts(
    sessions: list[dict],
    targets: dict,
    activities: list[dict],
    start_date: str,
    reference: date,
) -> list[str]:
    """Set every straight-sets load in place from next_working_weight;
    returns warnings. A past session shows what its history prescribed then;
    a future one assumes each session before it goes to plan, capped at the
    goal."""
    today = reference.isoformat()
    lifting = sorted(
        (a for a in activities if a.get("type") == "strength" and a.get("date")),
        key=lambda a: a["date"],
    )
    warnings = []
    for lift, entry in targets.items():
        planned = [
            (s, exercise)
            for s in sessions
            if s["sport"] == "strength"
            for exercise in s["params"].get("exercises", [])
            if exercise["exercise"] == lift
        ]
        if not planned:
            continue
        sets, reps = planned[0][1]["sets"], planned[0][1]["reps"]
        increment = lift_increment(lift)
        history = _lift_history(lift, lifting, start_date, today)

        start = working_weight_from_1rm(
            entry["current_e1rm_kg"], max(reps, STRENGTH_START_REPS), increment
        )

        def level_at(before: str) -> float:
            done = [h for h in history if h["date"] < before]
            return next_working_weight(done, sets, reps, increment, start)

        now = next_working_weight(history, sets, reps, increment, start)
        future = sum(1 for s, _ in planned if s["date"] > today)
        reached = now + increment * max(future - 1, 0)
        if "goal_e1rm_kg" in entry:
            cap = max(
                working_weight_from_1rm(entry["goal_e1rm_kg"], reps, increment), now
            )
            if reached < cap:
                warnings.append(
                    f"{lift.replace('_', ' ')}: reaching "
                    f"{entry['goal_e1rm_kg']:.0f}kg needs more than "
                    f"{increment:g}kg a session over this plan — it will get to "
                    f"about {compute.estimated_1rm(reached, reps):.0f}kg. Start "
                    "earlier, or aim lower."
                )
        else:
            cap = reached
            entry["goal_e1rm_kg"] = compute.estimated_1rm(reached, reps)
            entry["goal_from"] = (
                f"one {increment:g}kg step per session — what this plan's length "
                "can deliver"
            )

        ahead = 0
        for session, exercise in planned:
            if session["date"] <= today:
                level = level_at(session["date"])
            else:
                level = min(now + increment * ahead, cap)
                ahead += 1
            exercise["target_weight_kg"] = _round_to_plates(level, increment)

    # Bodyweight work progresses on reps, by the same rules.
    by_rep_exercise: dict[str, list] = {}
    for session in sessions:
        if session["sport"] != "strength":
            continue
        for exercise in session["params"].get("exercises", []):
            if exercise.get("rep_cap"):
                by_rep_exercise.setdefault(exercise["exercise"], []).append(
                    (session, exercise)
                )
    for name, planned in by_rep_exercise.items():
        first = planned[0][1]
        sets, start, cap = first["sets"], first["reps"], first["rep_cap"]
        history = _lift_history(name, lifting, start_date, today)
        now = next_rep_target(history, sets, start, cap)
        if now >= cap:
            warnings.append(
                f"{name.replace('_', ' ')}: at {sets}x{cap} — ready to move on "
                "to the next progression."
            )
        ahead = 0
        for session, exercise in planned:
            if session["date"] <= today:
                done = [h for h in history if h["date"] < session["date"]]
                exercise["reps"] = next_rep_target(done, sets, start, cap)
                continue
            exercise["reps"] = min(now + ahead, cap)
            ahead += 1

    for session in sessions:
        if session["sport"] == "strength":
            session["workout_name"] = planner.workout_name(
                "strength", "straight_sets", session["params"]
            )
    return warnings


# --- riding volume --------------------------------------------------------


def derive_volume_scale(
    spec: dict, activities: list[dict], reference: date, opening_week_seconds: int
) -> tuple[float, str]:
    """(scale, why) for the opening week. `volume:` wins; else the *mean*
    weekly riding over RECENT_VOLUME_WEEKS, measured against the plan's
    opening week and clamped.

    Mean, not median: with sparse training the median collapses to zero once
    half the weeks are empty, handing somebody barely riding the full
    volume. Empty weeks are real information and weigh in as zeroes."""
    if "volume" in spec:
        return spec["volume"] / 100, "set in the plan description"
    if not opening_week_seconds:
        return 1.0, "plan default"

    relevant = compute.filter_by_types(
        planner.recent_activities(activities, reference), _SCALED_SPORTS
    )
    # The current week is still filling up; counting it would understate form.
    complete_weeks = [
        week
        for week in compute.weekly_volumes(relevant, through=reference)
        if not compute.is_current_week(week["week"], reference)
    ]
    recent_weeks = complete_weeks[-RECENT_VOLUME_WEEKS:]
    # No history means "unknown", not "untrained" — leave the plan alone.
    if not any(week["duration_seconds"] for week in recent_weeks):
        return 1.0, "plan default (no recent riding to measure)"

    mean_seconds = statistics.mean(week["duration_seconds"] for week in recent_weeks)
    raw = mean_seconds / opening_week_seconds
    scale = max(VOLUME_SCALE_MIN, min(VOLUME_SCALE_MAX, raw))
    why = (
        f"your recent {mean_seconds / 3600:.1f}h/week against the plan's "
        f"{opening_week_seconds / 3600:.1f}h opening week"
    )
    if scale != raw:
        why += f" (clamped to {round(scale * 100)}%)"
    return scale, why


def _volume_scale_for_week(start_scale: float, index: int, weeks: int) -> float:
    """Converge from start_scale to the plan's own level by the last week, so a
    rider who starts below it still arrives where the plan meant to."""
    last = max(weeks - 1, 1)
    return start_scale + (1.0 - start_scale) * min(index, last) / last


def _week_seconds(week: list[dict]) -> int:
    """Estimated seconds of riding in one unscaled week, for sizing the
    opening week."""
    total = 0
    for template_session in week:
        scale = template_session["scale"]
        if not scale:
            continue
        params = {**template_session["params"], scale["param"]: _scaled(scale, 1.0)}
        total += planner.estimate_seconds(
            template_session["sport"], template_session["session_type"], params
        )
    return total


# --- expansion ------------------------------------------------------------


def _build_session(
    template_session: dict, session_date: date, week: int, multiplier: float
) -> dict:
    # Deep, not shallow: exercise dicts are shared with the template, so a
    # shallow copy would have week 12 writing its load into week 1.
    params = copy.deepcopy(template_session["params"])
    scale = template_session["scale"]
    if scale:
        params[scale["param"]] = _scaled(scale, multiplier)
    sport, session_type = template_session["sport"], template_session["session_type"]
    return {
        "date": session_date.isoformat(),
        "week": week,
        "sport": sport,
        "session_type": session_type,
        "params": params,
        "workout_name": planner.workout_name(sport, session_type, params),
    }


def expand_plan(
    spec: dict, activities: list[dict], reference: date, volume: dict | None = None
) -> dict:
    """The engine: spec + history -> the full plan dict (metadata, targets, and
    a flat list of dated sessions, oldest first). reference is today.

    Nothing here is persisted — `fit train` re-runs this on every command, so
    loads track what you have lifted with nothing to re-run (see
    storage.write_training_plan). `volume`, when given, is the
    derive_volume_scale result pinned at import: the *starting* volume is a
    decision made once about where you were then, and re-measuring it weekly
    would rewrite session sizes as you train.

    Sessions outside start_date..end_date are dropped."""
    first_monday, weeks = _plan_weeks(spec["start_date"], spec["end_date"])
    low_days, high_days = days_per_week_range(spec)

    def week_sessions(index: int) -> list[dict]:
        # Alternating workouts first, so the trim sees one session per day.
        running = [s for s in WEEKLY_SESSIONS if s["week_parity"] in (None, index % 2)]
        return _select_sessions(
            running, _days_for_week(low_days, high_days, index, weeks)
        )

    # Measured against week 1's *own* session list, which is smaller when
    # frequency builds — otherwise a plan opening with fewer rides would look
    # like the user was riding far below a week they were never asked to do.
    if volume:
        start_scale, volume_why = volume["start_scale"], volume["why"]
    else:
        start_scale, volume_why = derive_volume_scale(
            spec, activities, reference, _week_seconds(week_sessions(0))
        )

    ramp = 1 + WEEKLY_RAMP_PCT / 100
    sessions = []
    for index in range(weeks):
        monday = first_monday + timedelta(weeks=index)
        multiplier = ramp**index * _volume_scale_for_week(start_scale, index, weeks)
        sessions.extend(
            _build_session(
                template_session,
                monday + timedelta(days=template_session["day"]),
                index + 1,
                multiplier,
            )
            for template_session in week_sessions(index)
        )

    sessions = [
        s for s in sessions if spec["start_date"] <= s["date"] <= spec["end_date"]
    ]
    sessions.sort(key=lambda s: (s["date"], s["sport"]))
    targets = derive_targets(spec, activities, reference)
    warnings = _progress_lifts(
        sessions, targets, activities, spec["start_date"], reference
    )

    return {
        "start_date": spec["start_date"],
        "end_date": spec["end_date"],
        "weeks": weeks,
        "created": reference.isoformat(),
        "spec": spec,
        "targets": targets,
        "volume": {"start_scale": round(start_scale, 3), "why": volume_why},
        "days_per_week": {"start": low_days, "end": high_days},
        "warnings": warnings,
        "sessions": sessions,
    }


def session_to_build_args(session: dict) -> tuple[str, str, dict]:
    """(sport, workout_type, params) for planner.build_plan."""
    return session["sport"], session["session_type"], session["params"]


# --- completion and views -------------------------------------------------


def _lifts(session_or_activity: dict) -> set:
    """Barbell lifts a strength session prescribes or an activity recorded.
    Bodyweight work is left out: pull-ups in two sessions would otherwise make
    them share a lift whatever was squatted or pressed."""
    params = session_or_activity.get("params", {})
    named = [e.get("exercise") for e in params.get("exercises", [])]
    named += [e.get("name") for e in session_or_activity.get("exercises") or []]
    return {name for name in named if name in LIFT_INCREMENT_KG}


def match_completion(sessions: list[dict], activities: list[dict]) -> list[dict]:
    """Copies with "completed" set: same sport in the same ISO week, each
    activity claiming at most one session so one ride can't tick off a whole
    week.

    The week, not a day window, is the unit: Friday's lift done on the Sunday
    is still that week's work. A ±1 day window scored three sessions as
    missed that had all been trained 2-3 days off their date.

    Pairs are claimed closest first across the whole week, not session by
    session, and a lifting session only takes an activity sharing a lift with
    it (when both record lifts). Going session by session let Monday's deadlift
    day claim Wednesday's squat session, leaving Wednesday missed."""
    candidates = [a for a in activities if a.get("type") and a.get("date")]
    pairs = []
    for si, session in enumerate(sessions):
        session_date = date.fromisoformat(session["date"])
        wanted = _lifts(session)
        for ai, activity in enumerate(candidates):
            activity_date = date.fromisoformat(activity["date"])
            if activity.get("type") != session.get("sport"):
                continue
            if activity_date.isocalendar()[:2] != session_date.isocalendar()[:2]:
                continue
            done = _lifts(activity)
            if wanted and done and not wanted & done:
                continue
            pairs.append((abs((activity_date - session_date).days), si, ai))

    completed: set[int] = set()
    claimed: set[int] = set()
    for _, si, ai in sorted(pairs):
        if si not in completed and ai not in claimed:
            completed.add(si)
            claimed.add(ai)
    return [
        {**session, "completed": si in completed} for si, session in enumerate(sessions)
    ]


def group_by_week(sessions: list[dict]) -> list[dict]:
    """[{week, start, sessions}], oldest first."""
    weeks: dict[int, dict] = {}
    for session in sessions:
        week = weeks.setdefault(
            session["week"],
            {"week": session["week"], "start": session["date"], "sessions": []},
        )
        week["sessions"].append(session)
        week["start"] = min(week["start"], session["date"])
    return [weeks[key] for key in sorted(weeks)]


def plan_summary(plan: dict, today: date) -> dict:
    """Header figures for `fit train show`."""
    sessions = plan.get("sessions", [])
    end = date.fromisoformat(plan["end_date"])
    return {
        "title": TITLE,
        "start_date": plan["start_date"],
        "end_date": plan["end_date"],
        "weeks": plan.get("weeks", 0),
        "days_to_go": (end - today).days,
        "sessions": len(sessions),
        "completed": sum(1 for s in sessions if s.get("completed")),
        "scheduled": sum(1 for s in sessions if s.get("pushed")),
        "stale": sum(1 for s in sessions if s.get("stale")),
        "targets": plan.get("targets", {}),
        "volume": plan.get("volume", {}),
        "warnings": plan.get("warnings", []),
    }


# --- the Garmin ledger ----------------------------------------------------
#
# A session is derived; what was *pushed* is a fact about a remote account and
# cannot be. plan.json therefore stores only the description plus this ledger,
# and everything else is re-expanded on every command. A pushed session renders
# from what was actually sent, not from a fresh derivation, because Garmin has
# no update endpoint and the watch holds the old copy.


def ledger_key(session: dict) -> tuple:
    """What identifies a session across re-derivations. Dates are a pure
    function of the spec, so (date, sport) is stable — the same key
    match_completion and scripts/diff_workout.py already use."""
    return session["date"], session["sport"]


def apply_pushed(sessions: list[dict], pushed: list[dict]) -> list[dict]:
    """Copies of `sessions` overlaid with the ledger: a pushed session takes
    back the params and name that were actually sent, and is flagged "stale"
    when the live derivation has since moved away from them."""
    by_key = {(e["date"], e["sport"]): e for e in pushed}
    out = []
    for session in sessions:
        entry = by_key.get(ledger_key(session))
        if entry is None:
            out.append({**session, "pushed": False, "stale": False})
            continue
        stale = entry.get("params") != session.get("params")
        out.append(
            {
                **session,
                "params": entry.get("params", session.get("params")),
                "workout_name": entry.get("workout_name", session.get("workout_name")),
                "garmin_workout_id": entry.get("workout_id"),
                "scheduled_workout_id": entry.get("schedule_id"),
                "pushed": True,
                "stale": stale,
            }
        )
    return out


def ledger_entry(session: dict, workout_id, schedule_id) -> dict:
    """One ledger row: the identity, the ids, and exactly what was sent."""
    return {
        "date": session["date"],
        "sport": session["sport"],
        "workout_id": workout_id,
        "schedule_id": schedule_id,
        "workout_name": session["workout_name"],
        "params": session["params"],
    }


def sync_window(sessions: list[dict], today: date, window_days: int) -> list[dict]:
    """Not-yet-pushed, not-yet-done sessions in [today, today + window_days].
    Re-running sync finds fewer, which is what makes it idempotent; a session
    trained already today has nothing left to push."""
    end = (today + timedelta(days=window_days)).isoformat()
    return [
        s
        for s in sessions
        if not s.get("pushed")
        and not s.get("completed")
        and today.isoformat() <= s["date"] <= end
    ]


def followed_other_session(rides: list[dict], pushed: list[dict]) -> list[dict]:
    """Activities that followed a pushed workout scheduled for another day.

    `rides` are {date, workout_id} — the workout a device actually ran, which
    Garmin's activity summary records. A device that has not synced since a
    push still holds the old calendar and prompts with whatever it last had:
    on 2026-10-13 an Edge offered the unridden 06/10 2x6:00 in place of that
    day's 4x6:00, and same-week completion matching ticked the session off.
    Each returned {date, followed, scheduled} pairs the ledger row that was
    ridden with the one meant for that day (None when nothing was).

    A session moved within its own week onto a free day is not stale — a ride
    planned for Saturday and ridden on Sunday — so it passes unremarked, as
    completion matching already treats it.
    """
    by_id = {e["workout_id"]: e for e in pushed if e.get("workout_id") is not None}
    by_key = {(e["date"], e["sport"]): e for e in pushed}
    out = []
    for ride in rides:
        followed = by_id.get(ride["workout_id"])
        if followed is None or followed["date"] == ride["date"]:
            continue
        scheduled = by_key.get((ride["date"], followed["sport"]))
        if scheduled is None and _iso_week(ride["date"]) == _iso_week(followed["date"]):
            continue
        out.append({"date": ride["date"], "followed": followed, "scheduled": scheduled})
    return out


def future_pushed(
    pushed: list[dict], today: date, sessions: list[dict] = ()
) -> list[dict]:
    """Ledger rows still ahead of today — what `train clear` unschedules —
    less any whose session in `sessions` is already done. "Ahead" includes
    today, and clearing a session already trained today would only strip it
    from the calendar it was ridden from."""
    done = {ledger_key(s) for s in sessions if s.get("completed")}
    return [
        e
        for e in pushed
        if e["date"] >= today.isoformat() and (e["date"], e["sport"]) not in done
    ]
