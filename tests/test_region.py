"""Undecided branches planned locally, and the candidates that gives them (design.md
D66, stage 3).

A branch whose condition is produced during the run is a region: each of its outcomes
is planned on its own, within its own spots, with every Object it hands on left on a
spot every outcome reaches in at most one move. The candidates are the ways to do that
-- carried over interchangeable machines -- and a branch with none cannot be planned
whichever arm the run takes (`arm_unplannable`).
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml
from ofplang.validate import validate

from ofplang.schedule import schedule
from ofplang.schedule.scheduler.api import ASSUMED_ARM, _switched_expansions
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.region import CANDIDATE_CAP, branch_candidates
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _example(name: str):
    def load(kind: str):
        return yaml.safe_load((EXAMPLES / f"{name}.{kind}.yaml").read_text(encoding="utf-8"))

    return load("workflow"), load("env"), load("document")


def _candidates(workflow, env_doc, document, region):
    """`branch_candidates` for `region`, on the outcomes the arm check reads."""
    interface, expansion = document.get("interface"), document.get("expansion")
    env, result = load_environment(copy.deepcopy(env_doc))
    assert env is not None, result.diagnostics
    read, diags = parse_workflow(
        copy.deepcopy(workflow), interface=interface, expansion=expansion, assume=ASSUMED_ARM
    )
    assert not [d for d in diags.items if d.severity == "error"]

    def built(wf):
        instance, built_diags = build_instance(
            wf, env, interface=interface, check_reachability=False
        )
        assert instance is not None, [d.message for d in built_diags.items]
        return instance

    outcomes = [built(read)]
    for _chain, wf, errs, paths in _switched_expansions(
        copy.deepcopy(workflow), interface, expansion, read
    ):
        if wf is not None and not errs and all(p[: len(region)] == region for p in paths):
            outcomes.append(built(wf))
    return branch_candidates(region, outcomes)


# --- the worked example ------------------------------------------------------------------


def test_the_arms_share_the_one_place_both_reach():
    # A dirty cup is washed at the sink, a clean one polished at the buffer, and no
    # route joins the two: the racks are where both arms can leave the cup in one move,
    # so that is where it waits for whatever follows (design.md D66 Q3 (A)).
    workflow, env, document = _example("inspect_then_wash")
    region = ("Each", 0, "Choose")
    candidates, why = _candidates(workflow, env, document, region)
    assert why is None and candidates
    gate = ("Each", 0, "Choose", "@out", "cup")
    base = candidates[0]
    assert base.exits[gate].startswith("rack.")
    assert {"sink.basin", "buffer.pad", base.exits[gate]} <= base.spots
    # Both outcomes planned, the one assumed (wash) first.
    assert len(base.plans) == len(base.durations) == 2
    assert base.plans[0][("Each", 0, "Choose")].devices == ("sink",)
    assert base.plans[1][("Each", 0, "Choose")].devices == ("buffer",)


def test_a_place_the_next_step_cannot_take_from_is_no_exit():
    # Each cup is bound to a rack slot of its own: a candidate leaving cup 0 on another
    # slot is a plan that cannot go on, so cup 0's only exit is its own slot.
    workflow, env, document = _example("inspect_then_wash")
    candidates, _why = _candidates(workflow, env, document, ("Each", 0, "Choose"))
    gate = ("Each", 0, "Choose", "@out", "cup")
    assert [c.exits[gate] for c in candidates] == ["rack.a"]


def test_interchangeable_places_give_one_candidate_each():
    # With the outputs left for the plan to place, the three rack slots are alike to the
    # region, so each is a candidate: three cups need not all want the first one.
    workflow, env, document = _example("inspect_then_wash")
    unbound = {**document, "interface": {"inputs": document["interface"]["inputs"]}}
    candidates, _why = _candidates(workflow, env, unbound, ("Each", 0, "Choose"))
    gate = ("Each", 0, "Choose", "@out", "cup")
    assert sorted(c.exits[gate] for c in candidates) == ["rack.a", "rack.b", "rack.c"]
    assert len({c.spots for c in candidates}) == len(candidates)


def test_interchangeable_machines_give_one_candidate_each():
    # A second sink, the same machine with the same routes: a candidate washing on it.
    workflow, env, document = _example("inspect_then_wash")
    env = copy.deepcopy(env)
    env["devices"].append({"id": "sink2", "spots": ["basin"]})
    env["transports"] += [
        {**t, "from": t["from"].replace("sink.", "sink2."),
         "to": t["to"].replace("sink.", "sink2.")}
        for t in env["transports"]
        if "sink." in t["from"] or "sink." in t["to"]
    ]
    (mode,) = env["processes"]["wash"]["modes"]
    env["processes"]["wash"]["modes"] = [
        {**mode, "id": "one"},
        {**mode, "id": "two", "devices": ["sink2"],
         "input_spots": {"cup": "sink2.basin"}, "output_spots": {"cup": "sink2.basin"}},
    ]
    candidates, _why = _candidates(workflow, env, document, ("Each", 0, "Choose"))
    assert len(candidates) <= CANDIDATE_CAP
    washed_on = {c.plans[0][("Each", 0, "Choose")].devices for c in candidates}
    assert washed_on == {("sink",), ("sink2",)}


def test_the_same_inputs_give_the_same_candidates():
    workflow, env, document = _example("inspect_then_wash")
    first = _candidates(workflow, env, document, ("Each", 1, "Choose"))
    again = _candidates(workflow, env, document, ("Each", 1, "Choose"))
    assert first == again


# --- a branch with no place every arm reaches ---------------------------------------------

APART = """\
spec_version: "0.5"
types:
  Cup: {domain: object}
