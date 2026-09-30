"""Asking for the constructed schedule instead of a searched one.

`planner="greedy"` builds a schedule and hands it back. What it must never do
is hand back something that is not a schedule, or say anything it has not
earned -- and the three ways it can come back empty are different in kind:

* the shape is one the construction does not handle. That is a fact about the
  **planner**; the solver plans it perfectly well.
* it ran and found nothing. A fact about the **attempt**, and a proof of
  nothing either way. With nothing yet settled, an instance that has no
  schedule is refused before this point and told why (`objects_deadlocked`,
  `final_outputs_crowded`, `stock_cannot_last`); with reported history or a spot
  held since a stated time it may not be, because those checks walk with the
  clock erased and cannot see the order the history fixed.
* it built something that did not survive being read back. A defect, and the
  plan is dropped rather than offered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ofplang.schedule import schedule
from ofplang.schedule.scheduler import greedy
from ofplang.schedule.scheduler.result import Solution

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _codes(report, severity=None):
    return [d.code for d in report.diagnostics if severity is None or d.severity == severity]


def _plan(planner: str, name: str = "reformatter", **kwargs):
    return schedule(
        EXAMPLES / f"{name}.workflow.yaml",
        EXAMPLES / f"{name}.env.yaml",
        planner=planner,
        max_time_seconds=20,
        **kwargs,
    )


def test_the_solver_is_what_a_caller_gets_by_default():
    report = schedule(
        EXAMPLES / "reformatter.workflow.yaml",
        EXAMPLES / "reformatter.env.yaml",
        max_time_seconds=20,
    )
    assert report.outcome == "optimal"
    assert "plan_constructed" not in _codes(report)


def test_the_constructed_planner_returns_a_plan_and_says_it_is_one():
    report = _plan("greedy")
    assert report.plan is not None
    assert report.outcome == "feasible"
    # 🔴 It never claims optimality, and it says out loud that it did not search.
    assert "plan_constructed" in _codes(report)


def test_the_constructed_plan_is_a_plan_in_the_ordinary_shape():
    built = _plan("greedy").plan
    searched = _plan("cpsat").plan
    assert built is not None and searched is not None
    assert set(built) == set(searched), "the same sections"
    # The same work, however each of them ordered it -- the two schedule it
    # differently, which is the whole point of having both.
    assert sorted(a["kind"] for a in built["activities"]) == sorted(
        a["kind"] for a in searched["activities"]
    )


def test_a_shape_the_construction_does_not_handle_says_so_and_nothing_more(monkeypatch):
    # 🔴 A statement about the planner, not about the plan. Reported as a warning
    # for that reason: the instance is fine, and the solver would schedule it.
    monkeypatch.setattr(greedy, "_refuse", lambda *_args: "cancelled")
    report = _plan("greedy")
    assert report.plan is None
    assert report.outcome == "unknown"
    assert "planner_unsupported" in _codes(report)
    assert "infeasible" not in _codes(report)


def test_finding_nothing_is_not_a_proof_of_anything(monkeypatch):
    monkeypatch.setattr(greedy, "construct", lambda *_args, **_kwargs: None)
    report = _plan("greedy")
    assert report.plan is None
    assert report.outcome == "unknown"
    assert "plan_not_constructed" in _codes(report)
    assert "infeasible" not in _codes(report)


def test_a_schedule_that_is_not_one_is_dropped_rather_than_handed_out(monkeypatch):
    """🔴 The gate the whole slice turns on.

    The construction has had three bugs and every one of them was caught by
    reading its answer back against the constraints; the first returned optimal
    values while breaking spot exclusion eighteen times. While this only made
    hints that cost nothing. It makes plans now.
    """
    original = greedy.construct

    def broken(instance, **kwargs):
        built = original(instance, **kwargs)
        if built is None:
            return None
        # Move one activity on top of another: still well-formed, no longer a
        # schedule.
        first, second, *rest = built.processing
        return Solution(
            built.outcome,
            built.makespan,
            (first, type(second)(**{**second.__dict__, "start": first.start}), *rest),
            built.transport,
            built.replenishment,
            objective_kind=built.objective_kind,
            objective_values=built.objective_values,
        )

    monkeypatch.setattr(greedy, "construct", broken)
    report = _plan("greedy")
    assert report.plan is None
    assert "plan_not_constructed" in _codes(report)
    assert "plan_constructed" not in _codes(report)


def _stock_nobody_can_refill():
    """The consumable example with its replenishers taken away: the reader starts
    empty and nothing can top it up."""
    import copy

    import yaml

    env = yaml.safe_load((EXAMPLES / "consumable.env.yaml").read_text(encoding="utf-8"))
    env = copy.deepcopy(env)
    env.pop("replenishers", None)
    env.pop("replenishments", None)
    return env


@pytest.mark.parametrize("planner", ["cpsat", "greedy"])
def test_both_planners_agree_that_an_impossible_plan_is_impossible(planner):
    # 🔴 The pre-solve checks run whichever planner is asked: they are about the
    # instance, not about how it would be scheduled. So the answer to "is this
    # possible" does not depend on who was going to schedule it.
    report = schedule(
        EXAMPLES / "consumable.workflow.yaml",
        _stock_nobody_can_refill(),
        document_path=EXAMPLES / "consumable.document.yaml",
        planner=planner,
        max_time_seconds=20,
    )
    assert report.plan is None
    assert "stock_cannot_last" in _codes(report, severity="error")
