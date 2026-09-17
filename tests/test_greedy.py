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
from ofplang.schedule.scheduler.instance import BoundaryInfo, Instance, build_instance
from ofplang.schedule.scheduler.result import Solution
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


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
        if placement.end != placement.start + placement.mode.duration:
            wrong.append(f"{placement.node}: end is not start plus the mode's duration")
        if placement.start < 0:
            wrong.append(f"{placement.node}: starts before zero")

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


def test_a_boundary_node_is_refused():
    # An output or held node's length is a decision rather than a duration, and
    # list scheduling has no way to choose it. Marking one activity as a boundary
    # node is enough to take the instance out of scope, and refusing says so.
    instance = _instance("simple")
    marked = replace(
        instance,
        activities=(
            replace(instance.activities[0], boundary=BoundaryInfo(kind="output")),
            *instance.activities[1:],
        ),
    )
    assert construct(instance) is not None
    assert construct(marked) is None


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
