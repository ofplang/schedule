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
from ofplang.schedule.scheduler.instance import Instance, TransportOption, job_membership
from ofplang.schedule.scheduler.model import JobSpec, Mode
from ofplang.schedule.scheduler.result import ProcessingResult, Solution, TransportResult
from ofplang.schedule.scheduler.status import Fixation

# Shapes `construct` declines, and why each needs more than list scheduling.
REFUSALS = {
    "replenishment": "a refill is scheduled work whose need depends on the draws",
    "consumption": "a stock draw makes an activity's admissibility depend on the order",
    "relay": "a transport junction is a chain whose legs share one arc",
    "held": "an occupied spot is held to the horizon, which is not a duration",
    "fixation": "a replan has activities already placed, which is a different problem",
    "bound": "a promised completion has to be measured the way the model measures it",
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

    def free(self, start: int, duration: int) -> bool:
        """Whether the resource is free over exactly this span. A zero-length span
        is free unless it falls strictly inside a use, which is what `AddNoOverlap`
        does with a point (§Parameters, design.md D54)."""
        if self.pending is not None:
            return False
        end = start + duration
        if start == end:
            return not any(busy_start < start < busy_end for busy_start, busy_end in self.busy)
        return all(end <= busy_start or start >= busy_end for busy_start, busy_end in self.busy)

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
    """The first shape this cannot handle, or None when the instance is in scope.

    A joint plan is in scope while no job carries a promised completion. The
    release is easy -- it is a floor on when a job's work may start, and a forward
    pass has a floor already -- but the promise is a cap on $C_j$, and $C_j$ is
    measured over the job's *own* work: not its boundary nodes, and not the parts
    of a move that are the material resting rather than travelling (§J, and
    `cpsat._resting`). Measuring it some other way here would mean checking a
    promise against a number the model does not use. So a roster with promises in
    it is declined, and a fresh one -- which is every roster the case-study
    laboratories submit -- is not.
    """
    if instance.replenishments:
        return "replenishment"
    if any(spec.bound is not None for spec in jobs):
        return "bound"
    if fixation is not None and (
        fixation.activities or fixation.arcs or fixation.replenishments or fixation.levels
    ):
        return "fixation"
    for act in instance.activities:
        if act.relay is not None:
            return "relay"
        if act.boundary is not None and act.boundary.kind == "held":
            return "held"
        if any(mode.consumption for mode in act.modes):
            return "consumption"
    return None


def _floors(instance: Instance, jobs: tuple[JobSpec, ...]) -> dict[int, int]:
    """The earliest each activity may start, where a job's release says so (§6.11).

    An ordinary activity of a job is *held* at its release; an input node is
    *pinned* there, the entry material being a fact about the world rather than
    something the schedule decides. Boundary nodes are otherwise exempt, as they
    are in the model.
    """
    if not jobs:
        return {}
    membership = job_membership(instance, [spec.id for spec in jobs])
    by_job = {spec.id: spec.release for spec in jobs}
    floors: dict[int, int] = {}
    for index, act in enumerate(instance.activities):
        if act.boundary is not None:
            if act.boundary.kind == "input":
                floors[index] = by_job.get(act.boundary.job or "", 0)
        else:
            owner = membership[index]
            if owner is not None:
                floors[index] = by_job.get(owner, 0)
    return floors


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
    pressure: dict[str, float],
) -> bool:
    """Try to send the material along one arc, committing the move if it fits.

    The route is chosen for the earliest landing, and choosing it chooses the
    receiving activity's mode too -- a route names the spots both ends bound (§4).
    A destination already committed by an earlier arrival narrows the choice to
    the routes that agree with it.

    **Into an output node the route is chosen for where it parks**, and only then
    for when it lands. The material is not going anywhere afterwards, so an early
    arrival buys nothing and a badly chosen bay costs the rest of the run
    (`_pressure`). Everywhere else the earliest landing wins, which is what moves
    work along.
    """
    arc = instance.arcs[arc_index]
    parking = _is_output(instance, arc.dst_activity)
    chosen: tuple[tuple, int, int, TransportOption] | None = None
    for option_index, option in enumerate(arc.options):
        if option.src_mode_index != src_mode:
            continue
        if arc.dst_activity in settled and option.dst_mode_index != settled[arc.dst_activity]:
            continue
        timing = _time_move(board, option, ready_at)
        if timing is None:
            continue
        start, landed = timing
        rank = (
            (pressure.get(option.to_spot, 0.0), landed, option_index)
            if parking
            else (landed, option_index)
        )
        if chosen is None or rank < chosen[0]:
            chosen = (rank, start, option_index, option)
    if chosen is None:
        return False
    _rank, start, option_index, option = chosen
    landed = start + (0 if option.from_spot == option.to_spot else option.duration)
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


