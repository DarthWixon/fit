"""Multi-week periodised training plans: the pure engine behind `fit train`.

Takes a compact plan description (the YAML "standard form" an external AI bot
emits — see parse_plan_spec), expands it into a full dated schedule of session
intents, and derives every intensity target from the user's own history. No
file I/O, no network, no typer: cli.py reads the description text and hands it
in, the same split importers.py has with path reading. May import compute and
planner (pure -> pure).

The description is deliberately thin — goal plus preferences. All the training
content lives here in GOAL_TEMPLATES, so the bot never has to know how to
periodise anything.

Scheduling stays built on the atomic seam that already exists: each expanded
session is pushed with planner.build_plan -> garmin.push_workout and placed
with garmin.schedule_workout one date at a time (cli.py wires that up), rather
than through any batch-shaped entry point.
"""

import statistics
from datetime import date, timedelta

from fit import compute, planner

# --- progression shape ----------------------------------------------------

# Standard endurance-coaching defaults, all overridable per description:
# three build weeks then one recovery week, each build week ~8% bigger than
# the last, and a two-week taper into the event.
PROGRESSION_DEFAULTS = {
    "build_recover": [3, 1],
    "taper_weeks": 2,
}

# The ramp a goal template is calibrated against. It is not a plan default:
# unless the description names one, the ramp is solved per plan from its actual
# length (derive_weekly_ramp), so a longer block climbs more gently to the same
# peak rather than compounding past it. This is the rate used to define what
# that peak is, at each template's own length.
REFERENCE_RAMP_PCT = 8

# A recovery week drops to 60% of the level the build block reached; the taper
# steps down from 75% of peak to 45% across its weeks. Both are volume
# multipliers applied to the template's base session sizes.
RECOVERY_FACTOR = 0.6
TAPER_START = 0.75
TAPER_END = 0.45

# Session size grows through the plan by the weekly multiplier alone — the
# template gives each session one base size and a clamp, and the multiplier
# ramps, dips and tapers it. Deliberately one mechanism rather than a separate
# per-week growth increment on top, which would compound into nonsense.

MIN_PLAN_WEEKS = 4

# --- benchmarks -----------------------------------------------------------
#
# Intensity targets are measured, never projected: every session in a plan is
# built against the fitness derive_targets found when the plan was expanded, and
# it does not drift upward on the assumption you will improve. Guessing a future
# pace risks prescribing work you cannot complete, which is worse than
# prescribing work that is slightly easy.
#
# The way a plan earns a faster pace is therefore to re-measure. A benchmark
# lands on recovery weeks — you test rested, which is what makes one test
# comparable to the next — cycling through the sports the goal trains, and
# replacing that sport's quality session for the week. Its own numbers never
# scale: a 5km test is only a benchmark if it is the same 5km every time.
#
# Doing the test then re-running `fit train retarget` re-derives every target
# from the updated history and rewrites the sessions still ahead of you.
# Whether a test can afford a warmup inside the same recording depends on the
# sport, not on taste: the recorded activity must BE the test unless the
# sport's split machinery can isolate it from within.
BENCHMARK_SESSIONS = {
    # Run keeps its warmup and cooldown. compute.fastest_split finds the
    # fastest 5k window anywhere in the track, so the test is isolated from the
    # jogging around it. 5km rather than 3km because 3km appears in neither
    # SPLIT_DISTANCES_KM nor MILESTONES_KM — a 3km effort is measurable
    # nowhere, and the best 5k window containing it necessarily drags in 2km of
    # warmup, which made the test worse than no test at all.
    "run": {
        "session_type": "baseline",
        "params": {
            "warmup_minutes": 10,
            "test_distance_m": 5000,
            "cooldown_minutes": 10,
        },
    },
    # Bare. Stored avg_power is the whole-activity mean with no split analogue,
    # so anything else in the recording dilutes the result — a 20+20+10 session
    # reads roughly 40% easy riding into the FTP estimate.
    "cycle": {"session_type": "baseline", "params": {"test_minutes": 20}},
    # Bare, and 1km: a whole swim of 1.000-1.060km lands in the existing
    # fastest_1k milestone, which derive_swim_css already reads. Pool swims
    # often carry no cumulative-distance stream, so the milestone — computed
    # from distance and duration on every read — is the only measurement that
    # can be relied on. A shorter test would have needed a new split distance
    # and a reworked CSS model for a less reliable signal.
    "swim": {"session_type": "baseline", "params": {"test_distance_m": 1000}},
}

