"""A branch whose condition is produced during the run (design.md D64).

The scheduler is told which arm to assume for one -- `then`, for now -- and plans it
on that arm until the run states the arm in `expansion.arms`. Either way nothing of
the branch starts before the condition exists: the arm's activities wait for the
condition's producer, and so does every move into the arm and every consumer of a
value the branch hands on untouched (its "gates"). The plan marks each branch still
waiting with a `decision`, and the arm not planned is checked without a solve
(`arm_unplannable`).

Every base workflow here is valid v0 (checked against ofplang-validate below).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from ofplang.validate import validate

from ofplang.schedule import JobInput, schedule, schedule_jobs, validate_document
from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.api import ASSUMED_ARM, _arm_expansions
from ofplang.schedule.scheduler.model import Arc, Endpoint
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

_ATOMICS = """\
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
  soak:
    kind: atomic
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {map: {outputs.cup: inputs.cup}}
"""

# A sample is inspected; the cup -- a different Object -- is washed if the sample was
# dirty, and polished afterwards either way. With no `else`, a cup the branch does not
# wash is handed on untouched.
SAMPLE_DECIDES = f"""\
spec_version: "0.5"
types:
  Cup: {{domain: object}}
processes:
{_ATOMICS}  main:
    kind: composite
    inputs: {{cup: {{type: Cup, phase: data}}, sample: {{type: Cup, phase: data}}}}
    outputs: {{cup: {{type: Cup, phase: data}}, sample: {{type: Cup, phase: data}}}}
    body:
      nodes:
        - {{id: I, process: inspect, state: {{cup: {{from: inputs.sample}}}}}}
        - id: H
          kind: branch
          condition: {{from: I.dirty}}
          args: {{cup: {{from: inputs.cup}}}}
          then: {{process: wash}}
        - {{id: F, process: polish, state: {{cup: {{from: H.cup}}}}}}
      returns: {{cup: {{from: F.cup}}, sample: {{from: I.cup}}}}
entry: main
"""

# A branch inside an arm, measured again: a dirty cup is cleaned, and soaked first if
# a second look finds it stained; a clean one is finished, and soaked if a look there
# finds it stained. The inner branch of `finish` exists only on the `else` arm.
NESTED_MEASURED = f"""\
spec_version: "0.5"
types:
  Cup: {{domain: object}}
processes:
{_ATOMICS}  clean:
    kind: composite
    inputs: {{cup: {{type: Cup, phase: data}}}}
    outputs: {{cup: {{type: Cup, phase: data}}}}
    body:
      nodes:
        - {{id: J, process: inspect, state: {{cup: {{from: inputs.cup}}}}}}
        - id: S
          kind: branch
          condition: {{from: J.dirty}}
          args: {{cup: {{from: J.cup}}}}
          then: {{process: soak}}
        - {{id: W, process: wash, state: {{cup: {{from: S.cup}}}}}}
      returns: {{cup: {{from: W.cup}}}}
  finish:
    kind: composite
    inputs: {{cup: {{type: Cup, phase: data}}}}
    outputs: {{cup: {{type: Cup, phase: data}}}}
    body:
      nodes:
        - {{id: K, process: inspect, state: {{cup: {{from: inputs.cup}}}}}}
        - id: T
          kind: branch
          condition: {{from: K.dirty}}
          args: {{cup: {{from: K.cup}}}}
          then: {{process: soak}}
        - {{id: P, process: polish, state: {{cup: {{from: T.cup}}}}}}
      returns: {{cup: {{from: P.cup}}}}
  main:
    kind: composite
    inputs: {{cup: {{type: Cup, phase: data}}}}
    outputs: {{cup: {{type: Cup, phase: data}}}}
    body:
      nodes:
        - {{id: I, process: inspect, state: {{cup: {{from: inputs.cup}}}}}}
        - id: H
          kind: branch
          condition: {{from: I.dirty}}
          args: {{cup: {{from: I.cup}}}}
          then: {{process: clean}}
          else: {{process: finish}}
      returns: {{cup: {{from: H.cup}}}}
