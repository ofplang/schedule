"""A self-check on the plan this scheduler is about to hand out (SPEC §4.7.2).

The consumable model lives in the solver: a stock is a reservoir whose level must
stay in `[0, capacity]` (FORMULATION §11). The plan is then *rendered* from the
solved model, and the refill amounts are not read off the solver but derived --
§4.7.1 says a planned refill fills to capacity, and the reservoir offers no level
variable to write that against. So the document that leaves here is not literally
the thing the solver proved; it is a second computation over the same answer.

Two computations that must agree, and once agreed on nothing but an argument. This
module checks instead of arguing: replay the rendered plan's own activities against
the levels the run started from, and refuse to hand out a plan whose stocks go
outside `[0, capacity]`.

It is not a validator of anyone's input. Every input has already been checked --
the environment by its schema, the document's `inventories` against the
environment, the reported history by `status_inventory_inconsistent`. Reaching a
finding here means the solver's answer and the rendered document disagree, which is
a defect in this package. It is worth the cost anyway: the failure it guards is
silent. A plan that under-fills a stock schedules cleanly, reports `optimal`, and
runs dry in a real lab hours later.

**Completions before starts.** A refill that ends exactly when a draw starts is the
normal way a schedule packs work -- a device is released at one op's end and taken
at the next op's start, at the same instant. At such an instant the completion is
applied first and the level is checked, then the start (SPEC §4.7). Both levels are
real and both are checked: the one the refill leaves behind must fit in the device
(`<= capacity`), and the one the draw leaves behind must not be negative. This is
the order the solver was built under -- its reservoir sees completions at `2t` and
starts at `2t + 1` -- so this replay agrees with it by construction rather than by
argument.
"""

from __future__ import annotations

from ofplang.schedule.core.identifiers import parse_qualified_resource
from ofplang.schedule.scheduler.instance import Instance
from ofplang.schedule.scheduler.result import Solution
from ofplang.schedule.scheduler.status import COMPLETION, START, Fixation

# ---------------------------------------------------------------------------
# The schedule itself, read back against the constraints.
#
# 🔴 **Written from the formulation rather than from the implementation**, which
# is the whole of its value: a second reading of what the model means, agreeing
# with neither the solver nor the construction by construction. Three bugs in the
# constructive pass were all caught by it, and the first of them returned optimal
# values while breaking spot exclusion eighteen times (report section 45.6) --
# numbers that look right are not a schedule.
#
# It lived in the tests until the construction started returning plans rather
# than hints. A wrong hint costs nothing; a wrong plan is a wrong plan, so what
# was a test is now a gate (report section 46.13). It matters more than it did
# for another reason too: the construction now leans on `mobility` for its last
# resort (section 50.6), and this is the only reading of the rules that leans on
# neither.
# ---------------------------------------------------------------------------




def _declared_levels(env, inventories: dict | None) -> dict[tuple[str, str], int]:
    """Every stock the environment declares, at the level the run started with.

    Filled from the environment first so a stock the document does not name starts
    at 0 -- the same rule `_initial_levels` applies when the document is read.
    """
    levels = {
        (device_id, resource): 0
        for device_id, device in env.devices.items()
        for resource in device.resources
    }
    stated = (inventories or {}).get("levels") or {}
    if not isinstance(stated, dict):
        return levels
    for device_id, stock in stated.items():
        if not isinstance(stock, dict):
            continue
        for resource, level in stock.items():
            if isinstance(level, int) and not isinstance(level, bool):
                levels[(device_id, resource)] = level
    return levels


