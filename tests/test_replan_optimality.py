"""Replan / re-route optimality: valid statuses must yield the correct optimal
makespan, with the fixed part honoured and the pending part re-optimised. Small
hand-built environments keep every expected makespan hand-verifiable.
"""

from __future__ import annotations

from ofplang.schedule import schedule, validate_document
from ofplang.schedule.scheduler.plan import to_yaml
from tests.schedutil import (
    MULTI_INPUT_WF,
    SIMPLE_WF,
    committed_source_and_leg,
    kinds,
    st_env,
    write,
)

# Canonical linear env: source(2)@station_0 -> transport(1) -> target(2)@station_1.
_LINE = dict(
    devices=[("station_0", ["core"]), ("station_1", ["core"])],
    transports=[("station_0.core", "station_1.core", 1)],
)


# --- B: replan core --------------------------------------------------------


def test_running_source_clamped_by_now_and_margin(tmp_path):
    # source running with expected finish 1 but now=5 (overrun): its end clamps
    # to now + margin, and the pending transport+target follow.
    env = write(tmp_path, "env.yaml", st_env(**_LINE))
    status = """
time: {unit: second}
now: 5
activities:
- { kind: processing, status: running, start: 0, end: 1, process: source, mode: '0', node: [SampleSource], output_spots: { source_out: station_0.core } }
"""
    s = write(tmp_path, "s.yaml", status)
    assert schedule(SIMPLE_WF, env, status_path=s, running_task_margin=0).makespan == 8   # end 5 + 1 + 2
    assert schedule(SIMPLE_WF, env, status_path=s, running_task_margin=3).makespan == 11  # end 8 + 1 + 2


def test_pending_pushed_to_now_even_if_source_finished_earlier(tmp_path):
    env = write(tmp_path, "env.yaml", st_env(**_LINE))
    status = """
time: {unit: second}
now: 10
activities:
- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], output_spots: { source_out: station_0.core } }
"""
    report = schedule(SIMPLE_WF, env, status_path=write(tmp_path, "s.yaml", status))
    assert report.makespan == 13  # transport 10->11, target 11->13 (not from 2)


def test_absolute_makespan_includes_fixed_history(tmp_path):
    env = write(tmp_path, "env.yaml", st_env(**_LINE))
    status = """
time: {unit: second}
now: 3
activities:
- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], output_spots: { source_out: station_0.core } }
"""
    report = schedule(SIMPLE_WF, env, status_path=write(tmp_path, "s.yaml", status))
    assert report.makespan == 6  # t=0 origin, not measured from now
    src = next(a for a in kinds(report.plan, "processing") if a["node"] == ["SampleSource"])
    assert src["start"] == 0


def test_idempotent_all_pending_equals_initial(tmp_path):
    env = write(tmp_path, "env.yaml", st_env(**_LINE))
    initial = schedule(SIMPLE_WF, env)
    status = write(tmp_path, "s.yaml", "time: {unit: second}\nnow: 0\nactivities: []\n")
    replan = schedule(SIMPLE_WF, env, status_path=status)
    assert replan.makespan == initial.makespan == 5


def test_idempotent_all_completed_reoptimises_nothing(tmp_path):
    env = write(tmp_path, "env.yaml", st_env(**_LINE))
    status = """
time: {unit: second}
now: 5
activities:
- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], output_spots: { source_out: station_0.core } }
- { kind: transport, status: completed, start: 2, end: 3, seq: 0, from_spot: station_0.core, to_spot: station_1.core, transporter: transport, arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } } }
- { kind: processing, status: completed, start: 3, end: 5, process: target, mode: '0', node: [SampleTarget], input_spots: { target_in: station_1.core } }
"""
    report = schedule(SIMPLE_WF, env, status_path=write(tmp_path, "s.yaml", status))
    assert report.makespan == 5
    assert not kinds(report.plan, "relay")  # dst fixed -> no relay
    assert all("status" in a for a in report.plan["activities"])


