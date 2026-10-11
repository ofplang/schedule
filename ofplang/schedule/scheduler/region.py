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
    every outcome leaves it (design.md D66 Q3 (A)). Where the outcome assumed leaves it
    comes first, so that a plan costing that outcome no move is tried first."""
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
    if not allowed:
        return []
    first = sorted(allowed & shapes[0].left_on(gate))
    return first + sorted(allowed - set(first))


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
                image = _image(candidate, swap, shapes, choices)
                if image is None or image.spots in found:
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


def _image(candidate: Candidate, swap, shapes: list[_Shape], choices) -> Candidate | None:
    """`candidate` with `swap` applied, planned again to confirm it, or None."""
    sub_spot = swap[0]
    exits = {gate: sub_spot(spot) for gate, spot in candidate.exits.items()}
    if any(spot not in choices[gate] for gate, spot in exits.items()):
        return None
    plans: list[dict[NodePath, Mode]] = []
    durations: list[int] = []
    for shape, plan in zip(shapes, candidate.plans, strict=True):
        moved: dict[NodePath, Mode] = {}
        for path, mode in plan.items():
            if path in exits:
                continue
            image = _mode_image(shape, path, mode, swap)
            if image is None:
                return None
            moved[path] = image
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
