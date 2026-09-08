"""Interface boundary condition (SPECIFICATIONS.md §6.8): pinning a workflow's
Object-bearing entry inputs to spots constrains the upstream activity's mode.

These drive `schedule()` end to end on small hand-built inputs. The key fixture is
a one-step workflow whose entry input `sample` feeds `heat`, and an environment
where `heat` has two modes at two different spots with no transporter route
between them — so the interface spot fully determines which mode is feasible.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ofplang.schedule import schedule, validate_document
from ofplang.schedule.scheduler.plan import to_yaml
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# A workflow: entry input `sample` -> heat.plate; heat.out -> output `result`.
WORKFLOW = """
spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  heat:
    kind: atomic
    inputs:  { plate: { type: Sample, phase: data } }
    outputs: { out:   { type: Sample, phase: data } }
    objects: { transform: [inputs.plate, outputs.out] }
  main:
    kind: composite
    inputs:  { sample: { type: Sample, phase: data } }
    outputs: { result: { type: Sample, phase: data } }
    body:
      nodes:
        - id: Heat
          process: heat
          state: { plate: { from: inputs.sample } }
      returns: { result: { from: Heat.out } }
entry: main
"""

# heat has two modes at two spots; `arm` has an empty transports table, so it can
# only do 0-distance moves (slot_a <-> slot_b is impossible). slot_c is reachable
# by no mode and no transport.
ENV = """
time: { unit: second }
devices:
  - id: rack
    spots: [slot_a, slot_b, slot_c]
transporters:
  - id: arm
transports: []
processes:
  heat:
    modes:
      - id: at_a
        devices: [rack]
        duration: 10
        input_spots:  { plate: rack.slot_a }
        output_spots: { out: rack.slot_a }
      - id: at_b
        devices: [rack]
        duration: 10
        input_spots:  { plate: rack.slot_b }
        output_spots: { out: rack.slot_b }
