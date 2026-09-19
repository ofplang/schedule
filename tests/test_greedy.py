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

from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.greedy import construct
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    ArcInstance,
    BoundaryInfo,
    Instance,
    TransportOption,
    build_instance,
)
from ofplang.schedule.scheduler.model import Arc, Endpoint, Environment, Mode
from ofplang.schedule.scheduler.result import Solution
from ofplang.schedule.scheduler.workflow import parse_workflow

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
    assert construct(instance) is not None
    assert construct(held) is None


def test_the_same_instance_gives_the_same_schedule_twice():
    instance = _instance("reformatter")
    first, second = construct(instance), construct(instance)
    assert first is not None and second is not None
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
