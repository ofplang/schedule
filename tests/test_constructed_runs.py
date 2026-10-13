"""Runs whose every replan is constructed (design.md D67).

A run is played decision by decision: before each one, now and then, something
overruns; each decision is stated -- on the arm or end the plan assumed, or on any. Every
replan is handed a solver that gives up, so what comes back is the previous plan laid
out again, with what a decision changed spliced in, or nothing -- and there must always
be something: only durations moved, or a region whose outcome was open kept what its
other outcomes need. A run stopping here is a replan that would stop a laboratory whose
solver ran out of budget.
"""

from __future__ import annotations

import copy
import random

import pytest

from ofplang.schedule import JobInput, schedule, schedule_jobs
from ofplang.schedule.scheduler import api
from ofplang.schedule.scheduler.result import Solution
from ofplang.schedule.validation import errors
from tests.test_loops import COLLECTED, LOOP_IN_BRANCH
from tests.test_loops import DOCUMENT as RINSE_DOCUMENT
from tests.test_loops import ENV as RINSE_ENV
from tests.test_replay_previous import _overrun
from tests.test_reservations import (
    _CUPS_ENV,
    _DECIDES,
    _LOOKS_THEN_CHECKS,
    _PLATES_ENV,
    _SOAKS,
    _WASHES_AGAIN,
    _cup_workflow,
    _cups,
    _two_rinses,
)

_LOOKS_THEN_POLISHES = _cup_workflow(
    "        - {id: L, process: inspect, state: {cup: {from: inputs.cup}}}" + chr(10)
    + "        - {id: P, process: polish, state: {cup: {from: L.cup}}}" + chr(10),
    "P.cup",
)


def _unsolved(*args, **kwargs) -> Solution:
    return Solution("unknown", None, (), ())


def _stamped(plan: dict, now: int) -> dict:
    """`plan` handed back at `now`, everything on time."""
    document = copy.deepcopy(plan)
    for entry in document["activities"]:
        if entry["kind"] in ("processing", "transport") and entry["start"] < now:
            entry["status"] = "completed" if entry["end"] <= now else "running"
    document["now"] = now
    return document


def _ended(plan: dict, decision: dict) -> bool:
    """Whether `decision` waits on a loop the run has already ended (at its limit)."""
    if "iteration" not in decision:
        return False
    job = decision.get("job")
    holder = plan if job is None else next(j for j in plan["jobs"] if j["id"] == job)
    stated = (holder.get("expansion") or {}).get("loops", [])
    return any(x["node"] == decision["node"] and x["ended"] for x in stated)


def _state_assumed(document: dict, decision: dict) -> None:
    """State `decision` as the plan assumed it: a branch on its arm, a loop ended."""
    job = decision.get("job")
    holder = document if job is None else next(j for j in document["jobs"] if j["id"] == job)
    expansion = holder.setdefault("expansion", {})
    if "iteration" in decision:
        count = decision["iteration"] + 1
        expansion.setdefault("loops", []).append(
            {"node": decision["node"], "count": count, "ended": True}
        )
    else:
        expansion.setdefault("arms", []).append({"node": decision["node"], "arm": decision["arm"]})
    if job is not None:
        holder.pop("bound", None)
        holder.pop("fingerprint", None)


def _state_any(document: dict, decision: dict, rng, limits: dict) -> str:
    """State `decision` as a run might: a branch on either arm, a loop ended or going on
    (ended where that is the last invocation its `max_iterations` allows)."""
    job = decision.get("job")
    holder = document if job is None else next(j for j in document["jobs"] if j["id"] == job)
    expansion = holder.setdefault("expansion", {})
    node = decision["node"]
    if "iteration" in decision:
        on = rng.random() < 0.6
        count = decision["iteration"] + (2 if on else 1)
        ended = not on or count >= limits.get(node[-1], 5)
        loops = [x for x in expansion.get("loops", []) if x["node"] != node]
        expansion["loops"] = [*loops, {"node": node, "count": count, "ended": ended}]
        what = "goes on" if on else "ends"
    else:
        what = rng.choice(["then", "else"])
        arms = [x for x in expansion.get("arms", []) if x["node"] != node]
        expansion["arms"] = [*arms, {"node": node, "arm": what}]
    if job is not None:
        holder.pop("bound", None)
        holder.pop("fingerprint", None)
    return what


def _play(plan_with, seed: int, give_up, limits=None) -> list[str]:
    """Play one run -- the first plan the solver's, every replan after `give_up()`
    constructed -- and return what happened, step by step. With `limits` (a loop's last
    node id -> its `max_iterations`) outcomes are any; without, the ones assumed."""
    rng = random.Random(seed)
    plan = plan_with(None).plan
    give_up()
    steps: list[str] = []
    for _ in range(40):
        pending = sorted(
            (a for a in plan["activities"] if a["kind"] == "decision" and not _ended(plan, a)),
            key=lambda a: a["start"],
        )
        if not pending:
            return steps
        decision = pending[0]
        settled = max([a["end"] for a in plan["activities"] if a.get("status")] + [0])
        late = [
            a for a in plan["activities"]
            if a["kind"] == "processing" and not a.get("status")
            and a["start"] >= settled and a["end"] <= decision["start"]
        ]
        if late and rng.random() < 0.5:
            which = rng.choice(late)
            ordered = sorted(
                (a for a in plan["activities"] if a["kind"] == "processing"),
                key=lambda a: (a["start"], a["end"]),
            )
            delay = rng.choice([1, 3, 7])
            document = _overrun(plan, delay, ordered.index(which))
            steps.append(f"{which.get('job')}{which['node']} +{delay}")
        else:
            document = _stamped(plan, decision["start"])
            if limits is None:
                _state_assumed(document, decision)
                steps.append(f"{decision.get('job')}{decision['node']} as assumed")
            else:
                what = _state_any(document, decision, rng, limits)
                steps.append(f"{decision.get('job')}{decision['node']} {what}")
        report = plan_with(document)
        assert report.plan is not None, (steps, [d.code for d in report.diagnostics])
        assert errors.PLAN_CONSTRUCTED_AFTER_SOLVER in [d.code for d in report.diagnostics]
        plan = report.plan
    raise AssertionError(f"the run did not end: {steps}")


