"""Tests for building the solver instance and the §9.3 execution-layer checks."""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load(name):
    wf, wdiags = parse_workflow(EXAMPLES / f"{name}.workflow.yaml")
    env, _ = load_environment(EXAMPLES / f"{name}.env.yaml")
    assert wf is not None and env is not None
    assert not [d for d in wdiags.items if d.severity == ERROR]
    return wf, env


def test_build_job_sample_instance():
    wf, env = _load("job_sample")
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


def test_unreachable_arc_is_reported():
    wf, env = _load("job_sample")
    # Drop the only transport entry -> the arc becomes unreachable.
    from dataclasses import replace

    env = replace(env, transports={})
    inst, diags = build_instance(wf, env)
    assert inst is None
    assert "arc_unreachable" in {d.code for d in diags.items if d.severity == ERROR}
