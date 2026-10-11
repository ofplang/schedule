"""The previous plan as a schedule known to exist (design.md D66, stage 0).

A replan is handed the plan it replaces, stamped with what has started. When the
only thing that moved is that some work took longer, the previous plan's order,
shifted later, is still a schedule. These tests pin that claim on every worked
example, and pin the two places it is used: the constructed planner's last resort
on a replan, and the plan a replan gets when the solver comes back empty-handed.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ofplang.schedule import schedule
from ofplang.schedule.scheduler import api, greedy
from ofplang.schedule.scheduler.result import Solution
from ofplang.schedule.validation import errors
from tests.schedutil import plannable_examples

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# How much later than planned the overrunning activity is still running.
_DELAY = 3


def _document(name: str) -> dict | None:
    path = EXAMPLES / f"{name}.document.yaml"
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _first_plan(name: str) -> dict:
    report = schedule(
        EXAMPLES / f"{name}.workflow.yaml",
        EXAMPLES / f"{name}.env.yaml",
        document_path=_document(name),
        max_time_seconds=20,
        random_seed=0,
    )
    assert report.plan is not None, [d.code for d in report.diagnostics]
    return report.plan


def _overrun(plan: dict, delay: int = _DELAY, which: int | None = None) -> dict | None:
    """The plan as a run would hand it back when one activity took `delay` longer.

    The processing activity `which` (in start order; by default the one in the
    middle of the plan) is still running `delay` after its planned end. Everything
    the plan started before that end has started -- completed if it ended by then,
    running otherwise -- and everything else is
    still pending, with the mode, route and times the plan gave it.
    """
    document = copy.deepcopy(plan)
    processing = sorted(
        (a for a in document["activities"] if a["kind"] == "processing"),
        key=lambda a: (a["start"], a["end"]),
    )
    if not processing:
        return None
    late = processing[(len(processing) - 1) // 2 if which is None else which]
    planned_end = late["end"]
    for entry in document["activities"]:
        if entry["kind"] not in ("processing", "transport", "replenishment"):
            continue
        if entry is late:
            entry["status"] = "running"
        elif entry["start"] < planned_end:
            entry["status"] = "completed" if entry["end"] <= planned_end else "running"
    document["now"] = planned_end + delay
    return document


def _replan(name: str, document: dict, **kwargs):
    return schedule(
        EXAMPLES / f"{name}.workflow.yaml",
        EXAMPLES / f"{name}.env.yaml",
        document_path=document,
        random_seed=0,
        **kwargs,
    )


def _codes(report) -> list[str]:
    return [d.code for d in report.diagnostics]


def _skip_if_out_of_scope(report) -> None:
    # A shape the constructed planner does not take at all (a running refill, say)
    # says nothing about the previous order either way.
    if errors.PLANNER_UNSUPPORTED in _codes(report):
        pytest.skip("out of the constructed planner's scope")


@pytest.fixture
def no_passes(monkeypatch):
    """The constructed planner with its forward passes switched off, so whatever it
    returns on a replan is the previous plan laid out again."""
    monkeypatch.setattr(greedy, "_pass", lambda *args, **kwargs: None)


@pytest.mark.parametrize("name", plannable_examples())
def test_an_overrun_leaves_the_previous_order_a_schedule(name, no_passes):
    document = _overrun(_first_plan(name))
    if document is None:
        pytest.skip("no processing activity to overrun")
    report = _replan(name, document, planner="greedy", check_arms=False)
    _skip_if_out_of_scope(report)
    assert report.plan is not None, _codes(report)
    assert errors.PLAN_CONSTRUCTED in _codes(report)
    # Nothing pending starts before `now`, and the overrunning work is still there.
    for entry in report.plan["activities"]:
        if entry["kind"] in ("processing", "transport") and "status" not in entry:
            assert entry["start"] >= document["now"]


@pytest.mark.parametrize("name", plannable_examples())
def test_the_previous_order_is_kept_spot_by_spot(name, no_passes):
    # The replayed plan uses each spot in the order the previous plan did: that is
    # what makes it a schedule, so it is what is checked.
    plan = _first_plan(name)
    document = _overrun(plan)
    if document is None:
        pytest.skip("no processing activity to overrun")
    report = _replan(name, document, planner="greedy", check_arms=False)
    _skip_if_out_of_scope(report)
    assert report.plan is not None, _codes(report)
    assert _spot_orders(report.plan) == _spot_orders(document)


@pytest.mark.parametrize("name", plannable_examples())
def test_every_activity_overrunning_by_any_delay_leaves_a_schedule(name, no_passes):
    # The claim is about any overrun, not one: every processing activity in turn,
    # late by a little and by a lot.
    plan = _first_plan(name)
    count = sum(1 for entry in plan["activities"] if entry["kind"] == "processing")
    for which in range(count):
        for delay in (1, 7, 40):
            document = _overrun(plan, delay, which)
            report = _replan(name, document, planner="greedy", check_arms=False)
            _skip_if_out_of_scope(report)
            assert report.plan is not None, (which, delay, _codes(report))
            assert _spot_orders(report.plan) == _spot_orders(document), (which, delay)


def _spot_orders(plan: dict) -> dict[str, list]:
    """Per spot, the processing that ran on it, in time order."""
    orders: dict[str, list[tuple]] = {}
    for entry in plan["activities"]:
        if entry["kind"] != "processing":
            continue
        identity = (entry.get("job"), tuple(entry["node"]))
        for spot in {**entry.get("input_spots", {}), **entry.get("output_spots", {})}.values():
            orders.setdefault(spot, []).append((entry["start"], identity))
    return {spot: [who for _, who in sorted(uses)] for spot, uses in orders.items()}


def test_a_pending_activity_the_previous_plan_lacks_withholds_the_order(no_passes):
    # All or nothing: drop one pending entry, and the previous plan no longer covers
    # the instance, so nothing is laid out and the passes' empty answer stands.
    name = "simple"
    document = _overrun(_first_plan(name))
    pending = [
        entry
        for entry in document["activities"]
        if entry["kind"] == "processing" and "status" not in entry
    ]
    assert pending
    document["activities"].remove(pending[-1])
    report = _replan(name, document, planner="greedy", check_arms=False)
    assert report.plan is None
    assert errors.PLAN_NOT_CONSTRUCTED in _codes(report)


def test_an_ordinary_replan_is_untouched_by_the_previous_order():
    # The order is a last resort: with the passes working, the constructed plan is
    # the one they build, whether or not a previous plan is there to fall back on.
    name = "simple"
    document = _overrun(_first_plan(name))
    with_previous = _replan(name, document, planner="greedy", check_arms=False)
    without = copy.deepcopy(document)
    without["activities"] = [entry for entry in without["activities"] if "status" in entry]
    without_previous = _replan(name, without, planner="greedy")
    assert with_previous.plan["activities"] == without_previous.plan["activities"]


def _unsolved(*args, **kwargs) -> Solution:
    return Solution("unknown", None, (), ())


def test_a_replan_the_solver_comes_back_empty_from_is_constructed(monkeypatch):
    name = "simple"
    document = _overrun(_first_plan(name))
    monkeypatch.setattr(api, "_solver", lambda: _unsolved)
    report = _replan(name, document)
    assert report.plan is not None
    assert errors.PLAN_CONSTRUCTED_AFTER_SOLVER in _codes(report)


def test_a_first_plan_the_solver_comes_back_empty_from_stays_empty(monkeypatch):
    # Only a replan falls back: a first plan that times out says so, as before.
    monkeypatch.setattr(api, "_solver", lambda: _unsolved)
    report = schedule(EXAMPLES / "simple.workflow.yaml", EXAMPLES / "simple.env.yaml")
    assert report.plan is None
    assert report.outcome == "unknown"
    assert errors.PLAN_CONSTRUCTED_AFTER_SOLVER not in _codes(report)


def _load(name: str):
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


def test_a_joint_plan_overrunning_anywhere_leaves_a_schedule(no_passes):
    # Two jobs of one workflow sharing one loading bay: the order that matters
    # crosses jobs, and every overrun still leaves it a schedule.
    from ofplang.schedule import JobInput, schedule_jobs

    workflow = _load("interface_load.workflow.yaml")
    env = _load("shared_bay.env.yaml")
    jobs = [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))]
    first = schedule_jobs(
        jobs, env, document_path=_load("shared_bay.document.yaml"), random_seed=0
    )
    assert first.plan is not None
    count = sum(1 for entry in first.plan["activities"] if entry["kind"] == "processing")
    assert count >= 2
    for which in range(count):
        for delay in (1, 7, 40):
            document = _overrun(first.plan, delay, which)
            # The constructed planner keeps promises as they stand and never relaxes
            # one (`api._construct_plan`), and an overrun can break one. Whether a
            # schedule exists is the question here, so the promises are taken away.
            for entry in document["jobs"]:
                entry.pop("bound", None)
            report = schedule_jobs(
                jobs, env, document_path=document, planner="greedy", check_arms=False,
                random_seed=0,
            )
            assert report.plan is not None, (which, delay, _codes(report))
            assert _spot_orders(report.plan) == _spot_orders(document), (which, delay)


def test_a_joint_replan_the_solver_comes_back_empty_from_relaxes_what_it_breaks(monkeypatch):
    # The fallback keeps the promises it can and relaxes the ones the overrun broke,
    # saying so, rather than handing back nothing.
    from ofplang.schedule import JobInput, schedule_jobs

    workflow = _load("interface_load.workflow.yaml")
    env = _load("shared_bay.env.yaml")
    jobs = [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))]
    first = schedule_jobs(
        jobs, env, document_path=_load("shared_bay.document.yaml"), random_seed=0
    )
    document = _overrun(first.plan, 5, 0)  # job1's heating runs 5 late
    monkeypatch.setattr(api, "_solver", lambda: _unsolved)
    report = schedule_jobs(jobs, env, document_path=document, check_arms=False)
    assert report.plan is not None, _codes(report)
    codes = _codes(report)
    assert errors.PLAN_CONSTRUCTED_AFTER_SOLVER in codes
    relaxed = [d.message for d in report.diagnostics if d.code == errors.JOB_BOUND_RELAXED]
    assert len(relaxed) == 1 and "'job1'" in relaxed[0]
    bounds = {entry["id"]: entry.get("bound") for entry in report.plan["jobs"]}
    assert bounds["job2"] == 44  # kept as promised
    assert bounds["job1"] > 14  # re-derived from what the constructed plan achieves