def _one(workflow, env, document):
    def plan_with(given):
        return schedule(copy.deepcopy(workflow), copy.deepcopy(env),
                        document_path=copy.deepcopy(document) if given is None else given,
                        random_seed=0, check_arms=False, max_time_seconds=30)
    return plan_with


def _two(workflow, env, roster):
    """Every job of `roster` running `workflow` -- or, given a list, one each."""
    workflows = workflow if isinstance(workflow, list) else [workflow] * len(roster["jobs"])

    def plan_with(given):
        jobs = [
            JobInput(j["id"], copy.deepcopy(w))
            for j, w in zip(roster["jobs"], workflows, strict=True)
        ]
        return schedule_jobs(jobs, copy.deepcopy(env),
                             document_path=copy.deepcopy(roster) if given is None else given,
                             random_seed=0, check_arms=False, max_time_seconds=30)
    return plan_with


def _rinse_env(env: dict) -> dict:
    """The rinse laboratory with a way from the rack to the reader, which a workflow
    that reads first needs."""
    env = copy.deepcopy(env)
    spots = {f"{d['id']}.{s}" for d in env["devices"] for s in d["spots"]}
    for source in ("rack.in", "rack.in2"):
        if source in spots:
            env["transports"].append(
                {"transporter": "arm", "from": source, "to": "reader.slot", "duration": 2}
            )
    return env


def _cases():
    cups = _cups(3)
    jobs, env, roster = _two_rinses()
    two_cups = {"activities": [], "jobs": [
        {"id": j, "release": 0,
         "interface": {"inputs": {"cup": f"tray.{j}"}, "outputs": {"cup": f"rack.{j}"}}}
        for j in "ab"
    ]}
    plates = {"activities": [], "jobs": [
        {"id": "a", "release": 0,
         "interface": {"inputs": {"plate": "rack.in"}, "outputs": {"plate": "rack.out"}}},
        {"id": "b", "release": 9,
         "interface": {"inputs": {"plate": "rack.in2"}, "outputs": {"plate": "rack.out2"}}},
    ]}
    logged = copy.deepcopy(env)
    logged["devices"].append({"id": "logger", "spots": []})
    logged["processes"]["log"] = {"modes": [{"devices": ["logger"], "duration": 1}]}
    return {
        "three cups, a branch each": _one(*cups),
        "two rinsing loops, nowhere to wait": _two(jobs[0].workflow, env, roster),
        "two cups, a branch inside a branch": _two(_WASHES_AGAIN, _CUPS_ENV, two_cups),
        # The other cup is only inspected and polished -- on a spot the first one's branch
        # keeps, before its window and late (D67 hole 1).
        "a branch and work beside it": _two([_DECIDES, _LOOKS_THEN_POLISHES], _CUPS_ENV, two_cups),
        "two plates, a loop inside a branch": _two(LOOP_IN_BRANCH, _rinse_env(env), roster),
        # A branch inside each invocation decided before the loop's own reading, and a
        # plate beside it that would take the branch's spot (D67 hole 7).
        "a loop deciding inside before itself": _two(
            [_LOOKS_THEN_CHECKS, _SOAKS], _PLATES_ENV, plates
        ),
        "one plate, a loop inside a branch": _one(
            LOOP_IN_BRANCH, _rinse_env(RINSE_ENV), RINSE_DOCUMENT
        ),
        # A map over what the loop collected: one invocation more for every one the loop
        # goes on (D67 hole 8).
        "two plates, every reading logged": _two(COLLECTED, logged, roster),
    }


# A loop's max_iterations, by its node id, where it is not five.
_LIMITS = {"a loop deciding inside before itself": {"Clean": 4}}


@pytest.mark.parametrize("case", list(_cases()))
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_a_run_of_assumed_outcomes_and_overruns_is_always_constructed(case, seed, monkeypatch):
    _play(_cases()[case], seed, lambda: monkeypatch.setattr(api, "_solver", lambda: _unsolved))


@pytest.mark.parametrize("case", list(_cases()))
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_a_run_of_any_outcomes_and_overruns_is_always_constructed(case, seed, monkeypatch):
    # A branch decided on the arm not assumed, a loop going on: the previous plan with
    # the decided outcome, or the next invocation, spliced in (design.md D67).
    _play(
        _cases()[case], seed,
        lambda: monkeypatch.setattr(api, "_solver", lambda: _unsolved),
        _LIMITS.get(case, {}),
    )
