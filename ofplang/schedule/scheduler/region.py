"""Regions decided only during the run, and the candidates a local plan gives them.

A branch whose condition is produced during the run has outcomes the planner cannot
all plan together: whichever arm is planned, the run may take the other. Design.md
D66 makes such a branch a **region** -- what happens inside it is planned on its own,
with the spots it uses kept for it, and what surrounds it sees only the gates it hands
things on through (`model.Gate`). Then whichever outcome happens, everything outside is
still a schedule, and checking the outcomes is checking each one locally.

This module is the local half (stage 3): for one undecided branch it plans every
outcome on its own, on a common place to leave each Object (the gates' spots), and
returns the **candidates** -- the modes each outcome runs in, the gates' spots, and the
spots the region needs (`Candidate.spots`). An environment that offers interchangeable
machines offers interchangeable candidates, which are what lets several such regions
run side by side instead of all wanting the same machine.

Nothing here constrains a plan yet: the candidates are what a later stage reserves.
What this stage settles is whether a region has one at all -- an outcome that cannot be
planned within its own spots, or with no place every outcome can leave an Object on in
one move, is an outcome the run may take and the plan could not follow.

The local plans are built by `greedy` (with its `mobility` walk), so they are
deterministic, cheap, and need no solver (design.md D66, stage 3 Q1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import product

from ofplang.schedule.scheduler import greedy
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    ArcInstance,
    Instance,
    routes,
    transport_options,
)
from ofplang.schedule.scheduler.model import (
    GATE_IN,
    GATE_OUT,
    GATE_OUT_PORT,
    Arc,
    Endpoint,
    Mode,
    NodePath,
    slot_key,
)
from ofplang.schedule.scheduler.symmetry import (
    _IDENTITY,
    DEVICE,
    SPOT,
    _device_swap,
    _mode_key,
    _spot_swap,
    interchangeable_classes,
)

# How many candidates a region offers at most, and how many combinations of gate spots
# a local plan tries. Internal limits, not settings (design.md D66, D3): a region with
# more interchangeable machines than this still has candidates, only not every one.
CANDIDATE_CAP = 8
EXIT_COMBINATIONS_CAP = 64

# The port a local plan's synthetic ends use.
_END = "end"


@dataclass(frozen=True)
class Candidate:
    """One way to run an undecided region, planned locally (design.md D66).

    `plans` are, per outcome -- the one the plan assumes first, as the outcomes were
    given -- the mode of every activity and gate of the region. `exits` are the holding
    gates' spots, the same in every outcome: wherever an outcome leaves an Object, it
    is moved to that spot in at most one move. `spots` are every spot any outcome uses,
    the gates' included: what the region needs kept for it. `durations` are the local
    plans' lengths, per outcome, in the same order."""

    region: NodePath
    plans: tuple[dict[NodePath, Mode], ...]
    exits: dict[NodePath, str]
    spots: frozenset[str]
    durations: tuple[int, ...]


