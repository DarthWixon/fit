"""The only display test, and deliberately so.

display.py is render-only and its wording changes constantly, so asserting on
what it says is pure maintenance cost — the rest of the suite covers the pure
functions that feed it. render_training_plan is the exception worth guarding:
it is the one renderer with a non-trivial two-dict contract (plan_summary plus
group_by_week), and it once shipped raising TypeError on every plan of one
shape, because nothing in the suite ever called it.

So this asserts only that it renders — never what it says.
"""

from datetime import date

import pytest

from fit import display, training

pytest.importorskip("yaml", reason="parse_plan_spec needs the optional [train] extra")

REFERENCE = date(2026, 8, 24)
PLAN = "start_date: 2026-08-24\nend_date: 2026-11-15\n"


def test_the_training_plan_view_renders(capsys):
    plan = training.expand_plan(training.parse_plan_spec(PLAN), [], REFERENCE)
    sessions = training.match_completion(plan["sessions"], [])
    display.render_training_plan(
        training.plan_summary({**plan, "sessions": sessions}, REFERENCE),
        training.group_by_week(sessions),
    )
    out = capsys.readouterr().out
    # Assert on a workout name, which appears only in the table — a date would
    # also match the header and pass on an empty table.
    assert sessions[0]["workout_name"][:18] in out, "no session rows in the table"
