"""Tests for storage.py's config text format and the data-dir plumbing."""

from fit import compute, storage


def test_parse_config_types_and_tolerance():
    text = (
        "# a comment\n"
        "sports = run, cycle  # inline comment\n"
        "pbs_window_months = 3\n"
        "show_pbs = false\n"
        "show_calendar = ture\n"
        "max_heart_rate = 18O\n"
        "unknown_key = whatever\n"
        "malformed line with no equals\n"
    )
    config, warnings = compute.parse_config(text, storage.DEFAULTS)
    assert config["sports"] == ["run", "cycle"]
    assert config["pbs_window_months"] == 3
    assert config["show_pbs"] is False
    # A bad value keeps its default and says so, rather than vanishing.
    assert config["show_calendar"] is True
    assert config["max_heart_rate"] == 0
    assert len(warnings) == 2
    assert "show_calendar" in warnings[0] and "max_heart_rate" in warnings[1]
    assert "unknown_key" not in config


def test_config_serialize_parse_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FIT_DATA_DIR", str(tmp_path))
    storage.ensure_data_dir()  # writes the default file
    assert compute.parse_config(storage.read_config_text(), storage.DEFAULTS) == (
        storage.DEFAULTS,
        [],
    )

    config = {
        **storage.DEFAULTS,
        "sports": ["run"],
        "show_sparkline": False,
        "max_heart_rate": 190,
    }
    text = storage._serialize_config_text(config)
    assert compute.parse_config(text, storage.DEFAULTS) == (config, [])


def test_activity_and_pbs_write_read_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("FIT_DATA_DIR", str(tmp_path))
    storage.ensure_data_dir()

    activity = {
        "id": "2024-01-15T08:30:00",
        "type": "run",
        "date": "2024-01-15",
        "distance_km": 10.2,
        "duration_seconds": 3120,
        "source": "garmin",
    }
    storage.write_activity(activity)
    assert storage.activity_exists("2024-01-15T08:30:00")
    activities, warnings = storage.read_activities_with_warnings()
    assert activities == [activity]
    assert warnings == []

    storage.write_pbs({"computed_from": 1, "run": {}})
    assert storage.read_pbs()["computed_from"] == 1


def test_corrupt_files_warn_for_activities_and_drop_silently_for_plans(
    tmp_path, monkeypatch
):
    """The asymmetry is deliberate: a lost activity is history the user should
    hear about, a lost plan file just drops out of the rep-progression
    defaults. (read_training_plan is the third case and raises — it is the
    whole feature's state; see storage.read_training_plan.)"""
    monkeypatch.setenv("FIT_DATA_DIR", str(tmp_path))
    storage.ensure_data_dir()
    (storage.activities_dir() / "bad.json").write_text("{not json")

    activities, warnings = storage.read_activities_with_warnings()
    assert activities == []
    assert len(warnings) == 1 and "bad.json" in warnings[0]

    # Two plans, so a filename that stopped keying on the id would show up as
    # one clobbering the other rather than both surviving.
    first = {"id": "2026-07-03T09:00:00", "sport": "run", "params": {"reps": 6}}
    second = {"id": "2026-07-10T09:00:00", "sport": "cycle", "params": {"reps": 4}}
    storage.write_plan(first)
    storage.write_plan(second)
    (storage.plans_dir() / "bad.json").write_text("{not json")
    assert storage.read_plans() == [first, second]

    # And the third case: no training plan yet reads as None, not an
    # exception — cli._stored_plan prints its own message on that.
    assert storage.read_training_plan() is None
    training_plan = {"spec": {"start_date": "2026-11-04", "end_date": "2027-01-31"}}
    storage.write_training_plan(training_plan)
    assert storage.read_training_plan() == training_plan