# --- starting volume ------------------------------------------------------
#
# A goal template's opening week assumes a base the user may not have. Rather
# than make them guess a percentage, the opening week is measured against what
# they are actually training now (derive_volume_scale), then converges back to
# the template's own level by the last build week — so the plan starts where
# they are but still arrives at a volume the event demands. `volume:` in the
# description overrides the measurement.
VOLUME_SCALE_MIN = 0.6
VOLUME_SCALE_MAX = 1.25
# How many recent weeks to measure. Long enough to survive one quiet week,
# short enough to reflect current form rather than last season's.
RECENT_VOLUME_WEEKS = 8
# Growth from week 1 to the peak beyond this multiple is flagged: it means the
# user is starting so far below the goal that the ramp itself is a risk.
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

# Extras (yoga/strength/...) are tracked locally only — never built as Garmin
# workouts, never pushed. The Garmin calendar has no note/non-workout endpoint,
# so there is nothing to schedule them onto.
EXTRA_DURATIONS_S = {
    "strength": 2700,
    "yoga": 1800,
    "mobility": 1200,
    "core": 1200,
}
DEFAULT_EXTRA_DURATION_S = 1800

# Used when history has nothing to derive from and the description gave no
# override: a plan with plausible targets beats no plan at all, and `fit train
# show` records where each target came from.
FALLBACK_TARGETS = {
    "run_5k_seconds": 1500,  # 25:00
    "bike_ftp": 200,
    "swim_css_100m": 120,  # 2:00
}


# --- goal templates -------------------------------------------------------

# One profile per goal: plan length, phase structure (the taper comes from the
# progression settings, so it is not listed here), and the weekly session mix.
# Standard endurance-coaching shapes; every number here is a tunable constant.
#
# Phase lengths sum to weeks - PROGRESSION_DEFAULTS["taper_weeks"]. A
# description that moves start_date reapportions them (see _assign_phases), so
# they need not sum exactly — but keeping them tidy makes the intent readable.


def _scale(param: str, base: int, low: int, high: int, step: int) -> dict:
    """The single session param that grows with the week's volume multiplier,
    with the clamp it may never escape (see _scaled)."""
    return {"param": param, "base": base, "min": low, "max": high, "step": step}


def _session(
    sport: str,
    session_type: str,
    day: int,
    priority: int,
    scale: dict,
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
    params    the fixed planner.build_plan params for the session type; the
              intensity one is filled in later by _apply_target
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
        ],
    },
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
}
_TARGET_KEYS = {"run_5k", "bike_ftp", "swim_css_100m"}

_YAML_INSTALL_HINT = (
    "fit train import needs the optional 'pyyaml' dependency.\n"
    "Install it with: pip install -e '.[train]'"
)


def _yaml():
    """Lazy import, mirroring garmin.py's optional-dependency pattern: only
    `train import` parses YAML — show/sync/clear read the stored JSON and work
    without PyYAML installed."""
    try:
        import yaml
    except ImportError as exc:
        raise ValueError(_YAML_INSTALL_HINT) from exc
    return yaml


def _as_date_string(value, field: str) -> str:
    """Normalise a description date to 'YYYY-MM-DD'. PyYAML resolves an
    unquoted 2026-06-14 to a datetime.date rather than a string, so accept
    both forms and hand the text to planner.parse_schedule_date — the single
    date-validation seam every scheduled date already passes through."""
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected a date like 2026-06-14, got {value!r}")
    try:
        return planner.parse_schedule_date(value)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc


def _as_seconds(value, field: str) -> int:
    """Normalise a m:ss time target to seconds. YAML 1.1 resolves an unquoted
    24:00 as a base-60 integer, which lands on exactly the seconds we want
    (24*60 = 1440), so a bare int is accepted as already-seconds — quoted or
    not, the description means the same thing."""
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


