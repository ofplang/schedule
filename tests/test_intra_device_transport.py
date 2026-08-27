"""A hand-off between two spots of the SAME device (SPEC §4.5).

A transport occupies its source device, its destination device and the
transporter. When the source and the destination are the *same* device that is
**one** occupation, not two: the same interval registered twice lands in that
device's non-overlap set against itself, which no positive duration can satisfy,
so every such environment came out infeasible however simple it was.

Contrast with test_inplace.py, where the two ends are the same *spot*. That case
never showed the bug, because its route is a zero-duration no-op and a
zero-length interval overlaps nothing -- which is why a lab with separate
machines could run for a long time without meeting this.
"""

from __future__ import annotations

from ofplang.schedule import schedule
from tests.schedutil import SIMPLE_WF, kinds, st_env, write


def _one_device_env(move: int = 1, source_dur: int = 2, target_dur: int = 2) -> str:
    """`source` on station_0.core, `target` on station_0.side, one arm between."""
    return st_env(
        devices=[("station_0", ["core", "side"])],
        transports=[("station_0.core", "station_0.side", move)],
        source_dur=source_dur,
        target_dur=target_dur,
        target_modes=(("station_0", "station_0.side"),),
    )


def test_two_spots_of_one_device_is_schedulable(tmp_path):
    report = schedule(SIMPLE_WF, write(tmp_path, "env.yaml", _one_device_env()))
    assert report.outcome == "optimal"
    assert report.makespan == 5  # source 2 + move 1 + target 2

    (t,) = kinds(report.plan, "transport")
    assert (t["from_spot"], t["to_spot"]) == ("station_0.core", "station_0.side")
    assert t["end"] - t["start"] == 1
    assert t["transporter"] == "transport"


def test_the_move_still_occupies_the_device(tmp_path):
    """Dropping the duplicate must not drop the occupation itself.

    The device is held for the whole move, so the two processing activities and
    the move cannot overlap: the makespan is their sum, not something shorter.
    """
    report = schedule(SIMPLE_WF, write(tmp_path, "env.yaml", _one_device_env(move=7)))
    assert report.outcome == "optimal"
    assert report.makespan == 11  # 2 + 7 + 2, fully serial on the one device

    (t,) = kinds(report.plan, "transport")
    src, dst = kinds(report.plan, "processing")[0], kinds(report.plan, "processing")[-1]
    assert src["end"] <= t["start"] and t["end"] <= dst["start"]
