"""Whether the Objects can all reach the end -- asked before any solve, and
asked without a clock.

A spot holds one thing at a time and material is always *somewhere*: it rests in
the spot it was made in until a move takes it away, and a move needs its
destination empty before it sets off (FORMULATION §7). A laboratory whose bays
are few can therefore reach a state where every Object's next bay is the one
another Object is standing in, and there is no schedule at all -- not a slow one,
not a bad one, none. That is an ordinary blocking deadlock, and this is the check
for it.

**Why it is here and not left to the solver.** The solver cannot say "impossible"
about an instance like that. It searches a horizon-bounded model and runs out of
budget, so what comes back is `unknown`, which is a statement about the budget
rather than about the laboratory. Measured on the RNA-seq minimal laboratory at
two jobs: two minutes of search, nothing proved. The same question settles here
in a few hundred states. That is the trade `report_crowded_outputs` already makes
next door, for the same reason.

**The abstraction, and the one direction it may err in.** What is searched is not
the model. Time is dropped; a move is one atomic step rather than an interval
holding both of its ends; devices, transporters, durations, inventories, the
fixation and promised completion times are all ignored. **Every one of those
differences takes a constraint away**, so anything the model can schedule this can
walk too -- which is the only direction that matters, because the conclusion drawn
here is *impossibility*. A relaxation that finds no way through proves there is
none.

⚠ **The exception is leaving out a step, and that is the one thing never done
here.** Dropping a relay, an arc or a boundary node would strand an Object the
laboratory can in fact carry, and this would then refuse a schedulable plan.
Relays and boundary nodes are searched as the ordinary activities the model makes
of them (`RelayInfo`, `BoundaryInfo`), not skipped. The same reasoning is why a
mode is dropped only when it is *provably* useless -- one no route can leave from.

**A necessary condition, not a sufficient one.** Silence says the Objects can be
got to the end, not that a schedule exists: nothing here is weighed against a
duration, a device, a transporter or a promise.

**Silence is also what a search too big to finish returns.** The state space grows
with the laboratory, so the walk is capped; reaching the cap claims nothing. What
that costs is nothing, because the instances this is for are the cramped ones, and
those are exactly the ones whose state space is small.
"""

from __future__ import annotations

import weakref
from array import array
from collections.abc import Iterator
from dataclasses import dataclass

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.core.identifiers import format_node_path
from ofplang.schedule.scheduler.instance import Instance
from ofplang.schedule.validation import errors

# How many states the walk will expand before giving up and claiming nothing. A
# schedulable instance almost never approaches this: the first dive follows one
# Object at a time to the end, which is a working order for any laboratory that
# has room for one Object at a time, so the walk reaches the goal in about as many
# steps as there are activities. The cap is for the other case -- an instance with
# no way through and too much room to enumerate -- where the honest answer is
# silence and the only thing that matters is arriving at it quickly.
_EXPANSIONS = 50_000

# An activity index paired with the mode it was placed in, or an arc index paired
# with the route it took. One step of the walk.
_Step = tuple[str, int, int]


@dataclass(frozen=True)
class _Shape:
    """The instance, read once into the form the walk wants.

    Everything here is derived and constant; the walk carries only which activity
    is placed in which mode, which arc took which route, and who is holding what.
    """

    # activity -> mode -> every spot that mode binds.
    spots: tuple[tuple[tuple[str, ...], ...], ...]
    # activity -> mode -> the spot each of its outgoing arcs leaves from, in the
    # order of `outgoing`. `None` marks a mode no route can leave from, which makes
    # the mode unusable rather than the instance unschedulable.
    departures: tuple[tuple[tuple[str | None, ...], ...], ...]
    incoming: tuple[tuple[int, ...], ...]
    outgoing: tuple[tuple[int, ...], ...]
    # Precedence sources, by the activity that waits on them.
    predecessors: tuple[tuple[int, ...], ...]
    # Bytes per choice in a state key; see `_read`.
    width: int
    # Whether the activity holds the spots of its mode for the rest of the run: an
    # `output` node parks a finished product there and a `held` node is material
    # left behind, and both are pinned to the end (FORMULATION §J5, SPEC §6.12).
    # This is the one occupancy that is never released, and it is what makes a
    # cramped laboratory impossible rather than merely slow.
    keeps: tuple[bool, ...]


