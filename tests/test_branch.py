"""Expanding a `branch` whose arm is known before the run (design.md D63, stage 1).

A branch takes one of two arms on a Boolean condition. Where the condition's value is
in the workflow (a literal) the scheduler reads the arm off it; where it is an entry
input -- or one element of one, for a branch inside a `map` -- the run holds the value
and states the arm in `expansion.arms`. The branch is then expanded as if its node
invoked the chosen arm's process: the arm's activity is the branch's own path, an
implicit `else` passes its Objects through. A condition produced during the run is
not known yet, and the workflow is refused.

Every base workflow here is valid v0 (checked against ofplang-validate below).
"""

from __future__ import annotations

import copy

import pytest
import yaml
from ofplang.validate import validate

from ofplang.schedule import JobInput, schedule, schedule_jobs, validate_document
from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.model import Arc, Endpoint, SourceRef
from ofplang.schedule.scheduler.workflow import parse_workflow, undecided_branches

# A cup is washed if it is dirty, polished if not.
WASH_OR_POLISH = """\
spec_version: "0.5"
types:
  Cup: {domain: object}
processes:
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
    inputs:
      cup: {type: Cup, phase: data}
      dirty: {type: Bool, phase: run}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - id: H
          kind: branch
          condition: {from: inputs.dirty}
          args: {cup: {from: inputs.cup}}
          then: {process: wash}
          else: {process: polish}
      returns: {cup: {from: H.cup}}
entry: main
"""

# Washed only if dirty: the implicit `else` returns the cup as it came.
WASH_IF_DIRTY = WASH_OR_POLISH.replace("          else: {process: polish}\n", "")

# The same choice for each of a rack of cups, one flag per cup.
PER_CUP = """\
spec_version: "0.5"
types:
  Cup: {domain: object}
processes:
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
  handle:
    kind: composite
    inputs:
      cup: {type: Cup, phase: data}
      dirty: {type: Bool, phase: run}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - id: H
          kind: branch
          condition: {from: inputs.dirty}
          args: {cup: {from: inputs.cup}}
          then: {process: wash}
          else: {process: polish}
      returns: {cup: {from: H.cup}}
  main:
    kind: composite
    inputs:
      cups: {type: "Array<Cup>", phase: data}
      dirty: {type: "Array<Bool>", phase: run}
    outputs: {cups: {type: "Array<Cup>", phase: data}}
    body:
      nodes:
        - id: M
          kind: map
          process: handle
          each: {cup: {from: inputs.cups}, dirty: {from: inputs.dirty}}
      returns: {cups: {from: M.cup}}
entry: main
"""

# The arm decided by a literal: `handle` is invoked with `dirty` bound to `true`.
LITERAL = """\
spec_version: "0.5"
types:
  Cup: {domain: object}
processes:
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
  handle:
    kind: composite
    inputs:
      cup: {type: Cup, phase: data}
      dirty: {type: Bool, phase: run}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - id: H
          kind: branch
          condition: {from: inputs.dirty}
          args: {cup: {from: inputs.cup}}
          then: {process: wash}
          else: {process: polish}
      returns: {cup: {from: H.cup}}
  main:
    kind: composite
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - {id: C, process: handle, state: {cup: {from: inputs.cup}}, bind: {dirty: {value: true}}}
      returns: {cup: {from: C.cup}}
entry: main
"""

# The condition is measured during the run: not known before it.
MEASURED = """\
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
        - {id: I, process: inspect, state: {cup: {from: inputs.cup}}}
        - id: H
          kind: branch
          condition: {from: I.dirty}
          args: {cup: {from: I.cup}}
          then: {process: wash}
          else: {process: polish}
      returns: {cup: {from: H.cup}}
entry: main
"""

# A branch inside an arm: a dirty cup is soaked first if it is also stained.
NESTED = """\
spec_version: "0.5"
types:
  Cup: {domain: object}
processes:
  soak:
    kind: atomic
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
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
  clean:
    kind: composite
    inputs:
      cup: {type: Cup, phase: data}
      stained: {type: Bool, phase: run}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - id: S
          kind: branch
          condition: {from: inputs.stained}
          args: {cup: {from: inputs.cup}}
          then: {process: soak}
        - {id: W, process: wash, state: {cup: {from: S.cup}}}
      returns: {cup: {from: W.cup}}
  finish:
    kind: composite
    inputs:
      cup: {type: Cup, phase: data}
      stained: {type: Bool, phase: run}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - {id: P, process: polish, state: {cup: {from: inputs.cup}}}
      returns: {cup: {from: P.cup}}
  main:
    kind: composite
    inputs:
      cup: {type: Cup, phase: data}
      dirty: {type: Bool, phase: run}
      stained: {type: Bool, phase: run}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - id: H
          kind: branch
          condition: {from: inputs.dirty}
          args: {cup: {from: inputs.cup}, stained: {from: inputs.stained}}
          then: {process: clean}
          else: {process: finish}
      returns: {cup: {from: H.cup}}
entry: main
"""

