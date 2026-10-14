"""Runs whose every replan is constructed, with lengths made during the run (design.md D70).

A run is played decision by decision, three jobs at once: each length is counted as
anything from none to three, each loop goes on or ends, and now and then something
overruns first. Every replan is handed a solver that gives up, so what comes back is the
previous plan laid out again with what a count changed put in -- the new invocations
after everything, or inside the window of the region around them; the ones that turned
out not to exist taken out -- or nothing. There must always be something.
"""

from __future__ import annotations

import copy
import random

import pytest

from ofplang.schedule import JobInput, schedule_jobs
from ofplang.schedule.scheduler import api
from ofplang.schedule.scheduler.result import Solution
from ofplang.schedule.validation import errors
from tests import test_made_lengths as made
from tests.test_replay_previous import _overrun

_JOBS = 3


def _env() -> dict:
    """The redo laboratory, with a rack spot in and out per job and the robot's hand
    reachable, so a cup made there can be put down elsewhere."""
    env = copy.deepcopy(made.ENV)
    ins, outs = [f"in{j}" for j in range(_JOBS)], [f"out{j}" for j in range(_JOBS)]
    env["devices"][0]["spots"] = ins + outs
    places = [f"rack.{s}" for s in ins + outs] + [
        "scope.stage", "sink.basin", "shelf.a", "shelf.b", "robot.hand",
    ]
    env["transports"] = [{"transporter": "arm", "from": a, "to": b, "duration": 1}
                         for a in places for b in places if a != b]
    env["processes"]["store"]["modes"] = [
        {"devices": ["rack"], "duration": 1, "input_spots": {"plate": f"rack.{o}"},
         "output_spots": {"plate": f"rack.{o}"}} for o in outs
    ]
    env["processes"]["survey"] = copy.deepcopy(env["processes"]["inspect"])
    env["processes"]["check"] = {"modes": [{"devices": ["bench"], "duration": 1}]}
    env["processes"]["look"] = {"modes": [{"devices": ["scope"], "duration": 2,
                                           "input_spots": {"plate": "scope.stage"},
                                           "output_spots": {"plate": "scope.stage"}}]}
    return env


_ROSTER = {"activities": [], "jobs": [
    {"id": f"j{j}", "interface": {"inputs": {"plate": f"rack.in{j}"}}} for j in range(_JOBS)
]}

_LOOPED = made._workflow("""\
  check:
    kind: atomic
    inputs: {oks: {type: "Array<Bool>", phase: data}}
    outputs: {again: {type: Bool, phase: data}}
  round:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}, again: {type: Bool, phase: data}}
    body:
      nodes:
        - {id: I, process: inspect, state: {plate: {from: inputs.plate}}}
        - {id: R, kind: map, process: fix_well, each: {well: {from: I.redo}}}
        - {id: C, process: check, bind: {oks: {from: R.ok}}}
      returns: {plate: {from: I.plate}, again: {from: C.again}}
  main:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    body:
      nodes:
        - {id: L, kind: do_while, process: round, carry: {plate: {from: inputs.plate}},
           condition: {output: again}, max_iterations: {value: 3}}
        - {id: S, process: store, state: {plate: {from: L.plate}}}
      returns: {plate: {from: S.plate}}
""")

# The plate washed and looked at again per well: on the scope every job inspects on.
_WASH_LOOK = made._workflow("""\
  look:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  wash_look:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}, well: {type: Int, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    body:
      nodes:
        - {id: P, process: prep}
        - {id: W, process: wash, state: {plate: {from: inputs.plate}},
           bind: {well: {from: inputs.well}, ready: {from: P.ready}}}
        - {id: K, process: look, state: {plate: {from: W.plate}}}
      returns: {plate: {from: K.plate}}
  main:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    body:
      nodes:
        - {id: I, process: inspect, state: {plate: {from: inputs.plate}}}
        - {id: F, kind: fold, process: wash_look, carry: {plate: {from: I.plate}},
           each: {well: {from: I.redo}}}
        - {id: S, process: store, state: {plate: {from: F.plate}}}
      returns: {plate: {from: S.plate}}
""")