def report_deadlocked_objects(instance: Instance, diags: Diagnostics) -> None:
    """Emit `objects_deadlocked` when the Objects cannot all be got to the end.

    Reported before the solve and the solve is then not run, which is the point:
    the alternative is the solver spending its whole budget to say `unknown`.
    """
    # An arc no route can serve is a different and better-named complaint, and
    # `report_unreachable` has already made it. Walking anyway would find the same
    # instance impossible and say so in the vaguer of the two ways.
    if any(not arc.options for arc in instance.arcs):
        return
    shape = _read(instance)
    if shape is None:
        return
    outcome, deepest, _order = _walked(instance, shape)
    if outcome != "exhausted":
        return
    diags.error(errors.OBJECTS_DEADLOCKED, _explain(instance, shape, deepest))


def _read(instance: Instance) -> _Shape | None:
    """Precompute the walk's view of the instance, or None when there is nothing
    to walk."""
    count = len(instance.activities)
    if not count:
        return None
    # States are keyed by a fixed number of bytes per activity and per arc, which
    # is what keeps the seen-set small enough to hold fifty thousand of them. A
    # choice that does not fit the width would make two different states share a
    # key -- the walk would skip one and could then call an instance exhausted
    # that is not -- so the width is chosen to fit, and an instance that would
    # need more than two bytes is not walked at all rather than risked.
    #
    # One byte was the first try and it was too narrow: the benchmark's arm-pool
    # rows reach 3,456 routes for a single arc, so three real instances were
    # being declined without anybody noticing.
    widest = max(
        (len(act.modes) for act in instance.activities),
        default=0,
    )
    widest = max(widest, max((len(arc.options) for arc in instance.arcs), default=0))
    if widest >= 65535:
        return None
    width = 1 if widest < 255 else 2
    incoming: list[list[int]] = [[] for _ in range(count)]
    outgoing: list[list[int]] = [[] for _ in range(count)]
    for index, arc in enumerate(instance.arcs):
        outgoing[arc.src_activity].append(index)
        incoming[arc.dst_activity].append(index)
    predecessors: list[list[int]] = [[] for _ in range(count)]
    for source, target in instance.precedence:
        predecessors[target].append(source)

    spots: list[tuple[tuple[str, ...], ...]] = []
    departures: list[tuple[tuple[str | None, ...], ...]] = []
    keeps: list[bool] = []
    for index, act in enumerate(instance.activities):
        per_mode_spots = []
        per_mode_departures = []
        for mode_index, mode in enumerate(act.modes):
            # A port on both sides names one spot twice; it is one place either way.
            bound = dict.fromkeys((*mode.input_spots.values(), *mode.output_spots.values()))
            per_mode_spots.append(tuple(bound))
            # Which spot a departure leaves from is the source mode's to say, so
            # every route out of this mode agrees and the first one answers.
            leaving: list[str | None] = []
            for arc_index in outgoing[index]:
                found = next(
                    (
                        option.from_spot
                        for option in instance.arcs[arc_index].options
                        if option.src_mode_index == mode_index
                    ),
                    None,
                )
                leaving.append(found)
            per_mode_departures.append(tuple(leaving))
        spots.append(tuple(per_mode_spots))
        departures.append(tuple(per_mode_departures))
        boundary = act.boundary
        keeps.append(boundary is not None and boundary.kind in ("output", "held"))

    return _Shape(
        spots=tuple(spots),
        departures=tuple(departures),
        incoming=tuple(tuple(items) for items in incoming),
        outgoing=tuple(tuple(items) for items in outgoing),
        predecessors=tuple(tuple(items) for items in predecessors),
        keeps=tuple(keeps),
        width=width,
    )