ENV = """
time: {unit: second}
devices:
  - {id: loader, spots: [a, b]}
  - {id: sink, spots: [basin]}
  - {id: buffer, spots: [pad]}
  - {id: tub, spots: [bath]}
  - {id: rack, spots: [a, b]}
transporters: [{id: arm}]
transports:
  - {transporter: arm, from: loader.a, to: sink.basin, duration: 1}
  - {transporter: arm, from: loader.b, to: sink.basin, duration: 1}
  - {transporter: arm, from: loader.a, to: buffer.pad, duration: 1}
  - {transporter: arm, from: loader.b, to: buffer.pad, duration: 1}
  - {transporter: arm, from: loader.a, to: tub.bath, duration: 1}
  - {transporter: arm, from: tub.bath, to: sink.basin, duration: 1}
  - {transporter: arm, from: sink.basin, to: rack.a, duration: 1}
  - {transporter: arm, from: sink.basin, to: rack.b, duration: 1}
  - {transporter: arm, from: buffer.pad, to: rack.a, duration: 1}
  - {transporter: arm, from: buffer.pad, to: rack.b, duration: 1}
  - {transporter: arm, from: loader.a, to: rack.a, duration: 1}
processes:
  wash:
    modes:
      - {devices: [sink], duration: 5,
         input_spots: {cup: sink.basin}, output_spots: {cup: sink.basin}}
  polish:
    modes:
      - {devices: [buffer], duration: 3,
         input_spots: {cup: buffer.pad}, output_spots: {cup: buffer.pad}}
  soak:
    modes:
      - {devices: [tub], duration: 8,
         input_spots: {cup: tub.bath}, output_spots: {cup: tub.bath}}
"""

ONE_CUP = {"inputs": {"cup": "loader.a"}, "outputs": {"cup": "rack.a"}}


def arms(*entries) -> dict:
    return {"arms": [{"node": list(node), "arm": arm} for node, arm in entries]}


def _parse(text, expansion=None, interface=None):
    wf, diags = parse_workflow(yaml.safe_load(text), interface=interface, expansion=expansion)
    return wf, [d.code for d in diags.items if d.severity == ERROR]


@pytest.mark.parametrize(
    "text", [WASH_OR_POLISH, WASH_IF_DIRTY, PER_CUP, LITERAL, MEASURED, NESTED],
    ids=["wash_or_polish", "wash_if_dirty", "per_cup", "literal", "measured", "nested"],
)
def test_the_fixtures_are_valid_v0(text):
    result = validate(yaml.safe_load(text))
    assert result.ok, [(d.code, d.message) for d in result.diagnostics]


# --- the chosen arm is invoked at the branch's own path ------------------------------------


def test_the_then_arm_is_the_activity_at_the_branch_path():
    wf, errs = _parse(WASH_OR_POLISH, arms((("H",), "then")))
    assert errs == []
    assert [(a.path, a.process) for a in wf.activities] == [(("H",), "wash")]
    assert wf.entry_arcs == (Arc(Endpoint((), "cup"), Endpoint(("H",), "cup")),)
    assert wf.exit_arcs == (Arc(Endpoint(("H",), "cup"), Endpoint((), "cup")),)
    assert wf.arms == {("H",): "then"}


def test_the_else_arm_likewise():
    wf, errs = _parse(WASH_OR_POLISH, arms((("H",), "else")))
    assert errs == [] and [(a.path, a.process) for a in wf.activities] == [(("H",), "polish")]


def test_an_implicit_else_passes_the_cup_through():
    wf, errs = _parse(WASH_IF_DIRTY, arms((("H",), "else")))
    assert errs == [] and wf.activities == () and wf.arms == {("H",): "else"}
    # Entry to exit, untouched: a through arc (D60).
    assert wf.through_arcs == (Arc(Endpoint((), "cup"), Endpoint((), "cup")),)