def _as_weekday(value, field: str) -> int:
    """'Mon'/'monday' -> 0. Guards the YAML 1.1 booleans too: an unquoted
    `rest_day: no` would otherwise arrive as False."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{field}: expected a weekday like Mon, got {value!r}")
    day = _WEEKDAY_LOOKUP.get(value.strip().lower())
    if day is None:
        raise ValueError(f"{field}: expected a weekday like Mon, got {value!r}")
    return day


def parse_plan_spec(text: str) -> dict:
    """Parse and validate a YAML plan description into a normalised spec dict,
    with every optional field defaulted from the goal template. Raises
    ValueError with a specific message on anything malformed (including a
    missing PyYAML).

    The standard form — only goal and event_date are required:

        goal: sprint_triathlon
        event_date: 2026-06-14
        start_date: 2026-03-23        # default: event_date - template length
        days_per_week: 6
        rest_day: Mon
        extras: {strength: 2, yoga: 1}
        targets: {run_5k: "24:00", bike_ftp: 250, swim_css_100m: "1:45"}
        progression: {build_recover: [3, 1], weekly_ramp_pct: 8, taper_weeks: 2}
    """
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
        "progression": dict(PROGRESSION_DEFAULTS),
    }

    if "start_date" in raw:
        start_date = _as_date_string(raw["start_date"], "start_date")
        if start_date >= event_date:
            raise ValueError(
                f"start_date {start_date} must be before event_date {event_date}"
            )
        spec["start_date"] = start_date
    else:
        spec["start_date"] = _default_start_date(event_date, template["weeks"])

    if "days_per_week" in raw:
        # Either a fixed count, or [start, end] to build frequency across the
        # plan — adding a session a few weeks in is how people actually ease
        # into a block, and it is not the same thing as scaling volume.
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
        # Percent of the template's opening week; without it, measured from
        # the user's own recent training (see derive_volume_scale).
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
        # A target for a sport this goal never trains would be silently
        # ignored, which reads as "fit disagreed with me" rather than "that
        # setting does nothing here".
        usable = {
            override_key
            for sport, (_, override_key) in _SPORT_TARGETS.items()
            if sport in template_sports(goal)
        }
        unusable = set(targets) - usable
        if unusable:
            raise ValueError(
                f"targets: {', '.join(sorted(unusable))} — {goal} has no "
                f"{', '.join(sorted(set(_SPORT_TARGETS) - template_sports(goal)))} "
                f"sessions, so only {', '.join(sorted(usable))} apply"
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
    """Monday of the week that is `weeks` weeks back from the event's week."""
    event_monday = compute.week_start(event_date)
    return (event_monday - timedelta(weeks=weeks - 1)).isoformat()


# --- periodisation --------------------------------------------------------


