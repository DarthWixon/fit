"""Filesystem boundary for fit. The only module that touches disk.

Activity dicts have the shape:
    {
        "id": "2024-01-15T08:30:00",      # ISO 8601, used as filename key
        "type": "run",                     # "run" | "cycle" | "walk" | "hike" | "swim" | "squash" | "canoe"
        "date": "2024-01-15",
        "distance_km": 10.2,
        "duration_seconds": 3120,
        "elevation_gain_m": 45,            # optional
        "avg_heart_rate": 152,             # optional
        "max_heart_rate": 171,             # optional
        "avg_power": 187,                  # optional, watts; TCX/FIT only
        "best_power": {"20min": 241, ...}, # optional, watts; FIT only, see "Power windows"
        "hr_zones": {"zone1_seconds": 120.0, ...},  # optional, see "HR zones"
        "splits": {"5k_seconds": 1423, ...},  # optional, see "Split PBs"
        "source": "garmin",                # "garmin" | "strava"
    }

pbs.json has the shape:
    {
        "computed_from": 47,
        "run": {"fastest_5k_seconds": 1423, "fastest_5k_date": "2024-03-12", ...},
        "cycle": {...},
    }

fitness.json has the shape:
    {
        "baseline_date": "2026-07-01",
        "baseline_value": 11.68   # raw EWMA units (MET-hours), unrescaled
    }
Unlike pbs.json, this has no "computed_from"/staleness field — it is never
auto-invalidated by new activities, only replaced by an explicit reset
(see cli.fitness_reset). See "Fitness index" in CLAUDE.md.
"""

import json
import os
import tempfile
from pathlib import Path

DEFAULTS = {
    "sports": [],  # empty = all types shown
    "pbs_window_months": 0,  # 0 = all-time PBs
    "history_count": 5,  # rows in the dashboard's embedded history table
    "dashboard_weeks": 12,  # weeks shown in dashboard volume/fitness sparklines (0 = all)
    "max_heart_rate": 0,  # bpm, 0 = unset (HR zone breakdown column shows "—" until set)
    "train_sync_window_days": 14,  # how far ahead `fit train sync` schedules sessions
    "show_sparkline": True,  # weekly volume (hours) sparkline block
    "show_pbs": True,  # personal bests block
    "show_sports_summary": True,  # sports summary block (all types, count + time + distance)
    "show_fitness_index": True,  # fitness index (EWMA training load rescaled to a baseline of 100) block
    "show_calendar": True,  # calendar block (active days over the last 2 months)
}

_CONFIG_COMMENTS = {
    "sports": "comma-separated types to show, e.g. run, cycle (blank = all)",
    "pbs_window_months": "how far back to look for PBs shown on dashboard/pbs (0 = all-time)",
    "history_count": "rows in the dashboard's recent-activity table",
    "dashboard_weeks": "weeks shown in dashboard volume/fitness sparklines (0 = all)",
    "max_heart_rate": "your max heart rate in bpm, used to compute HR zone % breakdowns (0 = unset)",
    "train_sync_window_days": "how many days ahead `fit train sync` pushes training-plan sessions",
    "show_sparkline": "weekly volume (hours) sparkline block",
    "show_pbs": "personal bests block",
    "show_sports_summary": "sports summary block (all types, count + time + distance)",
    "show_fitness_index": "fitness index (EWMA training load rescaled to a baseline of 100) block",
    "show_calendar": "calendar block (active days over the last 2 months)",
}


