"""A constructive first schedule: list scheduling over the built instance.

This is a second solver, not a helper. It takes what `cpsat.solve` takes and
returns what `cpsat.solve` returns -- a `Solution` naming a concrete mode, route,
spot and machine for everything, with times satisfying the constraints of
FORMULATION §7 -- and it knows nothing about CP-SAT. What the caller does with
that is the caller's business: today `cpsat.solve` hands it to the solver as a
`solution_hint`, and the same object would serve as a plan.

**Why a first schedule is worth constructing at all.** Reducing interchangeable
resources away (Part III) left the model proving the optimal *value* in a fifth
of a second while unable to exhibit a schedule attaining it: on the widest
synthetic instances CP-SAT's one-worker search returns nothing at all, and its
eight-worker portfolio spends three quarters of a minute reaching a schedule no
better than a serial one. Hinting a constructed schedule closes those instances
at optimal with zero branches (`dev-notes/report-model-size-and-presolve.md`
§29.6). The bound and the witness had ended up in different places, and this is
the witness.

**It may refuse, and refusing is the safe answer.** `construct` returns `None`
rather than a schedule it is unsure of. The shapes it declines are listed in
`REFUSALS`, and they were chosen by counting what instances actually contain:
every case-study laboratory carries refills and stock draws, and none of the
instances that return nothing today carries any of them. So the refusals are
disjoint from the rows this is for. Growing the coverage is a later question, and
one that has to be answered before a constructed schedule is ever *returned* to a
caller rather than hinted -- a wrong hint costs nothing, a wrong plan is a wrong
plan.

**No backtracking.** Activities are placed in one pass, each as early as the
resources allow, and a placement is never revisited. Where a placement cannot be
made the answer is `None`, not a search. That is the point: the solver is the
thing that searches, and this has to be fast enough to be free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ofplang.schedule.core import objective as objective_stages
from ofplang.schedule.core.identifiers import parse_qualified_spot
from ofplang.schedule.scheduler.instance import Instance, TransportOption
from ofplang.schedule.scheduler.model import JobSpec, Mode
from ofplang.schedule.scheduler.result import ProcessingResult, Solution, TransportResult
from ofplang.schedule.scheduler.status import Fixation

# Shapes `construct` declines, and why each needs more than list scheduling.
REFUSALS = {
    "replenishment": "a refill is scheduled work whose need depends on the draws",
    "consumption": "a stock draw makes an activity's admissibility depend on the order",
    "relay": "a transport junction is a chain whose legs share one arc",
    "boundary": "a boundary node's length is a decision, not a duration",
    "fixation": "a replan has activities already placed, which is a different problem",
    "jobs": "a joint plan's releases and bounds constrain what may be placed when",
}

# How many times a start may be pushed later before this gives up. Each push
# moves strictly forward, so the loop ends on its own; the cap is here so that a
# shape nobody anticipated ends in `None` rather than in a long wait.
_PUSHES = 64


@dataclass
class _Timeline:
    """When one resource is busy. Intervals are closed on the left and open on
    the right, so two uses that merely touch do not conflict -- which is what
    `AddNoOverlap` means by not overlapping.

    `pending` is a reservation with no end yet: material delivered to a bay,
    which is the bay's occupant until the activity receiving it starts. Until it
    closes nothing else may have the resource. Handing the bay to someone else
    and discovering the clash later is not an option, there being no
    backtracking here to recover with.
    """

    busy: list[tuple[int, int]] = field(default_factory=list)
    pending: int | None = None

    def earliest(self, after: int, duration: int) -> int | None:
        """The first instant at or after `after` where `duration` fits, or None
        while a pending reservation makes the answer unknowable."""
        if self.pending is not None:
            return None
        start = after
        # One pass suffices because the list is kept sorted: being pushed past one
        # interval can only push past later ones, never back before earlier ones.
        for busy_start, busy_end in self.busy:
            if start + duration <= busy_start:
                break
            if start < busy_end:
                start = busy_end
        return start

    def clear_from(self, after: int, *, ours: bool = False) -> int | None:
        """The first instant at or after `after` with nothing booked from then on,
        which is what taking a resource for an unknown length needs.

        `ours` says the open hold on this resource is the caller's own and about
        to be closed -- material that arrived in a bay and is about to be worked
        on there, and will go on resting in it afterwards.
        """
        if self.pending is not None and not ours:
            return None
        return max([after, *(end for _, end in self.busy)])

    def take(self, start: int, end: int) -> None:
        self.busy.append((start, end))
        self.busy.sort()

    def hold(self, start: int) -> None:
        """Take the resource from `start` for as long as it turns out to take."""
        self.pending = start

    def release(self, end: int) -> None:
        assert self.pending is not None
        self.take(self.pending, end)
        self.pending = None


class _Board:
    """When each resource is busy, keyed by what it is."""

    def __init__(self) -> None:
        self.spots: dict[str, _Timeline] = {}
        self.devices: dict[str, _Timeline] = {}
        self.transporters: dict[str, _Timeline] = {}

    def spot(self, name: str) -> _Timeline:
        return self.spots.setdefault(name, _Timeline())

    def device(self, name: str) -> _Timeline:
        return self.devices.setdefault(name, _Timeline())

    def transporter(self, name: str) -> _Timeline:
        return self.transporters.setdefault(name, _Timeline())


@dataclass
class _Placement:
    activity: int
    mode_index: int
    start: int
    end: int


@dataclass
class _Move:
    """One placed transport: which route it took, and when."""

    arc_index: int
    option_index: int
    option: TransportOption
    start: int
    end: int


def _spots_of(mode: Mode) -> tuple[str, ...]:
    """Every spot the mode binds. A port on both sides names the same spot twice,
    and the duplicate is dropped rather than reserved twice."""
    seen: dict[str, None] = {}
    for spot in (*mode.input_spots.values(), *mode.output_spots.values()):
        seen[spot] = None
    return tuple(seen)


def _refuse(instance: Instance, fixation: Fixation | None, jobs: tuple) -> str | None:
    """The first shape this cannot handle, or None when the instance is in scope."""
    if instance.replenishments:
        return "replenishment"
    if jobs:
        return "jobs"
    if fixation is not None and (
        fixation.activities or fixation.arcs or fixation.replenishments or fixation.levels
    ):
        return "fixation"
    for act in instance.activities:
        if act.relay is not None:
            return "relay"
        if act.boundary is not None:
            return "boundary"
        if any(mode.consumption for mode in act.modes):
            return "consumption"
    return None


def _edges(instance: Instance) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    """Arcs by the activity they leave, and arcs by the activity they arrive at."""
    leaving: dict[int, list[int]] = {i: [] for i in range(len(instance.activities))}
    arriving: dict[int, list[int]] = {i: [] for i in range(len(instance.activities))}
    for index, arc in enumerate(instance.arcs):
        leaving[arc.src_activity].append(index)
        arriving[arc.dst_activity].append(index)
    return leaving, arriving


def _waiting(instance: Instance) -> tuple[dict[int, int], dict[int, list[int]]]:
    """How many things each activity is still waiting for, and the bare
    precedence edges by their source.

    An activity waits on each arc arriving at it -- until the *move* is placed,
    not merely until its source activity is -- and on each precedence edge into
    it. Counting the move rather than the source is what lets material sit where
    it is until its destination's bay is free.
    """
    counts = dict.fromkeys(range(len(instance.activities)), 0)
    orders: dict[int, list[int]] = {i: [] for i in range(len(instance.activities))}
    for arc in instance.arcs:
        counts[arc.dst_activity] += 1
    for source, target in instance.precedence:
        orders[source].append(target)
        counts[target] += 1
    return counts, orders


def _spots_of_mode_free(board: _Board, mode: Mode, landed: set[str], start: int) -> int | None:
    """Push `start` until every resource the mode needs is free for its duration,
    or None if one of them is held by material that is not ours."""
    pushed = start
    for spot in _spots_of(mode):
        if spot in landed:
            continue  # already holding our material
        found = board.spot(spot).earliest(pushed, mode.duration)
        if found is None:
            return None
        pushed = max(pushed, found)
    if mode.device_access:
        for device in mode.devices:
            found = board.device(device).earliest(pushed, mode.duration)
            if found is None:
                return None
            pushed = max(pushed, found)
    return pushed


def _resting_spots(instance: Instance, leaving: list[int], mode_index: int) -> set[str]:
    """The spots this activity's products will be left resting in, given the mode.

    Which spot a departure leaves from is the source mode's to say, so every route
    out of this mode agrees on it and the first one found answers for the arc.
    """
    spots = set()
    for arc_index in leaving:
        for option in instance.arcs[arc_index].options:
            if option.src_mode_index == mode_index:
                spots.add(option.from_spot)
                break
    return spots


def _earliest_start(
    board: _Board,
    mode: Mode,
    floor: int,
    arriving: list[int],
    moves: dict[int, _Move],
    resting: set[str],
) -> int | None:
    """When an activity could start in this mode, or None if it cannot.

    Every move arriving at it has been placed by the time this is asked -- that is
    what the activity was waiting for -- so the arrivals are read here rather than
    decided.

    A spot the product will rest in has to be free not for the activity's duration
    but **from its end onwards**, because how long the rest lasts is not known
    until the departure is placed. Placements are made in no particular order in
    time, so a spot can already be booked for something later; resting material
    into it would then overrun that booking, and there would be no honest time to
    end the rest at.
    """
    landed = {moves[index].option.to_spot for index in arriving}
    start = max([floor, *(moves[index].end for index in arriving)])
    for _ in range(_PUSHES):
        pushed = _spots_of_mode_free(board, mode, landed, start)
        if pushed is None:
            return None
        if pushed == start:
            end = start + mode.duration
            for spot in resting:
                if board.spot(spot).clear_from(end, ours=spot in landed) != end:
                    return None
            return start
        start = pushed
    return None


def _devices_of(option: TransportOption) -> tuple[str, ...]:
    """The devices a move occupies: the one it takes from and the one it puts
    into, counted once where they are the same device (§7).

    This is easy to miss, and was: a move reaches inside both machines, so it
    holds both for as long as it takes, not merely the two bays and the arm. An
    environment where one device is both the resting place and the destination of
    every move is bound by that device long before it is bound by its bays.
    """
    source = parse_qualified_spot(option.from_spot)
    target = parse_qualified_spot(option.to_spot)
    assert source is not None and target is not None  # both are validated spots
    return (source[0],) if source[0] == target[0] else (source[0], target[0])


def _time_move(board: _Board, option: TransportOption, after: int) -> tuple[int, int] | None:
    """When a move on this route could set off and land, at or after `after`.

    A move books its arm and both of its devices for its duration, and takes its
    destination bay from the moment it sets off -- a bay has to be empty to be
    moved into, and stays the material's until the receiving activity starts. So
    the bay has to be free from the departure onwards, with nothing booked in it
    later. Where none of that lines up the answer is None and the material stays
    where it is: that is the whole reason a departure is attempted, not imposed.
    """
    if option.from_spot == option.to_spot:
        # A hand-off that stays put takes no time and names no arm (§Parameters):
        # the material does not move, so there is nothing to book.
        return after, after
    devices = _devices_of(option)
    start = after
    for _ in range(_PUSHES):
        pushed = board.spot(option.to_spot).clear_from(start)
        if pushed is None:
            return None
        if option.transporter is not None:
            booked = board.transporter(option.transporter).earliest(pushed, option.duration)
            if booked is None:
                return None
            pushed = max(pushed, booked)
        for device in devices:
            free = board.device(device).earliest(pushed, option.duration)
            if free is None:
                return None
            pushed = max(pushed, free)
        if pushed == start:
            return start, start + option.duration
        start = pushed
    return None


def _depart(
    instance: Instance,
    board: _Board,
    arc_index: int,
    ready_at: int,
    src_mode: int,
    moves: dict[int, _Move],
    settled: dict[int, int],
) -> bool:
    """Try to send the material along one arc, committing the move if it fits.

    The route is chosen for the earliest landing, and choosing it chooses the
    receiving activity's mode too -- a route names the spots both ends bound (§4).
    A destination already committed by an earlier arrival narrows the choice to
    the routes that agree with it.
    """
    arc = instance.arcs[arc_index]
    chosen: tuple[int, int, int, TransportOption] | None = None
    for option_index, option in enumerate(arc.options):
        if option.src_mode_index != src_mode:
            continue
        if arc.dst_activity in settled and option.dst_mode_index != settled[arc.dst_activity]:
            continue
        timing = _time_move(board, option, ready_at)
        if timing is None:
            continue
        start, landed = timing
        if chosen is None or landed < chosen[0]:
            chosen = (landed, start, option_index, option)
    if chosen is None:
        return False
    landed, start, option_index, option = chosen
    if option.transporter is not None:
        board.transporter(option.transporter).take(start, landed)
    if option.from_spot != option.to_spot:
        for device in _devices_of(option):
            board.device(device).take(start, landed)
    # The source spot is the material's until the move completes, and the
    # destination's from the moment it sets off (FORMULATION §7). The source's
    # open hold, taken when the material was left resting, ends here.
    source = board.spot(option.from_spot)
    if source.pending is not None:
        source.release(landed)
    else:
        source.take(ready_at, landed)
    board.spot(option.to_spot).hold(start)
    moves[arc_index] = _Move(arc_index, option_index, option, start, landed)
    settled[arc.dst_activity] = option.dst_mode_index
    return True


def _consumer_ready(instance: Instance, ready: list[int], counts: dict[int, int]) -> int:
    """Which of the ready activities to place next.

    Material waits in the bay its move delivered it to until the receiving
    activity starts, so an activity whose consumers are not ready leaves its
    product parked. Preferring one whose every consumer is waiting on nothing but
    this parks nothing that has nowhere to go -- a preference and not a rule,
    since standing still is worse. The topmost eligible candidate wins, which
    keeps the pass following the material it has just moved.
    """
    for position in range(len(ready) - 1, -1, -1):
        activity = ready[position]
        if all(
            counts[arc.dst_activity] == 1 for arc in instance.arcs if arc.src_activity == activity
        ):
            return ready.pop(position)
    return ready.pop()


def _breadth_first(instance: Instance, ready: list[int], counts: dict[int, int]) -> int:
    """The activity that became ready first. Spreads the work across branches
    instead of driving one to the end, which on a grid of like branches keeps
    every machine busy rather than queueing behind one."""
    return ready.pop(0)


def _lowest_index(instance: Instance, ready: list[int], counts: dict[int, int]) -> int:
    """The lowest-numbered activity, which is the order the workflow was written
    in. It commits to no strategy at all, and that is its use: the strategies
    above can talk themselves into a corner this one walks straight past."""
    return ready.pop(ready.index(min(ready)))


# The priority rules tried, in order. No rule wins everywhere: on the benchmark's
# two families each of these is best on one and beaten on another, and one
# instance is schedulable under `_lowest_index` alone. A pass costs milliseconds,
# so all of them are run and the best schedule kept. This is ordinary multi-pass
# list scheduling; the only judgement in it is which rules to carry, and that was
# settled by measuring (report §30.7).
_RULES = (_consumer_ready, _breadth_first, _lowest_index)


def construct(
    instance: Instance,
    *,
    fixation: Fixation | None = None,
    jobs: tuple[JobSpec, ...] = (),
    objective: tuple[str, ...] | None = None,
) -> Solution | None:
    """The best schedule a few forward passes find, or `None` when the instance
    is out of scope (`REFUSALS`) or no pass came out. The signature is
    `cpsat.solve`'s, so the two are interchangeable at the call site.
    """
    if _refuse(instance, fixation, jobs) is not None:
        return None
    best: Solution | None = None
    for rule in _RULES:
        found = _pass(instance, rule)
        if found is None:
            continue
        if best is None or (found.makespan or 0) < (best.makespan or 0):
            best = found
    return best


def _pass(instance: Instance, rule) -> Solution | None:
    """One forward pass under one priority rule.

    **A departure is attempted, not imposed.** The spot an activity ran in is
    occupied until the move taking the material away completes, and the bay it is
    moving into is occupied from the moment it sets off. Insisting on either end
    strands the pass: send everything on at once and one source's several outputs
    all claim one bay, wait for the receiving activity instead and the first
    branch to finish parks its plate in a single-spot device that every other
    branch needs. So material that cannot move stays where it is, resting in its
    own bay, and its departure is retried whenever something clears -- which is
    what the model lets it do, the move being free to happen any time between the
    two activities.
    """
    board = _Board()
    leaving, arriving = _edges(instance)
    counts, orders = _waiting(instance)
    placed: dict[int, _Placement] = {}
    moves: dict[int, _Move] = {}
    # A destination's mode is settled by the first move arriving at it.
    settled: dict[int, int] = {}
    # Material resting with its departure not yet placed, as arc -> when it was
    # ready to leave.
    resting: dict[int, int] = {}
    floor = dict.fromkeys(range(len(instance.activities)), 0)

    ready = sorted((i for i, count in counts.items() if count == 0), reverse=True)
    while ready or resting:
        moved = _send_what_can_go(instance, board, placed, moves, settled, resting, counts, ready)
        if not ready:
            if not moved:
                return None  # nothing can move and nothing can run
            continue
        activity = rule(instance, ready, counts)
        act = instance.activities[activity]
        candidates = [settled[activity]] if activity in settled else range(len(act.modes))
        best: tuple[int, int] | None = None
        for mode_index in candidates:
            start = _earliest_start(
                board,
                act.modes[mode_index],
                floor[activity],
                arriving[activity],
                moves,
                _resting_spots(instance, leaving[activity], mode_index),
            )
            if start is None:
                continue
            end = start + act.modes[mode_index].duration
            # Earliest finish wins and the lowest mode index breaks ties, so two
            # runs over one instance give the same schedule.
            if best is None or end < best[0]:
                best = (end, mode_index)
        if best is None:
            if not moved:
                return None  # this will not become placeable by waiting
            ready.append(activity)
            continue

        end, mode_index = best
        mode = act.modes[mode_index]
        start = end - mode.duration
        for index in arriving[activity]:
            # An arrival's hold on its bay ends where the activity begins.
            bay = board.spot(moves[index].option.to_spot)
            if bay.pending is not None:
                bay.release(start)
        for name in _spots_of(mode):
            board.spot(name).take(start, end)
        if mode.device_access:
            for device in mode.devices:
                board.device(device).take(start, end)
        placed[activity] = _Placement(activity, mode_index, start, end)

        # Whatever this activity produced is now resting in the spot it ran in,
        # waiting for its move. The hold keeps the spot until the move is placed.
        for arc_index in leaving[activity]:
            resting[arc_index] = end
            for option in instance.arcs[arc_index].options:
                if option.src_mode_index == mode_index:
                    board.spot(option.from_spot).hold(end)
                    break
        for target in orders[activity]:
            floor[target] = max(floor[target], end)
            counts[target] -= 1
            if counts[target] == 0:
                ready.append(target)

    if len(placed) != len(instance.activities) or len(moves) != len(instance.arcs):
        return None  # a cycle, or an arc whose ends were never both placed
    if any(line.pending is not None for line in board.spots.values()):
        return None  # material left resting with nothing to take it away
    return _assemble(instance, placed, moves)


def _send_what_can_go(
    instance: Instance,
    board: _Board,
    placed: dict[int, _Placement],
    moves: dict[int, _Move],
    settled: dict[int, int],
    resting: dict[int, int],
    counts: dict[int, int],
    ready: list[int],
) -> bool:
    """Place every departure that fits, and say whether any did.

    A departure whose destination is waiting on nothing else goes first: it frees
    a bay and makes an activity runnable, where one that merely parks material
    somewhere new does neither.
    """
    moved = False
    while resting:
        order = sorted(
            resting,
            key=lambda index: (counts[instance.arcs[index].dst_activity] != 1, index),
        )
        for arc_index in order:
            source = instance.arcs[arc_index].src_activity
            if _depart(
                instance,
                board,
                arc_index,
                resting[arc_index],
                placed[source].mode_index,
                moves,
                settled,
            ):
                del resting[arc_index]
                moved = True
                target = instance.arcs[arc_index].dst_activity
                counts[target] -= 1
                if counts[target] == 0:
                    ready.append(target)
                break
        else:
            return moved
    return moved


def _assemble(
    instance: Instance,
    placed: dict[int, _Placement],
    moves: dict[int, _Move],
) -> Solution:
    makespan = max([*(p.end for p in placed.values()), *(m.end for m in moves.values())], default=0)
    return Solution(
        outcome="feasible",
        makespan=makespan,
        processing=tuple(
            ProcessingResult(
                activity=index,
                node=instance.activities[index].node,
                process=instance.activities[index].process,
                mode=instance.activities[index].modes[placed[index].mode_index],
                start=placed[index].start,
                end=placed[index].end,
            )
            for index in sorted(placed)
        ),
        transport=tuple(
            TransportResult(
                arc=instance.arcs[index].arc,
                option=instance.arcs[index].options[moves[index].option_index],
                start=moves[index].start,
                end=moves[index].end,
                seq=instance.arcs[index].seq,
            )
            for index in sorted(moves)
        ),
        # Only the makespan is evaluated. The other stages (§4.8) are a solver's
        # concern -- a hint is judged on being feasible, not on being good -- and
        # anything meaning to *return* this as a plan has to fill them in first,
        # because a plan reports what its objective reached.
        objective_kind=(objective_stages.MAKESPAN,),
        objective_values=(makespan,),
    )
