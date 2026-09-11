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

from ofplang.schedule import JobInput, schedule, schedule_jobs
from ofplang.schedule.core import yamlnode
from ofplang.schedule.scheduler import normalize as normalizer
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import (
    build_instance,
    merge_instances,
    prefix_instance,
)
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


def _both(workflow, env, status):
    """Both levels the replay produces: the solver's, and the one `at = now` states.

    They are the same stocks at the same moment read at two points of it -- after all
    of `now`, and between `now`'s two phases -- so a test that cares about the
    boundary has to see both.
    """
    environment, env_diags = load_environment(copy.deepcopy(env))
    assert environment is not None, [d.code for d in env_diags.items]
    parsed, wf_diags = parse_workflow(copy.deepcopy(workflow))
    assert parsed is not None, [d.code for d in wf_diags.items]
    base, inst_diags = build_instance(parsed, environment, check_reachability=False)
    assert base is not None, [d.code for d in inst_diags.items]
    root = yamlnode.loads(yaml.safe_dump(status, sort_keys=False), "status")
    _instance, fixation, diags = normalizer.normalize(base, root, environment)
    assert fixation is not None, [d.code for d in diags.items]
    return dict(fixation.levels), dict(fixation.stated_levels)


def _joint_levels(workflow, env, status, ids):
    """The solver's levels for a joint document -- the same reading as `_levels`, with
    the instance built the way a joint plan builds it (one per job, prefixed and
    merged). Needed because the round trip a withdrawal makes only exists there."""
    environment, env_diags = load_environment(copy.deepcopy(env))
    assert environment is not None, [d.code for d in env_diags.items]
    parsed, wf_diags = parse_workflow(copy.deepcopy(workflow))
    assert parsed is not None, [d.code for d in wf_diags.items]
    bases = []
    for job_id in ids:
        base, inst_diags = build_instance(parsed, environment, check_reachability=False)
        assert base is not None, [d.code for d in inst_diags.items]
        bases.append(prefix_instance(base, (job_id,)))
    root = yamlnode.loads(yaml.safe_dump(status, sort_keys=False), "status")
    _instance, fixation, diags = normalizer.normalize(
        merge_instances(bases), root, environment, jobs=tuple(ids)
    )
    assert fixation is not None, [d.code for d in diags.items]
    return dict(fixation.levels)


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

# ---------------------------------------------------------------------------
# Where in an instant the moment sits (SPEC §4.7, §6.10).
#
# One instant has two phases: refills that land at it are added, then draws that
# begin at it are taken. `at` names the point *between* them -- so a refill landing
# exactly at `at` is already in the stated levels, and a draw beginning exactly at
# `at` is not. The pair below is what fixes that, and the round trip is why it has
# to be fixed: a withdrawal writes `at = now`, and the moment it writes has to be
# the moment the next replan reads.
# ---------------------------------------------------------------------------


def test_a_refill_landing_at_the_moment_is_already_in_the_stated_levels():
    """The refill phase comes first, so by the time the moment is taken the stock it
    added is on the shelf. Replaying it again would add it twice.

    Measured against the next instant: stating the moment as 4 (when the refill
    lands) and as 5 (after it) must arrive at the same level, because in both the
    refill is behind the moment.
    """
    workflow, env, document = _consumable()
    plan = schedule(copy.deepcopy(workflow), env, document_path=copy.deepcopy(document),
                    random_seed=0).plan
    landed = _history(plan, 40, at=4)
    # 6 is the level the refill leaves behind, which is what `at = 4` states.
    landed["inventories"] = {"levels": {"reader": {"reagent": 6}}, "at": 4}
    after = copy.deepcopy(landed)
    after["inventories"] = dict(after["inventories"], at=5)

    at_the_moment, _ = _levels(workflow, env, landed)
    just_after, _ = _levels(workflow, env, after)
    assert at_the_moment == just_after == {STOCK: 2}


def test_a_draw_beginning_at_the_moment_is_not():
    """The other side of the same point. The consumption phase comes after the moment,
    so a draw that begins exactly then is still to be replayed -- and stating the
    moment one tick later, when that draw is behind it, must differ by exactly it."""
    workflow, env, document = _consumable()
    plan = schedule(copy.deepcopy(workflow), env, document_path=copy.deepcopy(document),
                    random_seed=0).plan
    # A draw of 2 begins at 6; 6 is the level standing when it does.
    at_the_draw = _history(plan, 40, at=6)
    at_the_draw["inventories"] = {"levels": {"reader": {"reagent": 6}}, "at": 6}
    after = copy.deepcopy(at_the_draw)
    after["inventories"] = dict(after["inventories"], at=7)

    counted, _ = _levels(workflow, env, at_the_draw)
    skipped, _ = _levels(workflow, env, after)
    assert counted == {STOCK: 2}   # -2 at 6, -2 at 20
    assert skipped == {STOCK: 4}   # only the draw at 20


