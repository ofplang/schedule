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
from ofplang.schedule.scheduler.model import Arc, Endpoint, Environment, JobSpec, Mode
from ofplang.schedule.scheduler.result import Solution
from ofplang.schedule.scheduler.status import ActivityFixation, Fixation
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


def _violations(instance: Instance, solution: Solution) -> list[str]:
    """Everything wrong with the schedule, named. Empty means it is one."""
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
        if kind is None:
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
        if move.end != move.start + option.duration:
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
def test_the_shapes_outside_the_scope_are_refused_rather_than_guessed(name):
    # `consumable` draws stock and needs refills; `storage` is in scope, so this
    # asserts the refusal is about the stock and not about the example.
    instance = _instance(name)
    solution = construct(instance)
    if name == "consumable":
        assert solution is None
    else:
        assert solution is not None
        assert _violations(instance, solution) == []


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


def test_a_refill_is_declined():
    instance = replace(
        _in_scope(),
        replenishments=(
            RefillCandidate("r0", "lab", 0, (RefillOption("hand", 1),), ("lab.stock",)),
        ),
    )
    assert _refuse(instance, None, ()) == "replenishment"
    assert construct(instance) is None


def test_a_stock_draw_is_declined():
    base = _in_scope()
    drawing = replace(
        base.activities[0],
        modes=(Mode("m", ("lab",), 1, {}, {"o": "lab.a"}, consumption={"lab.stock": 1}),),
    )
    instance = replace(base, activities=(drawing,))
    assert _refuse(instance, None, ()) == "consumption"
    assert construct(instance) is None


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


def test_a_replan_is_declined():
    instance = _in_scope()
    fixation = Fixation(now=5, activities={0: ActivityFixation("completed", 0, 1, 0)}, arcs={})
    assert _refuse(instance, fixation, ()) == "fixation"
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
        "replenishment",
        "consumption",
        "relay",
        "held",
        "fixation",
        "bound",
    }
    assert set(REFUSALS) == covered
