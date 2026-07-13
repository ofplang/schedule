"""Tests for building the solver instance and the §9.3 execution-layer checks."""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.model import (
    AtomicProcess,
    Device,
    Environment,
    Mode,
    NodeInvocation,
    Port,
    ProcessCapability,
    Workflow,
)
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load(name):
    wf, wdiags = parse_workflow(EXAMPLES / f"{name}.workflow.yaml")
    env, _ = load_environment(EXAMPLES / f"{name}.env.yaml")
    assert wf is not None and env is not None
    assert not [d for d in wdiags.items if d.severity == ERROR]
    return wf, env


def test_build_simple_instance():
    wf, env = _load("simple")
    inst, diags = build_instance(wf, env)
    assert not [d for d in diags.items if d.severity == ERROR]
    assert inst is not None
    assert len(inst.activities) == 2
    assert inst.precedence == ((0, 1),)

    (arc,) = inst.arcs
    assert arc.src_activity == 0 and arc.dst_activity == 1
    assert len(arc.options) == 1
    opt = arc.options[0]
    assert opt.transporter == "transport"
    assert opt.from_spot == "station_0.core"
    assert opt.to_spot == "station_1.core"
    assert opt.duration == 1


def test_build_reformatter_instance():
    wf, env = _load("reformatter")
    inst, diags = build_instance(wf, env)
    assert not [d for d in diags.items if d.severity == ERROR]
    assert inst is not None
    assert len(inst.activities) == 8
    assert len(inst.arcs) == 12
    # Every arc has at least one transport option; the two rf_link handoffs are
    # same-spot (duration 0).
    assert all(a.options for a in inst.arcs)
    zero = [a for a in inst.arcs if any(o.duration == 0 for o in a.options)]
    assert len(zero) == 2


def test_nested_workflow_builds_equivalent_instance(tmp_path):
    # A nested variant of `simple` (source/target each wrapped in a composite)
    # flattens to the same two-activity, one-arc graph, so it schedules against the
    # unchanged simple environment — the pipeline downstream of the parser is
    # oblivious to how deep the composites were.
    from ofplang.schedule.scheduler.workflow import parse_workflow as _pw

    doc = tmp_path / "nested.yaml"
    doc.write_text(
        "spec_version: \"0.0\"\n"
        "types: {Sample: {domain: object}}\n"
        "processes:\n"
        "  source: {kind: atomic, outputs: {source_out: {type: Sample, phase: data}}, objects: {create: [outputs.source_out]}}\n"
        "  target: {kind: atomic, inputs: {target_in: {type: Sample, phase: data}}, objects: {consume: [inputs.target_in]}}\n"
        "  producer:\n"
        "    kind: composite\n"
        "    outputs: {p_out: {type: Sample, phase: data}}\n"
        "    body: {nodes: [{id: S, process: source}], returns: {p_out: {from: S.source_out}}}\n"
        "  main:\n"
        "    kind: composite\n"
        "    body:\n"
        "      nodes:\n"
        "        - {id: Prod, process: producer}\n"
        "        - {id: SampleTarget, process: target, state: {target_in: {from: Prod.p_out}}}\n"
        "entry: main\n",
        encoding="utf-8",
    )
    wf, wdiags = _pw(doc)
    assert not [d for d in wdiags.items if d.severity == ERROR]
    _, env = _load("simple")

    inst, diags = build_instance(wf, env)
    assert not [d for d in diags.items if d.severity == ERROR]
    assert inst is not None
    assert len(inst.activities) == 2
    assert len(inst.arcs) == 1
    opt = inst.arcs[0].options[0]
    assert opt.from_spot == "station_0.core" and opt.to_spot == "station_1.core" and opt.duration == 1


def test_unreachable_arc_is_reported():
    wf, env = _load("simple")
    # Drop the only transport entry -> the arc becomes unreachable.
    from dataclasses import replace

    env = replace(env, transports={})
    inst, diags = build_instance(wf, env)
    assert inst is None
    assert "arc_unreachable" in {d.code for d in diags.items if d.severity == ERROR}


# --- §9.3 per-port mode checks -------------------------------------------
#
# Each case builds a one-activity workflow and a matching capability directly
# from the model, so exactly one port rule is broken and the intended code is the
# only one produced. The env spots are arbitrary strings: spot existence is a
# §9.1 shape check, already done at load time, not here.


def _single(process: AtomicProcess, mode: Mode) -> tuple[Workflow, Environment]:
    inv = NodeInvocation((process.name,), process.name)
    workflow = Workflow((inv,), (), (), {process.name: process})
    env = Environment(
        time_unit="second",
        devices={"d": Device("d", frozenset({"s"}))},
        transporters=(),
        transports={},
        processes={process.name: ProcessCapability(process.name, (mode,))},
    )
    return workflow, env


def _codes(workflow: Workflow, env: Environment) -> set[str]:
    inst, diags = build_instance(workflow, env)
    assert inst is None
    return {d.code for d in diags.items if d.severity == ERROR}


def test_unknown_process_port_is_reported():
    process = AtomicProcess("p", (Port("a", True),), ())
    mode = Mode("m", ("d",), 1, {"a": "d.s", "bogus": "d.s"}, {})
    assert _codes(*_single(process, mode)) == {"unknown_process_port"}


def test_wrong_port_direction_is_reported():
    # `out` is an output port but the mode maps it under input_spots.
    process = AtomicProcess("p", (), (Port("out", True),))
    mode = Mode("m", ("d",), 1, {"out": "d.s"}, {"out": "d.s"})
    assert _codes(*_single(process, mode)) == {"wrong_port_direction"}


def test_pure_data_port_mapped_is_reported():
    # `x` is a real input port but Pure Data, so it must not occupy a spot.
    process = AtomicProcess("p", (Port("x", False),), ())
    mode = Mode("m", ("d",), 1, {"x": "d.s"}, {})
    assert _codes(*_single(process, mode)) == {"pure_data_port_mapped"}


def test_mode_ports_incomplete_is_reported():
    # Two Object-bearing outputs, but the mode maps only one.
    process = AtomicProcess("p", (), (Port("out1", True), Port("out2", True)))
    mode = Mode("m", ("d",), 1, {}, {"out1": "d.s"})
    assert _codes(*_single(process, mode)) == {"mode_ports_incomplete"}