entry: main
"""

# A cup is prepared on the scope's one stage, and a sample inspected there after it
# (the inspection reads the preparation's setting). Whether the cup is washed depends
# on the sample -- so it waits on the stage for an inspection that needs the stage.
STAGE_HELD = f"""\
spec_version: "0.5"
types:
  Cup: {{domain: object}}
processes:
{_ATOMICS}  prepare:
    kind: atomic
    inputs: {{cup: {{type: Cup, phase: data}}}}
    outputs: {{cup: {{type: Cup, phase: data}}, setting: {{type: Int, phase: data}}}}
    objects: {{map: {{outputs.cup: inputs.cup}}}}
  read:
    kind: atomic
    inputs: {{cup: {{type: Cup, phase: data}}, setting: {{type: Int, phase: data}}}}
    outputs: {{cup: {{type: Cup, phase: data}}, dirty: {{type: Bool, phase: data}}}}
    objects: {{map: {{outputs.cup: inputs.cup}}}}
  main:
    kind: composite
    inputs: {{cup: {{type: Cup, phase: data}}, sample: {{type: Cup, phase: data}}}}
    outputs: {{cup: {{type: Cup, phase: data}}, sample: {{type: Cup, phase: data}}}}
    body:
      nodes:
        - {{id: Q, process: prepare, state: {{cup: {{from: inputs.cup}}}}}}
        - id: R
          process: read
          state: {{cup: {{from: inputs.sample}}}}
          bind: {{setting: {{from: Q.setting}}}}
        - id: H
          kind: branch
          condition: {{from: R.dirty}}
          args: {{cup: {{from: Q.cup}}}}
          then: {{process: wash}}
          else: {{process: polish}}
      returns: {{cup: {{from: H.cup}}, sample: {{from: R.cup}}}}