def test_each_element_takes_its_own_arm():
    cups = {"inputs": {"cups": ["loader.a", "loader.b"]}}
    expansion = {"lengths": [], **arms((("M", 0, "H"), "then"), (("M", 1, "H"), "else"))}
    wf, errs = _parse(PER_CUP, expansion, cups)
    assert errs == []
    assert [(a.path, a.process) for a in wf.activities] == [
        (("M", 0, "H"), "wash"), (("M", 1, "H"), "polish"),
    ]


# --- deciding the arm ----------------------------------------------------------------------


def test_an_entry_condition_with_no_stated_arm_is_refused_and_reported_for_the_run():
    wf, errs = _parse(WASH_OR_POLISH)
    assert errs == ["branch_arm_unknown"]
    assert undecided_branches(yaml.safe_load(WASH_OR_POLISH)) == {("H",): SourceRef((), "dirty")}


def test_per_element_conditions_are_reported_per_element():
    cups = {"inputs": {"cups": ["loader.a", "loader.b"]}}
    assert undecided_branches(yaml.safe_load(PER_CUP), interface=cups) == {
        ("M", 0, "H"): SourceRef((), "dirty", (0,)),
        ("M", 1, "H"): SourceRef((), "dirty", (1,)),
    }


def test_a_branch_inside_an_arm_appears_once_the_outer_arm_is_stated():
    doc = yaml.safe_load(NESTED)
    assert undecided_branches(doc) == {("H",): SourceRef((), "dirty")}
    assert undecided_branches(doc, expansion=arms((("H",), "then"))) == {
        ("H", "S"): SourceRef((), "stained")
    }
    # The else arm has no inner branch: nothing more to decide.
    assert undecided_branches(doc, expansion=arms((("H",), "else"))) == {}
    wf, errs = _parse(NESTED, arms((("H",), "then"), (("H", "S"), "then")))
    assert errs == [] and [a.path for a in wf.activities] == [("H", "S"), ("H", "W")]


def test_a_literal_condition_is_decided_by_the_scheduler():
    wf, errs = _parse(LITERAL)
    assert errs == [] and [(a.path, a.process) for a in wf.activities] == [(("C", "H"), "wash")]
    _, errs = _parse(LITERAL, arms((("C", "H"), "then")))
    assert errs == []  # stated and agreeing: fine
    _, errs = _parse(LITERAL, arms((("C", "H"), "else")))
    assert errs == ["arm_mismatch"]


def test_a_measured_condition_is_not_known_before_the_run():
    # Not without an arm to assume (design.md D64): the reader never picks one itself.
    _, errs = _parse(MEASURED)
    assert errs == ["branch_arm_unknown"]


def test_an_arm_stated_for_a_measured_condition_is_taken_and_still_waits():
    # The run states it once the value exists (D64; D63 A is lifted). Holding it to
    # the value is the run's part; the arm still waits for the measurement.
    wf, errs = _parse(MEASURED, arms((("H",), "else")))
    assert errs == []
    assert [a.path for a in wf.activities] == [("I",), ("H",)]
    assert wf.activities[1].process == "polish"
    gate = wf.branch_gates[("H",)]
    assert (gate.condition.node, gate.condition.port, gate.arm, gate.assumed) == (
        ("I",), "dirty", "else", False
    )
    assert (("I",), ("H",)) in wf.precedence
    (arc,) = [a for a in wf.arcs if a.dst.node == ("H",)]
    assert wf.arc_gates[arc] == frozenset({("I",)})


# --- stated arms that name no branch -------------------------------------------------------


def test_a_stated_arm_that_names_no_branch_is_refused():
    _, errs = _parse(WASH_OR_POLISH, arms((("H",), "then"), (("X",), "then")))
    assert errs == ["arm_unknown_node"]
    cups = {"inputs": {"cups": ["loader.a", "loader.b"]}}
    expansion = arms((("M", 0, "H"), "then"), (("M", 1, "H"), "then"), (("M", 5, "H"), "then"))
    _, errs = _parse(PER_CUP, expansion, cups)
    assert errs == ["arm_unknown_node"]  # no fifth cup


def test_a_branch_inside_the_arm_not_taken_is_passed_over():
    _, errs = _parse(NESTED, arms((("H",), "else"), (("H", "S"), "then")))
    assert errs == []


# --- the document's shape ------------------------------------------------------------------


def _codes(expansion) -> list[str]:
    result = validate_document({"expansion": expansion, "activities": []})
    return [d.code for d in result.diagnostics]


