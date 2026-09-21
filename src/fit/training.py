"""The pure engine behind `fit train`: a YAML plan description becomes a full
dated, periodised schedule with intensities derived from the user's history.
No I/O — cli.py reads the text and hands it in.

**Nothing derived is stored.** expand_plan re-runs on every command, so targets
always reflect current fitness and there is no retarget step. The one thing
that cannot be derived — what was pushed to Garmin — lives in a small ledger
(see "the Garmin ledger" below), and a pushed session renders from what was
actually sent.

The description is thin by design; all training content lives in
templates.GOAL_TEMPLATES, so recalibrating a goal never touches this file.
"""

import copy
import statistics
from datetime import date, timedelta

from fit import compute, planner
from fit.templates import GOAL_TEMPLATES

# --- progression shape ----------------------------------------------------

# Standard coaching defaults, all overridable per description.
PROGRESSION_DEFAULTS = {
    "build_recover": [3, 1],
    "taper_weeks": 2,
}

# The rate templates are calibrated against, not a plan default: the ramp is
# solved per plan from its actual length (derive_weekly_ramp). This defines
# what peak a template is aiming at, at its own length.
REFERENCE_RAMP_PCT = 8

# Volume multipliers on the template's base session sizes.
RECOVERY_FACTOR = 0.6
TAPER_START = 0.75
TAPER_END = 0.45

# Size grows by the weekly multiplier alone — one mechanism, not a separate
# per-week increment on top, which would compound into nonsense.

MIN_PLAN_WEEKS = 4

# --- benchmarks -----------------------------------------------------------
#
# Intensity is measured, never projected: a target is only ever a reading of
# training you have actually done, never an assumption that you will improve.
# It does track your *measured* fitness, since expand_plan re-derives on every
# command — what it will not do is prescribe a pace you have not yet earned.
#
# Benchmarks land on recovery weeks (you test rested, so one test is comparable
# with the next) and never scale. Whether a test can afford a warmup in the
# same recording is a property of the sport: the recorded activity must BE the
# test unless the split machinery can isolate it from within.
#
# Nothing has to be re-run after a test: expand_plan derives targets fresh on
# every command, so importing the activity is what applies it.
BENCHMARK_SESSIONS = {
    # Keeps its wrap: fastest_split isolates the 5k from the jogging around it.
    # 5km not 3km — 3km is in neither SPLIT_DISTANCES_KM nor MILESTONES_KM, so
    # a 3km effort was measurable nowhere.
    "run": {
        "session_type": "baseline",
        "params": {
            "warmup_minutes": 10,
            "test_distance_m": 5000,
            "cooldown_minutes": 10,
        },
    },
    # Wrapped again: best_power_window recovers the 20-minute effort from
    # wherever it sits, so the test needn't be the whole recording.
    "cycle": {
        "session_type": "baseline",
        "params": {"warmup_minutes": 20, "test_minutes": 20, "cooldown_minutes": 10},
    },
    # Bare, and 1km: it lands in the fastest_1k milestone derive_swim_css reads.
    # Pool swims often carry no distance stream, so the milestone (computed from
    # distance and duration) is the only reliable measurement.
    "swim": {"session_type": "baseline", "params": {"test_distance_m": 1000}},
    # A heavy triple, not a true single: estimated_1rm reads a 3RM fine, and a
    # plan should not send anybody to a real 1RM alone in a gym every few weeks.
    # _build_benchmark fills in the lift from the session being replaced.
    "strength": {"session_type": "baseline", "params": {"reps": 3}},
}

# --- starting volume ------------------------------------------------------
#
# A template's opening week assumes a base the user may not have, so it is
# measured against what they train now (derive_volume_scale) and converges back
# to the template's level by the last build week. `volume:` overrides.
VOLUME_SCALE_MIN = 0.6
VOLUME_SCALE_MAX = 1.25
# Long enough to survive one quiet week, short enough to be current form.
RECENT_VOLUME_WEEKS = 8
# Week-1-to-peak growth beyond this is flagged: the ramp itself is then a risk.
VOLUME_RAMP_WARN = 2.2

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEKDAY_LOOKUP = {
    **{name.lower(): i for i, name in enumerate(DAY_NAMES)},
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "tues": 1,
    "thur": 3,
    "thurs": 3,
}

# Local-only: the Garmin calendar has no note/non-workout endpoint, so extras
# are never built or pushed.
EXTRA_DURATIONS_S = {
    "strength": 2700,
    "yoga": 1800,
    "mobility": 1200,
    "core": 1200,
}
DEFAULT_EXTRA_DURATION_S = 1800

# When history has nothing and the description gave no override: a plan with
# plausible targets beats no plan. `fit train show` says where each came from.
FALLBACK_TARGETS = {
    "run_5k_seconds": 1500,  # 25:00
    "bike_ftp": 200,
    "swim_css_100m": 120,  # 2:00
}

# --- strength progression -------------------------------------------------
#
# The one sport that projects intensity forward: volume stays fixed (3x10 stays
# 3x10) and load ramps, which is the exact opposite of every other sport here.
# That is what linear progression is, and a weight that turns out too heavy is
# a failed rep, not a failed session.
#
# The increment is both the plate step a working weight rounds to and the most
# a week may add — "add one increment a week" is the method. Upper body gets
# the smaller step because it genuinely progresses slower.
LIFT_INCREMENT_KG = {
    "deadlift": 2.5,
    "squat": 2.5,
    "bench_press": 1.25,
    "shoulder_press": 1.25,
}
DEFAULT_LIFT_INCREMENT_KG = 2.5

# A deload drops the bar, it does not empty it. RECOVERY_FACTOR (0.6) is a
# *volume* cut — 60% of a working weight stops being training.
STRENGTH_DELOAD_FACTOR = 0.85
STRENGTH_TAPER_FACTOR = 0.8

# PLAUSIBLE_TARGETS' job, kept separate because a strength target is per lift,
# not one number per sport.
PLAUSIBLE_LIFT_E1RM_KG = (20.0, 400.0)

# Deliberately light: starting under you wastes a week, over you is an injury.
FALLBACK_LIFT_E1RM_KG = {
    "deadlift": 80.0,
    "squat": 70.0,
    "bench_press": 50.0,
    "shoulder_press": 35.0,
}