processes:
  inspect:
    kind: atomic
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}, dirty: {type: Bool, phase: data}}
    objects: {map: {outputs.cup: inputs.cup}}
  wash:
    kind: atomic
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {map: {outputs.cup: inputs.cup}}
  polish:
    kind: atomic
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {map: {outputs.cup: inputs.cup}}
  main:
    kind: composite
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - {id: Look, process: inspect, state: {cup: {from: inputs.cup}}}
        - id: Choose
          kind: branch
          condition: {from: Look.dirty}
          args: {cup: {from: Look.cup}}
          then: {process: wash}
          else: {process: polish}
      returns: {cup: {from: Choose.cup}}
entry: main
"""

# The sink reaches only the left shelf, the buffer only the right one.
APART_ENV = {
    "time": {"unit": "second"},
    "devices": [
        {"id": "tray", "spots": ["a"]}, {"id": "scope", "spots": ["stage"]},
        {"id": "sink", "spots": ["basin"]}, {"id": "buffer", "spots": ["pad"]},
        {"id": "shelf", "spots": ["left", "right"]},
    ],
    "transporters": [{"id": "arm"}],
    "transports": [
        {"transporter": "arm", "from": "tray.a", "to": "scope.stage", "duration": 1},
        {"transporter": "arm", "from": "scope.stage", "to": "sink.basin", "duration": 1},
        {"transporter": "arm", "from": "scope.stage", "to": "buffer.pad", "duration": 1},
        {"transporter": "arm", "from": "sink.basin", "to": "shelf.left", "duration": 1},
        {"transporter": "arm", "from": "buffer.pad", "to": "shelf.right", "duration": 1},
    ],
    "processes": {
        "inspect": {"modes": [{"devices": ["scope"], "duration": 1,
                               "input_spots": {"cup": "scope.stage"},
                               "output_spots": {"cup": "scope.stage"}}]},
        "wash": {"modes": [{"devices": ["sink"], "duration": 2,
                            "input_spots": {"cup": "sink.basin"},
                            "output_spots": {"cup": "sink.basin"}}]},
        "polish": {"modes": [{"devices": ["buffer"], "duration": 2,
                              "input_spots": {"cup": "buffer.pad"},
                              "output_spots": {"cup": "buffer.pad"}}]},
    },
}
APART_DOCUMENT = {
    "time": {"unit": "second"}, "activities": [], "interface": {"inputs": {"cup": "tray.a"}},
}


def test_a_branch_whose_arms_share_no_place_has_no_candidate():
    assert validate(yaml.safe_load(APART)).ok
    candidates, why = _candidates(yaml.safe_load(APART), APART_ENV, APART_DOCUMENT, ("Choose",))
    assert candidates == () and "one move" in why


def test_and_so_it_cannot_be_planned_whichever_arm_the_run_takes():
    report = schedule(
        yaml.safe_load(APART), copy.deepcopy(APART_ENV), document_path=copy.deepcopy(APART_DOCUMENT)
    )
    assert report.plan is None
    unplannable = [d for d in report.diagnostics if d.code == "arm_unplannable"]
    assert unplannable and "Choose" in unplannable[0].message


def test_a_shelf_both_reach_lets_it_through():
    env = copy.deepcopy(APART_ENV)
    env["transports"].append(
        {"transporter": "arm", "from": "buffer.pad", "to": "shelf.left", "duration": 1}
    )
    candidates, why = _candidates(yaml.safe_load(APART), env, APART_DOCUMENT, ("Choose",))
    assert why is None
    assert candidates[0].exits[("Choose", "@out", "cup")] == "shelf.left"
    report = schedule(yaml.safe_load(APART), env, document_path=copy.deepcopy(APART_DOCUMENT))
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