_SHAPES = {
    "a map": made.REDO,
    "a map over a map": made.AGAIN,
    "a fold carrying the plate": made.WASH,
    "a fold back on the inspecting scope": _WASH_LOOK,
    "rows of a made list": made.ROWS,
    "a map in each invocation of a loop": _LOOPED,
    "a cup made per well": made.CUPS,
}


def _plan(workflow, document):
    jobs = [JobInput(j["id"], copy.deepcopy(workflow)) for j in document["jobs"]]
    return schedule_jobs(jobs, _env(), document_path=copy.deepcopy(document), random_seed=0,
                         check_arms=False)


def _stamped(plan: dict, now: int) -> dict:
    document = copy.deepcopy(plan)
    for entry in document["activities"]:
        if entry["kind"] in ("processing", "transport") and entry["start"] < now:
            entry["status"] = "completed" if entry["end"] <= now else "running"
    document["now"] = now
    return document


def _ended(plan: dict, decision: dict) -> bool:
    if "iteration" not in decision:
        return False
    job = next(j for j in plan["jobs"] if j["id"] == decision.get("job"))
    return any(x["node"] == decision["node"] and x["ended"]
               for x in (job.get("expansion") or {}).get("loops", []))


def _state(document: dict, decision: dict, rng) -> str:
    """State `decision` as a run might: a length from none to three, a loop on or not."""
    job = next(j for j in document["jobs"] if j["id"] == decision.get("job"))
    expansion = job.setdefault("expansion", {})
    if "length" in decision:
        where = decision["condition"]
        key = (tuple(where["node"]), where["port"], tuple(where.get("index") or ()))
        lengths = [e for e in expansion.get("lengths", [])
                   if (tuple(e["node"]), e["port"], tuple(e.get("index") or ())) != key]
        n = rng.choice([0, 1, 2, 3])
        entry = {"node": list(key[0]), "port": key[1], "length": n}
        if key[2]:
            entry["index"] = list(key[2])
        expansion["lengths"] = [*lengths, entry]
        what = f"{n} long"
    else:
        on = rng.random() < 0.5
        count = decision["iteration"] + (2 if on else 1)
        loops = [x for x in expansion.get("loops", []) if x["node"] != decision["node"]]
        expansion["loops"] = [*loops, {"node": decision["node"], "count": count,
                                       "ended": not on or count >= 3}]
        what = "goes on" if on else "ends"
    job.pop("bound", None)
    job.pop("fingerprint", None)
    return f"{decision.get('job')}{decision['node']} {what}"


@pytest.mark.parametrize("shape", list(_SHAPES))
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_a_run_of_any_lengths_and_overruns_is_always_constructed(shape, seed, monkeypatch):
    workflow = _SHAPES[shape]
    rng = random.Random(seed)
    plan = _plan(workflow, _ROSTER).plan
    assert plan is not None
    monkeypatch.setattr(api, "_solver", lambda: lambda *a, **k: Solution("unknown", None, (), ()))
    steps: list[str] = []
    for _ in range(60):
        pending = sorted(
            (a for a in plan["activities"] if a["kind"] == "decision" and not _ended(plan, a)),
            key=lambda a: a["start"],
        )
        if not pending:
            return
        decision = pending[0]
        settled = max([a["end"] for a in plan["activities"] if a.get("status")] + [0])
        late = [a for a in plan["activities"]
                if a["kind"] == "processing" and not a.get("status")
                and a["start"] >= settled and a["end"] <= decision["start"]]
        if late and rng.random() < 0.4:
            which = rng.choice(late)
            ordered = sorted((a for a in plan["activities"] if a["kind"] == "processing"),
                             key=lambda a: (a["start"], a["end"]))
            delay = rng.choice([1, 3, 5])
            document = _overrun(plan, delay, ordered.index(which))
            steps.append(f"{which.get('job')}{which['node']} +{delay}")
        else:
            document = _stamped(plan, decision["start"])
            steps.append(_state(document, decision, rng))
        report = _plan(workflow, document)
        assert report.plan is not None, (steps, [d.code for d in report.diagnostics])
        assert errors.PLAN_CONSTRUCTED_AFTER_SOLVER in [d.code for d in report.diagnostics]
        plan = report.plan
    raise AssertionError(f"the run did not end: {steps}")
