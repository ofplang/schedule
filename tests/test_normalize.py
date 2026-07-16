"""Replanning normalization (scheduler/normalize.py): match a status against the
workflow instance, pin fixed parts from their reported data, and derive relays /
re-transports for started transports feeding a pending successor.

The base instance is built from the `simple` example (SampleSource -> transport
-> SampleTarget, spots station_0.core / station_1.core, transporter `transport`)
with reachability deferred, as the replan path does.
"""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule.core import yamlnode
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.normalize import normalize
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _base():
    wf, _ = parse_workflow(EXAMPLES / "simple.workflow.yaml")
    env, _ = load_environment(EXAMPLES / "simple.env.yaml")
    inst, _ = build_instance(wf, env, check_reachability=False)
    assert inst is not None
    return inst, env


def _run(text):
    base, env = _base()
    return normalize(base, yamlnode.loads(text), env)


def _codes(diags):
    return [d.code for d in diags.items]


# --- errors ---------------------------------------------------------------


def test_missing_now():
    # A document with started activities but no `now` is an error: history cannot be
    # pinned against an absent reference time (SPEC §6.1 / §9.3).
    inst, fix, diags = _run(
        "activities:\n- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource] }\n"
    )
    assert _codes(diags) == ["status_missing_now"]
    assert inst is None


def test_no_now_no_history_is_initial():
    # No `now` and no started activities: the degenerate initial plan (now = 0), not
    # an error — an initial plan is a replan with empty history.
    inst, fix, diags = _run("activities: []")
    assert _codes(diags) == []
    assert inst is not None and fix is not None and fix.now == 0


def test_node_unknown():
    inst, fix, diags = _run(
        "now: 3\nactivities:\n- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [NOPE] }\n"
    )
    assert _codes(diags) == ["status_node_unknown"]


def test_duplicate_processing():
    text = (
        "now: 3\nactivities:\n"
        "- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource] }\n"
        "- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource] }\n"
    )
    inst, fix, diags = _run(text)
    assert _codes(diags) == ["status_duplicate"]


def test_time_inconsistent_completed_after_now():
    inst, fix, diags = _run(
        "now: 1\nactivities:\n- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource] }\n"
    )
    assert _codes(diags) == ["status_time_inconsistent"]


def test_broken_chain_started_transport_without_completed_source():
    # A committed transport but its source processing is not completed.
    text = (
        "now: 5\nactivities:\n"
        "- kind: transport\n"
        "  status: completed\n"
        "  start: 2\n  end: 3\n"
        "  from_spot: station_0.core\n  to_spot: station_1.core\n  transporter: transport\n"
        "  arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } }\n"
    )
    inst, fix, diags = _run(text)
    assert "broken_transport_chain" in _codes(diags)


# --- successful pinning + relay derivation --------------------------------


def test_fixed_processing_pinned_from_echo():
    text = (
        "now: 3\nactivities:\n"
        "- kind: processing\n  status: completed\n  start: 0\n  end: 2\n"
        "  process: source\n  mode: '0'\n  node: [SampleSource]\n"
        "  devices: [station_0]\n  output_spots: { source_out: station_0.core }\n"
    )
    inst, fix, diags = _run(text)
    assert _codes(diags) == []
    assert inst is not None and fix is not None
    # SampleSource is activity 0; fixed, single frozen mode from the echo.
    src = inst.activities[0]
    assert 0 in fix.activities and fix.activities[0].status == "completed"
    assert len(src.modes) == 1 and src.modes[0].output_spots == {"source_out": "station_0.core"}
    # SampleTarget stays pending (not in the fixation).
    assert 1 not in fix.activities