def _plan_weeks(start_date: str, event_date: str) -> tuple[date, int]:
    """(first Monday, number of whole ISO weeks through the event's week)."""
    start_monday = compute.week_start(start_date)
    event_monday = compute.week_start(event_date)
    weeks = ((event_monday - start_monday).days // 7) + 1
    if weeks < MIN_PLAN_WEEKS:
        raise ValueError(
            f"a plan needs at least {MIN_PLAN_WEEKS} weeks between start_date "
            f"{start_date} and event_date {event_date} (got {weeks})"
        )
    return start_monday, weeks


def _assign_phases(weeks: int, phases: list[tuple[str, int]], taper_weeks: int) -> list:
    """One phase name per week. The taper always takes the final taper_weeks
    (leaving at least one week for everything else); the template's remaining
    phases share what's left in proportion to their template lengths, so a
    plan started earlier or later than the template's own length still gets a
    sensible base/build/peak split."""
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
    """'build' | 'recover' | 'taper' for each week — the single source of truth
    for which weeks actually ramp. Shared by the multiplier curve and the ramp
    derivation so the two can never drift apart."""
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
    """The volume multiplier this goal reaches at its own default length with
    the reference ramp — the peak that any length of this plan should arrive
    at. Defined from the template rather than declared alongside it, so a
    plan run at its default length behaves exactly as before."""
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
    """Percent-per-week ramp that lands on the template's intended peak by the
    last build week, whatever the plan's length. A longer block should ascend
    more gently to the same place, not try to climb higher — compounding a
    fixed 8% over 26 weeks just pins every long session at its clamp and the
    extra weeks buy nothing."""
    builds = _week_roles(phase_by_week, build_recover).count("build")
    if builds <= 1:
        return float(REFERENCE_RAMP_PCT)
    solved = (intended_peak(template) ** (1 / (builds - 1)) - 1) * 100
    # Never steeper than the reference: a plan shorter than the template's own
    # length should arrive at a *lower* peak, which is what a short run-up
    # honestly buys you. Chasing the full peak over four weeks would demand a
    # 70%-a-week ramp, which is not a training plan.
    return min(solved, float(REFERENCE_RAMP_PCT))


def _week_multipliers(phase_by_week: list[str], progression: dict) -> list[float]:
    """One volume multiplier per week. Build weeks ramp by weekly_ramp_pct;
    every recovery week in the build/recover cycle dips to RECOVERY_FACTOR of
    the level reached so far without advancing it; taper weeks step down from
    TAPER_START to TAPER_END of the peak."""
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
    """Trim the template's week down to days_per_week training days, dropping
    the lowest-priority sessions first. A session whose day is already taken
    costs no extra day, so doubles survive as long as their day does."""
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


# Which target each sport needs, so a single-sport goal never derives (or
# reports) an intensity nothing in the plan uses.
_SPORT_TARGETS = {
    "run": ("run_5k_seconds", "run_5k"),
    "swim": ("swim_css_100m", "swim_css_100m"),
    "cycle": ("bike_ftp", "bike_ftp"),
}


def template_sports(goal: str) -> set:
    """The sports a goal's weekly session mix actually uses."""
    return {session["sport"] for session in GOAL_TEMPLATES[goal]["weekly_sessions"]}


def derive_targets(spec: dict, activities: list[dict], reference: date) -> dict:
    """{"run_5k_seconds", "bike_ftp", "swim_css_100m", "why": {...}} — the
    intensities every session in the plan is built against, limited to the
    sports the goal actually trains. Description targets win; otherwise the
    same history derivation `fit plan` uses, over the same recent window;
    otherwise a documented fallback."""
    recent = planner.recent_activities(activities, reference)
    overrides = spec.get("targets", {})
    targets: dict = {"why": {}}

    for sport in sorted(template_sports(spec["goal"])):
        key, override_key = _SPORT_TARGETS[sport]
        if override_key in overrides:
            targets[key] = overrides[override_key]
            targets["why"][key] = "set in the plan description"
            continue
        # planner.derive_target guards the measurement; a rejected one comes
        # back as None with a why saying what was thrown away and why, so the
        # fallback never looks like an absence of data when it was a bad
        # reading.
        derived = planner.derive_target(sport, recent)
        if derived["value"] is not None:
            targets[key] = derived["value"]
            targets["why"][key] = derived["why"]
        else:
            targets[key] = FALLBACK_TARGETS[key]
            targets["why"][key] = f"default — {derived['why']}"
    return targets


def derive_volume_scale(
    spec: dict,
    activities: list[dict],
    reference: date,
    template_week_seconds: int,
) -> tuple[float, str]:
    """(scale, why) for the plan's opening week. The description's `volume:`
    wins; otherwise the user's *average* weekly training in this goal's sports
    over the last RECENT_VOLUME_WEEKS is measured against the template's own
    opening week. Clamped to [VOLUME_SCALE_MIN, VOLUME_SCALE_MAX] so one
    freakish week can't rewrite the whole plan.

    The mean, not the median: with training this sparse the median collapses to
    zero the moment more than half the weeks are empty, which would hand
    somebody barely riding the full template volume — exactly backwards. Weeks
    with nothing logged are real information here and weigh in as zeroes, which
    is why weekly_volumes' zero-filling matters."""
    if "volume" in spec:
        return spec["volume"] / 100, "set in the plan description"
    if not template_week_seconds:
        return 1.0, "template default"

    relevant = compute.filter_by_types(
        planner.recent_activities(activities, reference),
        sorted(template_sports(spec["goal"])),
    )
    # Drop the current week before measuring: it is still filling up, and
    # counting a week that is one day old as a near-zero would systematically
    # understate current form (the same reason the dashboard dims that bar).
    complete_weeks = [
        week
        for week in compute.weekly_volumes(relevant, through=reference)
        if not compute.is_current_week(week["week"], reference)
    ]
    recent_weeks = complete_weeks[-RECENT_VOLUME_WEEKS:]
    # Nothing at all in these sports means "unknown", not "untrained" — the
    # user may simply not have imported any yet, so leave the template alone.
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
    """Converge from start_scale in week 1 to the template's own level by the
    last build week, and stay there. Scaling the whole plan uniformly would
    start the user in the right place but leave them under-prepared for a
    fixed-distance event; converging keeps the peak the goal actually needs."""
    last_build = max(weeks - max(taper_weeks, 0) - 1, 1)
    if index >= last_build:
        return 1.0
    return start_scale + (1.0 - start_scale) * (index / last_build)


def days_per_week_range(spec: dict) -> tuple[int, int]:
    """(first week, final week) training days. A plain number means both."""
    value = spec["days_per_week"]
    if isinstance(value, list):
        return value[0], value[1]
    return value, value


def _days_for_week(
    low: int, high: int, index: int, weeks: int, taper_weeks: int
) -> int:
    """Training days in week `index` (0-based): builds from `low` to `high` by
    the last build week, then holds through the taper. Frequency is kept in a
    taper — it is volume that comes down, and dropping a session in race week
    would lose the sharpening the taper exists for. Mirrors
    _volume_scale_for_week, which converges on the same schedule."""
    if low == high:
        return low
    last_build = max(weeks - max(taper_weeks, 0) - 1, 1)
    if index >= last_build:
        return high
    return round(low + (high - low) * index / last_build)


def _apply_target(sport: str, session_type: str, params: dict, targets: dict) -> None:
    """Fill in the one intensity param each session type needs, in place."""
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
        elif session_type in ("endurance", "long"):
            params["target_watts"] = planner.endurance_watts_from_ftp(
                targets["bike_ftp"]
            )
    elif sport == "swim" and session_type in ("intervals", "continuous"):
        params["target_pace_100m"] = targets["swim_css_100m"]


# --- expansion ------------------------------------------------------------


def _week_seconds(laid_out: list[dict], targets: dict, multiplier: float = 1.0) -> int:
    """Estimated training seconds in one templated week, used to size the
    plan's opening week against the user's actual recent volume."""
    total = 0
    for template_session in laid_out:
        params = dict(template_session["params"])
        scale = template_session["scale"]
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
    params = dict(template_session["params"])
    scale = template_session["scale"]
    params[scale["param"]] = _scaled(scale, multiplier)
    sport, session_type = template_session["sport"], template_session["session_type"]
    _apply_target(sport, session_type, params, targets)

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
        "garmin_workout_id": None,
        "scheduled_workout_id": None,
        "scheduled_date": None,
        "status": "planned",
    }