class _Board:
    """Who is holding which spot. An owner is a placed activity keeping its spot
    to the end, an activity's product waiting for the move that takes it away, or
    a delivery waiting for the activity that receives it -- and a spot with any
    owner at all is one nothing else may be put into."""

    def __init__(self) -> None:
        self.owners: dict[str, set[tuple[str, int, int]]] = {}

    def free(self, spot: str, *, ignoring: frozenset[tuple[str, int, int]] = frozenset()) -> bool:
        held = self.owners.get(spot)
        return not held or not (held - ignoring)

    def take(self, spot: str, owner: tuple[str, int, int]) -> None:
        self.owners.setdefault(spot, set()).add(owner)

    def release(self, spot: str, owner: tuple[str, int, int]) -> None:
        self.owners[spot].discard(owner)

    def occupied(self) -> list[str]:
        return sorted(spot for spot, held in self.owners.items() if held)


def find_order(instance: Instance) -> tuple[_Step, ...] | None:
    """An order of placements and moves that gets every Object to the end, or
    None when the walk finds none or is stopped before it can.

    This is the same walk `report_deadlocked_objects` makes -- literally the same
    one, since `_walked` hands back the walk that check already made of this
    instance -- read for its *witness* rather than for its verdict. Each step
    names an activity and the mode it runs in, or an arc and the route it takes,
    and the order respects
    what a spot can hold -- so laying times over it in this order, each thing as
    early as the resources allow, turns it into a schedule. That is what
    `greedy` does with it when every one of its own passes has come out empty.

    ⚠ **The order is a way through, not a good way through.** The walk follows
    one Object to its end before starting the next, because that is what makes
    it cheap; nothing in it is weighed against a duration.
    """
    if any(not arc.options for arc in instance.arcs):
        return None
    shape = _read(instance)
    if shape is None:
        return None
    outcome, _deepest, order = _walked(instance, shape)
    return order if outcome == "found" else None


# The verdict, the furthest the walk got (for the message), and the order it
# found (empty unless the verdict is "found").
_Walked = tuple[str, tuple[int, list[str]], tuple[_Step, ...]]


# The last walk, kept so that one instance is not walked twice.
#
# One instance is asked the same question from four places: `api` runs the refusal
# check on every instance before the solve, `greedy` walks the same instance again
# as its last resort, `cpsat` builds a constructed hint that walks it a third time,
# and a plan with promises builds one such hint per solve. On an instance the walk
# settles in milliseconds none of that matters. On one it cannot settle it is the
# whole cost paid over again -- measured at 13.7 seconds and then 12.4 on the LabOP
# growth curve in a one-slot laboratory, for an answer already in hand (report
# section 60.4).
#
# ⚠ **The cap is part of the key.** `_EXPANSIONS` is a module global a caller can
# move, and a verdict reached under one cap says nothing under another: a walk
# stopped at five states claims nothing where the same walk at fifty thousand
# refuses. Keying on the instance alone would hand the refusal back to the caller
# that asked for the smaller cap precisely to avoid it.
#
# ⚠ **The instance is held weakly**, so this keeps nothing alive. When it has been
# collected the entry misses and the walk is simply made again -- and holding it
# weakly is also what makes the identity test sound, since an address can only be
# reused after the object at it is gone, which is exactly when the reference dies.
_LAST: tuple[weakref.ref[Instance], int, _Walked] | None = None


def _walked(instance: Instance, shape: _Shape) -> _Walked:
    """`_walk`, but not a second time for an instance already walked."""
    global _LAST
    if _LAST is not None:
        ref, cap, remembered = _LAST
        if cap == _EXPANSIONS and ref() is instance:
            return remembered
    result = _walk(instance, shape)
    _LAST = (weakref.ref(instance), _EXPANSIONS, result)
    return result