"""


def _write(tmp_path, workflow=WORKFLOW, env=ENV, document=None):
    wf = tmp_path / "wf.yaml"
    wf.write_text(workflow, encoding="utf-8")
    ev = tmp_path / "env.yaml"
    ev.write_text(env, encoding="utf-8")
    doc = None
    if document is not None:
        doc = tmp_path / "doc.yaml"
        doc.write_text(document, encoding="utf-8")
    return wf, ev, doc


def _iface(spot, port="sample"):
    return f"interface:\n  inputs: {{ {port}: {spot} }}\nactivities: []\n"


def test_workflow_captures_boundary_bindings(tmp_path):
    wf, _, _ = _write(tmp_path)
    workflow, diags = parse_workflow(wf)
    assert not diags.items
    assert set(workflow.entry_inputs) == {"sample"}
    assert workflow.entry_inputs["sample"].node == ("Heat",)
    assert workflow.entry_inputs["sample"].port == "plate"
    assert workflow.exit_outputs["result"].node == ("Heat",)


def test_interface_input_required(tmp_path):
    # An Object-bearing entry input with no interface binding is an error: its
    # consumer's mode would otherwise be unconstrained (SPEC §6.8).
    wf, ev, _ = _write(tmp_path)
    report = schedule(wf, ev)
    assert not report.ok
    assert "interface_input_missing" in {d.code for d in report.diagnostics}


def test_interface_constrains_mode_to_slot_a(tmp_path):
    wf, ev, doc = _write(tmp_path, document=_iface("rack.slot_a"))
    report = schedule(wf, ev, document_path=doc)
    assert report.ok and report.outcome == "optimal"
    assert report.makespan == 10

    processing = [a for a in report.plan["activities"] if a["kind"] == "processing"]
    (heat,) = processing
    assert heat["mode"] == "at_a"  # constrained by the sample's actual position

    # A boundary transport bridges the interface spot to the consumer; here it is a
    # 0-distance no-op (same spot), so the transporter is omitted. `result` is not
    # bound, so it too has a boundary transport -- to the spot the scheduler chose,
    # which with nothing competing for it is where `heat` left it (§6.8).
    transports = [a for a in report.plan["activities"] if a["kind"] == "transport"]
    entry = [a for a in transports if a["arc"]["from"]["node"] == []]
    delivery = [a for a in transports if a["arc"]["to"]["node"] == []]
    assert len(transports) == 2
    (out,) = delivery
    assert (out["from_spot"], out["to_spot"]) == ("rack.slot_a", "rack.slot_a")
    assert out["start"] == out["end"] == 10
    (t,) = entry
    assert t["from_spot"] == "rack.slot_a" and t["to_spot"] == "rack.slot_a"
    assert "transporter" not in t
    assert t["arc"]["from"] == {"node": [], "port": "sample"}  # empty-path = the workflow interface
    assert t["arc"]["to"] == {"node": ["Heat"], "port": "plate"}

    # interface round-trips (echoed in the output).
    assert report.plan["interface"] == {"inputs": {"sample": "rack.slot_a"}}


def test_interface_constrains_mode_to_slot_b(tmp_path):
    wf, ev, doc = _write(tmp_path, document=_iface("rack.slot_b"))
    report = schedule(wf, ev, document_path=doc)
    assert report.ok
    (heat,) = [a for a in report.plan["activities"] if a["kind"] == "processing"]
    assert heat["mode"] == "at_b"


def test_interface_unreachable_spot_is_infeasible(tmp_path):
    # slot_c is served by no mode and no transport -> the boundary arc is unreachable.
    wf, ev, doc = _write(tmp_path, document=_iface("rack.slot_c"))
    report = schedule(wf, ev, document_path=doc)
    assert not report.ok
    assert "arc_unreachable" in {d.code for d in report.diagnostics}


def test_interface_unknown_port(tmp_path):
    wf, ev, doc = _write(tmp_path, document=_iface("rack.slot_a", port="nope"))
    report = schedule(wf, ev, document_path=doc)
    assert not report.ok
    assert "interface_unknown_port" in {d.code for d in report.diagnostics}


def test_interface_unknown_spot(tmp_path):
    wf, ev, doc = _write(tmp_path, document=_iface("rack.slot_z"))
    report = schedule(wf, ev, document_path=doc)
    assert not report.ok
    assert "unknown_spot" in {d.code for d in report.diagnostics}


# A workflow with a Pure Data entry input `knob` (bound to heat via `bind`).
WORKFLOW_PURE_DATA = """
spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  heat:
    kind: atomic
    inputs:  { plate: { type: Sample, phase: data }, knob: { type: Int, phase: data } }
    outputs: { out:   { type: Sample, phase: data } }
    objects: { transform: [inputs.plate, outputs.out] }
  main:
    kind: composite
    inputs:  { sample: { type: Sample, phase: data }, knob: { type: Int, phase: data } }
    outputs: { result: { type: Sample, phase: data } }
    body:
      nodes:
        - id: Heat
          process: heat
          state: { plate: { from: inputs.sample } }
          bind:  { knob:  { from: inputs.knob } }
      returns: { result: { from: Heat.out } }
entry: main
"""


def test_interface_pure_data_port(tmp_path):
    # `knob` is a Pure Data entry input; it occupies no spot, so binding it errors.
    # (sample, the Object entry input, is bound so only the Pure Data error fires.)
    doc = "interface:\n  inputs: { sample: rack.slot_a, knob: rack.slot_b }\nactivities: []\n"
    wf, ev, docp = _write(tmp_path, workflow=WORKFLOW_PURE_DATA, document=doc)
    report = schedule(wf, ev, document_path=docp)
    assert not report.ok
    assert "interface_pure_data_port" in {d.code for d in report.diagnostics}


# A workflow with two Object-bearing entry inputs feeding one two-input process.
WORKFLOW_TWO_INPUTS = """
spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  mix:
    kind: atomic
    inputs:  { a: { type: Sample, phase: data }, b: { type: Sample, phase: data } }
    outputs: { out: { type: Sample, phase: data } }
    objects: { consume: [inputs.b], transform: [inputs.a, outputs.out] }
  main:
    kind: composite
    inputs:  { s1: { type: Sample, phase: data }, s2: { type: Sample, phase: data } }
    outputs: { result: { type: Sample, phase: data } }
    body:
      nodes:
        - id: Mix
          process: mix
          state: { a: { from: inputs.s1 }, b: { from: inputs.s2 } }
      returns: { result: { from: Mix.out } }