def benchmark_sports(goal: str) -> list[str]:
    """Sports in this goal that have a benchmark workout. BENCHMARK_SESSIONS'
    own order is the rotation order."""
    trained = template_sports(goal)
    return [sport for sport in BENCHMARK_SESSIONS if sport in trained]


def _build_benchmark(sport: str, session_date: date, week: int, phase: str) -> dict:
    """A re-test session. Deliberately unscaled and untargeted: an open
    best-effort over a fixed distance or time, so this week's result can be
    compared with the last one."""
    spec = BENCHMARK_SESSIONS[sport]
    params = dict(spec["params"])
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
        "garmin_workout_id": None,
        "scheduled_workout_id": None,
        "scheduled_date": None,
        "status": "planned",
    }


def _extra_days(week_sessions: list[dict], occupied: dict[int, int]) -> list[int]:
    """Days an extra may be placed on, least-loaded first — so rest days fill
    before easy days, and a key-session day is never used at all."""
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
                    "status": "planned",
                }
            )
    return built


def expand_plan(spec: dict, activities: list[dict], reference: date) -> dict:
    """The engine: a normalised spec (parse_plan_spec's output) plus the user's
    history becomes the full training plan dict — metadata, the intensity
    targets everything was built against, and a flat list of dated sessions,
    oldest first. reference is the date history is derived as of (today).

    Sessions falling before start_date or on/after the event itself are
    dropped: race day is not a training day."""
    template = GOAL_TEMPLATES[spec["goal"]]
    start_monday, weeks = _plan_weeks(spec["start_date"], spec["event_date"])
    progression = dict(spec["progression"])
    phase_by_week = _assign_phases(
        weeks, template["phases"], progression["taper_weeks"]
    )
    # Solve the ramp from this plan's actual length unless the description
    # pinned one, so any length arrives at the template's intended peak.
    ramp_derived = "weekly_ramp_pct" not in progression
    if ramp_derived:
        progression["weekly_ramp_pct"] = derive_weekly_ramp(
            template, phase_by_week, progression["build_recover"]
        )
    multipliers = _week_multipliers(phase_by_week, progression)
    targets = derive_targets(spec, activities, reference)

    # Rotate the whole template week first, then trim it per week: selection
    # only counts distinct days, which rotation preserves.
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

    # Size the opening week against what the user is actually training now,
    # then converge back to the template's level by the last build week. Measured
    # against week 1's own session list, which is smaller when frequency builds.
    start_scale, volume_why = derive_volume_scale(
        spec, activities, reference, _week_seconds(week_sessions(0), targets)
    )
    warnings = []
    growth = (max(multipliers) if multipliers else 1.0) / max(start_scale, 0.01)
    if growth > VOLUME_RAMP_WARN:
        warnings.append(
            f"this plan grows {growth:.1f}x from week 1 to its peak — your recent "
            "training is well below where the goal needs to start. An earlier "
            "start_date, or a shorter goal, would be a gentler way in."
        )

    # Benchmarks land on recovery weeks — rested, so one test is comparable with
    # the next — taking turns between the sports the goal trains, and replacing
    # that sport's quality session for the week rather than adding to it.
    roles = _week_roles(phase_by_week, progression["build_recover"])
    testable = benchmark_sports(spec["goal"]) if spec["benchmarks"] else []
    bench_by_week: dict[int, tuple[str, dict]] = {}
    tested: dict[str, int] = {sport: 0 for sport in testable}
    for index, role in enumerate(roles):
        if not testable or role != "recover" or phase_by_week[index] == "taper":
            continue
        available = week_sessions(index)
        # Which session the test stands in for, most preferred first: a quality
        # session if the week has one, otherwise the long session — in a
        # recovery week a short best-effort in place of the long one is a
        # perfectly good session, and at low frequencies it is the only slot a
        # sport has.
        options = {}
        for sport in testable:
            slot = next(
                (
                    s
                    for wanted in ("intervals", "tempo", "long")
                    for s in available
                    if s["sport"] == sport and s["session_type"] == wanted
                ),
                None,
            )
            if slot is not None:
                options[sport] = slot
        if not options:
            continue  # nothing to stand in for; do not consume a turn either
        # Whichever testable sport has gone longest without a test.
        sport = min(options, key=lambda s: (tested[s], testable.index(s)))
        bench_by_week[index] = (sport, options[sport])
        tested[sport] += 1

    sessions = []
    benchmark_weeks = []
    for index in range(weeks):
        week, phase = index + 1, phase_by_week[index]
        monday = start_monday + timedelta(weeks=index)
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
    """(sport, workout_type, params) for planner.build_plan, or None for an
    extra — extras are local to fit and never become Garmin workouts."""
    if session.get("is_extra"):
        return None
    return session["sport"], session["session_type"], session["params"]