def branch_candidates(
    region: NodePath, outcomes: Sequence[Instance], cap: int = CANDIDATE_CAP
) -> tuple[tuple[Candidate, ...], str | None]:
    """The candidates of an undecided branch at `region`, or none and why.

    `outcomes` are full instances of the workflow, one per outcome of the region -- the
    one the plan assumes first. A branch whose arms hold undecided branches of their
    own has an outcome per arm of those too (design.md D66 Q4): every spot any of them
    uses is the region's.

    Planned on the common exit spots that let every outcome through and are best for
    the outcome assumed, then for the others (Q2): a spot an outcome already leaves its
    Object on costs it nothing, so where one exists it is what comes out first. Then
    the candidate is carried over every interchangeable machine of the environment, as
    far as the region's own activities can tell them apart.
    """
    shapes = [_Shape.of(region, outcome) for outcome in outcomes]
    gates = shapes[0].holding_gates()
    choices = {}
    for gate in gates:
        allowed = _exit_choices(gate, shapes)
        if not allowed:
            return (), (
                f"no spot is within one move of where every outcome leaves the Object "
                f"for gate {_name(gate)}"
            )
        choices[gate] = allowed

    best: tuple[tuple[int, ...], dict[NodePath, str], list] | None = None
    for count, chosen in enumerate(product(*(choices[gate] for gate in gates))):
        if count >= EXIT_COMBINATIONS_CAP:
            break
        if len(set(chosen)) != len(chosen):
            continue  # two Objects cannot wait on one spot
        exits = dict(zip(gates, chosen, strict=True))
        plans = [shape.plan(exits) for shape in shapes]
        if any(plan is None for plan in plans):
            continue
        durations = tuple(plan[1] for plan in plans)  # type: ignore[index]
        if best is None or durations < best[0]:
            best = (durations, exits, plans)
    if best is None:
        return (), "no outcome-by-outcome plan fits a common place to leave its Objects"

    durations, exits, plans = best
    base = Candidate(
        region=region,
        plans=tuple(plan[0] for plan in plans),  # type: ignore[index]
        exits=exits,
        spots=_spots_of(tuple(plan[0] for plan in plans), exits),  # type: ignore[index]
        durations=durations,
    )
    return _images(base, shapes, choices, cap), None


def _spots_of(
    plans: tuple[dict[NodePath, Mode], ...], exits: dict[NodePath, str]
) -> frozenset[str]:
    """Every spot the plans' modes bind, and the exits."""
    spots = set(exits.values())
    for plan in plans:
        for mode in plan.values():
            spots.update(mode.input_spots.values())
            spots.update(mode.output_spots.values())
    return frozenset(spots)


def _name(path: NodePath) -> str:
    return "/".join(str(step) for step in path)