# --- C: re-routing optimality ---------------------------------------------


def test_stays_when_arrival_mode_still_available(tmp_path):
    # target still runs at the arrival spot: the re-transport is zero-distance.
    env = write(tmp_path, "env.yaml", st_env(**_LINE))
    report = schedule(SIMPLE_WF, env, status_path=write(tmp_path, "s.yaml", committed_source_and_leg(now=5)))
    assert report.makespan == 7  # relay@station_1, 0-dist re-transport, target 5->7
    target = kinds(report.plan, "processing")[-1]
    assert target["input_spots"] == {"target_in": "station_1.core"}


def test_reroutes_when_arrival_mode_removed(tmp_path):
    env = write(tmp_path, "env.yaml", st_env(
        devices=[("station_0", ["core"]), ("station_1", ["core"]), ("station_2", ["core"])],
        transports=[("station_0.core", "station_1.core", 1), ("station_1.core", "station_2.core", 4)],
        target_modes=(("station_2", "station_2.core"),),
    ))
    report = schedule(SIMPLE_WF, env, status_path=write(tmp_path, "s.yaml", committed_source_and_leg(now=5)))
    assert report.makespan == 11  # re-transport station_1->station_2 (4): 5->9, target 9->11
    assert kinds(report.plan, "processing")[-1]["input_spots"] == {"target_in": "station_2.core"}


def test_reroute_picks_min_makespan_destination(tmp_path):
    # target may run on station_2 (far, 4) or station_3 (near, 1); the solver
    # picks the one giving the smaller makespan.
    env = write(tmp_path, "env.yaml", st_env(
        devices=[("station_0", ["core"]), ("station_1", ["core"]), ("station_2", ["core"]), ("station_3", ["core"])],
        transports=[
            ("station_0.core", "station_1.core", 1),
            ("station_1.core", "station_2.core", 4),
            ("station_1.core", "station_3.core", 1),
        ],
        target_modes=(("station_2", "station_2.core"), ("station_3", "station_3.core")),
    ))
    report = schedule(SIMPLE_WF, env, status_path=write(tmp_path, "s.yaml", committed_source_and_leg(now=5)))
    assert report.makespan == 8  # station_3: 5->6, target 6->8  (station_2 would give 11)
    assert kinds(report.plan, "processing")[-1]["input_spots"] == {"target_in": "station_3.core"}


def test_chained_reroute_makespan(tmp_path):
    env = write(tmp_path, "env.yaml", st_env(
        devices=[("station_0", ["core"]), ("station_1", ["core"]), ("station_2", ["core"]), ("station_3", ["core"])],
        transports=[
            ("station_0.core", "station_1.core", 1),
            ("station_1.core", "station_2.core", 4),
            ("station_2.core", "station_3.core", 2),
        ],
        target_modes=(("station_3", "station_3.core"),),
    ))
    status = """
time: {unit: second}
now: 10
activities:
- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], output_spots: { source_out: station_0.core } }
- { kind: transport, status: completed, start: 2, end: 3, seq: 0, from_spot: station_0.core, to_spot: station_1.core, transporter: transport, arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } } }
- { kind: transport, status: completed, start: 5, end: 9, seq: 2, from_spot: station_1.core, to_spot: station_2.core, transporter: transport, arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } } }
"""
    report = schedule(SIMPLE_WF, env, status_path=write(tmp_path, "s.yaml", status))
    assert report.makespan == 14  # re-transport station_2->station_3 (2): 10->12, target 12->14
    assert {r["spot"] for r in kinds(report.plan, "relay")} == {"station_1.core", "station_2.core"}