def test_the_section_validates():
    assert _codes(arms((("H",), "then"), (("M", 0, "H"), "else"))) == []
    assert _codes({"arms": [{"node": ["H"], "arm": "maybe"}]}) == ["unknown_arm"]
    assert _codes({"arms": [{"node": [], "arm": "then"}]}) == ["empty_node_path"]
    assert _codes(arms((("H",), "then"), (("H",), "else"))) == ["duplicate_arm"]
    assert _codes({"arms": [{"node": ["H"], "arm": "then", "x": 1}]}) == ["unknown_key"]


# --- plan, replan, joint plan --------------------------------------------------------------


def _all_done(plan) -> dict:
    status = copy.deepcopy(plan)
    for activity in status["activities"]:
        activity["status"] = "completed"
    status["now"] = max(a["end"] for a in status["activities"])
    return status


def test_it_plans_echoes_and_replans():
    document = {"interface": ONE_CUP, "expansion": arms((("H",), "then")), "activities": []}
    report = schedule(yaml.safe_load(WASH_OR_POLISH), yaml.safe_load(ENV),
                      document_path=document, random_seed=0)
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]
    processing = [a for a in report.plan["activities"] if a["kind"] == "processing"]
    assert [(a["node"], a["process"]) for a in processing] == [(["H"], "wash")]
    assert report.plan["expansion"] == arms((("H",), "then"))
    assert validate_document(report.plan).ok
    again = schedule(yaml.safe_load(WASH_OR_POLISH), yaml.safe_load(ENV),
                     document_path=_all_done(report.plan), random_seed=0)
    assert again.ok, [d.code for d in again.diagnostics]


def test_each_job_takes_its_own_arm():
    workflow = yaml.safe_load(WASH_OR_POLISH)
    document = {
        "jobs": [
            {"id": "a", "expansion": arms((("H",), "then")),
             "interface": {"inputs": {"cup": "loader.a"}, "outputs": {"cup": "rack.a"}}},
            {"id": "b", "expansion": arms((("H",), "else")),
             "interface": {"inputs": {"cup": "loader.b"}, "outputs": {"cup": "rack.b"}}},
        ],
        "activities": [],
    }
    report = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        yaml.safe_load(ENV), document_path=document, random_seed=0,
    )
    assert report.ok, [d.code for d in report.diagnostics]
    made = sorted((a["job"], a["process"]) for a in report.plan["activities"]
                  if a["kind"] == "processing")
    assert made == [("a", "wash"), ("b", "polish")]


# --- the reader's guards ---------------------------------------------------------------------


def test_an_argument_no_arm_takes_is_refused_as_validate_refuses_it():
    doc = yaml.safe_load(WASH_OR_POLISH)
    doc["processes"]["main"]["body"]["nodes"][0]["args"]["extra"] = {"from": "inputs.dirty"}
    _, diags = parse_workflow(copy.deepcopy(doc), expansion=arms((("H",), "then")))
    codes = {d.code for d in diags.items if d.severity == ERROR}
    assert codes == {"binding_port_not_found"}
    assert codes <= {d.code for d in validate(doc).diagnostics}


def test_a_data_output_a_branch_does_not_expose_is_refused():
    # Both arms report how long they took; with no outputs section a branch exposes
    # no Data output (v0 20.3), so reading it is output_not_exposed -- as validate says.
    doc = yaml.safe_load(WASH_OR_POLISH)
    for name in ("wash", "polish"):
        doc["processes"][name]["outputs"]["took"] = {"type": "Int", "phase": "data"}
    doc["processes"]["main"]["outputs"]["took"] = {"type": "Int", "phase": "data"}
    doc["processes"]["main"]["body"]["returns"]["took"] = {"from": "H.took"}
    _, diags = parse_workflow(copy.deepcopy(doc), expansion=arms((("H",), "then")))
    by_validate = sorted({d.code for d in validate(doc).diagnostics if d.severity == "error"})
    by_reader = [d.code for d in diags.items if d.severity == ERROR]
    assert by_reader == ["output_not_exposed"] == by_validate
    # Listed as common, it is exposed, and read from the chosen arm.
    doc["processes"]["main"]["body"]["nodes"][0]["outputs"] = {
        "cup": {"mode": "common"}, "took": {"mode": "common"}
    }
    wf, diags = parse_workflow(copy.deepcopy(doc), expansion=arms((("H",), "else")))
    assert [d.code for d in diags.items if d.severity == ERROR] == []
    assert wf.output_sources["took"] == SourceRef(("H",), "took")