# --- description parsing --------------------------------------------------

_SPEC_KEYS = {
    "goal",
    "event_date",
    "start_date",
    "days_per_week",
    "rest_day",
    "extras",
    "targets",
    "progression",
    "volume",
    "benchmarks",
    "test_week",
}
_TARGET_KEYS = {"run_5k", "bike_ftp", "swim_css_100m"} | {
    f"{lift}_goal_kg" for lift in LIFT_INCREMENT_KG
}

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


def _as_seconds(value, field: str) -> int:
    """m:ss -> seconds. YAML 1.1 resolves an unquoted 24:00 to the base-60 int
    1440 — exactly the seconds meant — so a bare int is taken as seconds."""
    if isinstance(value, bool):
        raise ValueError(f"{field}: expected a time like '24:00', got {value!r}")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"{field}: must be greater than zero")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected a time like '24:00', got {value!r}")
    try:
        return planner.parse_pace(value)
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


def _as_weekday(value, field: str) -> int:
    """'Mon'/'monday' -> 0. An unquoted `rest_day: no` arrives as False."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{field}: expected a weekday like Mon, got {value!r}")
    day = _WEEKDAY_LOOKUP.get(value.strip().lower())
    if day is None:
        raise ValueError(f"{field}: expected a weekday like Mon, got {value!r}")
    return day


def parse_plan_spec(text: str) -> dict:
    """YAML description -> normalised spec, every optional field defaulted from
    the goal template. Only goal and event_date are required; the full standard
    form is in CLAUDE.md and examples/training-plan.yaml."""
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

    if "goal" not in raw:
        raise ValueError(
            "goal is required, e.g. goal: "
            f"{sorted(GOAL_TEMPLATES)[0]} — available: {', '.join(sorted(GOAL_TEMPLATES))}"
        )
    goal = raw["goal"]
    if goal not in GOAL_TEMPLATES:
        raise ValueError(
            f"unknown goal {goal!r} — available: {', '.join(sorted(GOAL_TEMPLATES))}"
        )
    template = GOAL_TEMPLATES[goal]

    if "event_date" not in raw:
        raise ValueError("event_date is required, e.g. event_date: 2026-06-14")
    event_date = _as_date_string(raw["event_date"], "event_date")

    spec = {
        "goal": goal,
        "event_date": event_date,
        "days_per_week": template["days_per_week"],
        "rest_day": template["rest_day"],
        "extras": {},
        "targets": {},
        "benchmarks": True,
        "test_week": False,
        "progression": dict(PROGRESSION_DEFAULTS),
    }

    if "test_week" in raw:
        value = raw["test_week"]
        if not isinstance(value, bool):
            raise ValueError("test_week: expected true or false")
        spec["test_week"] = value

    if "start_date" in raw:
        start_date = _as_date_string(raw["start_date"], "start_date")
        if start_date >= event_date:
            raise ValueError(
                f"start_date {start_date} must be before event_date {event_date}"
            )
        spec["start_date"] = start_date
    else:
        spec["start_date"] = _default_start_date(
            event_date, template["weeks"] + (1 if spec["test_week"] else 0)
        )

    if "days_per_week" in raw:
        # A fixed count, or [start, end] to build frequency across the plan —
        # a separate axis from scaling volume.
        value = raw["days_per_week"]
        if isinstance(value, list):
            if len(value) != 2:
                raise ValueError(
                    "days_per_week: expected a number, or [start, end] to build "
                    "frequency across the plan, e.g. [2, 4]"
                )
            low = _as_int(value[0], "days_per_week[0]", 1, 7)
            high = _as_int(value[1], "days_per_week[1]", 1, 7)
            if high < low:
                raise ValueError(
                    f"days_per_week: {low} -> {high} would train less often as the "
                    "event approaches; a taper cuts volume, not frequency"
                )
            spec["days_per_week"] = [low, high]
        else:
            spec["days_per_week"] = _as_int(value, "days_per_week", 1, 7)
    if "benchmarks" in raw:
        value = raw["benchmarks"]
        if not isinstance(value, bool):
            raise ValueError("benchmarks: expected true or false")
        spec["benchmarks"] = value
    if "volume" in raw:
        # Percent of the template's opening week; else measured from history.
        spec["volume"] = _as_int(raw["volume"], "volume", 40, 150)
    if "rest_day" in raw:
        spec["rest_day"] = _as_weekday(raw["rest_day"], "rest_day")

    if "extras" in raw:
        extras = raw["extras"]
        if not isinstance(extras, dict):
            raise ValueError("extras: expected a mapping, e.g. {strength: 2, yoga: 1}")
        spec["extras"] = {
            str(name): _as_int(count, f"extras.{name}", 0, 7)
            for name, count in extras.items()
        }

    if "targets" in raw:
        targets = raw["targets"]
        if not isinstance(targets, dict):
            raise ValueError('targets: expected a mapping, e.g. {run_5k: "24:00"}')
        unknown_targets = set(targets) - _TARGET_KEYS
        if unknown_targets:
            raise ValueError(
                f"targets: unknown key(s) {', '.join(sorted(unknown_targets))} — "
                f"valid: {', '.join(sorted(_TARGET_KEYS))}"
            )
        # Silently ignoring a target for an untrained sport reads as "fit
        # disagreed with me" rather than "that setting does nothing here".
        usable = {
            override_key
            for sport, (_, override_key) in _SPORT_TARGETS.items()
            if sport in template_sports(goal)
        } | {f"{lift}_goal_kg" for lift in template_lifts(goal)}
        unusable = set(targets) - usable
        if unusable:
            raise ValueError(
                f"targets: {', '.join(sorted(unusable))} — {goal} does not "
                f"train that, so only {', '.join(sorted(usable))} apply"
            )
        if "run_5k" in targets:
            spec["targets"]["run_5k"] = _as_seconds(targets["run_5k"], "targets.run_5k")
        if "swim_css_100m" in targets:
            spec["targets"]["swim_css_100m"] = _as_seconds(
                targets["swim_css_100m"], "targets.swim_css_100m"
            )
        if "bike_ftp" in targets:
            spec["targets"]["bike_ftp"] = _as_int(
                targets["bike_ftp"], "targets.bike_ftp", 50, 600
            )
        for lift in LIFT_INCREMENT_KG:
            key = f"{lift}_goal_kg"
            if key in targets:
                spec["targets"][key] = _as_weight(targets[key], f"targets.{key}")

    if "progression" in raw:
        progression = raw["progression"]
        if not isinstance(progression, dict):
            raise ValueError("progression: expected a mapping")
        unknown_prog = (
            set(progression) - set(PROGRESSION_DEFAULTS) - {"weekly_ramp_pct"}
        )
        if unknown_prog:
            raise ValueError(
                f"progression: unknown key(s) {', '.join(sorted(unknown_prog))} — "
                f"valid: {', '.join(sorted({*PROGRESSION_DEFAULTS, 'weekly_ramp_pct'}))}"
            )
        if "build_recover" in progression:
            cycle = progression["build_recover"]
            if not isinstance(cycle, list) or len(cycle) != 2:
                raise ValueError(
                    "progression.build_recover: expected [build_weeks, recovery_weeks]"
                )
            spec["progression"]["build_recover"] = [
                _as_int(cycle[0], "progression.build_recover[0]", 1, 12),
                _as_int(cycle[1], "progression.build_recover[1]", 0, 4),
            ]
        if "weekly_ramp_pct" in progression:
            spec["progression"]["weekly_ramp_pct"] = _as_int(
                progression["weekly_ramp_pct"], "progression.weekly_ramp_pct", 0, 25
            )
        if "taper_weeks" in progression:
            spec["progression"]["taper_weeks"] = _as_int(
                progression["taper_weeks"], "progression.taper_weeks", 0, 4
            )

    return spec


def _default_start_date(event_date: str, weeks: int) -> str:
    """Monday `weeks` weeks back from the event's week."""
    event_monday = compute.week_start(event_date)
    return (event_monday - timedelta(weeks=weeks - 1)).isoformat()