entry: main
"""

ENV_TWO_INPUTS = """
time: { unit: second }
devices:
  - id: rack
    spots: [slot_a, slot_b]
transporters:
  - id: arm
transports: []
processes:
  mix:
    modes:
      - id: m0
        devices: [rack]
        duration: 5
        input_spots:  { a: rack.slot_a, b: rack.slot_b }
        output_spots: { out: rack.slot_a }
"""


def test_interface_duplicate_spot(tmp_path):
    # Two entry inputs bound to the same spot: two Objects cannot occupy one spot.
    doc = "interface:\n  inputs: { s1: rack.slot_a, s2: rack.slot_a }\nactivities: []\n"
    wf, ev, docp = _write(tmp_path, workflow=WORKFLOW_TWO_INPUTS, env=ENV_TWO_INPUTS, document=doc)
    report = schedule(wf, ev, document_path=docp)
    assert not report.ok
    assert "interface_duplicate_spot" in {d.code for d in report.diagnostics}


# --- output boundary (phase 1b) -------------------------------------------------

# A create-based workflow (no entry input, so no input binding is required): `make`
# creates the final output `result`, which its two modes place at slot_a / slot_b.
WORKFLOW_OUT = """
spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  make:
    kind: atomic
    outputs: { out: { type: Sample, phase: data } }
    objects: { create: [outputs.out] }
  main:
    kind: composite
    inputs: {}
    outputs: { result: { type: Sample, phase: data } }
    body:
      nodes:
        - id: Make
          process: make
      returns: { result: { from: Make.out } }
entry: main
"""

ENV_OUT = """
time: { unit: second }
devices:
  - id: rack
    spots: [slot_a, slot_b, slot_c]
transporters:
  - id: arm
transports: []
processes:
  make:
    modes:
      - { id: at_a, devices: [rack], duration: 10, output_spots: { out: rack.slot_a } }
      - { id: at_b, devices: [rack], duration: 10, output_spots: { out: rack.slot_b } }
