"""Match an execution status against an instance -> fixation (scheduler/status.py).

Instances are built directly from the model so the matching is isolated from
workflow/env parsing, and status documents are parsed with `yamlnode.loads` so
`build_fixation` sees the position-tracking tree it expects.
"""

from __future__ import annotations

from ofplang.schedule.core import yamlnode
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    ArcInstance,
    Instance,
    TransportOption,
)
from ofplang.schedule.scheduler.model import Arc, Endpoint, Environment, Mode
from ofplang.schedule.scheduler.status import build_fixation

_ENV = Environment("second", {}, (), {}, {})


def _instance(dst_modes=None):
    """A source -> target chain. `dst_modes` overrides the target's modes (and,
    correspondingly, the arc's transport options) to exercise route matching."""
    src = ActivityInstance(("S",), "src", (Mode("0", ("ds",), 2, {}, {"o": "ds.p"}),))
    if dst_modes is None:
        dst_modes = (Mode("0", ("dt",), 2, {"i": "dt.p"}, {}),)
    dst = ActivityInstance(("T",), "tgt", dst_modes)
    options = tuple(
        TransportOption(0, n, "arm", "ds.p", dst_modes[n].input_spots["i"], 1)
        for n in range(len(dst_modes))
    )
    arc = ArcInstance(Arc(Endpoint(("S",), "o"), Endpoint(("T",), "i")), 0, 1, options)
    return Instance(_ENV, "second", (src, dst), (arc,), ((0, 1),))


def _fix(text, inst):
    return build_fixation(yamlnode.loads(text), inst)


def _codes(diags):
    return [d.code for d in diags.items]


# --- successful matching --------------------------------------------------


def test_matches_completed_processing_and_carries_now_and_placements():
    fix, diags = _fix(
        """
time: { unit: second }
now: 3
placements:
  - object: { input: sample }
    spot: ds.p
activities:
  - kind: processing
    status: completed
    start: 0
    end: 2
    process: src
    mode: '0'
    node: [S]
""",
        _instance(),
    )
    assert _codes(diags) == []
    assert fix is not None
    assert fix.now == 3
    assert 0 in fix.activities and fix.activities[0].status == "completed"
    assert fix.activities[0].mode_index == 0
    assert fix.activities[0].start == 0 and fix.activities[0].end == 2
    assert 1 not in fix.activities  # target is pending: re-derived, not fixed
    assert fix.placements == [{"object": {"input": "sample"}, "spot": "ds.p"}]


def test_pending_and_statusless_entries_are_ignored():
    # A prior plan (no status on any activity) fed back verbatim fixes nothing.
    fix, diags = _fix(
        """
time: { unit: second }
now: 0
activities:
  - kind: processing
    start: 0
    end: 2
    process: src
    mode: '0'
    node: [S]
  - kind: processing
    status: pending
    start: 3
    end: 5
    process: tgt
    mode: '0'
    node: [T]
""",
        _instance(),
    )
    assert _codes(diags) == []
    assert fix is not None and fix.activities == {} and fix.arcs == {}


def test_matches_completed_transport_between_fixed_endpoints():
    fix, diags = _fix(
        """
time: { unit: second }
now: 5
activities:
  - { kind: processing, status: completed, start: 0, end: 2, process: src, mode: '0', node: [S] }
  - kind: transport
    status: completed
    start: 2
    end: 3
    from_spot: ds.p
    to_spot: dt.p
    transporter: arm
    arc: { from: { node: [S], port: o }, to: { node: [T], port: i } }
  - { kind: processing, status: completed, start: 3, end: 5, process: tgt, mode: '0', node: [T] }
""",
        _instance(),
    )
    assert _codes(diags) == []
    assert fix is not None
    assert 0 in fix.arcs and fix.arcs[0].option_index == 0
    assert set(fix.activities) == {0, 1}


# --- error codes ----------------------------------------------------------


def test_missing_now():
    fix, diags = _fix("activities: []", _instance())
    assert _codes(diags) == ["status_missing_now"]
    assert fix is None


def test_node_unknown():
    fix, diags = _fix(
        """
now: 1
activities:
  - { kind: processing, status: completed, start: 0, end: 1, process: src, mode: '0', node: [NOPE] }
""",
        _instance(),
    )
    assert _codes(diags) == ["status_node_unknown"]
    assert fix is None