def _pressure(instance: Instance) -> dict[str, float]:
    """How much the real work wants each spot.

    A finished product parks in its spot **until the run is over** (§J5), so where
    it parks matters more than when it gets there -- and the laboratory's bays are
    not alike in how badly they are wanted. Measured on the standard RNA-seq
    laboratory at five jobs: the first two libraries parked in the only two tecan
    bays, which every job's library preparation needs, while ten pcr bays stood
    empty, and the third job had nowhere to work.

    Each activity contributes one unit spread over the spots it could use, so a
    step with ten bays to choose from presses on each of them a tenth as hard as
    one with a single bay. Boundary nodes are left out: what is wanted here is the
    demand from work that has to happen somewhere, and a resting place is not that.
    """
    wanted: dict[str, float] = {}
    for act in instance.activities:
        if act.boundary is not None:
            continue
        spots = {spot for mode in act.modes for spot in _spots_of(mode)}
        if not spots:
            continue
        share = 1.0 / len(spots)
        for spot in spots:
            wanted[spot] = wanted.get(spot, 0.0) + share
    return wanted


def _is_output(instance: Instance, activity: int) -> bool:
    boundary = instance.activities[activity].boundary
    return boundary is not None and boundary.kind == "output"


def _choose(
    instance: Instance, rule, ready: list[int], counts: dict[int, int], placed: set[int]
) -> int:
    """Apply the priority rule, but only to real work while any is left.

    An output node parks its material in a bay **until the run is over** (§8), so
    placing one early takes that bay out of circulation for good -- and where the
    bay is a machine's, every other job that needs the machine is then stuck. It
    is not work, and nothing waits on it, so it costs nothing to leave until last.
    """
    work = [activity for activity in ready if not _is_output(instance, activity)]
    if not work:
        return rule(instance, ready, counts, placed)
    chosen = rule(instance, work, counts, placed)
    ready.remove(chosen)
    return chosen


def _consumer_ready(
    instance: Instance, ready: list[int], counts: dict[int, int], placed: set[int]
) -> int:
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


def _breadth_first(
    instance: Instance, ready: list[int], counts: dict[int, int], placed: set[int]
) -> int:
    """The activity that became ready first. Spreads the work across branches
    instead of driving one to the end, which on a grid of like branches keeps
    every machine busy rather than queueing behind one."""
    return ready.pop(0)


def _lowest_index(
    instance: Instance, ready: list[int], counts: dict[int, int], placed: set[int]
) -> int:
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


class _JobByJob:
    """A rule that will not start a job while the one before it is unfinished.

    The last resort, and a different kind of thing from the rules above: they
    choose among what is ready, this one narrows what counts as ready. It is here
    because of how a forward pass fails on a shared laboratory -- never for want
    of somewhere to *start*, always for want of somewhere to *send* material that
    has already been made (measured: every standstill is on the departure side).
    Two jobs in flight through the same machines can each be holding the bay the
    other needs next, and no ordering of the ready set can undo that once it has
    happened. One job at a time cannot get there.

    It paces the *placement*, not the clock: a later job is still put as early as
    the resources allow, so the schedule is not simply one job after another.

    Where the current job has work left that is not ready yet, anything is let
    through rather than nothing -- standing still is what this is here to avoid.
    """

    def __init__(self, instance: Instance, jobs: tuple[JobSpec, ...]) -> None:
        owner = job_membership(instance, [spec.id for spec in jobs])
        self.owner = owner
        self.order = tuple(spec.id for spec in jobs)
        self.members = {
            spec.id: [index for index, job in enumerate(owner) if job == spec.id] for spec in jobs
        }
        self.index = 0

    def __call__(
        self, instance: Instance, ready: list[int], counts: dict[int, int], placed: set[int]
    ) -> int:
        while self.index < len(self.order):
            job = self.order[self.index]
            if any(member not in placed for member in self.members[job]):
                mine = [activity for activity in ready if self.owner[activity] == job]
                if mine:
                    return ready.pop(ready.index(min(mine)))
                break
            self.index += 1
        return ready.pop(ready.index(min(ready)))


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
    floors = _floors(instance, jobs)
    best: Solution | None = None
    for rule in _RULES:
        found = _pass(instance, rule, floors)
        if found is None:
            continue
        if best is None or (found.makespan or 0) < (best.makespan or 0):
            best = found
    if best is None and jobs:
        # Only when nothing else came out. Pacing the jobs gives up the good
        # schedules the rules above find when they find one, so it is a last
        # resort and not a fourth opinion -- and leaving it out of the ordinary
        # path is also what keeps every instance that already works unchanged.
        best = _pass(instance, _JobByJob(instance, jobs), floors)
    return best