def test_committed_transport_derives_relay_and_retransport():
    text = (
        "now: 5\nactivities:\n"
        "- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], output_spots: { source_out: station_0.core } }\n"
        "- kind: transport\n  status: completed\n  start: 2\n  end: 3\n  seq: 0\n"
        "  from_spot: station_0.core\n  to_spot: station_1.core\n  transporter: transport\n"
        "  arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } }\n"
    )
    inst, fix, diags = _run(text)
    assert _codes(diags) == []
    assert inst is not None
    # A relay was derived at the arrival spot, and it is fixed (leg completed).
    relays = [(i, a) for i, a in enumerate(inst.activities) if a.relay is not None]
    assert len(relays) == 1
    idx, relay = relays[0]
    assert relay.relay.spot == "station_1.core"
    assert idx in fix.activities and fix.activities[idx].status == "completed"
    # Two legs on the arc: the committed leg (fixed) and a pending re-transport.
    assert len(inst.arcs) == 2
    assert 0 in fix.arcs and 1 not in fix.arcs


# --- multi-input successor (each incoming arc normalized independently) ---

_MULTI_ENV = """
time: {unit: second}
devices:
  - {id: d1, spots: [s]}
  - {id: d2, spots: [s]}
  - {id: dt, spots: [a, b]}
transporters: [{id: arm}]
transports:
  - {transporter: arm, from: d1.s, to: dt.a, duration: 1}
  - {transporter: arm, from: d2.s, to: dt.b, duration: 1}
processes:
  source:  {modes: [{devices: [d1], duration: 1, output_spots: {o: d1.s}}]}
  source2: {modes: [{devices: [d2], duration: 1, output_spots: {o: d2.s}}]}
  target2: {modes: [{devices: [dt], duration: 2, input_spots: {i1: dt.a, i2: dt.b}}]}
"""


def _multi_base(tmp_path):
    from ofplang.schedule.scheduler.instance import ActivityInstance, ArcInstance, Instance, _transport_options
    from ofplang.schedule.scheduler.model import Arc, Endpoint

    env_path = tmp_path / "multi.env.yaml"
    env_path.write_text(_MULTI_ENV, encoding="utf-8")
    env, _ = load_environment(env_path)
    s1 = ActivityInstance(("S1",), "source", env.processes["source"].modes)
    s2 = ActivityInstance(("S2",), "source2", env.processes["source2"].modes)
    tgt = ActivityInstance(("T",), "target2", env.processes["target2"].modes)
    arc1 = ArcInstance(Arc(Endpoint(("S1",), "o"), Endpoint(("T",), "i1")), 0, 2, tuple(_transport_options(s1, "o", tgt, "i1", env)))
    arc2 = ArcInstance(Arc(Endpoint(("S2",), "o"), Endpoint(("T",), "i2")), 1, 2, tuple(_transport_options(s2, "o", tgt, "i2", env)))
    return Instance(env, "second", (s1, s2, tgt), (arc1, arc2), ((0, 2), (1, 2))), env


def test_multi_input_each_arrived_input_gets_its_own_relay(tmp_path):
    from ofplang.schedule.core import yamlnode

    base, env = _multi_base(tmp_path)
    status = """
now: 5
activities:
- { kind: processing, status: completed, start: 0, end: 1, process: source,  mode: '0', node: [S1], output_spots: { o: d1.s } }
- { kind: processing, status: completed, start: 0, end: 1, process: source2, mode: '0', node: [S2], output_spots: { o: d2.s } }
- { kind: transport, status: completed, start: 1, end: 2, seq: 0, from_spot: d1.s, to_spot: dt.a, transporter: arm, arc: { from: { node: [S1], port: o }, to: { node: [T], port: i1 } } }
- { kind: transport, status: completed, start: 1, end: 2, seq: 0, from_spot: d2.s, to_spot: dt.b, transporter: arm, arc: { from: { node: [S2], port: o }, to: { node: [T], port: i2 } } }
"""
    inst, fix, diags = normalize(base, yamlnode.loads(status), env)
    assert [d.code for d in diags.items] == []
    relays = [a for a in inst.activities if a.relay is not None]
    assert {r.relay.spot for r in relays} == {"dt.a", "dt.b"}  # one relay per arrived input