"""


def _oface(spot, port="result"):
    return f"interface:\n  outputs: {{ {port}: {spot} }}\nactivities: []\n"


def _write_out(tmp_path, document):
    return _write(tmp_path, workflow=WORKFLOW_OUT, env=ENV_OUT, document=document)


def test_interface_output_constrains_mode_and_emits_boundary_transport(tmp_path):
    # Delivering `result` to slot_a is only reachable from mode at_a (out at slot_a);
    # no transporter can move slot_b -> slot_a, so make must run at_a.
    wf, ev, doc = _write_out(tmp_path, _oface("rack.slot_a"))
    report = schedule(wf, ev, document_path=doc)
    assert report.ok and report.outcome == "optimal"
    (make,) = [a for a in report.plan["activities"] if a["kind"] == "processing"]
    assert make["mode"] == "at_a"

    (t,) = [a for a in report.plan["activities"] if a["kind"] == "transport"]
    assert t["from_spot"] == "rack.slot_a" and t["to_spot"] == "rack.slot_a"
    assert t["arc"]["from"] == {"node": ["Make"], "port": "out"}
    assert t["arc"]["to"] == {"node": [], "port": "result"}  # empty-path = the workflow interface
    assert report.plan["interface"] == {"outputs": {"result": "rack.slot_a"}}


def test_interface_output_slot_b(tmp_path):
    wf, ev, doc = _write_out(tmp_path, _oface("rack.slot_b"))
    report = schedule(wf, ev, document_path=doc)
    assert report.ok
    (make,) = [a for a in report.plan["activities"] if a["kind"] == "processing"]
    assert make["mode"] == "at_b"


def test_interface_input_and_output_combined(tmp_path):
    # WORKFLOW's heat runs in-place at a single spot, so binding both sample and
    # result to slot_a is consistent (heat = at_a): an input and an output boundary
    # transport, both 0-distance.
    doc = (
        "interface:\n  inputs: { sample: rack.slot_a }\n"
        "  outputs: { result: rack.slot_a }\nactivities: []\n"
    )
    wf, ev, docp = _write(tmp_path, document=doc)
    report = schedule(wf, ev, document_path=docp)
    assert report.ok
    (heat,) = [a for a in report.plan["activities"] if a["kind"] == "processing"]
    assert heat["mode"] == "at_a"
    transports = [a for a in report.plan["activities"] if a["kind"] == "transport"]
    assert len(transports) == 2


def test_interface_output_unknown_port(tmp_path):
    wf, ev, doc = _write_out(tmp_path, _oface("rack.slot_a", port="nope"))
    report = schedule(wf, ev, document_path=doc)
    assert not report.ok
    assert "interface_unknown_port" in {d.code for d in report.diagnostics}


def test_interface_output_unreachable(tmp_path):
    wf, ev, doc = _write_out(tmp_path, _oface("rack.slot_c"))
    report = schedule(wf, ev, document_path=doc)
    assert not report.ok
    assert "arc_unreachable" in {d.code for d in report.diagnostics}


# `make` produces the final output at slot_a and delivers early. `after` also needs
# slot_a but is held until t=20 by a Pure Data dependency on `long` (a precedence
# edge, no transport), so it can only run *after* make's delivery. Used to show a
# bound final output holds its spot to the makespan (not just until it is produced).
WORKFLOW_HOLD = """
spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  make:
    kind: atomic
    outputs: { out: { type: Sample, phase: data } }
    objects: { create: [outputs.out] }
  long:
    kind: atomic
    outputs: { tick: { type: Int, phase: data } }
  after:
    kind: atomic
    inputs:  { go:   { type: Int, phase: data } }
    outputs: { done: { type: Sample, phase: data } }
    objects: { create: [outputs.done] }
  main:
    kind: composite
    inputs: {}
    outputs: { result: { type: Sample, phase: data } }
    body:
      nodes:
        - id: Make
          process: make
        - id: Long
          process: long
        - id: After
          process: after
          bind: { go: { from: Long.tick } }
      returns: { result: { from: Make.out } }
entry: main
"""

ENV_HOLD = """
time: { unit: second }
devices:
  - id: dev_a
    spots: [slot_a]
  - id: dev_b
    spots: [slot_b]
transporters:
  - id: arm
transports: []
processes:
  make:
    modes:
      - { devices: [dev_a], duration: 5, output_spots: { out: dev_a.slot_a } }
  long:
    modes:
      - { devices: [dev_b], duration: 20 }
  after:
    modes:
      - { devices: [dev_a], duration: 5, output_spots: { done: dev_a.slot_a } }