def test_the_two_levels_part_company_over_a_draw_at_now():
    """🔴 The solver and the document want the level at different points of `now`.

    A started activity has taken its consumption and the model does not re-model it,
    so the level the solver starts from has `now`'s draws in it. `at = now` means the
    point before them. Writing one under the other's name is what made the round trip
    below lose stock.
    """
    workflow, env, document = _consumable()
    plan = schedule(copy.deepcopy(workflow), env, document_path=copy.deepcopy(document),
                    random_seed=0).plan
    # A draw begins at exactly 20.
    for_solver, as_stated = _both(workflow, env, _history(plan, 20, at=None))
    assert for_solver == {STOCK: 2}    # 0 +6 -2 (at 6) -2 (at 20)
    assert as_stated == {STOCK: 4}     # the snapshot before 20's draws

    # With nothing happening at `now` the two coincide, which is the ordinary case.
    same_for_solver, same_as_stated = _both(workflow, env, _history(plan, 21, at=None))
    assert same_for_solver == same_as_stated == {STOCK: 2}


def test_a_withdrawal_round_trips_across_an_event_at_now():
    """🔴 The invariant the whole boundary exists for: what a withdrawal writes is
    what the next replan reads.

    The withdrawal states the levels as of `now` and the next replan replays from
    there. If the two disagree about what `now` means, the events at exactly `now`
    fall in the gap -- and they are counted twice, so the plan believes stock it has
    that it does not.
    """
    shared = _load("shared_refill.workflow.yaml")
    env = _load("consumable.env.yaml")
    first = schedule_jobs(
        [JobInput(i, copy.deepcopy(shared)) for i in ("job1", "job2")], env,
        document_path=_load("shared_refill.document.yaml"), random_seed=0,
    )
    assert first.ok, [d.code for d in first.diagnostics]
    # job2 draws at exactly 20, which is where the two readings differ.
    status = _history(first.plan, 20, at=None)
    out = schedule_jobs(
        [JobInput("job2", copy.deepcopy(shared))], env,
        document_path=copy.deepcopy(status), withdraw=["job1"], random_seed=0,
    )
    assert out.ok, [d.code for d in out.diagnostics]
    stated = out.plan["inventories"]
    assert stated["at"] == 20
    # The level before 20's draw: 2 stated + 4 refilled - 2 drawn by job1.
    assert stated["levels"] == {"reader": {"reagent": 4}}

    # Read it back. The draw at 20 is replayed once, leaving the level the solver
    # had all along -- which is the test, since the number is not stated anywhere.
    back = _history(out.plan, 20, at=None)
    back["inventories"] = copy.deepcopy(stated)
    assert _joint_levels(shared, env, back, ["job2"]) == {STOCK: 2}

    # And 2 is what the level actually was: the same moment of the same run, read
    # without the withdrawal ever happening.
    assert _joint_levels(shared, env, status, ["job1", "job2"]) == {STOCK: 2}


def test_the_history_is_checked_at_every_event_not_only_at_the_end():
    """A history that overflows part-way disagrees with the environment even if it
    lands back in range, and saying so is this code's whole job (§9.3).

    🔴 Only the endpoint was checked, so this reached the solver and came back as
    `plan_inventory_inconsistent` -- the plan self-check, whose findings mean the
    scheduler has a defect, reporting a fault in the input. And when the solve was
    infeasible for any other reason nothing reported it at all.
    """
    workflow, env, document = _consumable()
    plan = schedule(copy.deepcopy(workflow), env, document_path=copy.deepcopy(document),
                    random_seed=0).plan
    status = _history(plan, 40, at=None)
    # 4 + the refill's 6 is 10, over the capacity of 6; 4 + 6 - 2 - 2 is 6, inside it.
    status["inventories"] = {"levels": {"reader": {"reagent": 4}}}
    _levels_out, codes = _levels(workflow, env, status)
    assert codes == ["status_inventory_inconsistent"]

    report = schedule(copy.deepcopy(workflow), env, document_path=status, random_seed=0)
    assert not report.ok
    assert [d.code for d in report.diagnostics if d.severity == "error"] == [
        "status_inventory_inconsistent"
    ]
    # It says which event, because "somewhere in the history" is not actionable.
    assert "at time 4" in next(
        d.message for d in report.diagnostics if d.code == "status_inventory_inconsistent"
    )


def test_a_refill_landing_when_the_work_begins_feeds_it():
    """The reason the phases are ordered rather than netted (§4.7). Replayed the other
    way round the stock would read as overdrawn at the instant it was topped up."""
    workflow, env, document = _consumable()
    plan = schedule(copy.deepcopy(workflow), env, document_path=copy.deepcopy(document),
                    random_seed=0).plan
    status = _history(plan, 40, at=None)
    status["inventories"] = {"levels": {"reader": {"reagent": 0}}}
    for activity in status["activities"]:
        if activity["kind"] == "replenishment":
            activity["end"] = 6  # lands exactly when the first draw begins
    levels, codes = _levels(workflow, env, status)
    assert codes == []
    assert levels == {STOCK: 2}
