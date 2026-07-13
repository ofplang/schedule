"""End-to-end CP-SAT solve on the example fixtures."""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    ArcInstance,
    Instance,
    TransportOption,
    build_instance,
)
from ofplang.schedule.scheduler.model import Arc, Endpoint, Environment, Mode
from ofplang.schedule.scheduler.status import ActivityFixation, ArcFixation, Fixation
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _instance(name):
    wf, _ = parse_workflow(EXAMPLES / f"{name}.workflow.yaml")
    env, _ = load_environment(EXAMPLES / f"{name}.env.yaml")
    inst, diags = build_instance(wf, env)
    assert inst is not None, [d.code for d in diags.items]
    return inst


def test_solve_simple_makespan_5():
    sol = solve(_instance("simple"))
    assert sol.outcome == "optimal"
    # source (2) -> transport (1) -> target (2) on the critical path.
    assert sol.makespan == 5

    by_node = {p.node: p for p in sol.processing}
    assert by_node[("SampleSource",)].start == 0
    assert by_node[("SampleSource",)].end == 2
    (t,) = sol.transport
    assert t.start >= 2 and t.end == t.start + 1
    assert by_node[("SampleTarget",)].start >= t.end
    assert by_node[("SampleTarget",)].end == sol.makespan == 5


def test_solve_reformatter_feasible():
    sol = solve(_instance("reformatter"))
    assert sol.outcome == "optimal"
    assert sol.makespan and sol.makespan > 0
    assert len(sol.processing) == 8
    assert len(sol.transport) == 12
    # Every scheduled interval is well-formed and respects arc ordering.
    for p in sol.processing:
        assert 0 <= p.start <= p.end <= sol.makespan
    for t in sol.transport:
        assert t.start <= t.end


# --- multiple transporters (FORMULATION §7; SPEC §4.6) -------------------
#
# These build the solver instance directly from the model so the transporter
# behaviour is isolated from workflow/env parsing. `solve` does not read
# `Instance.env`, so a bare placeholder environment is enough.

_ENV = Environment("second", {}, (), {}, {})


def _chain(idx: int, duration: int, transporters: tuple[str, ...]):
    """A source->target chain on its own devices/spots; the transport takes
    `duration` and may use any of `transporters`. Returns (source activity,
    target activity, arc-options) with processing duration 1 each."""
    sd, td = f"ds{idx}", f"dt{idx}"
    src = ActivityInstance((f"s{idx}",), "src", (Mode("m", (sd,), 1, {}, {"o": f"{sd}.p"}),))
    dst = ActivityInstance((f"t{idx}",), "tgt", (Mode("m", (td,), 1, {"i": f"{td}.p"}, {}),))
    options = tuple(
        TransportOption(0, 0, tr, f"{sd}.p", f"{td}.p", duration) for tr in transporters
    )
    return src, dst, options


def _two_chain_instance(transporters: tuple[str, ...]) -> Instance:
    """Two independent source->target chains, each transport taking 10. With one
    transporter the two moves serialise; with two they can run concurrently."""
    s1, t1, o1 = _chain(1, 10, transporters)
    s2, t2, o2 = _chain(2, 10, transporters)
    arcs = (
        ArcInstance(Arc(Endpoint(("s1",), "o"), Endpoint(("t1",), "i")), 0, 1, o1),
        ArcInstance(Arc(Endpoint(("s2",), "o"), Endpoint(("t2",), "i")), 2, 3, o2),
    )
    return Instance(_ENV, "second", (s1, t1, s2, t2), arcs, ((0, 1), (2, 3)))


def test_two_transporters_run_transports_in_parallel():
    # One transporter serialises the two 10-unit moves; two let them overlap.
    one = solve(_two_chain_instance(("arm0",)))
    two = solve(_two_chain_instance(("arm0", "arm1")))
    assert one.outcome == two.outcome == "optimal"
    # Each chain is source(1) + move(10) + target(1) = 12. Parallel keeps 12;
    # serial pushes the second chain's move behind the first: 12 + 10 = 22.
    assert two.makespan == 12
    assert one.makespan == 22
    # With two transporters the solver actually uses both (one move each).
    assert {t.option.transporter for t in two.transport} == {"arm0", "arm1"}


def test_transports_on_one_transporter_do_not_overlap():
    # The two moves share the single transporter, so their [start, end) intervals
    # must be disjoint (one move at a time per transporter, FORMULATION §7).
    sol = solve(_two_chain_instance(("arm0",)))
    a, b = sorted((t.start, t.end) for t in sol.transport)
    assert a[1] <= b[0]  # first move ends no later than the second begins


