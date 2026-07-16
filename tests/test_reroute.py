"""End-to-end re-routing on a replan: a committed transport delivered an Object
to a spot, the destination device became unavailable (its mode removed from the
env, its spot + transport routes kept), and the scheduler re-routes via a relay
and a re-transport. Uses the `simple` workflow (SampleSource -> SampleTarget) and
per-test environments so the target's reachable device(s) vary.
"""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule import schedule, validate_document
from ofplang.schedule.scheduler.plan import to_yaml

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
WORKFLOW = EXAMPLES / "simple.workflow.yaml"

# Committed: source done, transport done delivering to station_1.core, target
# still pending. `seq: 0` marks the first (and, so far, only) leg.
_COMMITTED = """
time: {unit: second}
now: 5
activities:
- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], output_spots: { source_out: station_0.core } }
- kind: transport
  status: completed
  start: 2
  end: 3
  seq: 0
  from_spot: station_0.core
  to_spot: station_1.core
  transporter: transport
  arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } }
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _env(devices_spots, transports, target_device, target_spot):
    devs = "\n".join(f"  - {{ id: {d}, spots: [{s}] }}" for d, s in devices_spots)
    trs = "\n".join(
        f"  - {{ transporter: transport, from: {f}, to: {t}, duration: {d} }}" for f, t, d in transports
    )
    return f"""time: {{ unit: second }}
devices:
{devs}
transporters: [ {{ id: transport }} ]
transports:
{trs}
processes:
  source: {{ modes: [ {{ devices: [station_0], duration: 2, output_spots: {{ source_out: station_0.core }} }} ] }}
  target: {{ modes: [ {{ devices: [{target_device}], duration: 2, input_spots: {{ target_in: {target_spot} }} }} ] }}
"""


def _kinds(plan, kind):
    return [a for a in plan["activities"] if a["kind"] == kind]


def test_reroute_via_relay_and_retransport(tmp_path):
    # target now only on station_2; no DIRECT station_0->station_2 route exists,
    # so this also exercises the arc_unreachable relaxation (the committed leg is
    # not re-checked; only the pending re-transport must be reachable).
    env = _write(tmp_path, "env.yaml", _env(
        [("station_0", "core"), ("station_1", "core"), ("station_2", "core")],
        [("station_0.core", "station_1.core", 1), ("station_1.core", "station_2.core", 4)],
        "station_2", "station_2.core",
    ))
    status = _write(tmp_path, "status.yaml", _COMMITTED)
    report = schedule(WORKFLOW, env, document_path=status)
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]

    relays = _kinds(report.plan, "relay")
    assert len(relays) == 1 and relays[0]["spot"] == "station_1.core"
    target = _kinds(report.plan, "processing")[-1]
    assert target["input_spots"] == {"target_in": "station_2.core"}  # re-routed
    # A real re-transport station_1.core -> station_2.core carries the Object.
    legs = _kinds(report.plan, "transport")
    assert any(t["from_spot"] == "station_1.core" and t["to_spot"] == "station_2.core" for t in legs)


def test_reroute_round_trips(tmp_path):
    env = _write(tmp_path, "env.yaml", _env(
        [("station_0", "core"), ("station_1", "core"), ("station_2", "core")],
        [("station_0.core", "station_1.core", 1), ("station_1.core", "station_2.core", 4)],
        "station_2", "station_2.core",
    ))
    first = schedule(WORKFLOW, env, document_path=_write(tmp_path, "s.yaml", _COMMITTED))
    fed = _write(tmp_path, "fed.yaml", to_yaml(first.plan))
    assert validate_document(fed).ok
    second = schedule(WORKFLOW, env, document_path=fed)
    assert second.ok and second.makespan == first.makespan


def test_reroute_infeasible_when_destination_unreachable(tmp_path):
    # target only on station_2, but nothing can move station_1.core -> station_2.core.
    env = _write(tmp_path, "env.yaml", _env(
        [("station_0", "core"), ("station_1", "core"), ("station_2", "core")],
        [("station_0.core", "station_1.core", 1)],  # no station_1 -> station_2 route
        "station_2", "station_2.core",
    ))
    report = schedule(WORKFLOW, env, document_path=_write(tmp_path, "s.yaml", _COMMITTED))
    assert not report.ok
    assert "arc_unreachable" in [d.code for d in report.diagnostics]


def test_chained_reroute(tmp_path):
    # Two committed legs (station_0->station_1->station_2); target now on
    # station_3, reached by a third (pending) leg. Relays chain.
    env = _write(tmp_path, "env.yaml", _env(
        [("station_0", "core"), ("station_1", "core"), ("station_2", "core"), ("station_3", "core")],
        [
            ("station_0.core", "station_1.core", 1),
            ("station_1.core", "station_2.core", 4),
            ("station_2.core", "station_3.core", 2),
        ],
        "station_3", "station_3.core",
    ))
    status = """
