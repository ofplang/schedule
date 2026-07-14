"""Initial-plan optimality: valid inputs must yield the correct optimal makespan
and the choices (mode, transporter) that achieve it. Parallelism / serialization
/ DAG / zero-distance optima are anchored by test_example_makespans and
test_cpsat; here we cover the choices those examples don't isolate.
"""

from __future__ import annotations

from ofplang.schedule import schedule
from tests.schedutil import SIMPLE_WF, kinds, write


def test_mode_selection_picks_the_faster(tmp_path):
    # target may run fast@station_1 (1) or slow@station_2 (3), both reachable by a
    # 1-unit move; the solver picks the fast mode.
    env = write(tmp_path, "env.yaml", _env(
        [("station_0", ["core"]), ("station_1", ["core"]), ("station_2", ["core"])],
        [("station_0.core", "station_1.core", 1), ("station_0.core", "station_2.core", 1)],
        [("station_1", "station_1.core", 1), ("station_2", "station_2.core", 3)],
    ))
    report = schedule(SIMPLE_WF, env)
    assert report.outcome == "optimal" and report.makespan == 4  # 2 + 1 + 1
    assert kinds(report.plan, "processing")[-1]["input_spots"] == {"target_in": "station_1.core"}


def test_unreachable_mode_is_not_chosen(tmp_path):
    # fast@station_1 is cheaper but unreachable (no move to station_1); the solver
    # falls back to slow@station_2.
    env = write(tmp_path, "env.yaml", _env(
        [("station_0", ["core"]), ("station_1", ["core"]), ("station_2", ["core"])],
        [("station_0.core", "station_2.core", 1)],  # only station_2 is reachable
        [("station_1", "station_1.core", 1), ("station_2", "station_2.core", 3)],
    ))
    report = schedule(SIMPLE_WF, env)
    assert report.outcome == "optimal" and report.makespan == 6  # 2 + 1 + 3
    assert kinds(report.plan, "processing")[-1]["input_spots"] == {"target_in": "station_2.core"}


def test_faster_transporter_is_chosen(tmp_path):
    # Two transporters serve station_0.core -> station_1.core; the faster wins.
    env = """time: {unit: second}
devices:
  - { id: station_0, spots: [core] }
  - { id: station_1, spots: [core] }
transporters: [ { id: slow_arm }, { id: fast_arm } ]
transports:
  - { transporter: slow_arm, from: station_0.core, to: station_1.core, duration: 10 }
  - { transporter: fast_arm, from: station_0.core, to: station_1.core, duration: 3 }
processes:
  source: { modes: [ { devices: [station_0], duration: 2, output_spots: { source_out: station_0.core } } ] }
  target: { modes: [ { devices: [station_1], duration: 2, input_spots: { target_in: station_1.core } } ] }
"""
    report = schedule(SIMPLE_WF, write(tmp_path, "env.yaml", env))
    assert report.outcome == "optimal" and report.makespan == 7  # 2 + 3 + 2
    assert kinds(report.plan, "transport")[0]["transporter"] == "fast_arm"


def test_makespan_equals_last_processing_end(tmp_path):
    env = _env(
        [("station_0", ["core"]), ("station_1", ["core"])],
        [("station_0.core", "station_1.core", 1)],
        [("station_1", "station_1.core", 2)],
    )
    report = schedule(SIMPLE_WF, write(tmp_path, "env.yaml", env))
    ends = [a["end"] for a in kinds(report.plan, "processing")]
    assert report.makespan == max(ends) == 5


def _env(devices, transports, target_modes):
    from tests.schedutil import st_env

    return st_env(devices=devices, transports=transports, target_modes=target_modes)