# --- completion and views -------------------------------------------------


def match_completion(sessions: list[dict], activities: list[dict]) -> list[dict]:
    """Copies of `sessions` with "completed" set: a non-extra session counts as
    done when an activity of the same sport falls within a day of it. Each
    activity matches at most one session, nearest date first, so a single ride
    can't tick off a whole week. Extras are never matched — fit has no
    strength/yoga activity type to match against."""
    candidates = [a for a in activities if a.get("type") and a.get("date")]
    claimed: set[int] = set()
    matched = []
    for session in sessions:
        completed = False
        if not session.get("is_extra"):
            session_date = date.fromisoformat(session["date"])
            nearby = sorted(
                (abs((date.fromisoformat(a["date"]) - session_date).days), i)
                for i, a in enumerate(candidates)
                if i not in claimed and a.get("type") == session.get("sport")
            )
            if nearby and nearby[0][0] <= 1:
                claimed.add(nearby[0][1])
                completed = True
        matched.append({**session, "completed": completed})
    return matched


def group_by_week(sessions: list[dict]) -> list[dict]:
    """[{"week", "phase", "start", "sessions": [...]}, ...], oldest first — the
    row structure display.render_training_plan renders. Each session gains a
    "description" line here so display.py prints text rather than composing it
    (the same split as planner.describe_plan -> render_plan_saved)."""
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
    """Header figures for `fit train show`: what the plan is, how far off the
    event is, and how much of it is done and scheduled."""
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
        "days_to_go": (event - today).days,
        "sessions": len(sessions),
        "extras": len(sessions) - len(real),
        "completed": sum(1 for s in sessions if s.get("completed")),
        "scheduled": sum(1 for s in real if s.get("status") == "scheduled"),
        "targets": plan.get("targets", {}),
        "volume": plan.get("volume", {}),
        "benchmark_weeks": plan.get("benchmark_weeks", []),
        "warnings": plan.get("warnings", []),
    }