# Ordering key for one level change: the time it happens at, then whether it is a
# completion (0) or a start (1). Completions go first at a shared instant (§4.7),
# and the level is checked between them -- the same separation the solver's
# reservoir gets from mapping completions to `2t` and starts to `2t + 1`.
def _events(activities: list[dict]) -> dict[tuple[int, int], dict[tuple[str, str], int]]:
    """Every level change the plan contains, summed per instant-phase and per stock.

    A processing draws its `consumption` echo at its `start` -- consumption is taken
    when the activity starts (§4.7.2). A refill adds its `amounts` at its `end` --
    stock that has not landed cannot be drawn on. Nothing else touches a stock.
    """
    events: dict[tuple[int, int], dict[tuple[str, str], int]] = {}

    def change(time, phase, key, delta):
        at = (int(time), phase)
        events.setdefault(at, {})
        events[at][key] = events[at].get(key, 0) + delta

    for activity in activities:
        kind = activity.get("kind")
        if kind == "processing":
            for qualified, amount in (activity.get("consumption") or {}).items():
                parsed = parse_qualified_resource(qualified)
                if parsed is not None and isinstance(amount, int):
                    change(activity.get("start", 0), START, parsed, -amount)
        elif kind == "replenishment":
            device = activity.get("device")
            for resource, amount in (activity.get("amounts") or {}).items():
                if device is not None and isinstance(amount, int):
                    change(activity.get("end", 0), COMPLETION, (device, resource), amount)
    return events


def check_plan_inventories(plan: dict, env, inventories: dict | None) -> list[str]:
    """Every way the rendered `plan` drives a stock outside `[0, capacity]`.

    Returns one message per offending stock (the first moment it goes wrong, so a
    single mistake does not read as a dozen), or an empty list when the plan is
    coherent. An empty list is also the answer when nothing consumes: with no
    `consumption` echo anywhere there is no level to get wrong, which covers a
    resource-free environment and `--ignore-resources` alike (§4.7.3).
    """
    activities = plan.get("activities") or []
    events = _events(activities)
    if not events:
        return []

    levels = _declared_levels(env, inventories)
    # The levels are the levels as of `inventories.at` (§6.10, default 0), so the
    # events they already account for are the ones before it. Replaying those again
    # would report the plan as driving a stock out of range on the strength of
    # history the levels had already absorbed.
    #
    # 🔴 The cut is `(at, START)`, not `at`: the stated levels sit *between* that
    # instant's two phases -- after its refills, before its draws -- so a refill
    # landing exactly at `at` is one of the events they already account for.
    since = (inventories or {}).get("at")
    since = since if isinstance(since, int) and not isinstance(since, bool) else 0
    cut = (since, START)
    reported: dict[tuple[str, str], str] = {}
    for at in sorted(events):
        time, phase = at
        if at < cut:
            continue
        for key, delta in events[at].items():
            levels[key] = levels.get(key, 0) + delta
        for key in sorted(events[at]):
            if key in reported:
                continue
            device, resource = key
            entry = env.devices.get(device)
            capacity = entry.resources.get(resource) if entry is not None else None
            level = levels.get(key, 0)
            if level < 0 or (capacity is not None and level > capacity):
                what = "a refill" if phase == COMPLETION else "a draw"
                reported[key] = (
                    f"the plan leaves {device}.{resource} at {level} after {what} "
                    f"at time {time}, outside [0, {capacity}]"
                )
    return [reported[key] for key in sorted(reported)]


def _claims(instance: Instance, solution: Solution):
    """Every claim the schedule makes on a resource, as
    `(kind, resource, start, end, what)`.

    Read off the formulation: an activity holds each spot its mode binds and each
    device it runs on for its duration; a move holds its arm for its duration, its
    source spot until it lands, and its destination spot from when it sets off
    until the receiving activity starts (§7).
    """
    # `what` is only ever printed, so it is whatever names the claim best: a node
    # path for an activity, a made-up label for a move's several claims.
    claims: list[tuple[str, str, int, int, object]] = []
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


def check_schedule(
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
        # And not before the condition of a branch it enters or passes through
        # (design.md D64) -- unless it is history, which is not re-decided.
        if told is None:
            for gate in arc.gates:
                if move.start < by_activity[gate].end:
                    wrong.append(f"arc{index}: sets off before activity {gate} it waits for ends")
        if target.start < move.end:
            wrong.append(f"arc{index}: the destination starts before the material lands")

    for before, after in instance.precedence:
        if by_activity[after].start < by_activity[before].end:
            wrong.append(f"precedence {before}->{after} is not respected")

    # The exclusions. Two claims on one resource may touch but not overlap, and a
    # zero-length claim strictly inside another is a clash too -- which is what
    # `AddNoOverlap` does with a point (§Parameters, design.md D54).
    claims = _claims(instance, solution)
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