def _walk(instance: Instance, shape: _Shape) -> _Walked:
    """Depth-first over placements and moves, with time erased.

    Returns `"found"` when the Objects can all be got to the end, `"exhausted"`
    when no order gets them there, and `"capped"` when the walk was stopped --
    which claims nothing. Also returns the furthest the walk ever got, for the
    message.

    The walk is iterative rather than recursive because its depth is the number of
    activities plus arcs, which on a joint plan is in the hundreds.
    """
    activities = len(instance.activities)
    legs = len(instance.arcs)
    mode = [-1] * activities
    route = [-1] * legs
    board = _Board()

    seen: set[bytes] = set()
    narrow = shape.width == 1
    budget = _EXPANSIONS
    deepest: tuple[int, list[str]] = (0, [])

    # Each entry is one still-open decision, suspended where it last handed a step
    # out. `applied` is what has to be undone to go back to it.
    stack: list[Iterator[_Step]] = []
    applied: list[_Step] = []
    placed = 0
    choices = _steps(instance, shape, mode, route, board)

    while True:
        if placed == activities and all(chosen >= 0 for chosen in route):
            return "found", deepest, tuple(applied)
        if placed > deepest[0]:
            deepest = (placed, board.occupied())

        step = next(choices, None)
        if step is not None:
            _apply(instance, shape, step, mode, route, board)
            # Packed inline rather than through a helper: this runs once per
            # state, and a call per state was measurably half the walk's time
            # on the widest environment.
            packed = (chosen + 1 for chosen in (*mode, *route))
            key = bytes(packed) if narrow else array("H", packed).tobytes()
            if key in seen:
                _undo(instance, shape, step, mode, route, board)
                continue
            seen.add(key)
            budget -= 1
            if budget <= 0:
                return "capped", deepest, ()
            placed += step[0] == "place"
            stack.append(choices)
            applied.append(step)
            choices = _steps(instance, shape, mode, route, board)
            continue

        if not stack:
            return "exhausted", deepest, ()
        undone = applied.pop()
        placed -= undone[0] == "place"
        _undo(instance, shape, undone, mode, route, board)
        choices = stack.pop()


def _steps(
    instance: Instance, shape: _Shape, mode: list[int], route: list[int], board: _Board
) -> Iterator[_Step]:
    """Everything that could happen next, in the order worth trying first.

    Placements come before moves, and that ordering is the whole reason the walk
    is affordable: it follows one Object through to its end before starting the
    next, which is a working order for any laboratory with room for one Object at
    a time -- so on a schedulable instance the very first dive reaches the goal and
    nothing is ever undone.

    **Yielded rather than returned**, and that is worth the awkwardness of holding
    a suspended generator on the stack: on the dive only the first step is ever
    taken, so building the rest is work thrown away. Measured on the widest
    synthetic environment, enumerating every step at every node cost half a second
    where stopping at the first costs a twentieth of that.

    The generator reads the board as it goes, which is sound only because the walk
    resumes it exactly where it suspended it -- every step applied after that point
    has been undone, so the state it sees on resumption is the state it was made
    in.
    """
    for index in range(len(instance.activities)):
        if mode[index] >= 0:
            continue
        if any(route[arc] < 0 for arc in shape.incoming[index]):
            continue  # its material has not arrived
        if any(mode[source] < 0 for source in shape.predecessors[index]):
            continue
        # A delivery already chose this activity's mode when it landed: a route
        # names the spots at both of its ends (SPEC §4).
        settled = {
            instance.arcs[arc].options[route[arc]].dst_mode_index for arc in shape.incoming[index]
        }
        if len(settled) > 1:
            continue  # two deliveries disagree; this activity can never run
        mine = frozenset(("delivery", arc, 0) for arc in shape.incoming[index])
        for candidate in range(len(instance.activities[index].modes)):
            if settled and candidate not in settled:
                continue
            if any(spot is None for spot in shape.departures[index][candidate]):
                continue  # no route leaves this mode; choosing it strands the arc
            if all(board.free(spot, ignoring=mine) for spot in shape.spots[index][candidate]):
                yield ("place", index, candidate)
    for index in range(len(instance.arcs)):
        if route[index] >= 0:
            continue
        arc = instance.arcs[index]
        chosen = mode[arc.src_activity]
        if chosen < 0:
            continue
        settled = {
            instance.arcs[other].options[route[other]].dst_mode_index
            for other in shape.incoming[arc.dst_activity]
            if route[other] >= 0
        }
        for option_index, option in enumerate(arc.options):
            if option.src_mode_index != chosen:
                continue
            if settled and option.dst_mode_index not in settled:
                continue
            # A hand-off that stays put moves nothing, so there is no bay to free;
            # any other move needs its destination empty before it sets off.
            if option.from_spot != option.to_spot and not board.free(option.to_spot):
                continue
            yield ("move", index, option_index)


