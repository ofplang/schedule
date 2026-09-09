"""The moment the stated levels are the levels of: `inventories.at` (SPEC §6.10).

A level is replayed, never reported (§4.7.2): the document says what the stocks held
at one moment, and the history says what has happened to them since. Until now that
moment could only be the start of the run, so the replay applied the whole history.
`at` names it, which makes the replay start there -- an event before `at` is one the
stated levels already account for, and applying it again would count it twice.

The pair these tests pin is the one that makes the field safe to add:

- with `at` absent, or 0, **every level is exactly what it was**. That is what lets a
  document written before the field existed keep its meaning, and it is why the
  committed examples plan byte-identically;
- with `at` later, the earlier history is skipped -- in the direction a re-baselined
  document needs, since its levels are the levels *at that moment*.

Nothing writes a non-zero `at` yet. What will (a job leaving the plan takes its
history with it, so the levels have to be carried forward some other way) is
design.md D42; this is the reading half, on its own.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ofplang.schedule import schedule
from ofplang.schedule.core import yamlnode
from ofplang.schedule.scheduler import normalize as normalizer
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.plancheck import check_plan_inventories
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
STOCK = ("reader", "reagent")


def _load(name):
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


def _consumable():
    """The reader that starts empty, so one refill has to feed two assays -- a history
    with a draw *and* an addition in it, which is what a moment can sit between."""
    return (
        _load("consumable.workflow.yaml"),
        _load("consumable.env.yaml"),
        _load("consumable.document.yaml"),
    )


def _history(plan, now: int, at: int | None):
    """`plan` fed back as a status at `now`, optionally stating a moment.

    The levels are left exactly as they were, so the only difference between the
    documents this returns is the filter -- which is what the tests want to see. A
    real re-baselining would move the levels too, and that is stage two.
    """
    status = copy.deepcopy(plan)
    status["now"] = now
    for activity in status["activities"]:
        if activity["end"] <= now:
            activity["status"] = "completed"
        elif activity["start"] <= now:
            activity["status"] = "running"
    status["activities"] = [a for a in status["activities"] if a.get("status")]
    if at is not None:
        status["inventories"] = dict(status["inventories"], at=at)
    return status


def _levels(workflow, env, status):
    """The levels the replay arrives at, straight out of `normalize`.

    Read here rather than inferred from a makespan: the whole of `at` is *which
    events are replayed*, and a plan can come out the same shape from different
    levels. What is being measured is the number itself.
    """
    environment, env_diags = load_environment(copy.deepcopy(env))
    assert environment is not None, [d.code for d in env_diags.items]
    parsed, wf_diags = parse_workflow(copy.deepcopy(workflow))
    assert parsed is not None, [d.code for d in wf_diags.items]
    base, inst_diags = build_instance(parsed, environment, check_reachability=False)
    assert base is not None, [d.code for d in inst_diags.items]
    root = yamlnode.loads(yaml.safe_dump(status, sort_keys=False), "status")
    _instance, fixation, diags = normalizer.normalize(base, root, environment)
    return (dict(fixation.levels) if fixation is not None else None,
            [d.code for d in diags.items])


def test_the_moment_defaults_to_the_start_of_the_run():
    """🔴 The equivalence the field rests on: absent and 0 are the same document.

    The refill (+6) and both draws (-2, -2) all happen after the start of the run, so
    a replay from the start applies every one of them -- which is what it did before
    the moment could be named, and has to keep doing.
    """
    workflow, env, document = _consumable()
    plan = schedule(copy.deepcopy(workflow), copy.deepcopy(env),
                    document_path=copy.deepcopy(document), random_seed=0).plan
    assert plan is not None

    absent, absent_codes = _levels(workflow, env, _history(plan, 30, None))
    zero, zero_codes = _levels(workflow, env, _history(plan, 30, 0))
    assert absent_codes == [] and zero_codes == []
    assert absent == zero
    # 0 stated + 6 landed - 2 - 2 drawn.
    assert absent[STOCK] == 2


def test_a_later_moment_skips_the_history_before_it():
    """The levels are the levels *at* `at`, so what came before them is theirs.

    Held against the same stated levels, a moment past the whole history leaves them
    untouched -- the refill and both draws are all before it. That is the filter, seen
    on its own; a document that moved the moment without moving the levels is saying
    something false about this run, and saying it is exactly how the difference shows.
    """
    workflow, env, document = _consumable()
    plan = schedule(copy.deepcopy(workflow), copy.deepcopy(env),
                    document_path=copy.deepcopy(document), random_seed=0).plan
    assert plan is not None

    later, codes = _levels(workflow, env, _history(plan, 30, 30))
    assert codes == []
    assert later[STOCK] == 0  # the stated level, nothing replayed onto it


def test_a_moment_in_the_future_is_refused():
    """Nothing could have been replayed against levels the run has not reached: every
    event the history records happened before `now`, so a moment after it describes a
    stock at a time the document knows nothing about."""
    workflow, env, document = _consumable()
    plan = schedule(copy.deepcopy(workflow), copy.deepcopy(env),
                    document_path=copy.deepcopy(document), random_seed=0).plan
    assert plan is not None

    report = schedule(copy.deepcopy(workflow), copy.deepcopy(env),
                      document_path=_history(plan, 30, 31), random_seed=0)
    assert not report.ok
    assert "inventory_moment_in_future" in {d.code for d in report.diagnostics}


def test_the_self_check_starts_at_the_moment_too():
    """`plancheck` replays the *rendered* plan the same way, so it needs the same
    filter -- otherwise a plan whose levels are as of a later moment reads as driving
    its stock negative on the strength of draws those levels had already absorbed.

    Here the stock starts at 0 as of moment 30, and the plan carries a draw from
    before it. Replayed from the start that is -2 and out of range; replayed from 30
    it is history the levels already know about.
    """
    environment, diags = load_environment(_load("consumable.env.yaml"))
    assert environment is not None, [d.code for d in diags.items]
    plan = {
        "activities": [
            {
                "kind": "processing", "status": "completed", "start": 6, "end": 16,
                "node": ["assay"], "consumption": {"reader.reagent": 2},
            }
        ]
    }

    without = check_plan_inventories(plan, environment, {"levels": {"reader": {"reagent": 0}}})
    assert without, "a draw from an empty stock is out of range when replayed"

    with_moment = check_plan_inventories(
        plan, environment, {"levels": {"reader": {"reagent": 0}}, "at": 30}
    )
    assert with_moment == []