def test_faster_transporter_is_selected():
    # One chain, two transporters for the move: arm0 slow (10), arm1 fast (3).
    src = ActivityInstance(("s",), "src", (Mode("m", ("ds",), 1, {}, {"o": "ds.p"}),))
    dst = ActivityInstance(("t",), "tgt", (Mode("m", ("dt",), 1, {"i": "dt.p"}, {}),))
    options = (
        TransportOption(0, 0, "arm0", "ds.p", "dt.p", 10),
        TransportOption(0, 0, "arm1", "ds.p", "dt.p", 3),
    )
    arc = ArcInstance(Arc(Endpoint(("s",), "o"), Endpoint(("t",), "i")), 0, 1, options)
    sol = solve(Instance(_ENV, "second", (src, dst), (arc,), ((0, 1),)))
    assert sol.outcome == "optimal"
    (t,) = sol.transport
    assert t.option.transporter == "arm1"        # picks the fast one
    assert sol.makespan == 1 + 3 + 1              # source + fast move + target


# --- replanning fixation (FORMULATION §9) --------------------------------
#
# A source(2) -> move(1) -> target(2) chain built directly from the model.


def _chain_instance() -> Instance:
    src = ActivityInstance(("S",), "src", (Mode("0", ("ds",), 2, {}, {"o": "ds.p"}),))
    dst = ActivityInstance(("T",), "tgt", (Mode("0", ("dt",), 2, {"i": "dt.p"}, {}),))
    options = (TransportOption(0, 0, "arm", "ds.p", "dt.p", 1),)
    arc = ArcInstance(Arc(Endpoint(("S",), "o"), Endpoint(("T",), "i")), 0, 1, options)
    return Instance(_ENV, "second", (src, dst), (arc,), ((0, 1),))


def test_pending_is_pushed_to_now_and_completed_is_pinned():
    # Source completed at [0, 2]; replanning at now=5 leaves the transport and
    # target pending -> both start at or after now.
    fix = Fixation(now=5, activities={0: ActivityFixation("completed", 0, 2, 0)}, arcs={}, placements=[])
    sol = solve(_chain_instance(), fixation=fix)
    assert sol.outcome == "optimal"
    src = next(p for p in sol.processing if p.node == ("S",))
    tgt = next(p for p in sol.processing if p.node == ("T",))
    (t,) = sol.transport
    assert (src.start, src.end, src.status) == (0, 2, "completed")
    assert t.start >= 5 and t.status is None       # pending transport not before now
    assert tgt.start >= t.end and tgt.status is None
    assert sol.makespan == t.start + 1 + 2         # move(1) + target(2) after now


def test_running_end_clamped_up_to_now_plus_margin():
    # Target running with expected end 4 but now=10 (overrun). With margin 0 the
    # fixed end is max(4, 10) = 10; with margin 5 it is max(4, 15) = 15.
    def run(margin):
        fix = Fixation(
            now=10,
            activities={
                0: ActivityFixation("completed", 0, 2, 0),
                1: ActivityFixation("running", 3, 4, 0),
            },
            arcs={0: ArcFixation("completed", 2, 3, 0)},
            placements=[],
        )
        return solve(_chain_instance(), fixation=fix, running_task_margin=margin)

    s0 = run(0)
    tgt0 = next(p for p in s0.processing if p.node == ("T",))
    assert (tgt0.start, tgt0.end, tgt0.status) == (3, 10, "running")
    assert s0.makespan == 10

    s5 = run(5)
    tgt5 = next(p for p in s5.processing if p.node == ("T",))
    assert tgt5.end == 15 and s5.makespan == 15


def test_fixed_transport_route_and_times_are_pinned():
    fix = Fixation(
        now=5,
        activities={
            0: ActivityFixation("completed", 0, 2, 0),
            1: ActivityFixation("completed", 3, 5, 0),
        },
        arcs={0: ArcFixation("completed", 2, 3, 0)},
        placements=[],
    )
    sol = solve(_chain_instance(), fixation=fix)
    assert sol.outcome == "optimal"
    (t,) = sol.transport
    assert (t.start, t.end, t.status) == (2, 3, "completed")
    assert t.option.transporter == "arm"
    assert sol.makespan == 5                        # everything fixed