def _pass(instance: Instance, rule, floors: dict[int, int]) -> Solution | None:
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
    pressure = _pressure(instance)
    leaving, arriving = _edges(instance)
    counts, orders = _waiting(instance)
    placed: dict[int, _Placement] = {}
    moves: dict[int, _Move] = {}
    # A destination's mode is settled by the first move arriving at it.
    settled: dict[int, int] = {}
    # Material resting with its departure not yet placed, as arc -> when it was
    # ready to leave.
    resting: dict[int, int] = {}
    floor = {index: floors.get(index, 0) for index in range(len(instance.activities))}
    # Output nodes are placed twice: once here, to settle when their material
    # arrives, and again at the end, because their end *is* the makespan (§8) and
    # that is not known until everything else is placed.
    outputs: list[int] = []

    ready = sorted((i for i, count in counts.items() if count == 0), reverse=True)
    while ready or resting:
        moved = _send_what_can_go(
            instance, board, placed, moves, settled, resting, counts, ready, pressure
        )
        if not ready:
            if not moved:
                return None  # nothing can move and nothing can run
            continue
        activity = _choose(instance, rule, ready, counts, set(placed))
        act = instance.activities[activity]
        candidates = [settled[activity]] if activity in settled else range(len(act.modes))
        if act.boundary is not None:
            pinned = _place_boundary(
                board, instance, activity, candidates, floor[activity], arriving[activity], moves
            )
            if pinned is None:
                if not moved:
                    return None  # waiting will not free the instant it is pinned to
                ready.append(activity)
                continue
            mode_index, at, until = pinned
            placed[activity] = _Placement(activity, mode_index, at, until)
            if act.boundary.kind == "output":
                # Its bay stays held: the material arrived and stays put, and the
                # hold closes at the makespan once that is known.
                outputs.append(activity)
            for arc_index in leaving[activity]:
                resting[arc_index] = until
                for option in instance.arcs[arc_index].options:
                    if option.src_mode_index == mode_index:
                        board.spot(option.from_spot).hold(until)
                        break
            for target in orders[activity]:
                floor[target] = max(floor[target], until)
                counts[target] -= 1
                if counts[target] == 0:
                    ready.append(target)
            continue
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
    makespan = _makespan(instance, placed, moves)
    for activity in outputs:
        # An output node's end *is* the makespan (§8), so its bay is the material's
        # from the delivery until the run is over. Nothing could have taken that bay
        # meanwhile: the delivery's hold was never closed.
        placement = placed[activity]
        placed[activity] = _Placement(activity, placement.mode_index, placement.start, makespan)
        for index in arriving[activity]:
            bay = board.spot(moves[index].option.to_spot)
            if bay.pending is not None:
                bay.release(makespan)
    if any(line.pending is not None for line in board.spots.values()):
        return None  # material left resting with nothing to take it away
    return _assemble(instance, placed, moves, makespan)


def _send_what_can_go(
    instance: Instance,
    board: _Board,
    placed: dict[int, _Placement],
    moves: dict[int, _Move],
    settled: dict[int, int],
    resting: dict[int, int],
    counts: dict[int, int],
    ready: list[int],
    pressure: dict[str, float],
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
            key=lambda index: (
                # A delivery into an output node parks its material for the rest of
                # the run, so it goes after every delivery that does not.
                _is_output(instance, instance.arcs[index].dst_activity),
                counts[instance.arcs[index].dst_activity] != 1,
                index,
            ),
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
                pressure,
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


def _makespan(
    instance: Instance,
    placed: dict[int, _Placement],
    moves: dict[int, _Move],
) -> int:
    """The makespan the model would report (§8).

    Not simply the last thing to happen. An output or held node's end is not work
    -- the first is pinned to this very number and the second to the horizon -- so
    neither is counted. A delivery *into* an output node is counted, because that
    node is pinned here and a delivery arriving later than every real end has to
    fit before it. Every other move is followed by the activity that receives it,
    so counting it would change nothing.
    """
    ends = [
        placement.end
        for index, placement in placed.items()
        if (boundary := instance.activities[index].boundary) is None
        or boundary.kind not in ("output", "held")
    ]
    ends += [
        move.end
        for index, move in moves.items()
        if (boundary := instance.activities[instance.arcs[index].dst_activity].boundary) is not None
        and boundary.kind == "output"
    ]
    return max(ends, default=0)


def _place_boundary(
    board: _Board,
    instance: Instance,
    activity: int,
    candidates,
    floor: int,
    arriving: list[int],
    moves: dict[int, _Move],
) -> tuple[int, int, int] | None:
    """Place a boundary node by its pinning rather than by looking for a slot.

    An input node sits at its job's release and takes no time: the material is
    given, so the schedule does not choose when it appears. An output node starts
    where its material lands and ends at the makespan, which is settled once
    everything else is placed -- so it is given a zero length here and stretched
    afterwards, and its bay is deliberately *not* taken, the delivery's own hold
    already covering it.
    """
    act = instance.activities[activity]
    assert act.boundary is not None
    mode_index = next(iter(candidates))
    if act.boundary.kind == "input":
        start = floor
        # Zero length, so the pinned instant must not fall inside another use of
        # the spot -- which is exactly what `AddNoOverlap` refuses (§Parameters).
        for spot in _spots_of(act.modes[mode_index]):
            if not board.spot(spot).free(start, 0):
                return None
        return mode_index, start, start
    start = max([floor, *(moves[index].end for index in arriving)])
    return mode_index, start, start


def _assemble(
    instance: Instance,
    placed: dict[int, _Placement],
    moves: dict[int, _Move],
    makespan: int,
) -> Solution:
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