"""


# ENV_HOLD with one route out of slot_a, so an unbound result has somewhere to go.
ENV_HOLD_ESCAPE = ENV_HOLD.replace(
    "transports: []",
    "transports:\n  - { transporter: arm, from: dev_a.slot_a, to: dev_b.slot_b, duration: 1 }",
)


def test_a_delivered_object_holds_its_spot_bound_or_not(tmp_path):
    """A final output holds its spot to the end of the plan either way (§6.8): a
    binding says *where* it rests, not *whether* it does.

    `make` (5) would like to run early on slot_a and let `after` (5, held to [20,25]
    by its dependency on `long`) have it afterwards -- makespan 25. It cannot: its
    result is still sitting there. So `make` is pushed past `after` and the makespan
    is 30, and that increase is the observable effect of the hold.

    🔴 The unbound case used to give 25, on the strength of the plate vanishing when
    its producer ended. That was the hole: the plan packed two Objects onto one spot,
    and nothing in the document said otherwise.
    """
    # Unbound, and nowhere to move it (ENV_HOLD declares no transports at all), so
    # the only spot it can rest on is the one it was produced on.
    wf, ev, _ = _write(tmp_path, workflow=WORKFLOW_HOLD, env=ENV_HOLD)
    unbound = schedule(wf, ev)
    assert unbound.ok and unbound.makespan == 30
    assert "interface_output_unbound" in {d.code for d in unbound.diagnostics}

    # Bound to that same spot: the identical instance, said out loud.
    wf, ev, doc = _write(
        tmp_path, workflow=WORKFLOW_HOLD, env=ENV_HOLD, document=_oface("dev_a.slot_a")
    )
    bound = schedule(wf, ev, document_path=doc)
    assert bound.ok and bound.outcome == "optimal"
    assert bound.makespan == 30
    assert "interface_output_unbound" not in {d.code for d in bound.diagnostics}


def test_an_unbound_output_steps_aside_when_the_spot_is_wanted(tmp_path):
    """What the scheduler's choice buys (§6.8). Given one route off slot_a, the
    unbound result is carried to slot_b and `after` gets its spot -- makespan 26
    rather than 30. Binding it to slot_a forbids exactly that, which is the
    difference between "rest anywhere" and "rest here".

    26 and not 25: the move holds its destination device as well as its source
    (§4.5), and `long` is on dev_b until 20, so the plate cannot leave before then.
    """
    wf, ev, _ = _write(tmp_path, workflow=WORKFLOW_HOLD, env=ENV_HOLD_ESCAPE)
    report = schedule(wf, ev)
    assert report.ok and report.outcome == "optimal"
    assert report.makespan == 26

    (delivery,) = [
        a
        for a in report.plan["activities"]
        if a["kind"] == "transport" and a["arc"]["to"]["node"] == []
    ]
    assert (delivery["from_spot"], delivery["to_spot"]) == ("dev_a.slot_a", "dev_b.slot_b")

    # Bound to where it was produced, the move is forbidden and the makespan is back
    # to 30 -- so the 25 above really is the step aside, not some other rearrangement.
    wf, ev, doc = _write(
        tmp_path,
        workflow=WORKFLOW_HOLD,
        env=ENV_HOLD_ESCAPE,
        document=_oface("dev_a.slot_a"),
    )
    pinned = schedule(wf, ev, document_path=doc)
    assert pinned.ok and pinned.makespan == 30


def test_a_returned_object_holds_a_spot_with_no_interface_section(tmp_path):
    """The workflow here has no Object-bearing entry input, so it has no reason to
    carry an `interface` section at all -- and its final output is still bound, to a
    spot the scheduler chooses. The boundary output is built either way; only the
    input side needs a binding to exist."""
    wf, ev, _ = _write(tmp_path, workflow=WORKFLOW_HOLD, env=ENV_HOLD)
    report = schedule(wf, ev)
    assert report.ok
    assert "interface" not in report.plan  # nothing was supplied, nothing is echoed
    (delivery,) = [
        a
        for a in report.plan["activities"]
        if a["kind"] == "transport" and a["arc"]["to"]["node"] == []
    ]
    assert delivery["arc"]["to"] == {"node": [], "port": "result"}
    assert delivery["start"] == delivery["end"]  # it rests where it was made


# --- replan with interface (phase 1c) -------------------------------------------

def test_replan_with_interface_input(tmp_path):
    # Initial plan with the input boundary (sample -> slot_a): heat at_a plus a
    # 0-distance boundary transport.
    wf, ev, doc = _write(tmp_path, document=_iface("rack.slot_a"))
    initial = schedule(wf, ev, document_path=doc)
    assert initial.ok

    # Feed the plan back as a replanning status at now=5: the boundary transport is
    # completed and heat is running. interface rides along in the plan already.
    status = dict(initial.plan)
    status["now"] = 5
    for a in status["activities"]:
        if a["kind"] != "transport":
            a["status"] = "running"
        elif a["arc"]["from"]["node"] == []:
            a["status"] = "completed"  # the entry move, at time 0
        # The delivery of the unbound `result` is at 10, still to come: left pending.
    sp = tmp_path / "status.yaml"
    sp.write_text(yaml.safe_dump(status), encoding="utf-8")

    replan = schedule(wf, ev, document_path=sp)
    assert replan.ok, [d.code for d in replan.diagnostics]
    # heat stays pinned to at_a (running); the committed boundary leg is preserved;
    # interface round-trips.
    (heat,) = [a for a in replan.plan["activities"] if a["kind"] == "processing"]
    assert heat["mode"] == "at_a" and heat["status"] == "running"
    assert replan.makespan == 10
    assert replan.plan["interface"] == {"inputs": {"sample": "rack.slot_a"}}
    (t,) = [
        a
        for a in replan.plan["activities"]
        if a["kind"] == "transport" and a["arc"]["from"]["node"] == []
    ]
    assert t["arc"]["from"] == {"node": [], "port": "sample"}


def test_replan_with_interface_input_pending(tmp_path):
    # Replan before anything started (now=0, no statuses): the boundary input arc is
    # rebuilt from scratch and heat is still constrained to at_a.
    wf, ev, doc = _write(tmp_path, document=_iface("rack.slot_a"))
    initial = schedule(wf, ev, document_path=doc)
    status = dict(initial.plan)
    status["now"] = 0
    # Drop per-activity statuses so everything is re-derived (pending).
    for a in status["activities"]:
        a.pop("status", None)
    sp = tmp_path / "status.yaml"
    sp.write_text(yaml.safe_dump(status), encoding="utf-8")

    replan = schedule(wf, ev, document_path=sp)
    assert replan.ok, [d.code for d in replan.diagnostics]
    (heat,) = [a for a in replan.plan["activities"] if a["kind"] == "processing"]
    assert heat["mode"] == "at_a"


def test_replan_with_interface_output(tmp_path):
    # Output boundary: after make and its delivery complete, a replan keeps the
    # boundary intact (output node re-created, committed leg preserved).
    wf, ev, doc = _write_out(tmp_path, _oface("rack.slot_a"))
    initial = schedule(wf, ev, document_path=doc)
    assert initial.ok
    status = dict(initial.plan)
    status["now"] = 10
    for a in status["activities"]:
        a["status"] = "completed"
    sp = tmp_path / "status.yaml"
    sp.write_text(yaml.safe_dump(status), encoding="utf-8")

    replan = schedule(wf, ev, document_path=sp)
    assert replan.ok, [d.code for d in replan.diagnostics]
    (make,) = [a for a in replan.plan["activities"] if a["kind"] == "processing"]
    assert make["mode"] == "at_a" and make["status"] == "completed"
    assert replan.plan["interface"] == {"outputs": {"result": "rack.slot_a"}}


# --- committed example ----------------------------------------------------------

def test_interface_load_example_end_to_end(tmp_path):
    # The committed interface example: sample loaded on loader, heated, result
    # delivered to output. Boundary transports move it in and out.
    report = schedule(
        EXAMPLES / "interface_load.workflow.yaml",
        EXAMPLES / "interface_load.env.yaml",
        document_path=EXAMPLES / "interface_load.document.yaml",
    )
    assert report.ok and report.outcome == "optimal"
    assert report.makespan == 14  # load(2) + heat(10) + deliver(2)

    transports = [a for a in report.plan["activities"] if a["kind"] == "transport"]
    endpoints = [(t["arc"]["from"], t["arc"]["to"]) for t in transports]
    assert ({"node": [], "port": "sample"}, {"node": ["Heat"], "port": "plate"}) in endpoints
    assert ({"node": ["Heat"], "port": "out"}, {"node": [], "port": "result"}) in endpoints
    assert report.plan["interface"] == {
        "inputs": {"sample": "loader.stage"},
        "outputs": {"result": "output.slot"},
    }

    # The rendered plan is itself a valid execution document (round-trips).
    out = tmp_path / "plan.yaml"
    out.write_text(to_yaml(report.plan), encoding="utf-8")
    assert validate_document(out).ok, [(d.code, d.path) for d in validate_document(out).errors]