def test_bounce_makespan_and_round_trip(tmp_path):
    env = write(tmp_path, "env.yaml", st_env(
        devices=[("station_0", ["core"]), ("station_1", ["core"]), ("station_2", ["core"])],
        transports=[
            ("station_0.core", "station_1.core", 1),
            ("station_1.core", "station_2.core", 4),
            ("station_2.core", "station_1.core", 4),
        ],
        target_modes=(("station_1", "station_1.core"),),
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
    report = schedule(SIMPLE_WF, env, status_path=write(tmp_path, "s.yaml", status))
    assert report.makespan == 22  # 0-dist re-transport at 20, target 20->22
    at1 = [r for r in kinds(report.plan, "relay") if r["spot"] == "station_1.core"]
    assert len(at1) == 2 and len({r["seq"] for r in at1}) == 2  # revisit distinguished by seq
    fed = write(tmp_path, "fed.yaml", to_yaml(report.plan))
    assert validate_document(fed).ok


# --- C: multi-input --------------------------------------------------------

_MULTI_ENV = """time: {unit: second}
devices:
  - { id: d1, spots: [s] }
  - { id: d2, spots: [s] }
  - { id: dt, spots: [a, b] }
transporters: [ { id: transport } ]
transports:
  - { transporter: transport, from: d1.s, to: dt.a, duration: 1 }
  - { transporter: transport, from: d2.s, to: dt.b, duration: 1 }
processes:
  source:  { modes: [ { devices: [d1], duration: 1, output_spots: { o: d1.s } } ] }
  source2: { modes: [ { devices: [d2], duration: 1, output_spots: { o: d2.s } } ] }
  merge:   { modes: [ { devices: [dt], duration: 2, input_spots: { i1: dt.a, i2: dt.b } } ] }
"""

# The two committed transports both occupy device `dt` (the conservative 3-device
# model, §4.5), so they are staggered — two deliveries to one device cannot
# overlap in time.
_MULTI_STATUS = """time: {unit: second}
now: 5
activities:
- { kind: processing, status: completed, start: 0, end: 1, process: source,  mode: '0', node: [S1], output_spots: { o: d1.s } }
- { kind: processing, status: completed, start: 0, end: 1, process: source2, mode: '0', node: [S2], output_spots: { o: d2.s } }
- { kind: transport, status: completed, start: 1, end: 2, seq: 0, from_spot: d1.s, to_spot: dt.a, transporter: transport, arc: { from: { node: [S1], port: o }, to: { node: [M], port: i1 } } }
- { kind: transport, status: completed, start: 2, end: 3, seq: 0, from_spot: d2.s, to_spot: dt.b, transporter: transport, arc: { from: { node: [S2], port: o }, to: { node: [M], port: i2 } } }
"""


def test_multi_input_both_arrived_optimal(tmp_path):
    wf = write(tmp_path, "wf.yaml", MULTI_INPUT_WF)
    env = write(tmp_path, "env.yaml", _MULTI_ENV)
    report = schedule(wf, env, status_path=write(tmp_path, "s.yaml", _MULTI_STATUS))
    assert report.ok, [d.code for d in report.diagnostics]
    # Both inputs stay at their arrival spots (0-distance); merge runs 5->7.
    assert report.makespan == 7
    assert {r["spot"] for r in kinds(report.plan, "relay")} == {"dt.a", "dt.b"}


def test_multi_input_infeasible_when_no_reachable_mode(tmp_path):
    # merge only runs at spots on device dz, unreachable from the arrival spots.
    env = _MULTI_ENV.replace(
        "  merge:   { modes: [ { devices: [dt], duration: 2, input_spots: { i1: dt.a, i2: dt.b } } ] }",
        "  merge:   { modes: [ { devices: [dz], duration: 2, input_spots: { i1: dz.p, i2: dz.q } } ] }",
    ).replace("  - { id: dt, spots: [a, b] }", "  - { id: dt, spots: [a, b] }\n  - { id: dz, spots: [p, q] }")
    wf = write(tmp_path, "wf.yaml", MULTI_INPUT_WF)
    report = schedule(wf, write(tmp_path, "env.yaml", env), status_path=write(tmp_path, "s.yaml", _MULTI_STATUS))
    assert not report.ok
    assert "arc_unreachable" in [d.code for d in report.diagnostics]