def _apply(
    instance: Instance,
    shape: _Shape,
    step: _Step,
    mode: list[int],
    route: list[int],
    board: _Board,
) -> None:
    kind, index, choice = step
    if kind == "place":
        mode[index] = choice
        # What was delivered here is no longer waiting to be received.
        for arc in shape.incoming[index]:
            board.release(instance.arcs[arc].options[route[arc]].to_spot, ("delivery", arc, 0))
        # What it produced now rests where it was made, until its move takes it.
        for position, arc in enumerate(shape.outgoing[index]):
            leaves = shape.departures[index][choice][position]
            assert leaves is not None  # unusable modes are never offered
            board.take(leaves, ("product", arc, 0))
        if shape.keeps[index]:
            for spot in shape.spots[index][choice]:
                board.take(spot, ("kept", index, 0))
    else:
        route[index] = choice
        option = instance.arcs[index].options[choice]
        board.release(option.from_spot, ("product", index, 0))
        board.take(option.to_spot, ("delivery", index, 0))


def _undo(
    instance: Instance,
    shape: _Shape,
    step: _Step,
    mode: list[int],
    route: list[int],
    board: _Board,
) -> None:
    kind, index, choice = step
    if kind == "place":
        if shape.keeps[index]:
            for spot in shape.spots[index][choice]:
                board.release(spot, ("kept", index, 0))
        for position, arc in enumerate(shape.outgoing[index]):
            leaves = shape.departures[index][choice][position]
            assert leaves is not None
            board.release(leaves, ("product", arc, 0))
        for arc in shape.incoming[index]:
            board.take(instance.arcs[arc].options[route[arc]].to_spot, ("delivery", arc, 0))
        mode[index] = -1
    else:
        option = instance.arcs[index].options[choice]
        board.release(option.to_spot, ("delivery", index, 0))
        board.take(option.from_spot, ("product", index, 0))
        route[index] = -1


def _explain(instance: Instance, shape: _Shape, deepest: tuple[int, list[str]]) -> str:
    """Say what was found, in terms of the laboratory rather than of the search.

    The furthest the walk ever got is the useful part: it names the spots that were
    full when everything stopped, and those are the ones to add or to bind
    elsewhere.
    """
    placed, occupied = deepest
    total = len(instance.activities)
    kept = sorted(
        {
            spot
            for index, act in enumerate(instance.activities)
            if shape.keeps[index]
            for mode in act.modes
            for spot in (*mode.input_spots.values(), *mode.output_spots.values())
        }
    )
    parked = [
        _name(instance, index)
        for index, act in enumerate(instance.activities)
        if act.boundary is not None and act.boundary.kind == "output"
    ]
    detail = (
        f" The places that were full are {', '.join(occupied)}." if occupied else ""
    )
    ending = (
        f" {len(parked)} of the activities park material until the run is over "
        f"({', '.join(kept)}), which is the occupancy that never lifts."
        if parked and kept
        else ""
    )
    return (
        f"the objects cannot all reach the end: the best any order does is place "
        f"{placed} of {total} activities, and then every remaining move is into a "
        f"place something else is standing in. No schedule exists, whatever the "
        f"durations are.{detail}{ending} Either give the laboratory more places, "
        f"bind the finished products to places of their own (interface.outputs, "
        f"SPEC §6.8), or plan fewer jobs at once"
    )


def _name(instance: Instance, index: int) -> str:
    """How to call an activity in a message. A boundary node has no node path --
    an empty path is what marks it as the interface side -- so it is named by what
    it is and, on a joint plan, by whose it is."""
    act = instance.activities[index]
    if act.boundary is not None:
        owner = f" of {act.boundary.job}" if act.boundary.job else ""
        return f"the {act.boundary.kind} boundary{owner}"
    if act.relay is not None:
        return f"a relay on {format_node_path(act.relay.arc.src.node)}"
    return format_node_path(act.node)