# --- periodisation --------------------------------------------------------


def _plan_weeks(
    start_date: str, event_date: str, test_week: bool = False
) -> tuple[date, int]:
    """(first Monday, whole ISO weeks through the event's week). A test week
    comes out of that span, so it raises the floor by one."""
    start_monday = compute.week_start(start_date)
    event_monday = compute.week_start(event_date)
    weeks = ((event_monday - start_monday).days // 7) + 1
    needed = MIN_PLAN_WEEKS + (1 if test_week else 0)
    if weeks < needed:
        because = " — a test week takes one of them" if test_week else ""
        raise ValueError(
            f"a plan needs at least {needed} weeks between start_date "
            f"{start_date} and event_date {event_date} (got {weeks}){because}"
        )
    return start_monday, weeks


def _assign_phases(weeks: int, phases: list[tuple[str, int]], taper_weeks: int) -> list:
    """One phase per week. The taper takes the final taper_weeks; the rest
    share what's left by largest-remainder apportionment, so any plan length
    gets a sensible base/build/peak split."""
    taper = min(taper_weeks, weeks - 1)
    remaining = weeks - taper
    total_weight = sum(length for _, length in phases)

    # Largest-remainder apportionment, so the parts sum to `remaining` exactly.
    exact = [(name, remaining * length / total_weight) for name, length in phases]
    counts = [(name, int(value)) for name, value in exact]
    leftover = remaining - sum(count for _, count in counts)
    order = sorted(
        range(len(exact)), key=lambda i: exact[i][1] - counts[i][1], reverse=True
    )
    for i in order[:leftover]:
        counts[i] = (counts[i][0], counts[i][1] + 1)

    assigned = []
    for name, count in counts:
        assigned.extend([name] * count)
    assigned.extend(["taper"] * taper)
    return assigned


def _week_roles(phase_by_week: list[str], build_recover: list[int]) -> list[str]:
    """'build' | 'recover' | 'taper' per week. Shared by the multiplier curve
    and the ramp derivation, so the two can't disagree about which weeks ramp."""
    build_len, recover_len = build_recover
    cycle_length = build_len + recover_len
    roles, position = [], 0
    for phase in phase_by_week:
        if phase == "taper":
            roles.append("taper")
            continue
        roles.append("recover" if recover_len and position >= build_len else "build")
        position = (position + 1) % cycle_length
    return roles


def intended_peak(template: dict) -> float:
    """The multiplier this goal reaches at its own default length under
    REFERENCE_RAMP_PCT — the peak any length of it should arrive at. Derived,
    not declared, so a default-length plan behaves exactly as before."""
    phases = _assign_phases(
        template["weeks"], template["phases"], PROGRESSION_DEFAULTS["taper_weeks"]
    )
    return max(
        _week_multipliers(
            phases,
            {
                **PROGRESSION_DEFAULTS,
                "weekly_ramp_pct": REFERENCE_RAMP_PCT,
            },
        )
    )


def derive_weekly_ramp(
    template: dict, phase_by_week: list[str], build_recover: list[int]
) -> float:
    """Ramp that lands on intended_peak by the last build week, whatever the
    length. A longer block ascends more gently to the same place; compounding a
    fixed 8% over 26 weeks just pins every session at its clamp."""
    builds = _week_roles(phase_by_week, build_recover).count("build")
    if builds <= 1:
        return float(REFERENCE_RAMP_PCT)
    solved = (intended_peak(template) ** (1 / (builds - 1)) - 1) * 100
    # Never steeper than the reference: a short plan should reach a *lower*
    # peak. Chasing the full one over four weeks would demand ~70% a week.
    return min(solved, float(REFERENCE_RAMP_PCT))


def _week_multipliers(phase_by_week: list[str], progression: dict) -> list[float]:
    """One multiplier per week: build weeks ramp, recovery weeks dip to
    RECOVERY_FACTOR *without advancing*, taper weeks step TAPER_START ->
    TAPER_END of the peak."""
    ramp = 1 + progression["weekly_ramp_pct"] / 100
    roles = _week_roles(phase_by_week, progression["build_recover"])

    multipliers: list[float] = []
    level = 1.0
    for role in roles:
        if role == "taper":
            multipliers.append(0.0)  # filled in below, once the peak is known
        elif role == "recover":
            multipliers.append(level * RECOVERY_FACTOR)
        else:
            multipliers.append(level)
            level *= ramp

    taper_count = phase_by_week.count("taper")
    if taper_count:
        peak = max(multipliers) if any(multipliers) else 1.0
        for i in range(taper_count):
            fraction = i / (taper_count - 1) if taper_count > 1 else 0.0
            share = TAPER_START + (TAPER_END - TAPER_START) * fraction
            multipliers[len(multipliers) - taper_count + i] = peak * share
    return multipliers


def _select_sessions(templates: list[dict], days_per_week: int) -> list[dict]:
    """Trim to days_per_week training days, lowest priority dropped first. A
    session on an already-taken day costs no extra day, so doubles survive."""
    selected: list[dict] = []
    days: set[int] = set()
    for session in sorted(templates, key=lambda s: s["priority"]):
        if session["day"] in days or len(days) < days_per_week:
            selected.append(session)
            days.add(session["day"])
    return selected


def _rotate(day: int, template_rest_day: int, rest_day: int) -> int:
    return (day + rest_day - template_rest_day) % 7


def _scaled(scale: dict, multiplier: float) -> int:
    value = round(scale["base"] * multiplier / scale["step"]) * scale["step"]
    return int(max(scale["min"], min(scale["max"], value)))


# --- intensity targets ----------------------------------------------------


# Scopes derivation, so a single-sport goal never derives an intensity nothing
# uses. Strength is absent on purpose — one target per *lift*, not per sport —
# and is resolved alongside these in derive_targets.
_SPORT_TARGETS = {
    "run": ("run_5k_seconds", "run_5k"),
    "swim": ("swim_css_100m", "swim_css_100m"),
    "cycle": ("bike_ftp", "bike_ftp"),
}


def lift_increment(exercise: str) -> float:
    return LIFT_INCREMENT_KG.get(exercise, DEFAULT_LIFT_INCREMENT_KG)


def _round_to_increment(value: float, increment: float) -> float:
    """A bar only holds what the plates allow."""
    return round(round(value / increment) * increment, 2)


def working_weight_from_1rm(e1rm_kg: float, reps: int, increment: float) -> float:
    """Bar weight for `reps` reps: compute.estimated_1rm read backwards, so a
    PB and a target derived from it can't disagree. Inverting rather than
    applying a flat 75% keeps it honest across rep schemes."""
    if e1rm_kg <= 0 or reps <= 0:
        return 0.0
    return _round_to_increment(e1rm_kg / (1 + reps / 30), increment)


def strength_weekly_e1rm(
    current_kg: float, goal_kg: float, roles: list[str], increment: float
) -> list[float]:
    """One target e1RM per week, current -> goal. Mirrors _week_multipliers'
    shape so the two progressions agree about the week structure.

    Capped at one increment per week, so a goal further off than the plan is
    long simply isn't reached — expand_plan warns rather than prescribing it."""
    builds = roles.count("build")
    span = max(goal_kg - current_kg, 0.0)
    step = min(increment, span / max(builds - 1, 1)) if builds > 1 else 0.0

    weekly: list[float] = []
    level = current_kg
    started = False
    for role in roles:
        if role == "build":
            if started:
                level = min(level + step, goal_kg)
            started = True
            weekly.append(level)
        elif role == "recover":
            weekly.append(level * STRENGTH_DELOAD_FACTOR)
        else:  # taper
            weekly.append(level * STRENGTH_TAPER_FACTOR)
    return weekly


def reachable_e1rm(current_kg: float, roles: list[str], increment: float) -> float:
    """The most this length can honestly add: one increment per build week.
    Both the derived goal and the too-ambitious warning measure against it."""
    return current_kg + increment * max(roles.count("build") - 1, 0)


def derive_lift_1rm(recent: list[dict], exercise: str) -> tuple[float, str] | None:
    """(best e1RM, why), or None. The raw measurement, unguarded — same split
    planner.derive_run_5k has with planner.derive_target."""
    strength = [a for a in recent if a.get("type") == "strength"]
    lift = compute.all_personal_bests(strength).get("strength", {}).get(exercise, {})
    value = lift.get("best_e1rm_kg")
    if not value:
        return None
    seen = lift.get("best_e1rm_date", "")
    where = f" ({seen})" if seen else ""
    return value, f"best {exercise.replace('_', ' ')} e1RM in training{where}"


def derive_lift_target(recent: list[dict], exercise: str) -> dict:
    """{"value", "why"} — the guarded front door to derive_lift_1rm, mirroring
    planner.derive_target. Separate from PLAUSIBLE_TARGETS because strength
    needs one bound per lift, not one per sport."""
    derived = derive_lift_1rm(recent, exercise)
    if derived is None:
        return {"value": None, "why": "no recent history to derive from"}
    value, why = derived
    low, high = PLAUSIBLE_LIFT_E1RM_KG
    if not low <= value <= high:
        return {
            "value": None,
            "why": f"{why} gave {value:g}kg, outside the plausible "
            f"{low:g}–{high:g}kg — ignored",
        }
    return {"value": value, "why": why}


def template_sports(goal: str) -> set:
    """Sports the goal's session mix uses."""
    return {session["sport"] for session in GOAL_TEMPLATES[goal]["weekly_sessions"]}


def volume_sports(goal: str) -> set:
    """Sports the multiplier can actually scale — everything but strength,
    whose sessions carry no scale. Measuring against training it cannot move
    would only bias the result."""
    return {
        session["sport"]
        for session in GOAL_TEMPLATES[goal]["weekly_sessions"]
        if session["scale"]
    }


def template_lifts(goal: str) -> list[str]:
    """Lifts the goal prescribes, in first-appearance order. Scopes target
    derivation so a plan never derives a lift it never prescribes."""
    lifts: list[str] = []
    for session in GOAL_TEMPLATES[goal]["weekly_sessions"]:
        for exercise in session["params"].get("exercises", []):
            if exercise["exercise"] not in lifts:
                lifts.append(exercise["exercise"])
    return lifts


def derive_targets(spec: dict, activities: list[dict], reference: date) -> dict:
    """The intensities the whole plan is built against, scoped to the sports
    the goal trains, each with a "why". Precedence: description targets ->
    planner.derive_* over the recent window -> FALLBACK_TARGETS.

    Strength has its own key: one figure per lift, and its target moves weekly
    (_attach_weekly_lifts fills that in once the length is known)."""
    recent = planner.recent_activities(activities, reference)
    overrides = spec.get("targets", {})
    targets: dict = {"why": {}}

    for sport in sorted(template_sports(spec["goal"]) & set(_SPORT_TARGETS)):
        key, override_key = _SPORT_TARGETS[sport]
        if override_key in overrides:
            targets[key] = overrides[override_key]
            targets["why"][key] = "set in the plan description"
            continue
        # A rejected measurement comes back None with a why, so the fallback
        # never looks like absent data when it was a bad reading.
        derived = planner.derive_target(sport, recent)
        if derived["value"] is not None:
            targets[key] = derived["value"]
            targets["why"][key] = derived["why"]
        else:
            targets[key] = FALLBACK_TARGETS[key]
            targets["why"][key] = f"default — {derived['why']}"

    lifts = template_lifts(spec["goal"])
    if lifts:
        targets["strength"] = _derive_lift_targets(lifts, overrides, recent, targets)
    return targets


def _derive_lift_targets(
    lifts: list[str], overrides: dict, recent: list[dict], targets: dict
) -> dict:
    """{lift: {current_e1rm_kg, goal_e1rm_kg, goal_from}} — where each lift
    starts and where the plan aims it.

    `current` is measured, like every other target. `goal` cannot be — it is a
    decision about the future — so the description wins, else it defaults to
    reachable_e1rm (filled in by _attach_weekly_lifts). Deriving it keeps
    strength inside the derive-or-fall-back rule, so a plan expands with no
    `targets:` at all."""
    resolved = {}
    for lift in lifts:
        derived = derive_lift_target(recent, lift)
        if derived["value"] is not None:
            current = derived["value"]
            why = derived["why"]
        else:
            current = FALLBACK_LIFT_E1RM_KG.get(lift, 60.0)
            why = f"default — {derived['why']}"
        targets["why"][f"{lift}_e1rm_kg"] = why

        goal_key = f"{lift}_goal_kg"
        entry = {"current_e1rm_kg": round(current, 2)}
        if goal_key in overrides:
            entry["goal_e1rm_kg"] = float(overrides[goal_key])
            entry["goal_from"] = "set in the plan description"
        resolved[lift] = entry
    return resolved


def _attach_weekly_lifts(targets: dict, roles: list[str]) -> list[str]:
    """Fill in each lift's per-week e1RM path in place; returns warnings. The
    table is what keeps _apply_target a stateless lookup — the session already
    knows its week, so only an index is threaded through."""
    warnings = []
    for lift, entry in targets.get("strength", {}).items():
        increment = lift_increment(lift)
        current = entry["current_e1rm_kg"]
        reachable = reachable_e1rm(current, roles, increment)
        if "goal_e1rm_kg" not in entry:
            entry["goal_e1rm_kg"] = round(reachable, 2)
            entry["goal_from"] = (
                f"one {increment:g}kg step per build week — what this plan's "
                "length can deliver"
            )
        elif entry["goal_e1rm_kg"] > reachable + 0.01:
            warnings.append(
                f"{lift.replace('_', ' ')}: reaching {entry['goal_e1rm_kg']:g}kg "
                f"needs more than {increment:g}kg a week over this plan — it will "
                f"get to about {reachable:g}kg. Start earlier, or aim lower."
            )
        entry["by_week"] = [
            round(value, 2)
            for value in strength_weekly_e1rm(
                current, entry["goal_e1rm_kg"], roles, increment
            )
        ]
    return warnings


def derive_volume_scale(
    spec: dict,
    activities: list[dict],
    reference: date,
    template_week_seconds: int,
) -> tuple[float, str]:
    """(scale, why) for the opening week. `volume:` wins; else the *mean*
    weekly training in the goal's sports over RECENT_VOLUME_WEEKS, measured
    against the template's opening week and clamped.

    Mean, not median: with sparse training the median collapses to zero once
    half the weeks are empty, handing somebody barely riding the full template
    volume. Empty weeks are real information and weigh in as zeroes."""
    if "volume" in spec:
        return spec["volume"] / 100, "set in the plan description"
    if not template_week_seconds:
        return 1.0, "template default"

    relevant = compute.filter_by_types(
        planner.recent_activities(activities, reference),
        sorted(volume_sports(spec["goal"])),
    )
    # The current week is still filling up; counting it would understate form.
    complete_weeks = [
        week
        for week in compute.weekly_volumes(relevant, through=reference)
        if not compute.is_current_week(week["week"], reference)
    ]
    recent_weeks = complete_weeks[-RECENT_VOLUME_WEEKS:]
    # No history means "unknown", not "untrained" — leave the template alone.
    if not any(week["duration_seconds"] for week in recent_weeks):
        return 1.0, "template default (no recent training in these sports to measure)"

    mean_seconds = statistics.mean(week["duration_seconds"] for week in recent_weeks)
    raw = mean_seconds / template_week_seconds
    scale = max(VOLUME_SCALE_MIN, min(VOLUME_SCALE_MAX, raw))
    why = (
        f"your recent {mean_seconds / 3600:.1f}h/week against the template's "
        f"{template_week_seconds / 3600:.1f}h opening week"
    )
    if scale != raw:
        why += f" (clamped to {round(scale * 100)}%)"
    return scale, why


def _volume_scale_for_week(
    start_scale: float, index: int, weeks: int, taper_weeks: int
) -> float:
    """Converge from start_scale to the template's own level by the last build
    week. Uniform scaling would start them right but leave them under-prepared
    for a fixed-distance event."""
    last_build = max(weeks - max(taper_weeks, 0) - 1, 1)
    if index >= last_build:
        return 1.0
    return start_scale + (1.0 - start_scale) * (index / last_build)


def days_per_week_range(spec: dict) -> tuple[int, int]:
    """(first, final) training days; a plain number means both."""
    value = spec["days_per_week"]
    if isinstance(value, list):
        return value[0], value[1]
    return value, value


def _days_for_week(
    low: int, high: int, index: int, weeks: int, taper_weeks: int
) -> int:
    """Builds low -> high by the last build week, then holds through the taper:
    a taper cuts volume, not frequency. Same schedule as _volume_scale_for_week."""
    if low == high:
        return low
    last_build = max(weeks - max(taper_weeks, 0) - 1, 1)
    if index >= last_build:
        return high
    return round(low + (high - low) * index / last_build)


def _apply_target(
    sport: str, session_type: str, params: dict, targets: dict, week: int = 1
) -> None:
    """Fill in the one intensity param each session type needs, in place.

    `week` is read only by strength, as an index into _attach_weekly_lifts'
    table. Everywhere else intensity is a pure function of the target."""
    if sport == "run":
        if session_type == "intervals":
            params["target_pace"] = planner.recommended_interval_pace(
                targets["run_5k_seconds"], params["rep_distance_m"]
            )
        elif session_type == "tempo":
            params["target_pace"] = planner.tempo_pace_from_5k(
                targets["run_5k_seconds"]
            )
        elif session_type in ("easy", "long"):
            params["target_pace"] = planner.easy_pace_from_5k(targets["run_5k_seconds"])
    elif sport == "cycle":
        if session_type == "intervals":
            params["target_watts"] = targets["bike_ftp"]
    elif sport == "swim" and session_type in ("intervals", "continuous"):
        params["target_pace_100m"] = targets["swim_css_100m"]
    elif sport == "strength" and session_type == "straight_sets":
        # The one session type whose intensity is a list, not a single param.
        table = targets.get("strength", {})
        for exercise in params.get("exercises", []):
            lift = table.get(exercise["exercise"])
            if not lift or not lift.get("by_week"):
                continue
            by_week = lift["by_week"]
            e1rm = by_week[min(max(week, 1), len(by_week)) - 1]
            exercise["target_weight_kg"] = working_weight_from_1rm(
                e1rm, exercise["reps"], lift_increment(exercise["exercise"])
            )


# --- expansion ------------------------------------------------------------


def _week_seconds(laid_out: list[dict], targets: dict, multiplier: float = 1.0) -> int:
    """Estimated seconds in one templated week, for sizing the opening week.

    Only scaling sessions count: a strength session's size is fixed, so
    including it would put time into the ratio the multiplier can never move."""
    total = 0
    for template_session in laid_out:
        scale = template_session["scale"]
        if not scale:
            continue
        params = copy.deepcopy(template_session["params"])
        params[scale["param"]] = _scaled(scale, multiplier)
        sport, session_type = (
            template_session["sport"],
            template_session["session_type"],
        )
        _apply_target(sport, session_type, params, targets)
        total += planner.estimate_seconds(sport, session_type, params)
    return total


def _build_session(
    template_session: dict,
    session_date: date,
    week: int,
    phase: str,
    multiplier: float,
    targets: dict,
) -> dict:
    # Deep, not shallow: exercise dicts are shared with the template, so a
    # shallow copy would have week 12 writing its load into week 1.
    params = copy.deepcopy(template_session["params"])
    scale = template_session["scale"]
    if scale:
        params[scale["param"]] = _scaled(scale, multiplier)
    sport, session_type = template_session["sport"], template_session["session_type"]
    _apply_target(sport, session_type, params, targets, week)

    return {
        "date": session_date.isoformat(),
        "week": week,
        "phase": phase,
        "sport": sport,
        "session_type": session_type,
        "params": params,
        "workout_name": planner.workout_name(sport, session_type, params),
        "is_key": template_session["key"],
        "is_brick": template_session.get("brick", False),
        "is_extra": False,
    }


def benchmark_sports(goal: str) -> list[str]:
    """Testable sports in this goal; BENCHMARK_SESSIONS' order is the rotation."""
    trained = template_sports(goal)
    return [sport for sport in BENCHMARK_SESSIONS if sport in trained]


def _build_benchmark(
    sport: str,
    session_date: date,
    week: int,
    phase: str,
    replaced: dict | None = None,
) -> dict:
    """A re-test: unscaled and untargeted, so this week's result compares with
    the last. `replaced` matters only for strength — which lift to test is a
    property of the session, not the sport."""
    spec = BENCHMARK_SESSIONS[sport]
    params = dict(spec["params"])
    if sport == "strength":
        exercises = (replaced or {}).get("params", {}).get("exercises") or []
        if exercises:
            params["exercise"] = exercises[0]["exercise"]
    return {
        "date": session_date.isoformat(),
        "week": week,
        "phase": phase,
        "sport": sport,
        "session_type": spec["session_type"],
        "params": params,
        "workout_name": planner.workout_name(sport, spec["session_type"], params),
        "is_key": True,
        "is_brick": False,
        "is_extra": False,
        "is_benchmark": True,
    }


def _test_identity(sport: str, template_session: dict) -> tuple:
    """What makes two benchmarks the same test, for deduplicating a test week.
    Per sport for run/cycle/swim, but per *lift* for strength — squatting
    Wednesday and benching Friday is two tests, not one."""
    if sport != "strength":
        return (sport,)
    exercises = template_session.get("params", {}).get("exercises") or []
    return (sport, exercises[0]["exercise"] if exercises else None)


def _test_week_sessions(
    week_one: list[dict], monday: date, testable: list[str]
) -> list[dict]:
    """Week 0: one benchmark per distinct test, and nothing else — you turn up
    rested, test, and go home. That is what separates it from an in-plan
    re-test, which replaces one session and leaves the week intact.

    Drawn from week 1's own selection in priority order, so each test lands on
    a day that sport already owns."""
    built: list[dict] = []
    seen: set[tuple] = set()
    for template_session in week_one:
        sport = template_session["sport"]
        if sport not in testable:
            continue
        identity = _test_identity(sport, template_session)
        if identity in seen:
            continue
        seen.add(identity)
        built.append(
            _build_benchmark(
                sport,
                monday + timedelta(days=template_session["day"]),
                0,
                "test",
                template_session,
            )
        )
    return built


def _extra_days(week_sessions: list[dict], occupied: dict[int, int]) -> list[int]:
    """Placeable days, least-loaded first; key-session days never qualify."""
    key_days = {s["day"] for s in week_sessions if s["key"]}
    candidates = [day for day in range(7) if day not in key_days]
    return sorted(candidates, key=lambda day: (occupied.get(day, 0), day))


def _build_extras(
    extras: dict, week_sessions: list[dict], monday: date, week: int, phase: str
) -> list[dict]:
    occupied: dict[int, int] = {}
    for session in week_sessions:
        occupied[session["day"]] = occupied.get(session["day"], 0) + 1
    candidates = _extra_days(week_sessions, occupied)
    if not candidates:
        return []

    built = []
    slot = 0
    for name, count in sorted(extras.items()):
        for _ in range(count):
            day = candidates[slot % len(candidates)]
            slot += 1
            duration = EXTRA_DURATIONS_S.get(name, DEFAULT_EXTRA_DURATION_S)
            built.append(
                {
                    "date": (monday + timedelta(days=day)).isoformat(),
                    "week": week,
                    "phase": phase,
                    "sport": name,
                    "session_type": name,
                    "duration_s": duration,
                    "workout_name": f"{name.capitalize()} {duration // 60}min",
                    "is_key": False,
                    "is_brick": False,
                    "is_extra": True,
                }
            )
    return built


def expand_plan(
    spec: dict, activities: list[dict], reference: date, volume: dict | None = None
) -> dict:
    """The engine: spec + history -> the full plan dict (metadata, targets, and
    a flat list of dated sessions, oldest first). reference is today.

    Nothing here is persisted — `fit train` re-runs this on every command, so
    targets track your current fitness with no retarget step (see
    storage.write_training_plan). `volume`, when given, is the
    derive_volume_scale result pinned at import: the *starting* volume is a
    decision made once about where you were then, and re-measuring it weekly
    would rewrite session sizes as you train.

    Sessions before start_date or on/after the event are dropped."""
    template = GOAL_TEMPLATES[spec["goal"]]
    test_week = spec["test_week"]
    start_monday, span = _plan_weeks(spec["start_date"], spec["event_date"], test_week)
    # A test week comes out of the plan, not off the front: both dates are the
    # user's. So the periodised block runs from the *second* Monday.
    weeks = span - 1 if test_week else span
    first_monday = start_monday + timedelta(weeks=1) if test_week else start_monday
    progression = dict(spec["progression"])
    phase_by_week = _assign_phases(
        weeks, template["phases"], progression["taper_weeks"]
    )
    # Solved from this plan's length unless the description pinned one.
    ramp_derived = "weekly_ramp_pct" not in progression
    if ramp_derived:
        progression["weekly_ramp_pct"] = derive_weekly_ramp(
            template, phase_by_week, progression["build_recover"]
        )
    multipliers = _week_multipliers(phase_by_week, progression)
    roles = _week_roles(phase_by_week, progression["build_recover"])
    targets = derive_targets(spec, activities, reference)
    # Needs the week structure, so not inside derive_targets.
    lift_warnings = _attach_weekly_lifts(targets, roles)

    # Rotate first, trim per week: selection counts distinct days, which
    # rotation preserves.
    laid_out = [
        {
            **session,
            "day": _rotate(session["day"], template["rest_day"], spec["rest_day"]),
        }
        for session in template["weekly_sessions"]
    ]
    low_days, high_days = days_per_week_range(spec)
    taper_weeks = progression["taper_weeks"]

    def week_sessions(index: int) -> list[dict]:
        return _select_sessions(
            laid_out, _days_for_week(low_days, high_days, index, weeks, taper_weeks)
        )

    # Measured against week 1's *own* session list, which is smaller when
    # frequency builds — otherwise a plan opening at two rides would look like
    # the user was training far below a week they were never asked to do.
    if volume:
        start_scale, volume_why = volume["start_scale"], volume["why"]
    else:
        start_scale, volume_why = derive_volume_scale(
            spec, activities, reference, _week_seconds(week_sessions(0), targets)
        )
    warnings = list(lift_warnings)
    growth = (max(multipliers) if multipliers else 1.0) / max(start_scale, 0.01)
    if growth > VOLUME_RAMP_WARN:
        warnings.append(
            f"this plan grows {growth:.1f}x from week 1 to its peak — your recent "
            "training is well below where the goal needs to start. An earlier "
            "start_date, or a shorter goal, would be a gentler way in."
        )

    # Benchmarks land on recovery weeks, taking turns between the goal's sports
    # and replacing that sport's session rather than adding to it.
    testable = benchmark_sports(spec["goal"]) if spec["benchmarks"] else []
    bench_by_week: dict[int, tuple[str, dict]] = {}
    tested: dict[str, int] = {sport: 0 for sport in testable}
    for index, role in enumerate(roles):
        if not testable or role != "recover" or phase_by_week[index] == "taper":
            continue
        available = week_sessions(index)
        # What the test stands in for, most preferred first: a quality session
        # if the week has one, else the long one — at low frequencies that is
        # the only slot a sport has.
        options = {}
        for sport in testable:
            slot = next(
                (
                    s
                    for wanted in ("intervals", "tempo", "straight_sets", "long")
                    for s in available
                    if s["sport"] == sport and s["session_type"] == wanted
                ),
                None,
            )
            if slot is not None:
                options[sport] = slot
        if not options:
            continue  # nothing to stand in for, and do not consume a turn
        # Whichever sport has gone longest without a test.
        sport = min(options, key=lambda s: (tested[s], testable.index(s)))
        bench_by_week[index] = (sport, options[sport])
        tested[sport] += 1

    sessions = []
    benchmark_weeks = []
    if test_week:
        # Independent of spec["benchmarks"], which governs in-plan re-tests
        # only: measuring once up front and re-measuring as you go are separate
        # decisions.
        sessions.extend(
            _test_week_sessions(
                week_sessions(0), start_monday, benchmark_sports(spec["goal"])
            )
        )
    for index in range(weeks):
        week, phase = index + 1, phase_by_week[index]
        monday = first_monday + timedelta(weeks=index)
        volume = _volume_scale_for_week(start_scale, index, weeks, taper_weeks)
        this_week = week_sessions(index)
        bench_sport, replaced = bench_by_week.get(index, (None, None))
        for template_session in this_week:
            if replaced is not None and template_session is replaced:
                sessions.append(
                    _build_benchmark(
                        bench_sport,
                        monday + timedelta(days=template_session["day"]),
                        week,
                        phase,
                        template_session,
                    )
                )
                benchmark_weeks.append(week)
                continue
            sessions.append(
                _build_session(
                    template_session,
                    monday + timedelta(days=template_session["day"]),
                    week,
                    phase,
                    multipliers[index] * volume,
                    targets,
                )
            )
        sessions.extend(_build_extras(spec["extras"], this_week, monday, week, phase))

    sessions = [
        s for s in sessions if spec["start_date"] <= s["date"] < spec["event_date"]
    ]
    sessions.sort(key=lambda s: (s["date"], not s["is_key"], s["sport"]))

    return {
        "goal": spec["goal"],
        "event_date": spec["event_date"],
        "start_date": spec["start_date"],
        "weeks": weeks,
        "test_week": test_week,
        "created": reference.isoformat(),
        "spec": spec,
        "targets": targets,
        "volume": {"start_scale": round(start_scale, 3), "why": volume_why},
        "days_per_week": {"start": low_days, "end": high_days},
        "benchmark_weeks": benchmark_weeks,
        "progression": {
            "weekly_ramp_pct": round(progression["weekly_ramp_pct"], 2),
            "derived": ramp_derived,
            "build_recover": progression["build_recover"],
            "taper_weeks": progression["taper_weeks"],
        },
        "warnings": warnings,
        "sessions": sessions,
    }


def session_to_build_args(session: dict) -> tuple[str, str, dict] | None:
    """(sport, workout_type, params) for build_plan; None for an extra."""
    if session.get("is_extra"):
        return None
    return session["sport"], session["session_type"], session["params"]


# --- completion and views -------------------------------------------------


def match_completion(sessions: list[dict], activities: list[dict]) -> list[dict]:
    """Copies with "completed" set: same sport in the same ISO week, each
    activity claiming at most one session (nearest first) so one ride can't
    tick off a whole week. Extras are never matched.

    The week, not a day window, is the unit: a plan is periodised in whole ISO
    weeks, so Friday's lift done on the Sunday is still that week's work. A ±1
    day window scored three sessions-as-missed that had all been trained 2-3
    days off their date."""
    candidates = [a for a in activities if a.get("type") and a.get("date")]
    claimed: set[int] = set()
    matched = []
    for session in sessions:
        completed = False
        if not session.get("is_extra"):
            session_date = date.fromisoformat(session["date"])
            session_week = session_date.isocalendar()[:2]
            nearby = sorted(
                (abs((date.fromisoformat(a["date"]) - session_date).days), i)
                for i, a in enumerate(candidates)
                if i not in claimed
                and a.get("type") == session.get("sport")
                and date.fromisoformat(a["date"]).isocalendar()[:2] == session_week
            )
            if nearby:
                claimed.add(nearby[0][1])
                completed = True
        matched.append({**session, "completed": completed})
    return matched


def group_by_week(sessions: list[dict]) -> list[dict]:
    """[{week, phase, start, sessions}], oldest first. Each session gains a
    "description" here so display.py prints text rather than composing it."""
    weeks: dict[int, dict] = {}
    for session in sessions:
        week = weeks.setdefault(
            session["week"],
            {
                "week": session["week"],
                "phase": session["phase"],
                "start": session["date"],
                "sessions": [],
            },
        )
        week["sessions"].append({**session, "description": describe_session(session)})
        week["start"] = min(week["start"], session["date"])
    return [weeks[key] for key in sorted(weeks)]


def plan_summary(plan: dict, today: date) -> dict:
    """Header figures for `fit train show`."""
    sessions = plan.get("sessions", [])
    real = [s for s in sessions if not s.get("is_extra")]
    event = date.fromisoformat(plan["event_date"])
    template = GOAL_TEMPLATES.get(plan.get("goal"), {})
    return {
        "goal": plan.get("goal"),
        "label": template.get("label", plan.get("goal", "")),
        "description": template.get("description", ""),
        "event_date": plan["event_date"],
        "start_date": plan.get("start_date"),
        "weeks": plan.get("weeks", 0),
        "test_week": plan.get("test_week", False),
        "days_to_go": (event - today).days,
        "sessions": len(sessions),
        "extras": len(sessions) - len(real),
        "completed": sum(1 for s in sessions if s.get("completed")),
        "scheduled": sum(1 for s in real if s.get("pushed")),
        "stale": sum(1 for s in real if s.get("stale")),
        "targets": plan.get("targets", {}),
        "volume": plan.get("volume", {}),
        "benchmark_weeks": plan.get("benchmark_weeks", []),
        "warnings": plan.get("warnings", []),
    }


def describe_session(session: dict) -> str:
    """One line per session, e.g. "Cycle long 45km @ 165W (brick)"."""
    name = session.get("workout_name", session.get("session_type", ""))
    if session.get("is_brick"):
        return f"{name} (brick)"
    if session.get("is_benchmark"):
        # Week 0 is labelled "test" and holds nothing else, so the suffix would
        # only restate the name ("Cycle baseline 20min test (test)").
        return name if session.get("week") == 0 else f"{name} (re-test)"
    return name


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
    """Not-yet-pushed, non-extra sessions in [today, today + window_days].
    Re-running sync finds fewer, which is what makes it idempotent."""
    end = (today + timedelta(days=window_days)).isoformat()
    return [
        s
        for s in sessions
        if not s.get("is_extra")
        and not s.get("pushed")
        and today.isoformat() <= s["date"] <= end
    ]


def future_pushed(pushed: list[dict], today: date) -> list[dict]:
    """Ledger rows still ahead of today — what `train clear` unschedules."""
    return [e for e in pushed if e["date"] >= today.isoformat()]


def unreadable_plan(stored: dict) -> list[dict] | None:
    """None when `stored` is the current {spec, created, volume, pushed}
    shape, else the sessions it claims are already on the Garmin calendar.

    A missing "pushed" is not an empty ledger. Plans written before the
    stateless rewrite kept no ledger at all — they stamped each session in a
    top-level "sessions" list with its own garmin_workout_id and
    scheduled_workout_id — so `stored.get("pushed", [])` reads as "nothing is
    scheduled" and any guard built on it waves the file through, stranding
    whatever is really on the calendar. That happened: three entries on
    2026-09-11. The returned list may be empty; the file is still one this
    version cannot reason about, so the caller must refuse either way.
    """
    if {"spec", "created", "volume", "pushed"} <= set(stored):
        return None
    return [
        session
        for session in stored.get("sessions", [])
        if session.get("scheduled_workout_id") is not None
    ]