entry: main
"""


def _env(*, stage_only_for: tuple[str, ...] = ()) -> dict:
    """A tray, a one-stage scope, a sink, a buffer, a soak bath and a rack, one arm
    between any two of them."""
    places = ["tray.a", "tray.b", "scope.stage", "sink.basin", "buffer.pad", "bath.tub",
              "rack.a", "rack.b"]
    modes = {
        "inspect": ("scope", "stage", 3),
        "prepare": ("scope", "stage", 2),
        "read": ("scope", "stage", 3),
        "wash": ("sink", "basin", 8),
        "polish": ("buffer", "pad", 4),
        "soak": ("bath", "tub", 5),
    }
    return {
        "time": {"unit": "second"},
        "devices": [
            {"id": "tray", "spots": ["a", "b"]},
            {"id": "scope", "spots": ["stage"]},
            {"id": "sink", "spots": ["basin"]},
            {"id": "buffer", "spots": ["pad"]},
            {"id": "bath", "spots": ["tub"]},
            {"id": "rack", "spots": ["a", "b"]},
        ],
        "transporters": [{"id": "arm"}],
        "transports": [
            {"transporter": "arm", "from": a, "to": b, "duration": 2}
            for a in places for b in places if a != b
        ],
        "processes": {
            name: {"modes": [{
                "devices": [device], "duration": duration,
                "input_spots": {"cup": f"{device}.{spot}"},
                "output_spots": {"cup": f"{device}.{spot}"},
            }]}
            for name, (device, spot, duration) in modes.items()
        },
    }


_TWO = {"inputs": {"cup": "tray.a", "sample": "tray.b"},
        "outputs": {"cup": "rack.a", "sample": "rack.b"}}


def _parse(text, expansion=None, interface=None, assume=ASSUMED_ARM):
    wf, diags = parse_workflow(
        yaml.safe_load(text), interface=interface, expansion=expansion, assume=assume
    )
    return wf, [d.code for d in diags.items if d.severity == ERROR]


def arms(*entries) -> dict:
    return {"arms": [{"node": list(node), "arm": arm} for node, arm in entries]}


def _example(name):
    return tuple(
        yaml.safe_load((EXAMPLES / f"inspect_then_wash.{kind}.yaml").read_text(encoding="utf-8"))
        for kind in ("workflow", "env", "document")
    ) if name == "inspect_then_wash" else None


@pytest.mark.parametrize("text", [SAMPLE_DECIDES, NESTED_MEASURED, STAGE_HELD],
                         ids=["sample_decides", "nested_measured", "stage_held"])
def test_the_fixtures_are_valid_v0(text):
    result = validate(yaml.safe_load(text))
    assert result.ok, [(d.code, d.message) for d in result.diagnostics]


# --- what waits, in the reading ----------------------------------------------------------


def test_the_assumed_arm_waits_for_the_condition():
    wf, errs = _parse(SAMPLE_DECIDES)
    assert errs == []
    gate = wf.branch_gates[("H",)]
    assert (gate.condition.node, gate.arm, gate.assumed) == (("I",), "then", True)
    # The arm's own activity waits for the inspection; the cup's move into it too.
    assert (("I",), ("H",)) in wf.precedence
    into_arm = Arc(Endpoint((), "cup"), Endpoint(("H",), "cup"))
    assert wf.arc_gates[into_arm] == frozenset({("I",)})
    # What the arm made needs no wait of its own: it was made after the condition.
    assert Arc(Endpoint(("H",), "cup"), Endpoint(("F",), "cup")) not in wf.arc_gates


def test_a_value_handed_on_untouched_waits_too():
    # On `else` (none written) the cup passes straight from the boundary to F: F may
    # still not have it before the sample is inspected -- the branch could have washed it.
    wf, errs = _parse(SAMPLE_DECIDES, arms((("H",), "else")))
    assert errs == []
    assert [a.path for a in wf.activities] == [("I",), ("F",)]
    assert (("I",), ("F",)) in wf.precedence
    through = Arc(Endpoint((), "cup"), Endpoint(("F",), "cup"))
    assert wf.arc_gates[through] == frozenset({("I",)})
    assert not wf.branch_gates[("H",)].assumed


# The cup after the branch goes through a composite: on `else` it reaches it untouched.
SAMPLE_THEN_COMPOSITE = SAMPLE_DECIDES.replace(
    "        - {id: F, process: polish, state: {cup: {from: H.cup}}}\n",
    "        - {id: F, process: finish_one, state: {cup: {from: H.cup}}}\n",
).replace(
    "  main:\n",
    "  finish_one:\n"
    "    kind: composite\n"
    "    inputs: {cup: {type: Cup, phase: data}}\n"
    "    outputs: {cup: {type: Cup, phase: data}}\n"
    "    body:\n"
    "      nodes:\n"
    "        - {id: P, process: polish, state: {cup: {from: inputs.cup}}}\n"
    "      returns: {cup: {from: P.cup}}\n"
    "  main:\n",
)


def test_a_composite_reading_a_value_handed_on_untouched_says_what_it_waits_for():
    # Its contracts read that value, which is not settled before the sample is
    # inspected -- the run holds them until then (design.md D64).
    assert validate(yaml.safe_load(SAMPLE_THEN_COMPOSITE)).ok
    wf, errs = _parse(SAMPLE_THEN_COMPOSITE, arms((("H",), "else")))
    assert errs == []
    assert wf.composites[("F",)].gates == frozenset({("I",)})
    # On `then` the cup it reads is the wash's, made after the condition: no wait.
    wf, errs = _parse(SAMPLE_THEN_COMPOSITE)
    assert errs == [] and wf.composites[("F",)].gates == frozenset()


def test_an_inner_branch_waits_for_both_conditions():
    wf, errs = _parse(NESTED_MEASURED)
    assert errs == []
    assert set(wf.branch_gates) == {("H",), ("H", "S")}
    soak = ("H", "S")
    assert {(("I",), soak), (("H", "J"), soak)} <= set(wf.precedence)
    # The cup moves from J into the soak: inside H's arm already, so only S's wait.
    into_soak = Arc(Endpoint(("H", "J"), "cup"), Endpoint(soak, "cup"))
    assert wf.arc_gates[into_soak] == frozenset({("H", "J")})


def test_each_invocation_waits_for_its_own_inspection():
    workflow, _env_doc, document = _example("inspect_then_wash")
    wf, diags = parse_workflow(
        workflow, interface=document["interface"], assume=ASSUMED_ARM
    )
    assert not diags.items
    for i in range(3):
        gate = wf.branch_gates[("Each", i, "Choose")]
        assert gate.condition.node == ("Each", i, "Look")
        waits = {src for src, dst in wf.precedence if dst == ("Each", i, "Choose")}
        assert waits == {("Each", i, "Look")}


def test_without_an_arm_to_assume_it_is_still_refused():
    _, errs = _parse(SAMPLE_DECIDES, assume=None)
    assert errs == ["branch_arm_unknown"]


# --- the plan ------------------------------------------------------------------------------


def _decisions(plan):
    return [a for a in plan["activities"] if a["kind"] == "decision"]


def _starts(plan, node):
    return [a["start"] for a in plan["activities"]
            if a["kind"] == "transport" and a["arc"]["to"]["node"] == node] + [
        a["start"] for a in plan["activities"]
        if a["kind"] == "processing" and a["node"] == node]


@pytest.mark.parametrize("planner", ["cpsat", "greedy"])
def test_nothing_of_the_branch_starts_before_its_decision(planner):
    workflow, env, document = _example("inspect_then_wash")
    report = schedule(workflow, env, document_path=document, planner=planner, random_seed=0)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    decisions = _decisions(report.plan)
    assert len(decisions) == 3
    for decision in decisions:
        assert decision["arm"] == "then" and decision["assumed"] is True
        assert decision["start"] == decision["end"]
        look = next(a for a in report.plan["activities"]
                    if a["kind"] == "processing" and a["node"] == decision["condition"]["node"])
        assert decision["start"] == look["end"]
        assert decision["condition"]["port"] == "dirty"
        assert min(_starts(report.plan, decision["node"])) >= decision["start"]


def test_the_plan_is_a_valid_document_and_replans_as_it_was():
    workflow, env, document = _example("inspect_then_wash")
    first = schedule(workflow, env, document_path=document)
    assert validate_document(first.plan).ok
    again = schedule(workflow, env, document_path=first.plan)
    assert again.ok and again.makespan == first.makespan
    assert len(_decisions(again.plan)) == 3


def test_a_decision_in_the_document_is_not_read_back():
    workflow, env, document = _example("inspect_then_wash")
    plan = schedule(workflow, env, document_path=document).plan
    for decision in _decisions(plan):
        decision["status"] = "cancelled"  # would stop the job, were it read
    again = schedule(workflow, env, document_path=plan)
    assert again.ok
    assert len(_decisions(again.plan)) == 3


def test_once_the_condition_is_in_the_decision_is_gone():
    # Cup 0 has been inspected and found clean: the run states `else`, and replans.
    workflow, env, document = _example("inspect_then_wash")
    plan = schedule(workflow, env, document_path=document, random_seed=0).plan
    history = []
    for a in plan["activities"]:
        to_look = a["kind"] == "transport" and a["arc"]["to"]["node"] == ["Each", 0, "Look"]
        if to_look or (a["kind"] == "processing" and a["node"] == ["Each", 0, "Look"]):
            history.append({**a, "status": "completed"})
    look_end = history[-1]["end"]
    status = {**document, "now": look_end, "activities": history,
              **{"expansion": arms((("Each", 0, "Choose"), "else"))}}
    report = schedule(workflow, env, document_path=status)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    nodes = [d["node"] for d in _decisions(report.plan)]
    assert nodes == [["Each", 1, "Choose"], ["Each", 2, "Choose"]]
    polish = [a for a in report.plan["activities"]
              if a["kind"] == "processing" and a["node"] == ["Each", 0, "Choose"]]
    assert [a["process"] for a in polish] == ["polish"]
    assert polish[0]["start"] >= look_end


def test_a_joint_plan_names_the_job_of_each_decision():
    workflow, env, _document = _example("inspect_then_wash")
    entry = {"interface": {"inputs": {"cups": ["tray.a"]}, "outputs": {"cups": ["rack.a"]}}}
    other = {"interface": {"inputs": {"cups": ["tray.b"]}, "outputs": {"cups": ["rack.b"]}}}
    report = schedule_jobs(
        [JobInput("one", workflow), JobInput("two", workflow)], env,
        document_path={"jobs": [{"id": "one", **entry}, {"id": "two", **other}],
                       "activities": []},
    )
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    assert sorted((d["job"], tuple(d["node"])) for d in _decisions(report.plan)) == [
        ("one", ("Each", 0, "Choose")), ("two", ("Each", 0, "Choose")),
    ]


# --- the arm not planned -------------------------------------------------------------------


def test_the_arm_not_planned_is_checked():
    workflow, env, document = _example("inspect_then_wash")
    blocked = copy.deepcopy(env)
    blocked["transports"] = [t for t in blocked["transports"] if t["to"] != "buffer.pad"]
    report = schedule(workflow, blocked, document_path=document)
    assert not report.ok
    found = [d for d in report.diagnostics if d.severity == ERROR]
    assert [d.code for d in found] == ["arm_unplannable"] * 3
    assert "'Each/0/Choose' on its else arm" in found[0].message
    assert "arc_unreachable" in found[0].message
    # Asked not to -- a replan where nothing it reads has changed -- it plans.
    assert schedule(workflow, blocked, document_path=document, check_arms=False).ok


def test_a_branch_only_on_the_other_arm_is_switched_with_it():
    workflow = yaml.safe_load(NESTED_MEASURED)
    wf, _ = parse_workflow(workflow, interface=None, assume=ASSUMED_ARM)
    chains = [chain for chain, _read, _errs in _arm_expansions(workflow, None, None, wf)]
    assert sorted(chains) == sorted([
        ((("H",), "else"),),
        ((("H",), "else"), (("H", "T"), "else")),
        ((("H", "S"), "else"),),
    ])


def test_an_inner_arm_that_cannot_be_planned_is_named_with_its_outer_one():
    env = _env()
    env["processes"].pop("polish")  # `finish` -- the outer else -- cannot be run
    report = schedule(yaml.safe_load(NESTED_MEASURED), env,
                      document_path={"interface": {"inputs": {"cup": "tray.a"},
                                                   "outputs": {"cup": "rack.a"}},
                                     "activities": []})
    found = [d.message for d in report.diagnostics if d.code == "arm_unplannable"]
    # The outer else on its own, and with the branch inside it switched too.
    assert len(found) == 2
    assert found[0].startswith("branch 'H' on its else arm could not")
    assert found[1].startswith("branch 'H' on its else arm with branch 'H/T' on its else arm")
    assert all("no_capability" in message for message in found)


# --- the mobility walk ---------------------------------------------------------------------


def test_a_wait_that_blocks_the_only_way_through_is_named():
    report = schedule(yaml.safe_load(STAGE_HELD), _env(),
                      document_path={"interface": _TWO, "activities": []})
    assert not report.ok
    (deadlock,) = [d for d in report.diagnostics if d.code == "objects_deadlocked"]
    assert "Without waiting for the condition of a branch (R)" in deadlock.message


def test_without_the_wait_the_same_laboratory_plans():
    # Stated `then`, the arm is known -- but it still waits: what changes it is the
    # cup having somewhere to wait other than the stage.
    env = _env()
    report = schedule(yaml.safe_load(SAMPLE_DECIDES), env,
                      document_path={"interface": _TWO, "activities": []})
    assert report.ok
