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

from dataclasses import dataclass, field, replace

from ofplang.schedule.core import objective as objective_stages
from ofplang.schedule.core.identifiers import (
    parse_qualified_resource,
    parse_qualified_spot,
)
from ofplang.schedule.scheduler import completion, mobility
from ofplang.schedule.scheduler.instance import Instance, TransportOption, job_membership
from ofplang.schedule.scheduler.model import JobSpec, Mode
from ofplang.schedule.scheduler.result import (
    ProcessingResult,
    RefillResult,
    Solution,
    TransportResult,
)
from ofplang.schedule.scheduler.status import Fixation

# Shapes `construct` declines, and why each needs more than list scheduling.
REFUSALS = {
    "running refill": "a refill already under way is history this does not carry",
    "cancelled": "work a stopped job abandoned is placed at an instant this does not derive",
}

# How many times a start may be pushed later before this gives up. Each push
# moves strictly forward, so the loop ends on its own; the cap is here so that a
# shape nobody anticipated ends in `None` rather than in a long wait.
_PUSHES = 64

# How many times the whole construction is retried with one more refill's machine
# time held back. Each round reshapes the schedule, which moves its own draws, so
# there is no argument that this settles -- the cap is what makes it terminate.
#
# Measured on the benchmark's stock rows, a row needs one round per refill it
# turns out to want, plus one: none for `r1_*`, two for `r2_cap32`'s single
# refill, four for `r2_cap16`'s three. Twelve is room for a stock that has to be
# topped up eleven times, and costs nothing where fewer will do -- the rounds
# stop the moment the levels come out.
_REFILL_ROUNDS = 12


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

    Stocks are in scope: a draw is taken at an activity's start and a refill lands
    at its end, and neither changes which spot anything occupies, so the pass runs
    as it always did and the levels are settled afterwards (`_stock_plan`).

    A replan is in scope too: what has run is put on the board before the pass
    starts, at the times and in the modes reported, and everything still to do is
    held at `now` (`_history`).

    So are transport junctions. The model makes a relay an ordinary activity --
    one 0-duration, device-less, single-spot mode -- and nothing here has to know
    it is one, beyond marking it as such in the answer (`_marked`).

    An occupied spot is in scope: it is not work, so it goes on the board before
    the pass and stays there (`_lay_out_held`).

    So is a promised completion. $C_j$ is measured over the job's *own* work --
    not its boundary nodes, and not the parts of a move that are the material
    resting rather than travelling -- and that rule is not restated here: it is
    read from `completion.job_end_parts`, which is the same thing the solver
    constrains against. A promise that one of them thinks is kept and the other
    thinks is broken is the failure a promise exists to prevent.
    """
    if fixation is not None and fixation.replenishments:
        # A refill already running is a fixed future increase the levels do not
        # yet carry (`cpsat._add_resources`). Nothing in the corpus has one, so
        # rather than carry history this has never seen, it is declined.
        return "running refill"
    if fixation is not None and (
        any(fx.status == "cancelled" for fx in fixation.activities.values())
        or any(fr.status == "cancelled" for fr in fixation.arcs.values())
    ):
        # Cancelled work is pinned to the instant its job stopped, which is
        # derived from every *other* activity of that job (`cpsat.stopped_at`).
        # Nothing in the corpus has any, so rather than re-derive a rule this has
        # never been measured against, it is declined.
        return "cancelled"
    return None


@dataclass(frozen=True)
class _History:
    """What a replan reports as already done, with its times resolved.

    Keyed the way the pass is keyed -- activity index and arc index -- and
    carrying the mode and route the report named, because a replan does not get
    to choose them again (SPEC §9.3).
    """

    # activity -> (mode index, start, end)
    activities: dict[int, tuple[int, int, int]]
    # arc -> (option index, start, end)
    arcs: dict[int, tuple[int, int, int]]


def _history(
    instance: Instance, fixation: Fixation | None, margin: int
) -> _History | None:
    """Read the fixation into resolved times, or None if it names something this
    cannot place.

    **A running activity's end is clamped up to `now + margin`** (FORMULATION §9):
    an overrun must not be fixed to a finish in the past, and the resources it
    holds have to be held for as long as it is really going to take. A completed
    one keeps the end it reported.
    """
    if fixation is None:
        return _History({}, {})
    now = fixation.now
    activities: dict[int, tuple[int, int, int]] = {}
    for index, fx in fixation.activities.items():
        if index >= len(instance.activities):
            return None  # the fixation does not match this instance
        if fx.mode_index >= len(instance.activities[index].modes):
            return None
        activities[index] = (fx.mode_index, fx.start, _fixed_end(fx, now, margin))
    arcs: dict[int, tuple[int, int, int]] = {}
    for index, fr in fixation.arcs.items():
        if index >= len(instance.arcs):
            return None
        if fr.option_index >= len(instance.arcs[index].options):
            return None
        arcs[index] = (fr.option_index, fr.start, _fixed_end(fr, now, margin))
    return _History(activities, arcs)


def _fixed_end(fix, now: int, margin: int) -> int:
    """The pinned end of something already under way. Mirrors `cpsat._fixed_end`;
    the two have to agree, or the plan a replan returns disagrees with the model
    that would have checked it."""
    if fix.status == "running":
        return max(fix.end, now + margin)
    return fix.end


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
    running_task_margin: int = 0,
) -> Solution | None:
    """The best schedule a few forward passes find, or `None` when the instance
    is out of scope (`REFUSALS`) or no pass came out. The signature is
    `cpsat.solve`'s, so the two are interchangeable at the call site.
    """
    if _refuse(instance, fixation, jobs) is not None:
        return None
    floors = _floors(instance, jobs)
    # Nothing pending may start before `now` (FORMULATION §9). A fixed activity is
    # history and is *not* held back: a release or a `now` that contradicted what
    # already ran would make the past infeasible rather than say anything about
    # what is left.
    now = fixation.now if fixation is not None else 0
    history = _history(instance, fixation, running_task_margin)
    if history is None:
        return None
    for index, act in enumerate(instance.activities):
        if index in history.activities:
            continue
        if act.boundary is not None and act.boundary.kind == "input":
            # Entry material is a fact about the world, pinned at its job's
            # release. `now` says nothing about it -- and holding it back would
            # contradict the move that has already carried it away.
            continue
        floors[index] = max(floors.get(index, 0), now)
    # What is being minimised, and therefore what "best" means below and what the
    # answer reports (§4.8). `effective` drops a stage this instance cannot tell
    # two schedules apart by, exactly as the solver drops it.
    stages = objective_stages.effective(
        objective or objective_stages.default(len(jobs)),
        replenishment_possible=bool(instance.replenishments),
    )
    levels = dict(fixation.levels) if fixation is not None else {}
    # Reserved machine time for refills the stocks turned out to need. Empty on
    # the first round, and grown by a window each time a schedule came out whose
    # stocks could not be made to last (`_reservation_for`).
    reserved: tuple[tuple[str, int, int], ...] = ()
    for _round in range(_REFILL_ROUNDS):
        best = _build(instance, fixation, jobs, stages, floors, reserved, history, now)
        if best is None:
            return None
        if not levels:
            return _scored(
                instance, fixation, jobs, stages,
                _promised(instance, fixation, jobs, _marked(instance, fixation, best)),
            )
        stocked, wanted = _stock_plan(instance, fixation, best)
        if stocked is not None:
            return _scored(
                instance, fixation, jobs, stages,
                _promised(instance, fixation, jobs, _marked(instance, fixation, stocked)),
            )
        if wanted is None:
            return None  # the stocks cannot last however the work is arranged
        window = _window_for(instance, *wanted)
        if window is None or window in reserved:
            return None
        reserved = (*reserved, window)
    return None


def _promised(
    instance: Instance,
    fixation: Fixation | None,
    jobs: tuple[JobSpec, ...],
    solution: Solution,
) -> Solution | None:
    """Work out when each job completed, and refuse the schedule if that breaks a
    promise.

    🔴 **Which ends count comes from `completion.job_end_parts`, not from here.**
    The solver constrains $C_j \\le B_j$ over the same selection; a second reading
    of it would let a promise be kept by one measure and broken by the other,
    which is the whole of what a promise is for.
    """
    if not jobs:
        return solution
    completions = _completions(instance, fixation, jobs, solution)
    for spec in jobs:
        if spec.bound is None:
            continue
        reached = completions.get(spec.id)
        if reached is not None and reached > spec.bound:
            # A constructed schedule is one schedule, not the best one, so there
            # is nothing to relax against here: it either keeps the promise or it
            # is not offered. Relaxing a bound is the solver's to do and to report
            # (`api._solve_within_bounds`).
            return None
    return replace(solution, job_completions=completions)


def _stage_values(
    instance: Instance,
    fixation: Fixation | None,
    jobs: tuple[JobSpec, ...],
    stages: tuple[str, ...],
    solution: Solution,
) -> tuple[int, ...]:
    """What each stage comes to for this schedule, in the objective's order.

    Read off the schedule the way the solver reads it off its own: the makespan,
    the number of refills *this plan* runs (a reported one is history and was
    never chosen), and the sum of the job completions -- whose definition is
    shared rather than restated (`completion.job_end_parts`).
    """
    reached = {
        objective_stages.MAKESPAN: solution.makespan or 0,
        objective_stages.REPLENISHMENT_COUNT: sum(
            1 for refill in solution.replenishment if refill.status is None
        ),
        objective_stages.COMPLETION_TIME_SUM: sum(
            _completions(instance, fixation, jobs, solution).values()
        ),
    }
    return tuple(reached[stage] for stage in stages)


def _scored(
    instance: Instance,
    fixation: Fixation | None,
    jobs: tuple[JobSpec, ...],
    stages: tuple[str, ...],
    solution: Solution | None,
) -> Solution | None:
    """The answer, saying what it was minimising and what it reached (§4.8).

    🔴 Only the makespan was reported for as long as this produced hints, which
    are judged on their times alone. Measured on the corpus, eighteen of twenty
    rows have effective stages that are not the makespan by itself -- so an
    answer returned as a *plan* was naming the wrong objective nearly always.
    """
    if solution is None:
        return None
    return replace(
        solution,
        objective_kind=stages,
        objective_values=_stage_values(instance, fixation, jobs, stages, solution),
    )


def _completions(
    instance: Instance,
    fixation: Fixation | None,
    jobs: tuple[JobSpec, ...],
    solution: Solution,
) -> dict[str, int]:
    """When each job completed. The selection is `completion.job_end_parts`; all
    this does is take the maximum over what it names."""
    membership = job_membership(instance, [spec.id for spec in jobs])
    ends = {p.activity: p.end for p in solution.processing}
    arc_ends = {index: move.end for index, move in enumerate(solution.transport)}
    return {
        job_id: max(
            [ends[i] for i in activities if i in ends]
            + [arc_ends[r] for r in arcs if r in arc_ends]
        )
        for job_id, (activities, arcs) in completion.job_end_parts(
            instance, fixation, membership
        ).items()
        if activities or arcs
    }


def _marked(
    instance: Instance, fixation: Fixation | None, solution: Solution
) -> Solution:
    """The same schedule with what each activity *is* written on it.

    `relay` drives rendering (a junction renders as `kind: relay`), `boundary`
    tells rendering to skip a synthetic node, and `status` is what a replan
    reported. None of the three changes a time; all three were empty while this
    only ever produced hints, and all three have to be right the moment it
    produces a plan.
    """
    acts = fixation.activities if fixation is not None else {}
    arcs = fixation.arcs if fixation is not None else {}
    return replace(
        solution,
        processing=tuple(
            replace(
                placement,
                relay=instance.activities[placement.activity].relay,
                boundary=instance.activities[placement.activity].boundary,
                status=(
                    acts[placement.activity].status if placement.activity in acts else None
                ),
            )
            for placement in solution.processing
        ),
        transport=tuple(
            replace(move, status=arcs[index].status if index in arcs else None)
            for index, move in enumerate(solution.transport)
        ),
    )


def _build(
    instance: Instance,
    fixation: Fixation | None,
    jobs: tuple[JobSpec, ...],
    stages: tuple[str, ...],
    floors: dict[int, int],
    reserved: tuple[tuple[str, int, int], ...],
    history: _History,
    now: int,
) -> Solution | None:
    """The best schedule the passes find, ignoring the stocks entirely.

    ⚠ **Best by the objective, not by the makespan.** Measured, the two agree on
    every row of the corpus -- but a rule that happens to agree is not the right
    rule, and a document is free to ask for the refills to be minimised first.
    """
    best: Solution | None = None
    mark: tuple[int, ...] | None = None
    for rule in _RULES:
        found = _pass(instance, rule, floors, reserved, history, now)
        if found is None:
            continue
        rank = _stage_values(instance, fixation, jobs, stages, found)
        if best is None or mark is None or rank < mark:
            best, mark = found, rank
    if best is None and jobs:
        # Only when nothing else came out. Pacing the jobs gives up the good
        # schedules the rules above find when they find one, so it is a last
        # resort and not a fourth opinion -- and leaving it out of the ordinary
        # path is also what keeps every instance that already works unchanged.
        best = _pass(instance, _JobByJob(instance, jobs), floors, reserved, history, now)
    # 🔴 **The end of the line, and the only part of this that backtracks.**
        # A forward pass fails on a shape no ordering of the ready set can undo:
        # the contended spots fill, every Object's next spot is the one another
        # Object is standing in, and nothing can move (measured on
        # `s1_b6r4_pool1`, report section 49.3 -- all five spots of the ring
        # holding material, in all four passes identically).
        #
        # `mobility` walks that same instance with the clock erased and *does*
        # backtrack, so where a way through exists it finds one -- and what it
        # finds is an order of exactly these placements and moves. Laying times
        # over it is the whole of what is left, and `_replay` does that.
        #
        # It runs only here, so every instance that already worked is untouched,
        # and the walk's cost is paid only by instances that would otherwise
        # have got nothing at all.
    #
    # ⚠ It is narrower than the passes above it, deliberately. The walk has no
    # clock, so it cannot honour a time that is already settled -- a reported
    # history, or a spot occupied since a stated moment (§6.12). Replaying its
    # order over either would put a settled thing somewhere it did not happen.
    # Such an instance therefore gets nothing when the forward passes fail,
    # which is what it got before any of this was in scope, so nothing
    # regresses.
    settled_already = bool(history.activities or history.arcs) or any(
        act.boundary is not None and act.boundary.kind == "held"
        for act in instance.activities
    )
    if best is None and not settled_already:
        order = mobility.find_order(instance)
        if order is not None:
            best = _replay(instance, order, floors)
    return best


def _window_for(instance: Instance, device: str, when: int) -> tuple[str, int, int] | None:
    """Machine time to keep clear on the next round, for a refill this schedule
    left no room for.

    🔴 **`when` is the draw that is still short once everything placeable has
    been placed**, not the first one that looked short. Reserving against the
    first one is what made the rounds chase their own tail: each window opened
    next to the last, they merged into a single block, and one block holds one
    useful refill however wide it is.

    The window ends exactly at that draw, which is the latest a refill can be and
    still feed it.
    """
    visits = [
        option.duration
        for candidate in instance.replenishments
        if candidate.device == device
        for option in candidate.options
    ]
    if not visits:
        return None
    longest = max(visits)
    if when < longest:
        return None  # the draw comes before any refill could have landed
    return device, when - longest, when


def _with_stocks(
    instance: Instance, fixation: Fixation | None, solution: Solution
) -> Solution | None:
    """The same schedule with its refills placed, or `None` if the stocks cannot
    be made to last.

    **A document that states no levels gets no reservoir at all** -- the model
    builds none (`cpsat._add_resources` returns at once), so neither does this.
    That is not a shortcut: an environment may declare stocks that no document
    ever puts a number to, and constraining them would be inventing a constraint
    the model does not have.
    """
    stocked, _wanted = _stock_plan(instance, fixation, solution)
    return stocked


def _stock_plan(
    instance: Instance, fixation: Fixation | None, solution: Solution
) -> tuple[Solution | None, tuple[str, int] | None]:
    """The same, and what it would have needed if it failed."""
    levels = dict(fixation.levels) if fixation is not None else {}
    if not levels:
        return solution, None
    draws = _draws(instance, solution)
    if not draws:
        return solution, None
    placed, wanted = _refills_for(instance, solution, levels, draws)
    if placed is None:
        return None, wanted
    if not placed:
        return solution, None
    return replace(solution, replenishment=tuple(placed)), None


def _draws(instance: Instance, solution: Solution) -> dict[tuple[str, str], list[tuple[int, int]]]:
    """Every draw the schedule makes, as stock -> [(when, how much)].

    Taken at the **start**, in full (§4.7). Boundary nodes and relays consume
    nothing, so they simply contribute none.
    """
    found: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for placement in solution.processing:
        for qualified, amount in placement.mode.consumption.items():
            parsed = parse_qualified_resource(qualified)
            if parsed is None:  # pragma: no cover - the environment validator refuses it
                continue
            found.setdefault(parsed, []).append((placement.start, amount))
    for events in found.values():
        events.sort()
    return found


# What the stocks needed: the refills to run, or the draw that could not be fed
# and the machine whose time a later round should keep clear for it. Both empty
# means the stocks cannot be made to last however the work is arranged.
_Stocked = tuple[list[RefillResult] | None, tuple[str, int] | None]


def _refills_for(
    instance: Instance,
    solution: Solution,
    levels: dict[tuple[str, str], int],
    draws: dict[tuple[str, str], list[tuple[int, int]]],
) -> _Stocked:
    """Which refills to run, and when, or None when no arrangement of them works.

    Taken stock by stock. The trajectory is read; where it first falls below zero
    a refill is placed to have landed by then; the trajectory is read **again,
    with that refill counted**, so the next shortfall is the one that is left
    rather than the one already dealt with.

    A stock nothing can refill only ever falls, so there the first shortfall is
    final -- the schedule is not offered, because a plan whose reagent runs out
    is not a plan. Where a refill *would* serve and there is simply nowhere on
    the machine for it, the draw and its machine are handed back so a later round
    can keep that time clear (`construct`).

    **Refills fill to capacity** (SPEC 4.7.1), so one is placed only where a draw
    would actually break, and never two where one will do.
    """
    board = _machine_use(instance, solution)
    placed: list[RefillResult] = []
    sequence = 0
    for stock in sorted(set(draws) | set(levels)):
        device, resource = stock
        opening = levels.get(stock, 0)
        capacity = _capacity(instance, device, resource)
        events = draws.get(stock, [])
        candidates = [
            candidate
            for candidate in instance.replenishments
            if candidate.device == device and resource in candidate.resources
        ]
        mine: list[int] = []  # when this stock's refills land
        # One refill per draw is the most that can ever help: a draw that is still
        # short after its own top-up is short of a full stock.
        for _ in range(len(events) + 1):
            shortfall = _first_shortfall(opening, capacity, events, mine)
            if shortfall is None:
                break
            if not candidates or capacity < shortfall[1]:
                return None, None  # nothing reaches this stock, or one draw exceeds it
            refill = _place_refill(instance, board, candidates, shortfall[0], sequence)
            if refill is None:
                # There is a refill that would serve this draw and no room for it.
                # That is a question about the schedule, not about the stock.
                return None, (device, shortfall[0])
            placed.append(refill)
            mine.append(refill.end)
            sequence += 1
        else:
            return None, None
    return placed, None


def _first_shortfall(
    opening: int, capacity: int, draws: list[tuple[int, int]], refills: list[int]
) -> tuple[int, int] | None:
    """When the level first goes below zero, and by how much it was short.

    Read as the model reads it: a draw takes its amount at the activity's
    **start**, a refill lands at its **end** and fills to capacity, and a refill
    landing at the same instant as a draw has landed first (the model splits the
    two into separate events and puts the increase before the decrease, SPEC 4.7).
    """
    level = opening
    landings = sorted(refills)
    position = 0
    for when, amount in draws:
        while position < len(landings) and landings[position] <= when:
            level = capacity
            position += 1
        if level < amount:
            return when, amount
        level -= amount
    return None


def _machine_use(instance: Instance, solution: Solution) -> _Board:
    """The machines the schedule already occupies, so a refill can be fitted
    around them. Only devices matter here: a refill visits two machines and takes
    no spot (§4.7.1)."""
    board = _Board()
    for placement in solution.processing:
        for device in placement.mode.occupied_devices:
            board.device(device).take(placement.start, placement.end)
    for move in solution.transport:
        if move.option.from_spot == move.option.to_spot:
            continue
        if move.option.transporter is not None:
            board.transporter(move.option.transporter).take(move.start, move.end)
        for device in _devices_of(move.option):
            board.device(device).take(move.start, move.end)
    return board


def _place_refill(
    instance: Instance,
    board: _Board,
    candidates,
    before: int,
    sequence: int,
) -> RefillResult | None:
    """Fit one refill in so that it has landed by `before`, or say there is
    nowhere for it.

    A refill holds the device it fills **and** the replenisher it uses for its
    whole visit (SPEC 4.7.1), and the schedule has already booked both -- so the
    visit goes in the earliest window wide enough for it, and if even the
    earliest lands too late there is nowhere to put it.

    🔴 **The latest landing wins, not the earliest.** A refill fills to capacity,
    so one that lands early is spent on the draws in between and leaves the draw
    it was placed for exactly as short as it was. The longer visit breaks a tie,
    and then the replenisher's name, so two runs over one instance place the same
    refills.
    """
    best: tuple[tuple[int, int, str], RefillResult] | None = None
    for candidate in candidates:
        for option in candidate.options:
            start = _machine_window(board, candidate.device, option, before)
            if start is None:
                continue
            end = start + option.duration
            rank = (-end, -option.duration, option.replenisher)
            if best is not None and rank >= best[0]:
                continue
            best = (
                rank,
                RefillResult(
                    id=f"{candidate.id}#{sequence}",
                    device=candidate.device,
                    replenisher=option.replenisher,
                    amounts={
                        resource: _capacity(instance, candidate.device, resource)
                        for resource in candidate.resources
                    },
                    start=start,
                    end=end,
                ),
            )
    if best is None:
        return None
    refill = best[1]
    board.device(refill.device).take(refill.start, refill.end)
    if refill.replenisher != refill.device:
        board.device(refill.replenisher).take(refill.start, refill.end)
    return refill


def _machine_window(board: _Board, device: str, option, before: int) -> int | None:
    """The **latest** moment a refill can set off and still have landed by
    `before`, with both of the machines it needs free throughout.

    An optimal start is always either the beginning of the run or the moment
    something else on one of the two machines finishes, so those are the only
    candidates worth trying -- there is no gain in a start that could have been
    later.
    """
    filled = board.device(device)
    helper = board.device(option.replenisher)
    moments = {0}
    for line in (filled, helper):
        if line.pending is not None:
            return None  # a machine held open-endedly has no window to offer
        moments.update(end for _, end in line.busy)
    best: int | None = None
    for start in sorted(moments):
        if start + option.duration > before:
            continue
        if filled.free(start, option.duration) and helper.free(start, option.duration):
            best = start if best is None else max(best, start)
    return best


def _capacity(instance: Instance, device: str, resource: str) -> int:
    entry = instance.env.devices.get(device)
    return (entry.resources.get(resource, 0) if entry is not None else 0) or 0


def _replay(instance: Instance, order, floors: dict[int, int]) -> Solution | None:
    """Lay times over an order that is already known to work.

    Nothing here chooses anything: the mode of every activity and the route of
    every move were settled by the walk, and so was the sequence. Each step is
    put as early as the resources allow, which is what turns an order into a
    schedule.

    **Why this cannot paint itself into a corner.** The walk only ever moved
    material into a spot nothing was standing in, so by the time a step here
    wants a spot, the step that emptied it is behind us and has already been
    timed. There is no refusal to make. The `None` returns below are therefore
    assertions in the shape of code -- if one ever fires, the walk and this
    disagree about what a spot holds, and returning nothing is the safe way to
    disagree.

    **Spots are taken at or after everything already booked in them, never in a
    gap.** A product rests where it was made until its move comes for it, and
    how long that is is not known yet, so resting into a gap would overrun
    whatever was booked after it. Devices and transporters have no such
    problem -- nothing rests on them -- so those do use the gaps.
    """
    board = _Board()
    leaving, arriving = _edges(instance)
    _counts, orders = _waiting(instance)
    placed: dict[int, _Placement] = {}
    moves: dict[int, _Move] = {}
    resting: dict[int, int] = {}
    floor = {index: floors.get(index, 0) for index in range(len(instance.activities))}
    outputs: list[int] = []

    for kind, index, choice in order:
        if kind == "place":
            if not _replay_place(
                instance, board, index, choice, floor, arriving, leaving,
                moves, placed, resting, outputs, orders,
            ):
                return None
        elif not _replay_move(instance, board, index, choice, moves, resting):
            return None

    if len(placed) != len(instance.activities) or len(moves) != len(instance.arcs):
        return None  # the order did not cover everything
    makespan = _makespan(instance, placed, moves)
    for activity in outputs:
        placement = placed[activity]
        placed[activity] = _Placement(activity, placement.mode_index, placement.start, makespan)
        for index in arriving[activity]:
            bay = board.spot(moves[index].option.to_spot)
            if bay.pending is not None:
                bay.release(makespan)
    if any(line.pending is not None for line in board.spots.values()):
        return None  # material left resting with nothing to take it away
    return _assemble(instance, placed, moves, makespan)


def _replay_place(
    instance: Instance,
    board: _Board,
    index: int,
    mode_index: int,
    floor: dict[int, int],
    arriving: dict[int, list[int]],
    leaving: dict[int, list[int]],
    moves: dict[int, _Move],
    placed: dict[int, _Placement],
    resting: dict[int, int],
    outputs: list[int],
    orders: dict[int, list[int]],
) -> bool:
    """Put one activity down in the mode the walk chose for it."""
    act = instance.activities[index]
    if act.boundary is not None:
        pinned = _place_boundary(
            board, instance, index, [mode_index], floor[index], arriving[index], moves
        )
        if pinned is None:
            return False
        _chosen, start, end = pinned
        if act.boundary.kind == "output":
            outputs.append(index)
    else:
        mode = act.modes[mode_index]
        landed = {moves[arc].option.to_spot for arc in arriving[index]}
        start = max([floor[index], *(moves[arc].end for arc in arriving[index])])
        # Spots first, then the machines, then the spots again: pushing past a
        # machine can only move the start later, and a spot's answer to "when is
        # everything booked in you over" only grows with the question, so one
        # more look settles it.
        for _ in range(2):
            for spot in _spots_of(mode):
                free = board.spot(spot).clear_from(start, ours=spot in landed)
                if free is None:
                    return False
                start = max(start, free)
            if mode.device_access:
                for device in mode.devices:
                    free = board.device(device).earliest(start, mode.duration)
                    if free is None:
                        return False
                    start = max(start, free)
        end = start + mode.duration
        for arc in arriving[index]:
            bay = board.spot(moves[arc].option.to_spot)
            if bay.pending is not None:
                bay.release(start)
        for spot in _spots_of(mode):
            board.spot(spot).take(start, end)
        if mode.device_access:
            for device in mode.devices:
                board.device(device).take(start, end)

    placed[index] = _Placement(index, mode_index, start, end)
    # Whatever it produced rests where it was made until its move is timed.
    for arc_index in leaving[index]:
        resting[arc_index] = end
        for option in instance.arcs[arc_index].options:
            if option.src_mode_index == mode_index:
                board.spot(option.from_spot).hold(end)
                break
    for target in orders[index]:
        floor[target] = max(floor[target], end)
    return True


def _replay_move(
    instance: Instance,
    board: _Board,
    index: int,
    option_index: int,
    moves: dict[int, _Move],
    resting: dict[int, int],
) -> bool:
    """Send one move on the route the walk chose for it."""
    option = instance.arcs[index].options[option_index]
    start = resting.pop(index, 0)
    if option.from_spot == option.to_spot:
        # A hand-off that stays put takes no time and names no arm (§Parameters).
        landed = start
    else:
        devices = _devices_of(option)
        for _ in range(2):
            free = board.spot(option.to_spot).clear_from(start)
            if free is None:
                return False
            start = max(start, free)
            if option.transporter is not None:
                booked = board.transporter(option.transporter).earliest(start, option.duration)
                if booked is None:
                    return False
                start = max(start, booked)
            for device in devices:
                free = board.device(device).earliest(start, option.duration)
                if free is None:
                    return False
                start = max(start, free)
        landed = start + option.duration
        board.transporter(option.transporter).take(start, landed) if option.transporter else None
        for device in devices:
            board.device(device).take(start, landed)
    source = board.spot(option.from_spot)
    if source.pending is not None:
        source.release(landed)
    else:
        source.take(start, landed)
    board.spot(option.to_spot).hold(start)
    moves[index] = _Move(index, option_index, option, start, landed)
    return True


def _pass(
    instance: Instance,
    rule,
    floors: dict[int, int],
    reserved: tuple[tuple[str, int, int], ...] = (),
    history: _History | None = None,
    now: int = 0,
) -> Solution | None:
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
    # Machine time spoken for before any of this instance's work: a refill the
    # stocks need and the pass has to leave room for (`construct`).
    for device, held_from, held_to in reserved:
        board.device(device).take(held_from, held_to)
    past = history or _History({}, {})
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

    # What already ran goes on the board before anything is chosen, at the times
    # and in the modes reported. Everything after this point is the ordinary
    # forward pass, arranging what is left around it.
    if not _lay_out_history(
        instance, past, now, board, placed, moves, settled, resting, counts, orders, floor,
        leaving, outputs,
    ):
        return None
    held = _lay_out_held(instance, now, board, placed, settled, orders, counts, floor)
    if held is None:
        return None

    ready = sorted(
        (i for i, count in counts.items() if count == 0 and i not in placed), reverse=True
    )
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
    for activity in held:
        # An occupied spot is held until the run is over (§6.12), which is what
        # the pending hold said and what closing it here settles.
        placement = placed[activity]
        until = max(makespan, placement.start)
        placed[activity] = _Placement(activity, placement.mode_index, placement.start, until)
        for spot in _spots_of(instance.activities[activity].modes[placement.mode_index]):
            line = board.spot(spot)
            if line.pending is not None:
                line.release(until)
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


def _place_entry(
    instance: Instance,
    board: _Board,
    index: int,
    floor: dict[int, int],
    placed: dict[int, _Placement],
    settled: dict[int, int],
) -> _Placement | None:
    """Put an entry boundary node down, for a history that has already moved its
    material away.

    A fixation never mentions one: it is not work, so there is nothing to report
    about it, and yet the move that collected its material is reported. So when a
    fixed move names a source nothing has placed, the source is the entry -- and
    anything else is a report this cannot make sense of.
    """
    act = instance.activities[index]
    if act.boundary is None or act.boundary.kind != "input":
        return None
    at = floor.get(index, 0)
    for spot in _spots_of(act.modes[0]):
        if not board.spot(spot).free(at, 0):
            return None
        board.spot(spot).take(at, at)
    placed[index] = _Placement(index, 0, at, at)
    settled[index] = 0
    return placed[index]


def _lay_out_held(
    instance: Instance,
    now: int,
    board: _Board,
    placed: dict[int, _Placement],
    settled: dict[int, int],
    orders: dict[int, list[int]],
    counts: dict[int, int],
    floor: dict[int, int],
) -> list[int] | None:
    """Book the spots the document says are already occupied (§6.12).

    Material a stopped job left behind. It is not work and it is not going
    anywhere: it sits from `since` -- or from `now`, if `since` is in the past,
    since nothing pending could have used the spot before `now` anyway -- until
    the run is over. So the pass never gets to choose anything about it; it only
    has to keep off.
    """
    kept: list[int] = []
    for index, act in enumerate(instance.activities):
        if act.boundary is None or act.boundary.kind != "held":
            continue
        at = max(act.boundary.since or 0, now)
        for spot in _spots_of(act.modes[0]):
            if not board.spot(spot).free(at, 0):
                return None  # something already reported is standing there
            # 🔴 A hold, not a booking. "Until the run is over" has no number
            # yet, and every number worked out from the durations is one the
            # hold itself invalidates by pushing work past it. A pending hold
            # says what is meant -- nothing may have this spot -- and closes at
            # the makespan, the way a finished product's bay does.
            board.spot(spot).hold(at)
        placed[index] = _Placement(index, 0, at, at)
        settled[index] = 0
        kept.append(index)
        for target in orders[index]:
            floor[target] = max(floor[target], at)
            counts[target] -= 1
    return kept


def _lay_out_history(
    instance: Instance,
    past: _History,
    now: int,
    board: _Board,
    placed: dict[int, _Placement],
    moves: dict[int, _Move],
    settled: dict[int, int],
    resting: dict[int, int],
    counts: dict[int, int],
    orders: dict[int, list[int]],
    floor: dict[int, int],
    leaving: dict[int, list[int]],
    outputs: list[int],
) -> bool:
    """Put the reported history on the board, or say it cannot be put there.

    Activities first, then the moves, because a move's source spot is held from
    the moment its source activity finished -- which has to be known before the
    hold can be taken.
    """
    for index, (mode_index, start, end) in sorted(past.activities.items()):
        act = instance.activities[index]
        mode = act.modes[mode_index]
        for spot in _spots_of(mode):
            if not board.spot(spot).free(start, end - start):
                return False  # the report contradicts itself about a spot
            board.spot(spot).take(start, end)
        if mode.device_access:
            for device in mode.devices:
                if not board.device(device).free(start, end - start):
                    return False
                board.device(device).take(start, end)
        placed[index] = _Placement(index, mode_index, start, end)
        settled[index] = mode_index
        if act.boundary is not None and act.boundary.kind == "output":
            outputs.append(index)
        for target in orders[index]:
            floor[target] = max(floor[target], end)
            counts[target] -= 1

    for index, (option_index, start, end) in sorted(past.arcs.items()):
        arc = instance.arcs[index]
        option = arc.options[option_index]
        source = placed.get(arc.src_activity)
        if source is None:
            source = _place_entry(instance, board, arc.src_activity, floor, placed, settled)
        if source is None:
            return False  # a move that ran before the work that produced it
        if option.from_spot != option.to_spot:
            if option.transporter is not None:
                board.transporter(option.transporter).take(start, end)
            for device in _devices_of(option):
                board.device(device).take(start, end)
        # The source spot was the material's from when it was made until the move
        # completed; the destination is its from the moment the move set off, and
        # stays so until whatever receives it starts (FORMULATION §7).
        board.spot(option.from_spot).take(source.end, end)
        arrival = placed.get(arc.dst_activity)
        if arrival is None:
            board.spot(option.to_spot).hold(start)
        else:
            board.spot(option.to_spot).take(start, arrival.start)
        moves[index] = _Move(index, option_index, option, start, end)
        settled[arc.dst_activity] = option.dst_mode_index
        counts[arc.dst_activity] -= 1

    # Whatever the history left resting with no move yet reported waits where it
    # was made, and its departure cannot set off before `now`.
    for index, placement in placed.items():
        for arc_index in leaving[index]:
            if arc_index in moves:
                continue
            resting[arc_index] = max(placement.end, now)
            for option in instance.arcs[arc_index].options:
                if option.src_mode_index == placement.mode_index:
                    board.spot(option.from_spot).hold(placement.end)
                    break
    return True


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
        # A placeholder the caller replaces: `_scored` fills in the stages this
        # instance is actually minimised by and what they reached. It is set here
        # so that a `Solution` is never half-built, not because the makespan is
        # the objective.
        objective_kind=(objective_stages.MAKESPAN,),
        objective_values=(makespan,),
    )
