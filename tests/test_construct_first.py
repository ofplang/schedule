"""A replan constructs its schedule first and lets the solver improve on it (design.md
D67, stage 2).

Where the plan being replaced covers the replan -- as it is, or with what a decision
changed spliced in -- the schedule it gives is built before the solve and handed to the
solver as its starting point. A solver that comes back within its budget with an answer
it has not proved keeps whichever of the two is better; one that comes back with nothing
gets the constructed schedule, as before.
"""

from __future__ import annotations

import copy
from dataclasses import replace

from ofplang.schedule import schedule
from ofplang.schedule.scheduler import api
from ofplang.schedule.validation import errors
from tests.test_reservations import _cups


def _decided_else():
    """Three cups; the first is decided `else` (a polish) once its inspection is over."""
    workflow, env, document = _cups(3)
    first = schedule(copy.deepcopy(workflow), copy.deepcopy(env),
                     document_path=copy.deepcopy(document), random_seed=0, check_arms=False)
    plan = first.plan
    decision = min((a for a in plan["activities"] if a["kind"] == "decision"),
                   key=lambda a: a["start"])
    replan = copy.deepcopy(plan)
    for entry in replan["activities"]:
        if entry["kind"] in ("processing", "transport") and entry["start"] < decision["start"]:
            entry["status"] = "completed" if entry["end"] <= decision["start"] else "running"
    replan["now"] = decision["start"]
    replan["expansion"] = {"arms": [{"node": decision["node"], "arm": "else"}]}
    return workflow, env, document, replan


def _spied(monkeypatch, answer=None):
    """The solver, recording the hint it is handed, and answering `answer(solution)`."""
    real = api._solver()
    hints = []

    def solve(*args, **kwargs):
        hints.append(kwargs.get("hint"))
        solved = real(*args, **kwargs)
        return answer(solved) if answer is not None else solved

    monkeypatch.setattr(api, "_solver", lambda: solve)
    return hints


def test_a_replan_hands_the_solver_the_schedule_it_constructed(monkeypatch):
    workflow, env, _document, replan = _decided_else()
    hints = _spied(monkeypatch)
    report = schedule(copy.deepcopy(workflow), copy.deepcopy(env), document_path=replan,
                      random_seed=0, check_arms=False)
    assert report.ok
    assert hints and hints[0] is not None
    assert errors.PLAN_CONSTRUCTED_AFTER_SOLVER not in [d.code for d in report.diagnostics]


def test_a_first_plan_is_not_handed_one(monkeypatch):
    workflow, env, document, _replan = _decided_else()
    hints = _spied(monkeypatch)
    schedule(copy.deepcopy(workflow), copy.deepcopy(env), document_path=document,
             random_seed=0, check_arms=False)
    assert hints == [None]


def test_an_unproved_answer_the_constructed_schedule_beats_gives_way_to_it(monkeypatch):
    # The solver comes back within its budget with a schedule it has not proved and that
    # is worse than the one constructed: the constructed one is the plan, and says so.
    workflow, env, _document, replan = _decided_else()
    _spied(monkeypatch, lambda solved: replace(
        solved, outcome="feasible", makespan=(solved.makespan or 0) + 100
    ))
    report = schedule(copy.deepcopy(workflow), copy.deepcopy(env), document_path=replan,
                      random_seed=0, check_arms=False)
    assert report.ok
    said = [d for d in report.diagnostics if d.code == errors.PLAN_CONSTRUCTED_AFTER_SOLVER]
    assert said and "beats" in said[0].message
    assert report.makespan < 100


def test_an_unproved_answer_as_good_is_kept(monkeypatch):
    workflow, env, _document, replan = _decided_else()
    _spied(monkeypatch, lambda solved: replace(solved, outcome="feasible"))
    report = schedule(copy.deepcopy(workflow), copy.deepcopy(env), document_path=replan,
                      random_seed=0, check_arms=False)
    assert report.ok
    assert errors.PLAN_CONSTRUCTED_AFTER_SOLVER not in [d.code for d in report.diagnostics]