time: {unit: second}
now: 10
activities:
- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], output_spots: { source_out: station_0.core } }
- { kind: transport, status: completed, start: 2, end: 3, seq: 0, from_spot: station_0.core, to_spot: station_1.core, transporter: transport, arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } } }
- { kind: transport, status: completed, start: 5, end: 9, seq: 2, from_spot: station_1.core, to_spot: station_2.core, transporter: transport, arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } } }
"""
    report = schedule(WORKFLOW, env, document_path=_write(tmp_path, "s.yaml", status))
    assert report.ok, [d.code for d in report.diagnostics]
    relays = _kinds(report.plan, "relay")
    assert {r["spot"] for r in relays} == {"station_1.core", "station_2.core"}
    target = _kinds(report.plan, "processing")[-1]
    assert target["input_spots"] == {"target_in": "station_3.core"}


def test_committed_reroute_example(tmp_path):
    # Golden anchor for the tracked reroute example (examples/reroute.*).
    report = schedule(WORKFLOW, EXAMPLES / "reroute.env.yaml", document_path=EXAMPLES / "reroute.status.yaml")
    assert report.outcome == "optimal" and report.makespan == 11
    assert [r["spot"] for r in _kinds(report.plan, "relay")] == ["station_1.core"]
    assert _kinds(report.plan, "processing")[-1]["input_spots"] == {"target_in": "station_2.core"}


def test_committed_reroute_output_is_valid_document():
    path = EXAMPLES / "outputs" / "reroute.replan.yaml"
    assert path.is_file()
    assert validate_document(path).ok


def test_committed_reroute_stay_example():
    # examples/reroute_stay.*: the target still consumes where the sample landed,
    # so the re-transport is a zero-distance no-op — it and its relay are folded
    # (§6.4.1), leaving the committed leg to deliver straight to the target.
    report = schedule(WORKFLOW, EXAMPLES / "reroute_stay.env.yaml", document_path=EXAMPLES / "reroute_stay.status.yaml")
    assert report.outcome == "optimal" and report.makespan == 5
    assert not _kinds(report.plan, "relay")  # folded away
    legs = _kinds(report.plan, "transport")
    assert len(legs) == 1 and not any(t["from_spot"] == t["to_spot"] for t in legs)
    path = EXAMPLES / "outputs" / "reroute_stay.replan.yaml"
    assert path.is_file() and validate_document(path).ok


def test_committed_reroute_chain_example():
    # examples/reroute_chain.*: two committed real legs carry the sample to
    # station_2, then a third real leg re-routes it to station_3. Both arrival
    # relays chain and are kept (every leg is a real move, so nothing is folded).
    report = schedule(WORKFLOW, EXAMPLES / "reroute_chain.env.yaml", document_path=EXAMPLES / "reroute_chain.status.yaml")
    assert report.outcome == "optimal" and report.makespan == 14
    assert {r["spot"] for r in _kinds(report.plan, "relay")} == {"station_1.core", "station_2.core"}
    assert _kinds(report.plan, "processing")[-1]["input_spots"] == {"target_in": "station_3.core"}
    path = EXAMPLES / "outputs" / "reroute_chain.replan.yaml"
    assert path.is_file() and validate_document(path).ok


def test_bounce_revisits_a_spot_distinguished_by_seq(tmp_path):
    # Committed legs bounce station_1 -> station_2 -> station_1; the target is on
    # station_3, so a final real leg leaves station_1 again. Both station_1 relays
    # sit between real moves (they are not stay-put no-ops), so folding keeps them:
    # two relays at station_1.core, told apart by seq. (A relay whose departing leg
    # is a zero-distance no-op would instead be folded; see the stay-put tests.)
    env = _write(tmp_path, "env.yaml", _env(
        [("station_0", "core"), ("station_1", "core"), ("station_2", "core"), ("station_3", "core")],
        [
            ("station_0.core", "station_1.core", 1),
            ("station_1.core", "station_2.core", 4),
            ("station_2.core", "station_1.core", 4),
            ("station_1.core", "station_3.core", 2),
        ],
        "station_3", "station_3.core",
    ))
    status = """
time: {unit: second}
now: 20
activities:
- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], output_spots: { source_out: station_0.core } }
- { kind: transport, status: completed, start: 2, end: 3, seq: 0, from_spot: station_0.core, to_spot: station_1.core, transporter: transport, arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } } }
- { kind: transport, status: completed, start: 5, end: 9, seq: 2, from_spot: station_1.core, to_spot: station_2.core, transporter: transport, arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } } }
- { kind: transport, status: completed, start: 10, end: 14, seq: 4, from_spot: station_2.core, to_spot: station_1.core, transporter: transport, arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } } }
"""
    report = schedule(WORKFLOW, env, document_path=_write(tmp_path, "s.yaml", status))
    assert report.ok, [d.code for d in report.diagnostics]
    relays = _kinds(report.plan, "relay")
    at_station_1 = [r for r in relays if r["spot"] == "station_1.core"]
    assert len(at_station_1) == 2  # two visits to station_1.core
    assert len({r["seq"] for r in at_station_1}) == 2  # distinguished by seq
    # The output round-trips despite the revisit.
    fed = _write(tmp_path, "fed.yaml", to_yaml(report.plan))
    assert validate_document(fed).ok