def test_mode_unknown():
    fix, diags = _fix(
        """
now: 3
activities:
  - { kind: processing, status: completed, start: 0, end: 2, process: src, mode: bogus, node: [S] }
""",
        _instance(),
    )
    assert _codes(diags) == ["status_mode_unknown"]


def test_arc_unknown():
    fix, diags = _fix(
        """
now: 5
activities:
  - kind: transport
    status: completed
    start: 2
    end: 3
    from_spot: ds.p
    to_spot: dt.p
    transporter: arm
    arc: { from: { node: [S], port: o }, to: { node: [T], port: WRONG } }
""",
        _instance(),
    )
    assert _codes(diags) == ["status_arc_unknown"]


def test_route_unknown():
    fix, diags = _fix(
        """
now: 5
activities:
  - kind: transport
    status: completed
    start: 2
    end: 3
    from_spot: ds.p
    to_spot: dt.p
    transporter: ghost
    arc: { from: { node: [S], port: o }, to: { node: [T], port: i } }
""",
        _instance(),
    )
    assert _codes(diags) == ["status_route_unknown"]


def test_time_inconsistent_completed_after_now():
    fix, diags = _fix(
        """
now: 1
activities:
  - { kind: processing, status: completed, start: 0, end: 2, process: src, mode: '0', node: [S] }
""",
        _instance(),
    )
    assert _codes(diags) == ["status_time_inconsistent"]


def test_time_inconsistent_running_starts_after_now():
    fix, diags = _fix(
        """
now: 1
activities:
  - { kind: processing, status: running, start: 2, end: 4, process: src, mode: '0', node: [S] }
""",
        _instance(),
    )
    assert _codes(diags) == ["status_time_inconsistent"]


def test_running_overrun_is_not_time_inconsistent():
    # A running activity whose expected end is already past now is a legitimate
    # overrun (clamped by the solver), not an inconsistency.
    fix, diags = _fix(
        """
now: 10
activities:
  - { kind: processing, status: running, start: 3, end: 4, process: src, mode: '0', node: [S] }
""",
        _instance(),
    )
    assert _codes(diags) == []
    assert fix is not None and fix.activities[0].status == "running"


def test_duplicate_processing():
    fix, diags = _fix(
        """
now: 3
activities:
  - { kind: processing, status: completed, start: 0, end: 2, process: src, mode: '0', node: [S] }
  - { kind: processing, status: completed, start: 0, end: 2, process: src, mode: '0', node: [S] }
""",
        _instance(),
    )
    assert _codes(diags) == ["status_duplicate"]


def test_unnormalized_transport_feeds_pending_processing():
    # Running transport, but the destination processing is still pending.
    fix, diags = _fix(
        """
now: 5
activities:
  - { kind: processing, status: completed, start: 0, end: 2, process: src, mode: '0', node: [S] }
  - kind: transport
    status: running
    start: 2
    end: 3
    from_spot: ds.p
    to_spot: dt.p
    transporter: arm
    arc: { from: { node: [S], port: o }, to: { node: [T], port: i } }
""",
        _instance(),
    )
    assert _codes(diags) == ["status_unnormalized"]


def test_route_inconsistent_with_endpoint_mode():
    # Target has two modes on two spots; the fixed transport delivers to mode 1's
    # spot, but the target is fixed to mode 0 -> the route disagrees.
    dst_modes = (
        Mode("0", ("dt",), 2, {"i": "dt.p0"}, {}),
        Mode("1", ("dt",), 2, {"i": "dt.p1"}, {}),
    )
    fix, diags = _fix(
        """
now: 5
activities:
  - { kind: processing, status: completed, start: 0, end: 2, process: src, mode: '0', node: [S] }
  - kind: transport
    status: completed
    start: 2
    end: 3
    from_spot: ds.p
    to_spot: dt.p1
    transporter: arm
    arc: { from: { node: [S], port: o }, to: { node: [T], port: i } }
  - { kind: processing, status: completed, start: 3, end: 5, process: tgt, mode: '0', node: [T] }
""",
        _instance(dst_modes),
    )
    assert _codes(diags) == ["status_route_inconsistent"]