# --------------------------------------------------------------------------
# One outcome, seen from inside the region.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Shape:
    """One outcome's region: its activities and gates, the arcs among them, the arcs
    entering it from outside, and the precedence inside -- all by index into the full
    instance, which keeps everything else they need (modes, options)."""

    region: NodePath
    full: Instance
    members: tuple[int, ...]
    inner: tuple[int, ...]       # arcs with both ends in the region
    entering: tuple[int, ...]    # arcs from outside into the region
    precedence: tuple[tuple[int, int], ...]

    @staticmethod
    def of(region: NodePath, full: Instance) -> _Shape:
        members = tuple(
            i for i, act in enumerate(full.activities)
            if act.boundary is None and act.relay is None and act.node[: len(region)] == region
        )
        inside = set(members)
        return _Shape(
            region=region,
            full=full,
            members=members,
            inner=tuple(
                r for r, arc in enumerate(full.arcs)
                if arc.src_activity in inside and arc.dst_activity in inside
            ),
            entering=tuple(
                r for r, arc in enumerate(full.arcs)
                if arc.src_activity not in inside and arc.dst_activity in inside
            ),
            precedence=tuple(
                (s, d) for s, d in full.precedence if s in inside and d in inside
            ),
        )

    def holding_gates(self) -> list[NodePath]:
        """The region's own holding gates (`[region, "@out", ...]`), in order."""
        own = self.region + (GATE_OUT,)
        return [
            act.node for i in self.members
            if (act := self.full.activities[i]).gate
            and act.node[: len(own)] == own
            and any(mode.input_spots for mode in act.modes)
        ]

    def left_on(self, gate: NodePath) -> set[str]:
        """Where this outcome leaves the Object that goes out through `gate`."""
        for i in self.members:
            act = self.full.activities[i]
            if act.node == gate:
                return {m.input_spots[GATE_IN] for m in act.modes if GATE_IN in m.input_spots}
        return set()

    def plan(
        self,
        exits: dict[NodePath, str],
        pinned: dict[NodePath, Mode] | None = None,
    ) -> tuple[dict[NodePath, Mode], int] | None:
        """This outcome planned on its own: the mode of each member, and how long it
        takes -- or None where it cannot be planned within its own spots with every
        Object left at its exit. `pinned` holds members to one mode each."""
        local, back = self._local({gate: (spot,) for gate, spot in exits.items()}, pinned)
        solution = greedy.construct(local)
        if solution is None:
            return None
        modes = {
            back[p.activity]: p.mode for p in solution.processing if p.activity in back
        }
        return modes, solution.makespan or 0

    def _local(
        self, exits: dict[NodePath, tuple[str, ...]], pinned: dict[NodePath, Mode] | None
    ) -> tuple[Instance, dict[int, NodePath]]:
        """The region alone, as an instance of its own.

        Every member keeps its modes -- a holding gate only those on the spots `exits`
        allows it (one, to plan; every choice, to compare machines), a pinned member
        only its pin. What enters from outside starts on a source of its
        own, with one mode per spot the outside could leave it on; what leaves through a
        holding gate ends on a sink that takes it where it is. Nothing outside the
        region is there, which is the point: the region's spots are kept for it.
        """
        env = self.full.env
        activities: list[ActivityInstance] = []
        index: dict[int, int] = {}
        back: dict[int, NodePath] = {}
        for i in self.members:
            act = self.full.activities[i]
            modes = act.modes
            if act.node in exits:
                modes = tuple(_held_on(spot) for spot in exits[act.node])
            elif pinned is not None and act.node in pinned:
                modes = (pinned[act.node],)
            index[i] = len(activities)
            back[len(activities)] = act.node
            activities.append(ActivityInstance(act.node, act.process, modes, gate=act.gate))

        arcs: list[ArcInstance] = []

        def add(arc: Arc, src: int, src_key: str, dst: int, dst_key: str) -> None:
            options = transport_options(activities[src], src_key, activities[dst], dst_key, env)
            arcs.append(ArcInstance(arc, src, dst, tuple(options)))

        for r in self.inner:
            arc = self.full.arcs[r]
            add(arc.arc, index[arc.src_activity], slot_key(arc.arc.src),
                index[arc.dst_activity], slot_key(arc.arc.dst))
        for k, r in enumerate(self.entering):
            arc = self.full.arcs[r]
            outside = self.full.activities[arc.src_activity]
            key = slot_key(arc.arc.src)
            spots = sorted({m.output_spots[key] for m in outside.modes if key in m.output_spots})
            source = len(activities)
            activities.append(ActivityInstance(
                ("@source", k), "",
                tuple(_source_on(spot) for spot in spots),
            ))
            add(arc.arc, source, _END, index[arc.dst_activity], slot_key(arc.arc.dst))
        for k, gate in enumerate(exits):
            g = next(i for i in self.members if self.full.activities[i].node == gate)
            sink = len(activities)
            activities.append(ActivityInstance(
                ("@sink", k), "", tuple(_sink_on(spot) for spot in exits[gate])
            ))
            add(Arc(Endpoint(gate, GATE_OUT_PORT), Endpoint(("@sink", k), _END)),
                index[g], GATE_OUT_PORT, sink, _END)

        precedence = tuple((index[s], index[d]) for s, d in self.precedence)
        return Instance(env, self.full.time_unit, tuple(activities), tuple(arcs), precedence), back


def _held_on(spot: str) -> Mode:
    return Mode(id=f"gate:{spot}", devices=(), duration=0,
                input_spots={GATE_IN: spot}, output_spots={GATE_OUT_PORT: spot})


def _source_on(spot: str) -> Mode:
    return Mode(id=f"source:{spot}", devices=(), duration=0,
                input_spots={}, output_spots={_END: spot})


def _sink_on(spot: str) -> Mode:
    return Mode(id=f"sink:{spot}", devices=(), duration=0,
                input_spots={_END: spot}, output_spots={})


