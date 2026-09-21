"""The constructive first schedule, and whether what it produces is a schedule.

The point of these tests is the checker, not the timings. `construct` is a
second, independent implementation of what the model means, so the only way to
trust it is to read its answer back against the constraints themselves -- every
spot, device and arm exclusion of FORMULATION §7, the route agreement of §4, and
the precedence the arcs impose. `_violations` is that reading, written from the
formulation rather than from the implementation, and every case here asserts it
comes back empty.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ofplang.schedule.scheduler import greedy, mobility
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.greedy import REFUSALS, _refuse, construct
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    ArcInstance,
    BoundaryInfo,
    Instance,
    RefillCandidate,
    RefillOption,
    RelayInfo,
    TransportOption,
    build_instance,
)
from ofplang.schedule.scheduler.model import (
    Arc,
    Device,
    Endpoint,
    Environment,
    JobSpec,
    Mode,
)
from ofplang.schedule.scheduler.result import Solution
from ofplang.schedule.scheduler.status import ActivityFixation, Fixation, RefillFixation
from ofplang.schedule.scheduler.workflow import parse_workflow
from tests.schedutil import self_contained_examples

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_ENV = Environment("second", {}, (), {}, {})


def _intervals(instance: Instance, solution: Solution):
    """Every claim the schedule makes on a resource, as
    `(kind, resource, start, end, what)`.

    Read off the formulation: an activity holds each spot its mode binds and each
    device it runs on for its duration; a move holds its arm for its duration, its
    source spot until it lands, and its destination spot from when it sets off
    until the receiving activity starts (§7).
    """
    claims = []
    by_activity = {p.activity: p for p in solution.processing}
    for placement in solution.processing:
        spots = {
            *placement.mode.input_spots.values(),
            *placement.mode.output_spots.values(),
        }
        for spot in spots:
            claims.append(("spot", spot, placement.start, placement.end, placement.node))
        if placement.mode.device_access:
            for device in placement.mode.devices:
                claims.append(("device", device, placement.start, placement.end, placement.node))
    for index, move in enumerate(solution.transport):
        arc = instance.arcs[index]
        option = move.option
        if option.transporter is not None:
            claims.append(("arm", option.transporter, move.start, move.end, f"arc{index}"))
        if option.from_spot != option.to_spot:
            # A move reaches inside both machines, so it holds both for as long as
            # it takes -- once where they are the same machine, because an interval
            # registered twice would overlap itself.
            for device in {option.from_spot.split(".")[0], option.to_spot.split(".")[0]}:
                claims.append(("device", device, move.start, move.end, f"arc{index}.dev"))
            source = by_activity[arc.src_activity]
            claims.append(("spot", option.from_spot, source.end, move.end, f"arc{index}.from"))
            target = by_activity[arc.dst_activity]
            claims.append(("spot", option.to_spot, move.start, target.start, f"arc{index}.to"))
    return claims


def _violations(
    instance: Instance, solution: Solution, fixation: Fixation | None = None
) -> list[str]:
    """Everything wrong with the schedule, named. Empty means it is one.

    `fixation` is what a replan reported as already done. It is needed because
    **a fixed activity's length is not its mode's duration**: the model gives its
    interval a free size (`cpsat`, the `psz` variable) so that something which
    overran holds its resources for as long as it really took. Every other rule
    applies to it exactly as it does to pending work.
    """
    wrong: list[str] = []
    by_activity = {p.activity: p for p in solution.processing}
    if len(by_activity) != len(instance.activities):
        wrong.append(f"placed {len(by_activity)} of {len(instance.activities)} activities")
    if len(solution.transport) != len(instance.arcs):
        wrong.append(f"placed {len(solution.transport)} of {len(instance.arcs)} moves")
    if wrong:
        return wrong

    for placement in solution.processing:
        act = instance.activities[placement.activity]
        if placement.mode not in act.modes:
            wrong.append(f"{placement.node}: mode is not one the activity offers")
        if placement.start < 0:
            wrong.append(f"{placement.node}: starts before zero")
        # A boundary node's length is not its mode's duration: an input node takes
        # no time and an output node runs to the makespan (§Activities, §8).
        kind = None if act.boundary is None else act.boundary.kind
        reported = (fixation.activities if fixation is not None else {}).get(placement.activity)
        if kind is None and reported is None:
            if placement.end != placement.start + placement.mode.duration:
                wrong.append(f"{placement.node}: end is not start plus the mode's duration")
        elif kind == "input":
            if placement.end != placement.start:
                wrong.append(f"input node {placement.activity}: takes time")
        elif kind == "output" and placement.end != solution.makespan:
            wrong.append(
                f"output node {placement.activity}: ends at {placement.end}, "
                f"not at the makespan {solution.makespan}"
            )

    # §8: the makespan is the last real end, counting a delivery into an output node
    # and counting neither an output nor a held node's own end.
    counted = [
        p.end
        for p in solution.processing
        if (b := instance.activities[p.activity].boundary) is None
        or b.kind not in ("output", "held")
    ]
    counted += [
        move.end
        for index, move in enumerate(solution.transport)
        if (b := instance.activities[instance.arcs[index].dst_activity].boundary) is not None
        and b.kind == "output"
    ]
    if counted and solution.makespan != max(counted):
        wrong.append(f"makespan is {solution.makespan}, but the last real end is {max(counted)}")

    for index, move in enumerate(solution.transport):
        arc = instance.arcs[index]
        source, target = by_activity[arc.src_activity], by_activity[arc.dst_activity]
        option = move.option
        if option not in arc.options:
            wrong.append(f"arc{index}: route is not one the arc offers")
            continue
        # §4: a route names the modes at both of its ends, and they have to be
        # the modes actually chosen there.
        if instance.activities[arc.src_activity].modes[option.src_mode_index] != source.mode:
            wrong.append(f"arc{index}: route's source mode is not the source's chosen mode")
        if instance.activities[arc.dst_activity].modes[option.dst_mode_index] != target.mode:
            wrong.append(f"arc{index}: route's destination mode is not the destination's")
        # A fixed move's length is free for the same reason a fixed activity's is
        # (`cpsat`, the `tbsz` variable): a move that overran held its arm and
        # both machines for as long as it really took.
        told = (fixation.arcs if fixation is not None else {}).get(index)
        if told is None and move.end != move.start + option.duration:
            wrong.append(f"arc{index}: end is not start plus the route's duration")
        if move.start < source.end:
            wrong.append(f"arc{index}: sets off before the source activity ends")
        if target.start < move.end:
            wrong.append(f"arc{index}: the destination starts before the material lands")

    for source, target in instance.precedence:
        if by_activity[target].start < by_activity[source].end:
            wrong.append(f"precedence {source}->{target} is not respected")

    # The exclusions. Two claims on one resource may touch but not overlap, and a
    # zero-length claim strictly inside another is a clash too -- which is what
    # `AddNoOverlap` does with a point (§Parameters, design.md D54).
    claims = _intervals(instance, solution)
    for i, (kind, name, start, end, what) in enumerate(claims):
        for other_kind, other_name, other_start, other_end, other in claims[i + 1 :]:
            if (kind, name) != (other_kind, other_name):
                continue
            if start == end:
                clash = other_start < start < other_end
            elif other_start == other_end:
                clash = start < other_start < end
            else:
                clash = start < other_end and other_start < end
            if clash:
                wrong.append(
                    f"{kind} {name}: {what} [{start},{end}] overlaps {other} "
                    f"[{other_start},{other_end}]"
                )
    return wrong


def _instance(name: str) -> Instance:
    workflow, _ = parse_workflow(EXAMPLES / f"{name}.workflow.yaml")
    environment, _ = load_environment(EXAMPLES / f"{name}.env.yaml")
    instance, diags = build_instance(workflow, environment)
    assert instance is not None, [d.code for d in diags.items]
    return instance


def test_a_two_step_workflow_is_scheduled_and_the_schedule_is_one():
    instance = _instance("simple")
    solution = construct(instance)
    assert solution is not None
    assert _violations(instance, solution) == []


def test_an_internal_move_keeps_both_ends_on_their_own_device():
    instance = _instance("internal_move")
    solution = construct(instance)
    assert solution is not None
    assert _violations(instance, solution) == []


def test_two_arms_are_used_without_either_carrying_two_moves_at_once():
    instance = _instance("two_arms")
    solution = construct(instance)
    assert solution is not None
    assert _violations(instance, solution) == []


def test_a_reformatter_chain_is_scheduled():
    instance = _instance("reformatter")
    solution = construct(instance)
    assert solution is not None
    assert _violations(instance, solution) == []


@pytest.mark.parametrize("name", ["consumable", "storage"])
def test_a_stock_no_document_puts_a_number_to_constrains_nothing(name):
    # Both of these build. `consumable` draws on a stock, but an instance with no
    # execution document states no `inventories.levels`, and the model builds no
    # reservoir without them (`cpsat._add_resources` returns at once) -- so there
    # is nothing here for the greedy to respect either. Inventing a constraint the
    # model does not have would be the error.
    instance = _instance(name)
    solution = construct(instance)
    assert solution is not None
    assert _violations(instance, solution) == []
    assert solution.replenishment == ()


def test_a_held_spot_is_refused_where_an_output_node_is_not():
    # A held node runs to the horizon (§6.12) and is not work, which list
    # scheduling has no way to place; an output node runs to the makespan, which it
    # can. So one is refused and the other is not.
    instance = _instance("simple")
    held = replace(
        instance,
        activities=(
            replace(instance.activities[-1], boundary=BoundaryInfo(kind="held", since=0)),
            *instance.activities[:-1],
        ),
    )
    plain = construct(instance)
    assert plain is not None
    assert _violations(instance, plain) == []
    assert construct(held) is None


def test_the_same_instance_gives_the_same_schedule_twice():
    instance = _instance("reformatter")
    first, second = construct(instance), construct(instance)
    assert first is not None and second is not None
    assert _violations(instance, first) == []
    assert first.makespan == second.makespan
    assert [(p.activity, p.mode.id, p.start) for p in first.processing] == [
        (p.activity, p.mode.id, p.start) for p in second.processing
    ]


def test_the_outcome_is_feasible_and_never_claims_optimality():
    # A constructed schedule is one schedule; nothing about it says no better one
    # exists, and saying so would be a lie the caller could act on.
    instance = _instance("simple")
    solution = construct(instance)
    assert solution is not None
    assert _violations(instance, solution) == []
    assert solution.outcome == "feasible"
    assert solution.objective_values == (solution.makespan,)


def test_a_finished_product_parks_where_it_is_least_wanted():
    # Two places to rest: a shelf nothing else uses, and the machine's own bay,
    # which the work needs. Arriving on the machine's bay is no slower, so the
    # earliest-landing rule used to take it -- and a product parks until the run is
    # over, which is how every later job found the machine occupied.
    maker = ActivityInstance(
        ("make",),
        "make",
        (Mode("m", ("mk",), 1, {}, {"o": "mk.bay"}),),
    )
    other = ActivityInstance(
        ("other",),
        "other",
        (Mode("m", ("mk",), 1, {"i": "mk.bay"}, {"o": "mk.bay"}),),
    )
    output = ActivityInstance(
        (),
        "",
        (
            Mode("machine", (), 0, {"i": "mk.bay"}, {}),
            Mode("shelf", (), 0, {"i": "shelf.bay"}, {}),
        ),
        boundary=BoundaryInfo(kind="output"),
    )
    arc = ArcInstance(
        Arc(Endpoint(("make",), "o"), Endpoint((), "out")),
        0,
        2,
        (
            TransportOption(0, 0, "arm0", "mk.bay", "mk.bay", 0),
            TransportOption(0, 1, "arm0", "mk.bay", "shelf.bay", 1),
        ),
    )
    instance = Instance(_ENV, "second", (maker, other, output), (arc,), ((0, 1),))
    solution = construct(instance)
    assert solution is not None
    assert _violations(instance, solution) == []
    resting = solution.processing[2].mode.input_spots["i"]
    assert resting == "shelf.bay"


# ---------------------------------------------------------------------------
# The last resort: a ring the forward passes cannot get round.
#
# Stage spots in a cycle, one place each, and more Objects than the cycle can
# hold. Every forward pass fills the ring and stops -- no ordering of the ready
# set undoes that, which is what the benchmark's `s1_b6r4_pool1` showed
# (report section 49.3). The walk in `mobility` backtracks, so it finds an
# order, and `construct` lays times over it.
# ---------------------------------------------------------------------------


# Stage durations, from the plate-batch family: uneven, because even ones let
# the passes fall into step and the standstill is about them not doing that.
_STAGE_DURATIONS = (1, 3, 1, 10, 1)


def _batch(objects: int, stages: int, laps: int) -> Instance:
    """`objects` Objects going `laps` times round `stages` single-place stages.

    The fork and the join are the point. One activity binds every loading spot
    at once and hands out an Object per spot, and one takes them all back --
    which is `plate_batch`'s shape, and the reason the forward passes lose: from
    the first instant every Object is resting somewhere and wanting to be pushed
    into the ring, so the ring fills and no ordering of the ready set unfills it.
    A ring without the fork does *not* defeat them (measured).
    """
    spots = [f"st{k}.core" for k in range(stages)]
    homes = [f"loader.s{b}" for b in range(objects)]
    activities = [
        ActivityInstance(
            ("source",),
            "source",
            (Mode("m", ("loader",), 1, {}, {f"p{b}": homes[b] for b in range(objects)}),),
        )
    ]
    arcs: list[ArcInstance] = []
    tails: list[tuple[int, str]] = []
    for b in range(objects):
        route = [spots[k % stages] for k in range(stages * laps)]
        first = len(activities)
        for step, spot in enumerate(route):
            activities.append(
                ActivityInstance(
                    (f"b{b}s{step}",),
                    "stage",
                    (
                        Mode(
                            "m",
                            (spot.split(".")[0],),
                            _STAGE_DURATIONS[step % len(_STAGE_DURATIONS)],
                            {"i": spot},
                            {"o": spot},
                        ),
                    ),
                )
            )
        hops = [homes[b], *route]
        for step in range(len(hops) - 1):
            source = 0 if step == 0 else first + step - 1
            arcs.append(
                ArcInstance(
                    Arc(Endpoint(("source",), f"p{b}"), Endpoint((f"b{b}s{step}",), "i")),
                    source,
                    first + step,
                    (TransportOption(0, 0, "arm", hops[step], hops[step + 1], 1),),
                )
            )
        tails.append((first + len(route) - 1, route[-1]))
    sink = len(activities)
    activities.append(
        ActivityInstance(
            ("sink",),
            "sink",
            (Mode("m", ("loader",), 1, {f"p{b}": homes[b] for b in range(objects)}, {}),),
        )
    )
    for b, (tail, spot) in enumerate(tails):
        arcs.append(
            ArcInstance(
                Arc(Endpoint((f"b{b}",), "o"), Endpoint(("sink",), f"p{b}")),
                tail,
                sink,
                (TransportOption(0, 0, "arm", spot, homes[b], 1),),
            )
        )
    return Instance(_ENV, "second", tuple(activities), tuple(arcs), ())


def test_a_ring_the_forward_passes_cannot_get_round_is_still_scheduled():
    # 🔴 The case the last resort exists for. The ring fills, every Object's
    # next spot holds another Object's material, and no priority rule undoes a
    # state already arrived at.
    instance = _batch(objects=3, stages=3, laps=2)
    built = construct(instance)
    assert built is not None
    assert _violations(instance, built) == []


def test_without_the_walk_that_ring_comes_out_empty(monkeypatch):
    # The control for the test above: it is the walk that rescues this, and not
    # something else that happened to change.
    instance = _batch(objects=3, stages=3, laps=2)
    monkeypatch.setattr(greedy.mobility, "find_order", lambda _instance: None)
    assert construct(instance) is None


def test_the_replayed_schedule_is_the_same_one_twice():
    # The walk is deterministic and so is the timing laid over it, which is what
    # lets a plan be compared against the one before it.
    instance = _batch(objects=4, stages=3, laps=2)
    first = construct(instance)
    second = construct(instance)
    assert first is not None and second is not None
    assert _violations(instance, first) == []
    assert first.processing == second.processing
    assert first.transport == second.transport


@pytest.mark.parametrize(
    "objects,stages,laps", [(3, 3, 2), (4, 3, 4), (5, 4, 4), (6, 4, 4)]
)
def test_every_size_of_that_ring_comes_back_a_schedule(objects, stages, laps):
    # The checker is the point, not the makespan: a replayed order is a schedule
    # only if it survives being read back against the constraints themselves.
    instance = _batch(objects, stages, laps)
    built = construct(instance)
    assert built is not None
    assert _violations(instance, built) == []


def test_an_instance_with_no_way_through_still_comes_out_empty():
    # A ring is one thing; a trap is another. Two Objects alternating between
    # two places cannot both finish however anybody orders it, and the walk
    # proves that rather than papering over it -- so the greedy returns nothing,
    # which is the honest answer and the one `mobility` refuses the plan on.
    bays = ("lab.a", "lab.b")
    activities: list[ActivityInstance] = []
    arcs: list[ArcInstance] = []
    for job in range(2):
        first = len(activities)
        entry = f"gate.s{job}"
        activities.append(
            ActivityInstance(
                (), "", (Mode("in", (), 0, {}, {"o": entry}),), boundary=BoundaryInfo(kind="input")
            )
        )
        for step in range(5):
            spot = bays[step % 2]
            activities.append(
                ActivityInstance(
                    (f"j{job}s{step}",),
                    "work",
                    (Mode("m", (spot.split(".")[0],), 1, {"i": spot}, {"o": spot}),),
                )
            )
        activities.append(
            ActivityInstance(
                (),
                "",
                tuple(Mode(bay, (), 0, {"i": bay}, {}) for bay in bays),
                boundary=BoundaryInfo(kind="output"),
            )
        )
        arcs.append(
            ArcInstance(
                Arc(Endpoint((), "in"), Endpoint((f"j{job}s0",), "i")),
                first,
                first + 1,
                (TransportOption(0, 0, "arm", entry, bays[0], 1),),
            )
        )
        for step in range(4):
            arcs.append(
                ArcInstance(
                    Arc(Endpoint((f"j{job}s{step}",), "o"), Endpoint((f"j{job}s{step + 1}",), "i")),
                    first + 1 + step,
                    first + 2 + step,
                    (TransportOption(0, 0, "arm", bays[step % 2], bays[(step + 1) % 2], 1),),
                )
            )
        last = first + 5
        arcs.append(
            ArcInstance(
                Arc(Endpoint((f"j{job}s4",), "o"), Endpoint((), "out")),
                last,
                last + 1,
                tuple(
                    TransportOption(0, k, "arm", bays[0], bay, 1) for k, bay in enumerate(bays)
                ),
            )
        )
    assert construct(Instance(_ENV, "second", tuple(activities), tuple(arcs), ())) is None


# ---------------------------------------------------------------------------
# The promise, swept over the worked examples.
#
# What the greedy is for: a plan CP-SAT cannot answer should still get *an*
# answer, however poor. So these sweep for coverage and for correctness, and
# say nothing about speed or about makespan -- being slower than the solver on
# a plan the solver can crack is not a defect here.
# ---------------------------------------------------------------------------

_EXAMPLES = self_contained_examples()


@pytest.mark.parametrize("name", _EXAMPLES)
def test_every_example_is_either_refused_by_name_or_comes_back_a_schedule(name):
    """No third outcome. Standing still without a reason is the failure mode
    this whole slice exists to remove, so it is asserted away here rather than
    noticed later on a benchmark."""
    instance = _instance(name)
    solution = construct(instance)
    reason = _refuse(instance, None, ())
    if reason is not None:
        assert reason in REFUSALS
        assert solution is None, f"{name} was refused as {reason} and still built something"
        return
    assert solution is not None, f"{name} is in scope and came back with nothing"
    assert _violations(instance, solution) == []


@pytest.mark.parametrize("name", _EXAMPLES)
def test_a_way_through_and_in_scope_means_an_answer_comes_back(name):
    """The coverage promise, stated as a test.

    `mobility.find_order` is the arbiter of whether a way through exists at all.
    Where it finds one and the shape is in scope, the greedy has no excuse: the
    last resort replays that very order (report section 50). This is what
    "returns something" means, and it is the only guarantee claimed.
    """
    instance = _instance(name)
    if _refuse(instance, None, ()) is not None:
        pytest.skip("out of scope, which is a different statement")
    if mobility.find_order(instance) is None:
        pytest.skip("no way through, or the walk was stopped -- neither is a promise")
    assert construct(instance) is not None


# ---------------------------------------------------------------------------
# Every refusal, by name.
#
# `REFUSALS` lists six shapes the construction declines, and until now only one
# of them was asserted anywhere. The point is not that these shapes are hard --
# it is that a refusal quietly disappearing is invisible while the construction
# is only a hint, and becomes a wrong plan the moment it is returned as one.
# `bound` is the sharp case: a promise this stops declining is a promise it
# starts breaking.
# ---------------------------------------------------------------------------


def _in_scope() -> Instance:
    """The smallest instance the construction accepts: one activity, one spot."""
    return Instance(
        _ENV,
        "second",
        (ActivityInstance(("only",), "work", (Mode("m", ("lab",), 1, {}, {"o": "lab.a"}),)),),
        (),
        (),
    )


def test_the_smallest_instance_is_in_scope():
    # The control. Each refusal below adds exactly one thing to this, so if this
    # were already refused the tests below would prove nothing.
    instance = _in_scope()
    assert _refuse(instance, None, ()) is None
    built = construct(instance)
    assert built is not None
    assert _violations(instance, built) == []


def test_a_running_refill_is_declined():
    # A refill already under way is a fixed future increase the levels at `now`
    # do not yet carry. Nothing in the corpus has one, so rather than carry
    # history this has never seen, it is declined.
    instance = _in_scope()
    fixation = Fixation(
        now=5,
        activities={},
        arcs={},
        levels={("lab", "stock"): 1},
        replenishments={
            "r0": RefillFixation(
                status="running", start=0, end=9,
                device="lab", replenisher="hand", amounts={"stock": 4},
            )
        },
    )
    assert _refuse(instance, fixation, ()) == "running refill"
    assert construct(instance, fixation=fixation) is None


def test_a_transport_junction_is_declined():
    base = _in_scope()
    junction = replace(
        base.activities[0],
        relay=RelayInfo(Arc(Endpoint(("a",), "o"), Endpoint(("b",), "i")), 0),
    )
    instance = replace(base, activities=(junction,))
    assert _refuse(instance, None, ()) == "relay"
    assert construct(instance) is None


def test_an_occupied_spot_is_declined():
    base = _in_scope()
    occupied = replace(base.activities[0], boundary=BoundaryInfo(kind="held", since=0))
    instance = replace(base, activities=(occupied,))
    assert _refuse(instance, None, ()) == "held"
    assert construct(instance) is None


def test_cancelled_work_is_declined():
    # Work a stopped job abandoned is pinned to the instant that job stopped,
    # and that instant is derived from every *other* activity of the job
    # (`cpsat.stopped_at`). Nothing in the corpus has any, so the rule is not
    # re-derived here on no evidence.
    instance = _in_scope()
    fixation = Fixation(now=5, activities={0: ActivityFixation("cancelled", 0, 0, 0)}, arcs={})
    assert _refuse(instance, fixation, ()) == "cancelled"
    assert construct(instance, fixation=fixation) is None


def test_a_promised_completion_is_declined():
    # 🔴 The one that matters most. C_j is measured over a job's own work --
    # not its boundary nodes, and not the parts of a move that are the material
    # resting rather than travelling -- so checking a promise here would mean
    # checking it against a number the model does not use.
    instance = _in_scope()
    promised = (JobSpec(id="job1", release=0, bound=100),)
    assert _refuse(instance, None, promised) == "bound"
    assert construct(instance, jobs=promised) is None


def test_a_fresh_roster_is_not_declined():
    # The other side of the promise test: a job without one is ordinary work.
    instance = _in_scope()
    fresh = (JobSpec(id="job1", release=0),)
    assert _refuse(instance, None, fresh) is None
    assert construct(instance, jobs=fresh) is not None


def test_every_listed_refusal_has_a_test_above():
    # The list and the tests drift apart silently otherwise: a seventh shape
    # added to `REFUSALS` with no test would look exactly like six with six.
    covered = {
        "running refill",
        "relay",
        "held",
        "cancelled",
        "bound",
    }
    assert set(REFUSALS) == covered

# ---------------------------------------------------------------------------
# Stocks.
#
# A draw is taken in full at an activity's **start**, a refill lands at its
# **end** and fills to capacity, and the level is held within `[0, capacity]` at
# every event (FORMULATION §11). Where a document states no levels there is no
# reservoir at all and nothing to respect.
# ---------------------------------------------------------------------------


def _drawing(draws: int, amount: int, duration: int = 2) -> Instance:
    """`draws` activities in a row on one machine, each taking `amount` from its
    stock. Each has a spot of its own, so nothing here is about spots."""
    activities = tuple(
        ActivityInstance(
            (f"take{k}",),
            "work",
            (
                Mode(
                    "m",
                    ("lab",),
                    duration,
                    {},
                    {"o": f"lab.bay{k}"},
                    consumption={"lab.tips": amount},
                ),
            ),
        )
        for k in range(draws)
    )
    return Instance(_ENV, "second", activities, (), ())


def _stocked(levels: dict[tuple[str, str], int]) -> Fixation:
    return Fixation(now=0, activities={}, arcs={}, levels=levels)


def _lab_with_tips(instance: Instance, capacity: int, refills: int = 0) -> Instance:
    """The same instance in a laboratory whose `lab` holds `capacity` tips, with
    `refills` ways to top it up.

    The environment matters here where it does not elsewhere in this file: the
    capacity a refill fills to is read off the device (`greedy._capacity`).
    """
    bays = frozenset(
        spot.split(".")[1]
        for act in instance.activities
        for mode in act.modes
        for spot in (*mode.input_spots.values(), *mode.output_spots.values())
    )
    devices = {
        "lab": Device("lab", bays, {"tips": capacity}),
        "hand": Device("hand", frozenset()),
    }
    return replace(
        instance,
        env=Environment("second", devices, (), {}, {}),
        replenishments=tuple(
            RefillCandidate(f"r{k}", "lab", 0, (RefillOption("hand", 1),), ("tips",))
            for k in range(refills)
        ),
    )


def test_draws_that_fit_need_no_refill():
    instance = _lab_with_tips(_drawing(draws=3, amount=2), capacity=10)
    built = construct(instance, fixation=_stocked({("lab", "tips"): 6}))
    assert built is not None
    assert _violations(instance, built) == []
    assert built.replenishment == ()


def test_draws_that_do_not_fit_and_cannot_be_topped_up_come_back_empty():
    # 🔴 A stock nothing can refill only ever falls, so this is not a matter of
    # ordering -- the reagent runs out whatever anybody does, and a plan whose
    # reagent runs out is not a plan.
    instance = _lab_with_tips(_drawing(draws=4, amount=3), capacity=10)
    assert construct(instance, fixation=_stocked({("lab", "tips"): 6})) is None


def test_a_refill_is_placed_where_the_level_would_break():
    instance = _lab_with_tips(_drawing(draws=4, amount=3), capacity=12, refills=1)
    built = construct(instance, fixation=_stocked({("lab", "tips"): 6}))
    assert built is not None
    assert _violations(instance, built) == []
    assert len(built.replenishment) == 1
    refill = built.replenishment[0]
    assert refill.device == "lab"
    assert refill.amounts == {"tips": 12}


def test_the_refill_lands_before_the_draw_that_needed_it():
    # 🔴 The property two wrong versions got wrong. A refill fills to capacity, so
    # one placed too early is spent on the draws in between and leaves the draw it
    # was placed for exactly as short as it was.
    instance = _lab_with_tips(_drawing(draws=4, amount=3), capacity=12, refills=1)
    built = construct(instance, fixation=_stocked({("lab", "tips"): 6}))
    assert built is not None
    level, landed = 6, [r.end for r in built.replenishment]
    for placement in sorted(built.processing, key=lambda p: p.start):
        amount = placement.mode.consumption.get("lab.tips", 0)
        while landed and landed[0] <= placement.start:
            level = 12
            landed.pop(0)
        assert level >= amount, f"the level went short at {placement.start}"
        level -= amount


def test_a_stock_is_ignored_when_the_document_states_no_level():
    # The same instance that fails above, with the levels left unstated: no
    # reservoir is built, so it schedules.
    instance = _lab_with_tips(_drawing(draws=4, amount=3), capacity=10)
    built = construct(instance, fixation=_stocked({}))
    assert built is not None
    assert _violations(instance, built) == []


# ---------------------------------------------------------------------------
# Replanning.
#
# What already ran is history: its mode, its route and its times are given, and
# the pass starts from them rather than choosing them. Everything still to do
# waits until `now` -- except the entry material, which is a fact about the
# world and is pinned at its job's release however late `now` is.
# ---------------------------------------------------------------------------


def _two_steps() -> Instance:
    """`first` on one machine, then `second` on another, with a move between."""
    activities = (
        ActivityInstance(("first",), "work", (Mode("m", ("a",), 4, {}, {"o": "a.bay"}),)),
        ActivityInstance(("second",), "work", (Mode("m", ("b",), 6, {"i": "b.bay"}, {}),)),
    )
    arcs = (
        ArcInstance(
            Arc(Endpoint(("first",), "o"), Endpoint(("second",), "i")),
            0,
            1,
            (TransportOption(0, 0, "arm", "a.bay", "b.bay", 1),),
        ),
    )
    return Instance(_ENV, "second", activities, arcs, ())


def test_what_already_ran_keeps_the_times_it_reported():
    instance = _two_steps()
    fixation = Fixation(
        now=20,
        activities={0: ActivityFixation("completed", 3, 7, 0)},
        arcs={},
    )
    built = construct(instance, fixation=fixation)
    assert built is not None
    assert _violations(instance, built, fixation) == []
    done = next(p for p in built.processing if p.activity == 0)
    assert (done.start, done.end) == (3, 7)


def test_what_is_left_waits_until_now():
    instance = _two_steps()
    fixation = Fixation(
        now=20, activities={0: ActivityFixation("completed", 3, 7, 0)}, arcs={}
    )
    built = construct(instance, fixation=fixation)
    assert built is not None
    assert _violations(instance, built, fixation) == []
    # The move and the activity that receives it are both still to do, so
    # neither may set off before `now` even though the material was ready at 7.
    assert built.transport[0].start >= 20
    assert next(p for p in built.processing if p.activity == 1).start >= 20


def test_a_running_activity_holds_its_machine_until_now_plus_the_margin():
    # 🔴 An overrun must not be fixed to a finish in the past (FORMULATION §9).
    # The reported end is 10 and `now` is 20, so it is still going at 20 and
    # holds its machine to 20 + margin -- and what follows waits for that, not
    # for the 10 it claimed.
    instance = _two_steps()
    fixation = Fixation(
        now=20, activities={0: ActivityFixation("running", 3, 10, 0)}, arcs={}
    )
    built = construct(instance, fixation=fixation, running_task_margin=5)
    assert built is not None
    assert _violations(instance, built, fixation) == []
    running = next(p for p in built.processing if p.activity == 0)
    assert (running.start, running.end) == (3, 25)
    assert built.transport[0].start >= 25


def test_a_replan_that_reports_a_mode_the_activity_lacks_is_refused():
    instance = _two_steps()
    fixation = Fixation(
        now=5, activities={0: ActivityFixation("completed", 0, 4, 7)}, arcs={}
    )
    assert construct(instance, fixation=fixation) is None


def test_a_history_that_contradicts_itself_about_a_machine_is_refused():
    # Two completed activities on one machine at the same time is not something
    # to arrange around; it is a report that cannot be true.
    activities = (
        ActivityInstance(("one",), "work", (Mode("m", ("a",), 4, {}, {"o": "a.bay1"}),)),
        ActivityInstance(("two",), "work", (Mode("m", ("a",), 4, {}, {"o": "a.bay2"}),)),
    )
    instance = Instance(_ENV, "second", activities, (), ())
    fixation = Fixation(
        now=20,
        activities={
            0: ActivityFixation("completed", 0, 10, 0),
            1: ActivityFixation("completed", 5, 15, 0),
        },
        arcs={},
    )
    assert construct(instance, fixation=fixation) is None
