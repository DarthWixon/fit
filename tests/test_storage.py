"""Tests for storage.py's config text format and the data-dir plumbing."""

from fit import storage


def test_parse_config_text_types_and_tolerance():
    text = (
        "# a comment\n"
        "sports = run, cycle  # inline comment\n"
        "pbs_window_months = 3\n"
        "show_pbs = false\n"
        "history_count = notanumber\n"
        "unknown_key = whatever\n"
        "malformed line with no equals\n"
    )
    parsed = storage._parse_config_text(text)
    assert parsed["sports"] == ["run", "cycle"]
    assert parsed["pbs_window_months"] == 3
    assert parsed["show_pbs"] is False
    assert "history_count" not in parsed      # bad int silently skipped
    assert "unknown_key" not in parsed


def test_config_serialize_parse_round_trip():
    config = {**storage.DEFAULTS, "sports": ["run"], "show_sparkline": False}
    assert storage._parse_config_text(storage._serialize_config_text(config)) == config


def test_read_config_merges_over_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("FIT_DATA_DIR", str(tmp_path))
    storage.ensure_data_dir()
    assert storage.read_config() == storage.DEFAULTS  # default file round-trips

    storage.config_path().write_text("pbs_window_months = 6\n")
    config = storage.read_config()
    assert config["pbs_window_months"] == 6
    assert config["show_pbs"] is True                 # untouched keys fall back


def test_activity_and_pbs_write_read_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FIT_DATA_DIR", str(tmp_path))
    storage.ensure_data_dir()

    activity = {"id": "2024-01-15T08:30:00", "type": "run", "date": "2024-01-15",
                "distance_km": 10.2, "duration_seconds": 3120, "source": "gpx"}
    storage.write_activity(activity)
    assert storage.activity_exists("2024-01-15T08:30:00")
    activities, warnings = storage.read_activities_with_warnings()
    assert activities == [activity]
    assert warnings == []

    storage.write_pbs({"computed_from": 1, "run": {}})
    assert storage.read_pbs()["computed_from"] == 1


def test_read_activities_warns_on_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FIT_DATA_DIR", str(tmp_path))
    storage.ensure_data_dir()
    (storage.activities_dir() / "bad.json").write_text("{not json")

    activities, warnings = storage.read_activities_with_warnings()
    assert activities == []
    assert len(warnings) == 1 and "bad.json" in warnings[0]


def test_plan_write_read_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FIT_DATA_DIR", str(tmp_path))
    storage.ensure_data_dir()
    assert storage.plans_dir().is_dir()

    plan = {"id": "2026-07-03T09:00:00", "sport": "run", "workout_type": "intervals",
            "params": {"reps": 6}, "workout_name": "Run intervals", "payload": {}}
    storage.write_plan(plan)
    (storage.plans_dir() / "bad.json").write_text("{not json")

    assert storage.read_plans() == [plan]