def _exit_choices(gate: NodePath, shapes: list[_Shape]) -> list[str]:
    """The spots `gate`'s Object may wait on: within one move -- or none -- of where
    every outcome leaves it (design.md D66 Q3 (A)), and from which what takes it after
    the region can have it. Where the outcome assumed leaves it comes first, so that a
    plan costing that outcome no move is tried first.

    The second condition is what keeps the candidates worth having: a spot the next
    step cannot be reached from is a plan that cannot go on, and offering it would
    only crowd out one that can -- measured, twelve cups each bound to a rack slot of
    its own came out with candidates on the first eight slots only, and no plan."""
    env = shapes[0].full.env
    everywhere = sorted(
        f"{device}.{spot}" for device, entry in env.devices.items() for spot in entry.spots
    )
    allowed = None
    for shape in shapes:
        left = shape.left_on(gate)
        reach = {spot for spot in everywhere if any(
            spot == start or routes(env, start, spot) for start in left
        )}
        allowed = reach if allowed is None else allowed & reach
    if allowed:
        allowed = {spot for spot in allowed if _goes_on_from(shapes[0].full, gate, spot)}
    if not allowed:
        return []
    first = sorted(allowed & shapes[0].left_on(gate))
    return first + sorted(allowed - set(first))


def _goes_on_from(full: Instance, gate: NodePath, spot: str) -> bool:
    """Whether everything taking the Object out of `gate` can take it from `spot`, in
    one of its modes: on that spot, or one move away. A gate nothing takes from yet
    goes on from anywhere."""
    env = full.env
    index = {act.node: i for i, act in enumerate(full.activities) if act.relay is None}
    g = index.get(gate)
    for arc in full.arcs:
        if arc.src_activity != g:
            continue
        key = slot_key(arc.arc.dst)
        wanted = {
            m.input_spots[key] for m in full.activities[arc.dst_activity].modes
            if key in m.input_spots
        }
        if not any(spot == w or routes(env, spot, w) for w in wanted):
            return False
    return True


# --------------------------------------------------------------------------
# The same candidate on interchangeable machines.
# --------------------------------------------------------------------------


def _images(
    base: Candidate, shapes: list[_Shape], choices: dict[NodePath, list[str]], cap: int
) -> tuple[Candidate, ...]:
    """`base` and its images under the swaps of interchangeable machines and spots,
    one per set of spots, at most `cap`, in a fixed order.

    A swap is used where it maps every outcome's region onto itself -- the region on
    its own, with every exit spot its gates may take: two machines the region cannot
    tell apart there run any of its plans equally (`symmetry`). Each image is planned
    again with its modes pinned anyway: cheap, and a disagreement would be a defect
    here rather than a candidate to offer."""
    swaps = _swaps(shapes, choices)
    found = {base.spots: base}
    frontier = [base]
    while frontier and len(found) < cap:
        nxt: list[Candidate] = []
        for candidate in frontier:
            for swap in swaps:
                image = _image(candidate, swap, shapes, choices, found)
                if image is None:
                    continue
                found[image.spots] = image
                nxt.append(image)
                if len(found) >= cap:
                    break
            if len(found) >= cap:
                break
        frontier = nxt
    rest = sorted((c for c in found.values() if c is not base), key=lambda c: sorted(c.spots))
    return (base, *rest)


def _swaps(shapes: list[_Shape], choices: dict[NodePath, list[str]]) -> list:
    """Every transposition of two devices or two spots that is an automorphism of
    every outcome's region (with all its exit choices open), in a fixed order."""
    together: dict[tuple[str, str, str], int] = {}
    for shape in shapes:
        # Holding gates with every exit open: the swap has to keep those valid too.
        local, _back = shape._local({gate: tuple(spots) for gate, spots in choices.items()}, None)
        for cls in interchangeable_classes(local):
            if cls.scope not in (DEVICE, SPOT):
                continue
            members = sorted(cls.members)
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    key = (cls.scope, a, b)
                    together[key] = together.get(key, 0) + 1
    swaps = []
    for (scope, a, b), seen in sorted(together.items()):
        if seen == len(shapes):
            swaps.append(_device_swap(a, b) if scope == DEVICE else _spot_swap(a, b))
    return swaps