def describe_session(session: dict) -> str:
    """One human line for a session, e.g. 'Cycle long 45km @ 165W (brick)'.
    The formatting split mirrors planner.describe_plan -> render_plan_saved:
    the text is composed here, display.py only prints it."""
    name = session.get("workout_name", session.get("session_type", ""))
    if session.get("is_brick"):
        return f"{name} (brick)"
    if session.get("is_benchmark"):
        return f"{name} (re-test)"
    return name


def sync_window(sessions: list[dict], today: date, window_days: int) -> list[dict]:
    """The still-unscheduled, non-extra sessions inside the rolling sync
    horizon [today, today + window_days]. Re-running `fit train sync` simply
    finds fewer of them, which is what makes it idempotent."""
    end = (today + timedelta(days=window_days)).isoformat()
    return [
        s
        for s in sessions
        if not s.get("is_extra")
        and s.get("status") == "planned"
        and today.isoformat() <= s["date"] <= end
    ]


# The intensity params _apply_target writes — exactly one per session, which is
# what makes an intensity-only rewrite possible at all. Volume is not
# recoverable from a stored session: the template's `scale` dict is never
# persisted, so a session knows its own size but not the rule that produced it.
_INTENSITY_PARAMS = ("target_pace", "target_watts", "target_pace_100m")


def retargetable(sessions: list[dict], today: date) -> list[dict]:
    """Sessions a retarget may rewrite. Skipped, in order: extras (no "params"
    key at all — touching one is a KeyError), benchmarks (deliberately
    untargeted; a test at a prescribed pace is not a test), anything already
    scheduled on Garmin (a pushed workout is a frozen copy on the account and
    garmin.py has no endpoint to update or delete it), and anything dated
    before today. `>= today` matches sync_window's horizon: a session dated
    today that is still "planned" has not been pushed."""
    return [
        s
        for s in sessions
        if not s.get("is_extra")
        and not s.get("is_benchmark")
        and s.get("status") == "planned"
        and s["date"] >= today.isoformat()
    ]


def retarget_sessions(plan: dict, targets: dict, today: date) -> dict:
    """Re-derive the intensity of every retargetable session against `targets`,
    in place, and report what moved:

        {"old_targets", "new_targets", "retargeted", "unchanged",
         "frozen", "past", "changed"}

    Mutates plan["sessions"] and plan["targets"] — the same in-place convention
    `fit train sync` uses (and unlike match_completion, which returns copies);
    cli.py writes the plan afterwards. "Pure" in this codebase means no I/O,
    not no mutation.

    Intensity only, never volume — see _INTENSITY_PARAMS."""
    old_targets = dict(plan.get("targets", {}))
    eligible = retargetable(plan["sessions"], today)
    eligible_ids = {id(s) for s in eligible}

    real = [s for s in plan["sessions"] if not s.get("is_extra")]
    frozen = sum(
        1
        for s in real
        if s.get("status") == "scheduled" and s["date"] >= today.isoformat()
    )
    past = sum(1 for s in real if s["date"] < today.isoformat())

    changed = []
    for session in eligible:
        sport, session_type = session["sport"], session["session_type"]
        # A hand-edited plan file could name a sport this goal never trained.
        if sport not in _SPORT_TARGETS or _SPORT_TARGETS[sport][0] not in targets:
            continue
        params = session["params"]
        before = {key: params.get(key) for key in _INTENSITY_PARAMS}
        _apply_target(sport, session_type, params, targets)
        if {key: params.get(key) for key in _INTENSITY_PARAMS} == before:
            continue
        # The stored name is derived from params, so it has to be rebuilt or it
        # will disagree with the payload that eventually gets pushed.
        session["workout_name"] = planner.workout_name(sport, session_type, params)
        changed.append(session)

    plan["targets"] = targets
    return {
        "old_targets": old_targets,
        "new_targets": targets,
        "retargeted": len(changed),
        "unchanged": len(eligible_ids) - len(changed),
        "frozen": frozen,
        "past": past,
        "changed": changed,
    }


def future_scheduled(sessions: list[dict], today: date) -> list[dict]:
    """Scheduled sessions still ahead of today — what `fit train clear`
    unschedules, and what `fit train import` warns about replacing."""
    return [
        s
        for s in sessions
        if s.get("status") == "scheduled" and s["date"] >= today.isoformat()
    ]
