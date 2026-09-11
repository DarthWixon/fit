"""Tests for cli.py. Only what the engine tests cannot see: how commands
compose, where getting it wrong writes to a live Garmin account."""

import sys
from datetime import date

import pytest
from typer.testing import CliRunner

from fit import cli, garmin, storage, training

pytest.importorskip("yaml", reason="parse_plan_spec needs the optional [train] extra")

TODAY = date(2026, 8, 24)
SPEC = "goal: run_10k\nevent_date: 2027-02-07\nstart_date: 2026-08-24\n"


class _FixedDate(date):
    @classmethod
    def today(cls):
        return TODAY


def test_refresh_re_pushes_what_it_unschedules(tmp_path, monkeypatch):
    """`refresh` runs `clear` then `sync` as plain function calls, where an
    omitted option is typer's OptionInfo default — truthy. Leaving out
    dry_run made it unschedule every future session, push none back and exit
    0: the emptied calendar `refresh` exists to avoid."""
    monkeypatch.setenv("FIT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "date_cls", _FixedDate)
    # Every Garmin call is stubbed; this blocks a real one if a stub is missed.
    monkeypatch.setitem(sys.modules, "garminconnect", None)

    spec = training.parse_plan_spec(SPEC)
    plan = training.expand_plan(spec, [], TODAY)
    due = [s for s in plan["sessions"] if not s["is_extra"]][:2]
    ledger = [training.ledger_entry(s, 100 + i, 200 + i) for i, s in enumerate(due)]
    storage.ensure_data_dir()
    storage.write_training_plan(
        {
            "spec": spec,
            "created": TODAY.isoformat(),
            "volume": plan["volume"],
            "pushed": ledger,
        }
    )

    unscheduled, pushed = [], []
    monkeypatch.setattr(garmin, "login", lambda: object())
    monkeypatch.setattr(
        garmin, "unschedule_workout", lambda client, sid: unscheduled.append(sid)
    )

    def push(client, payload):
        pushed.append(payload)
        return {"workoutId": 1000 + len(pushed)}

    monkeypatch.setattr(garmin, "push_workout", push)
    monkeypatch.setattr(
        garmin,
        "schedule_workout",
        lambda client, workout_id, day: {"workoutScheduleId": workout_id + 1},
    )

    result = CliRunner().invoke(cli.app, ["train", "refresh", "--yes"])
    assert result.exit_code == 0, result.output

    assert unscheduled == [200, 201]
    after = {training.ledger_key(e): e for e in storage.read_training_plan()["pushed"]}
    for entry in ledger:
        back = after.get((entry["date"], entry["sport"]))
        assert back is not None and back["workout_id"] > 1000