def _image(
    candidate: Candidate, swap, shapes: list[_Shape], choices, found
) -> Candidate | None:
    """`candidate` with `swap` applied, planned again to confirm it -- or None where it
    is not one, or keeps the spots of one already `found`.

    The spots are worked out from the moved modes before anything is planned: most
    swaps move nothing the region keeps (two trays the Objects come from, say), and
    planning those again only to throw them away was the whole cost of this."""
    sub_spot = swap[0]
    exits = {gate: sub_spot(spot) for gate, spot in candidate.exits.items()}
    if any(spot not in choices[gate] for gate, spot in exits.items()):
        return None
    moves: list[dict[NodePath, Mode]] = []
    for shape, plan in zip(shapes, candidate.plans, strict=True):
        moved: dict[NodePath, Mode] = {}
        for path, mode in plan.items():
            if path in exits:
                continue
            image = _mode_image(shape, path, mode, swap)
            if image is None:
                return None
            moved[path] = image
        moves.append(moved)
    if _spots_of(tuple(moves), exits) in found:
        return None
    plans: list[dict[NodePath, Mode]] = []
    durations: list[int] = []
    for shape, moved in zip(shapes, moves, strict=True):
        planned = shape.plan(exits, pinned=moved)
        if planned is None:
            return None
        plans.append(planned[0])
        durations.append(planned[1])
    return Candidate(
        region=candidate.region,
        plans=tuple(plans),
        exits=exits,
        spots=_spots_of(tuple(plans), exits),
        durations=tuple(durations),
    )


def _mode_image(shape: _Shape, path: NodePath, mode: Mode, swap) -> Mode | None:
    """The mode of the member at `path` that does what `mode` does with `swap` applied."""
    act = next(
        (shape.full.activities[i] for i in shape.members if shape.full.activities[i].node == path),
        None,
    )
    if act is None:
        return None
    wanted = _mode_key(mode, swap)
    return next((m for m in act.modes if _mode_key(m, _IDENTITY) == wanted), None)


