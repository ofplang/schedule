"""Interface boundary condition (SPECIFICATIONS.md §6.8): pinning a workflow's
Object-bearing entry inputs to spots constrains the upstream activity's mode.

These drive `schedule()` end to end on small hand-built inputs. The key fixture is
a one-step workflow whose entry input `sample` feeds `heat`, and an environment
where `heat` has two modes at two different spots with no transporter route
between them — so the interface spot fully determines which mode is feasible.
"""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule import schedule
from ofplang.schedule.scheduler.workflow import parse_workflow

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
objective: { kind: makespan }
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


def test_no_interface_leaves_mode_unconstrained(tmp_path):
    # Without interface the plan is feasible and carries no boundary transport
    # (the pre-interface behaviour): heat picks some mode freely.
    wf, ev, _ = _write(tmp_path)
    report = schedule(wf, ev)
    assert report.ok and report.outcome == "optimal"
    assert report.makespan == 10
    assert [a for a in report.plan["activities"] if a["kind"] == "transport"] == []
    assert "interface" not in report.plan


def test_interface_constrains_mode_to_slot_a(tmp_path):
    wf, ev, doc = _write(tmp_path, document=_iface("rack.slot_a"))
    report = schedule(wf, ev, document_path=doc)
    assert report.ok and report.outcome == "optimal"
    assert report.makespan == 10

    processing = [a for a in report.plan["activities"] if a["kind"] == "processing"]
    (heat,) = processing
    assert heat["mode"] == "at_a"  # constrained by the sample's actual position

    # A boundary transport bridges the interface spot to the consumer; here it is a
    # 0-distance no-op (same spot), so the transporter is omitted.
    transports = [a for a in report.plan["activities"] if a["kind"] == "transport"]
    (t,) = transports
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
    wf, ev, doc = _write(tmp_path, workflow=WORKFLOW_PURE_DATA, document=_iface("rack.slot_a", port="knob"))
    report = schedule(wf, ev, document_path=doc)
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
objective: { kind: makespan }
"""


def test_interface_duplicate_spot(tmp_path):
    # Two entry inputs bound to the same spot: two Objects cannot occupy one spot.
    doc = "interface:\n  inputs: { s1: rack.slot_a, s2: rack.slot_a }\nactivities: []\n"
    wf, ev, docp = _write(tmp_path, workflow=WORKFLOW_TWO_INPUTS, env=ENV_TWO_INPUTS, document=doc)
    report = schedule(wf, ev, document_path=docp)
    assert not report.ok
    assert "interface_duplicate_spot" in {d.code for d in report.diagnostics}
