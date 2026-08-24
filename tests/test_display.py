"""The only display test, and deliberately so.

display.py is render-only and its wording changes constantly, so asserting on
what it says is pure maintenance cost — the rest of the suite covers the pure
functions that feed it. render_training_plan is the exception worth guarding:
it is the one renderer with a non-trivial two-dict contract (plan_summary plus
group_by_week), and it shipped raising TypeError on every plan that schedules a
re-test, because nothing in the suite ever called it.

So this asserts only that both branches render — never what they say.
"""

from datetime import date

import pytest

from fit import display, training

pytest.importorskip("yaml", reason="parse_plan_spec needs the optional [train] extra")

REFERENCE = date(2026, 8, 24)
PLAN = "goal: sprint_triathlon\nevent_date: 2026-11-15\nextras: {strength: 1}\n"


def _render(text, capsys):
    plan = training.expand_plan(training.parse_plan_spec(text), [], REFERENCE)
    sessions = training.match_completion(plan["sessions"], [])
    display.render_training_plan(
        training.plan_summary({**plan, "sessions": sessions}, REFERENCE),
        training.group_by_week(sessions),
    )
    return capsys.readouterr().out, plan


@pytest.mark.parametrize("benchmarks", [True, False])
def test_the_training_plan_view_renders_either_way(benchmarks, capsys):
    """With re-tests the header prints a benchmark-week list, without them it
    does not — and the session table has to survive both."""
    out, plan = _render(PLAN + f"benchmarks: {str(benchmarks).lower()}\n", capsys)
    assert out.strip()
    # Assert on a workout name, which appears only in the table — a date would
    # also match the "12 weeks from ..." header and pass on an empty table.
    named = next(s for s in plan["sessions"] if not s["is_extra"])
    assert named["workout_name"][:18] in out, "no session rows in the rendered table"