# --------------------------------------------------------------------------
# What the plan keeps for a region (design.md D66, stage 4).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    """The spots an undecided region keeps for itself, and when, by index into one
    instance (design.md D66).

    While the region's outcome is open, nothing outside it may be on those spots: what
    the region may yet need is then always free when it needs it, whichever outcome
    happens, and the plan only gets later -- never stuck -- when it is not the one
    assumed.

    **When.** A branch keeps them from its decision -- the end of its condition's
    producers (`opens_after`) -- to its exit, the start of the last of its gates
    (`closes`); a loop from its first start to its exit (`opens_with_start`), since the
    next invocation may want any of them at any moment until the last condition says.

    **What.** Read off the plan, so a reservation can be an outcome of the same solve:
    a branch keeps the spots of the candidate chosen for it (`candidates`, one chosen,
    each with the modes its assumed outcome is held to, `pins`); a loop the spots of
    the modes its last invocation runs in (`derived`), and those of the candidate
    chosen for each undecided branch inside it (`borrowed`). `excluded` are pairs of
    mode choices that cannot both be taken -- a loop's holding gate on a spot its next
    invocation cannot be reached from.

    Where a branch's Objects wait for its decision needs no keeping: the Object resting
    there already holds it until it moves into the arm, and keeping it beyond that would
    only hold up the next one to use it (measured: a cup's inspection then waited for
    the previous cup's whole branch).

    Its own activities and moves (`members`, `arcs`) are on those spots as much as they
    like; only the rest is kept off -- and only what is still to be placed: what has
    run, or is running, is history, and a reservation cannot move it (design.md D66,
    F1)."""

    region: NodePath
    members: frozenset[int]
    arcs: frozenset[int]
    opens_after: tuple[int, ...] = ()
    opens_with_start: bool = False
    closes: tuple[int, ...] = ()
    candidates: tuple[tuple[frozenset[str], tuple[tuple[int, int], ...]], ...] = ()
    derived: tuple[int, ...] = ()
    borrowed: tuple[NodePath, ...] = ()
    excluded: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = ()

    def possible_spots(self, instance: Instance) -> frozenset[str]:
        """Every spot this reservation may come to keep, whichever choices are made."""
        spots: set[str] = set()
        for chosen, _pins in self.candidates:
            spots |= chosen
        for i in self.derived:
            for mode in instance.activities[i].modes:
                spots.update(mode.input_spots.values())
                spots.update(mode.output_spots.values())
        return frozenset(spots)

    def owns(self, kind: str, index: int) -> bool:
        """Whether a claim of activity (`kind` "a") or move ("r") `index` is this
        region's own work."""
        return index in (self.members if kind == "a" else self.arcs)


def _occupies(mode: Mode) -> tuple:
    """What a mode occupies: its devices and the spots of its ports."""
    return (
        tuple(mode.devices),
        tuple(sorted(mode.input_spots.items())),
        tuple(sorted(mode.output_spots.items())),
    )


def region_members(
    instance: Instance, region: NodePath
) -> tuple[frozenset[int], frozenset[int]]:
    """The activities and moves of `region` in `instance`: everything under its path --
    its gates included -- the relays of its moves, and every move into, inside or out
    of it."""

    def inside(path: NodePath) -> bool:
        return tuple(path[: len(region)]) == tuple(region)

    arcs = frozenset(
        r for r, arc in enumerate(instance.arcs)
        if inside(arc.arc.src.node) or inside(arc.arc.dst.node)
    )
    members = frozenset(
        i for i, act in enumerate(instance.activities)
        if (act.boundary is None and act.relay is None and inside(act.node))
        or (act.relay is not None
            and (inside(act.relay.arc.src.node) or inside(act.relay.arc.dst.node)))
    )
    return members, arcs


def branch_reservation(
    instance: Instance,
    region: NodePath,
    waits: Sequence[NodePath],
    candidates: Sequence[Candidate],
) -> Reservation | None:
    """What an undecided branch at `region` keeps, from its candidates -- each held to
    the modes its assumed outcome runs in and the spots its gates wait on. A candidate
    whose modes this instance does not offer is left out; None where none is left."""
    members, arcs = region_members(instance, region)
    index = {instance.activities[i].node: i for i in members}
    kept: list[tuple[frozenset[str], tuple[tuple[int, int], ...]]] = []
    for candidate in candidates:
        pins: list[tuple[int, int]] = []
        for path, mode in candidate.plans[0].items():
            i = index.get(path)
            if i is None:
                break
            modes = instance.activities[i].modes
            spot = candidate.exits.get(path)
            if spot is not None:
                found = [k for k, x in enumerate(modes) if x.input_spots.get(GATE_IN) == spot]
            else:
                # By what the mode occupies, not by the object: a member the history has
                # fixed carries its mode as read back from the plan (`normalize`).
                found = [k for k, x in enumerate(modes) if _occupies(x) == _occupies(mode)]
            if not found:
                break
            pins.append((i, found[0]))
        else:
            kept.append((candidate.spots, tuple(sorted(pins))))
    if not kept:
        return None
    by_path = {act.node: i for i, act in enumerate(instance.activities) if act.relay is None}
    return Reservation(
        region=region,
        members=members,
        arcs=arcs,
        opens_after=tuple(by_path[w] for w in waits if w in by_path),
        closes=tuple(sorted(i for i in members if instance.activities[i].gate)),
        candidates=tuple(kept),
    )


def loop_reservation(
    instance: Instance,
    region: NodePath,
    count: int,
    borrowed: Sequence[NodePath],
    going_on: Instance | None,
) -> Reservation:
    """What an undecided loop at `region` keeps: the spots of the modes its last
    invocation runs in, and its holding gates' -- the next invocation runs the same
    modes (design.md D66 D3) -- with each undecided branch inside that invocation's
    candidate spots (`borrowed`).

    `going_on` is the instance with one invocation more, where the moves from the last
    holding gates into the next invocation exist: a gate spot the next invocation's
    first steps cannot be reached from, in the modes the last one ran, is excluded."""
    members, arcs = region_members(instance, region)
    last = tuple(region) + (count - 1,)
    derived = tuple(sorted(
        i for i in members
        if tuple(instance.activities[i].node[: len(last)]) == last
    ))
    return Reservation(
        region=region,
        members=members,
        arcs=arcs,
        opens_with_start=True,
        closes=tuple(sorted(i for i in members if instance.activities[i].gate)),
        derived=derived,
        borrowed=tuple(borrowed),
        excluded=_unreachable_pairs(instance, region, count, going_on),
    )


def cannot_go_on(reservation: Reservation, instance: Instance) -> bool:
    """Whether a loop's `excluded` pairs leave some holding gate and the activity its
    Object goes to next with no choice at all: then the loop can never go on, whatever
    the plan chooses."""
    pairs: dict[tuple[int, int], int] = {}
    for (gate, _g), (receiver, _m) in reservation.excluded:
        pairs[(gate, receiver)] = pairs.get((gate, receiver), 0) + 1
    return any(
        count == len(instance.activities[gate].modes) * len(instance.activities[receiver].modes)
        for (gate, receiver), count in pairs.items()
    )


def _unreachable_pairs(
    instance: Instance, region: NodePath, count: int, going_on: Instance | None
) -> tuple[tuple[tuple[int, int], tuple[int, int]], ...]:
    """(gate mode, receiver mode) pairs with no move between them: the last invocation's
    holding gate on a spot, and the activity that takes the Object first in the next
    invocation -- read as its counterpart in the last one -- on a spot it cannot be
    moved to from there."""
    if going_on is None:
        return ()
    index = {act.node: i for i, act in enumerate(instance.activities) if act.relay is None}
    nxt = tuple(region) + (count,)
    env = instance.env
    pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for arc in going_on.arcs:
        src, dst = arc.arc.src, arc.arc.dst
        if GATE_OUT not in src.node or tuple(dst.node[: len(nxt)]) != nxt:
            continue
        gate = index.get(src.node)
        receiver = index.get(tuple(region) + (count - 1,) + tuple(dst.node[len(nxt):]))
        if gate is None or receiver is None:
            continue
        key = slot_key(dst)
        for g, gm in enumerate(instance.activities[gate].modes):
            held = gm.output_spots.get(GATE_OUT_PORT)
            for m, rm in enumerate(instance.activities[receiver].modes):
                wanted = rm.input_spots.get(key)
                if held is None or wanted is None:
                    continue
                if held != wanted and not routes(env, held, wanted):
                    pairs.append(((gate, g), (receiver, m)))
    return tuple(pairs)


def kept_reservations(instance: Instance, solution, now: int) -> tuple | None:
    """What each undecided region keeps in `solution`, read off its placements as the
    solver reads it (design.md D66): the window from its decision -- or its first start,
    for a loop -- to its exit, never before `now`; and the spots of the candidate its
    placed modes are, or of its last invocation's modes and the candidates inside it.

    For a schedule built some other way than by the solver -- the previous plan laid out
    again (`greedy._replay_previous`) -- so that it reports what it keeps and the
    read-back can hold it to that. None where a branch's placed modes are none of its
    candidates': such a schedule keeps nothing it could say."""
    from ofplang.schedule.scheduler.result import ReservationResult

    by_activity = {p.activity: p for p in solution.processing}
    chosen: dict = {}
    for res in instance.reservations:
        if not res.candidates:
            continue
        for spots, pins in res.candidates:
            if all(by_activity[i].mode == instance.activities[i].modes[m] for i, m in pins):
                chosen[res.region] = spots
                break
        else:
            return None
    found = []
    for res in instance.reservations:
        if res.opens_after:
            opened = max(by_activity[i].end for i in res.opens_after)
        else:
            starts = [by_activity[i].start for i in res.members]
            starts += [solution.transport[r].start for r in res.arcs]
            opened = min(starts)
        start = max(opened, now)
        end = max([by_activity[g].start for g in res.closes] or [start])
        if end <= start:
            continue
        spots = set(chosen.get(res.region, ()))
        for i in res.derived:
            mode = by_activity[i].mode
            spots.update(mode.input_spots.values())
            spots.update(mode.output_spots.values())
        for inner in res.borrowed:
            spots.update(chosen.get(inner, ()))
        if spots:
            found.append(ReservationResult(res.region, start, end, tuple(sorted(spots))))
    return tuple(found)