def resolve_data_dir() -> Path:
    override = os.environ.get("FIT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".fit"


def activities_dir() -> Path:
    return resolve_data_dir() / "activities"


def config_path() -> Path:
    return resolve_data_dir() / "config"


def pbs_path() -> Path:
    return resolve_data_dir() / "pbs.json"


def fitness_path() -> Path:
    return resolve_data_dir() / "fitness.json"


def plans_dir() -> Path:
    return resolve_data_dir() / "plans"


def train_dir() -> Path:
    return resolve_data_dir() / "train"


def ensure_data_dir() -> None:
    for path in (activities_dir(), plans_dir(), train_dir()):
        path.mkdir(parents=True, exist_ok=True)
    if not config_path().exists():
        _write_atomic(config_path(), _serialize_config_text(DEFAULTS))


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


def _write_json_atomic(path: Path, data: dict) -> None:
    _write_atomic(path, json.dumps(data, indent=2))


def _read_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def read_activities_with_warnings() -> tuple[list[dict], list[str]]:
    """All activity dicts, plus one warning string per corrupt file skipped —
    returned rather than printed, since printing is a display.py concern,
    not storage.py's."""
    activities = []
    warnings = []
    for file_path in sorted(activities_dir().glob("*.json")):
        try:
            with open(file_path) as f:
                activities.append(json.load(f))
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"skipping corrupt activity file {file_path}: {exc}")
    return activities, warnings


def write_activity(activity: dict) -> None:
    _write_json_atomic(activities_dir() / f"{activity['id']}.json", activity)


def activity_exists(activity_id: str) -> bool:
    return (activities_dir() / f"{activity_id}.json").exists()


def write_plan(plan: dict) -> None:
    _write_json_atomic(plans_dir() / f"{plan['id']}.json", plan)


def read_plans() -> list[dict]:
    """All saved plan dicts, silently skipping unparseable files — a corrupt
    plan just drops out of the rep-progression defaults, which is the only
    reason plans are read back."""
    plans = []
    for file_path in sorted(plans_dir().glob("*.json")):
        try:
            with open(file_path) as f:
                plans.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return plans


def training_plan_path() -> Path:
    return train_dir() / "plan.json"


def write_training_plan(plan: dict) -> None:
    """The single active training plan (see training.py). One plan at a time,
    so unlike write_plan there is no per-id filename."""
    _write_json_atomic(training_plan_path(), plan)


def read_training_plan() -> dict | None:
    """The active training plan, or None if there isn't one. A corrupt file
    raises rather than being skipped: unlike a single dropped plan file in
    read_plans, this is the whole feature's state and silently losing it would
    quietly unschedule nothing while claiming success."""
    return _read_json(training_plan_path(), default=None)


def _parse_config_text(text: str) -> dict:
    parsed = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key, raw_value = key.strip(), raw_value.strip()
        if key not in DEFAULTS:
            continue

        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                parsed[key] = raw_value.lower() in ("true", "1", "yes")
            elif isinstance(default, list):
                parsed[key] = [v.strip() for v in raw_value.split(",") if v.strip()]
            elif isinstance(default, int):
                parsed[key] = int(raw_value) if raw_value else default
            else:
                parsed[key] = raw_value
        except ValueError:
            continue
    return parsed


def _serialize_config_text(config: dict) -> str:
    lines = ["# ~/.fit/config — edit and save; changes apply next run.", ""]
    for key, default in DEFAULTS.items():
        value = config.get(key, default)
        if isinstance(value, bool):
            text_value = "true" if value else "false"
        elif isinstance(value, list):
            text_value = ", ".join(str(v) for v in value)
        else:
            text_value = str(value)
        lines.append(f"{key} = {text_value}  # {_CONFIG_COMMENTS[key]}")
    return "\n".join(lines) + "\n"


def read_config() -> dict:
    stored = (
        _parse_config_text(config_path().read_text()) if config_path().exists() else {}
    )
    return {**DEFAULTS, **stored}


def read_pbs() -> dict:
    return _read_json(pbs_path(), default={})


def write_pbs(pbs: dict) -> None:
    _write_json_atomic(pbs_path(), pbs)


def read_fitness_baseline() -> dict:
    return _read_json(fitness_path(), default={})


def write_fitness_baseline(baseline: dict) -> None:
    _write_json_atomic(fitness_path(), baseline)
