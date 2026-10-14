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
from dataclasses import dataclass, replace
from itertools import product

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
    # The activities a move may not set off before, by arc (design.md D64): the
    # producers of the conditions of the branches it enters or passes through. The
    # same form as a move waiting for its own source, so the walk stays a relaxation.
    gates: tuple[tuple[int, ...], ...]
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
    message = _explain(instance, shape, deepest)
    # Where moves wait for a branch's condition (design.md D64), say whether the wait
    # is what leaves no way through: an Object held back for a branch that has not
    # been decided may be standing where something else has to go. Walked again only
    # here, on the way to a refusal, so a plannable instance pays nothing for it.
    waited = sorted({gate for gates in shape.gates for gate in gates})
    if waited:
        free = replace(shape, gates=tuple(() for _ in shape.gates))
        if _walk(instance, free)[0] == "found":
            names = ", ".join(_name(instance, gate) for gate in waited)
            message += (
                f". Without waiting for the condition of a branch ({names}) there would"
                " be a way through: an Object held back until the branch is decided"
                " stands where something else has to go"
            )
    diags.error(errors.OBJECTS_DEADLOCKED, message)


def report_reservation_deadlocked(
    instance: Instance, diags: Diagnostics, history: bool
) -> None:
    """Emit `reservation_deadlocked` when no order keeps every undecided region's spots
    (design.md D68).

    The walk with the windows kept is the plan with its reservations and time erased:
    each region's choice -- which candidate a branch keeps, which modes a loop runs in --
    is a step of it, so where it finds no way through within its cap no choice keeps
    them all. Only where nothing has been reported (`history` False): a reservation does
    not move what has run (design.md D66, F1), and the walk cannot see the order history
    fixed. A walk that is stopped claims nothing, and neither does one the plain check
    has nothing to say about."""
    if not instance.reservations or history:
        return
    if any(not arc.options for arc in instance.arcs) or _read(instance) is None:
        return
    windows = windows_of(instance)
    if not windows:
        return
    outcome, _order = walk_with_windows(instance, windows)
    if outcome != "exhausted":
        return
    regions = ", ".join(sorted(format_node_path(window.region) for window in windows))
    shape = _read(instance)
    plain = _walked(instance, shape)[0] if shape is not None else "capped"
    without = (
        " Without keeping them there would be a way through: it is the keeping that"
        " leaves none."
        if plain == "found"
        else ""
    )
    diags.error(
        errors.RESERVATION_DEADLOCKED,
        f"no order of the work keeps the spots of every region decided only during the"
        f" run ({regions}) free for it from its opening to its exit, whichever"
        f" candidates and modes they keep. No schedule exists that keeps them, whatever"
        f" the durations are.{without} Either give the laboratory more places, free a"
        f" spot that is held, or plan fewer of these regions at once",
    )


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
        gates=tuple(arc.gates for arc in instance.arcs),
        keeps=tuple(keeps),
        width=width,
    )


@dataclass(frozen=True)
class Window:
    """An undecided region's reservation as the walk keeps it (design.md D68): its own
    work, the steps that open and close it, and what it chooses among.

    A branch opens once every producer of its condition is placed (`opens_after`); a
    loop once any of its own work is (`opens_after` empty). It closes once all its gates
    are placed (`closes`). While it is open, nothing that is not its own is on the spots
    it keeps -- and which spots those are is a choice the walk makes, as the solver does:
    a branch keeps one of its `candidates` (its assumed outcome held to that candidate's
    modes); a loop keeps the spots of the modes its last invocation runs in (`free`, the
    activities whose modes it picks, minus the pairs in `excluded`), those of the
    candidates chosen for the branches inside it (`borrowed`, by window), and one option
    of each of its `templates` groups.
    """

    region: tuple
    members: frozenset[int]
    arcs: frozenset[int]
    opens_after: tuple[int, ...]
    closes: tuple[int, ...]
    candidates: tuple[tuple[frozenset[str], tuple[tuple[int, int], ...]], ...] = ()
    free: tuple[int, ...] = ()
    excluded: frozenset[tuple[tuple[int, int], tuple[int, int]]] = frozenset()
    borrowed: tuple[int, ...] = ()
    templates: tuple[tuple[frozenset[str], ...], ...] = ()

    def is_open(self, mode: list[int], route: list[int]) -> bool:
        if all(mode[g] >= 0 for g in self.closes):
            return False
        if self.opens_after:
            return all(mode[i] >= 0 for i in self.opens_after)
        return any(mode[i] >= 0 for i in self.members) or any(route[r] >= 0 for r in self.arcs)

    def owns(self, owner: tuple[str, int, int]) -> bool:
        kind, index, _ = owner
        return index in (self.members if kind == "kept" else self.arcs)


def windows_of(instance: Instance) -> tuple[Window, ...]:
    """Every reservation of `instance` as a window (design.md D68) -- the branches' first,
    so that a loop choosing after them can keep what the branches inside it chose."""
    reservations = sorted(instance.reservations, key=lambda res: 0 if res.candidates else 1)
    index = {res.region: k for k, res in enumerate(reservations)}
    windows: list[Window] = []
    for res in reservations:
        inner = [r for r in res.borrowed if r in index]

        def under(i: int, inner=inner) -> bool:
            node = instance.activities[i].node
            return any(tuple(node[: len(r)]) == tuple(r) for r in inner)

        windows.append(Window(
            region=res.region,
            members=res.members,
            arcs=res.arcs,
            opens_after=res.opens_after,
            closes=res.closes,
            candidates=res.candidates,
            free=tuple(i for i in res.derived if not under(i)),
            excluded=frozenset(res.excluded),
            borrowed=tuple(index[r] for r in inner),
            templates=res.templates,
        ))
    return tuple(windows)


class _Choices:
    """What the walk has chosen for each window and what that holds it to: the spots each
    keeps, and the mode each activity a choice names is pinned to."""

    def __init__(self, windows: tuple[Window, ...]) -> None:
        self.picked = [-1] * len(windows)
        self.spots: list[frozenset[str]] = [frozenset()] * len(windows)
        self.pins: dict[int, int] = {}
        self.pinned_by: dict[int, int] = {}
        self.made: dict[tuple[int, int], tuple[frozenset[str], dict[int, int]]] = {}

    def next_window(self) -> int:
        return next((k for k, chosen in enumerate(self.picked) if chosen < 0), -1)

    def choose(self, window: int, option: int) -> None:
        spots, pins = self.made[(window, option)]
        self.picked[window] = option
        self.spots[window] = spots
        for activity, mode_index in pins.items():
            if activity not in self.pins:
                self.pins[activity] = mode_index
                self.pinned_by[activity] = window

    def unchoose(self, window: int) -> None:
        self.picked[window] = -1
        self.spots[window] = frozenset()
        for activity in [a for a, w in self.pinned_by.items() if w == window]:
            del self.pins[activity]
            del self.pinned_by[activity]


def _options(
    instance: Instance, windows: tuple[Window, ...], window: int, choices: _Choices
) -> Iterator[_Step]:
    """The choices open to `window`, in the order worth trying first, each one that holds
    no activity to a mode another choice already holds it to otherwise.

    First are those that keep the fewest spots another window already chose to keep
    (one inside or around this one aside, which shares them by design): two windows on
    one spot can only take turns, and where machines are interchangeable the walk would
    otherwise give every region the same one and spend its cap undoing that (design.md
    D68, the RNA-seq laboratory at two jobs). Only the order changes, so what the walk
    can find does not. A loop's modes are ordered one activity at a time, so its
    combinations are still made one by one rather than all up front."""
    w = windows[window]
    taken: set[str] = set()
    for k, other in enumerate(windows):
        if k == window or choices.picked[k] < 0:
            continue
        ra, rb = w.region, other.region
        if ra[: len(rb)] != rb and rb[: len(ra)] != ra:
            taken |= choices.spots[k]

    def fits(pins: dict[int, int]) -> bool:
        return all(choices.pins.get(i, m) == m for i, m in pins.items())

    if w.candidates:
        order = sorted(range(len(w.candidates)), key=lambda k: len(w.candidates[k][0] & taken))
        for k in order:
            kept, pins = w.candidates[k]
            held = dict(pins)
            if fits(held):
                choices.made[(window, k)] = (kept, held)
                yield ("choose", window, k)
        return
    base: set[str] = set()
    for inner in w.borrowed:
        base |= choices.spots[inner]

    def crowding(i: int, m: int) -> int:
        mode = instance.activities[i].modes[m]
        return len(({*mode.input_spots.values(), *mode.output_spots.values()} - base) & taken)

    ranges = [
        sorted(range(len(instance.activities[i].modes)), key=lambda m, i=i: crowding(i, m))
        for i in w.free
    ]
    ranges += [
        sorted(range(len(group)), key=lambda g, group=group: len((group[g] - base) & taken))
        for group in w.templates
    ]
    # A choice is named by its combination, not by where the order put it, so that one
    # name means one combination in every state the walk keys on.
    radices = [len(r) for r in ranges]
    for combo in product(*ranges):
        k = 0
        for value, radix in zip(combo, radices, strict=True):
            k = k * radix + value
        chosen = dict(zip(w.free, combo[: len(w.free)], strict=True))
        if any(
            chosen.get(g) == a and chosen.get(r) == b for (g, a), (r, b) in w.excluded
        ):
            continue
        if not fits(chosen):
            continue
        spots = set(base)
        for i, m in chosen.items():
            mode = instance.activities[i].modes[m]
            spots.update(mode.input_spots.values())
            spots.update(mode.output_spots.values())
        for group, pick in zip(w.templates, combo[len(w.free):], strict=True):
            spots |= group[pick]
        choices.made[(window, k)] = (frozenset(spots), chosen)
        yield ("choose", window, k)


def _breaks(
    windows: tuple[Window, ...],
    choices: _Choices,
    step: _Step,
    was_open: tuple[bool, ...],
    shape: _Shape,
    mode: list[int],
    route: list[int],
    board: _Board,
) -> bool:
    """Whether `step`, just applied, breaks a window: put work that is not a window's own
    on its spots while it was open, left something not its own on its spots while it is
    open, or has two windows open on one spot, neither inside the other."""
    kind, index, choice = step
    now_open = [w.is_open(mode, route) for w in windows]
    for k, (w, before, after) in enumerate(zip(windows, was_open, now_open, strict=True)):
        kept = choices.spots[k]
        if (
            kind == "place" and before and index not in w.members
            and any(spot in kept for spot in shape.spots[index][choice])
        ):
            return True
        if not after:
            continue
        for spot in kept:
            if any(not w.owns(owner) for owner in board.owners.get(spot, ())):
                return True
    opened = [k for k, flag in enumerate(now_open) if flag]
    for n, a in enumerate(opened):
        for b in opened[n + 1:]:
            ra, rb = windows[a].region, windows[b].region
            nested = ra[: len(rb)] == rb or rb[: len(ra)] == ra
            if not nested and choices.spots[a] & choices.spots[b]:
                return True
    return False


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


def find_order(
    instance: Instance, windows: tuple[Window, ...] = ()
) -> tuple[_Step, ...] | None:
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

    With `windows` (`windows_of`, design.md D68) the walk keeps undecided regions'
    reservations too, choosing what each keeps as the solver does: the order it finds
    keeps every window in its place (the choices are not steps of it), and none found
    within the cap means none does (`walk_with_windows`).
    """
    if any(not arc.options for arc in instance.arcs):
        return None
    shape = _read(instance)
    if shape is None:
        return None
    if windows:
        outcome, order = walk_with_windows(instance, windows)
        return order if outcome == "found" else None
    outcome, _deepest, order = _walked(instance, shape)
    return order if outcome == "found" else None


# The last windowed walk, kept so that the check before a solve and the construction
# after it do not walk one instance twice.
_LAST_WINDOWED: tuple | None = None


def walk_with_windows(
    instance: Instance, windows: tuple[Window, ...]
) -> tuple[str, tuple[_Step, ...]]:
    """The walk with the regions' windows kept (design.md D68): `"found"` and an order
    that keeps them (choices left out), `"exhausted"` when no order does -- the walk is
    the plan with its reservations and time erased, so then there is no schedule that
    keeps them -- or `"capped"`, which claims nothing."""
    global _LAST_WINDOWED
    if _LAST_WINDOWED is not None:
        ref, cap, walked_with, remembered = _LAST_WINDOWED
        if cap == _EXPANSIONS and ref() is instance and walked_with == windows:
            return remembered
    shape = _read(instance)
    if shape is None or any(not arc.options for arc in instance.arcs):
        result: tuple[str, tuple[_Step, ...]] = ("exhausted", ())
    else:
        outcome, _deepest, order = _walk(instance, shape, windows)
        result = (outcome, tuple(step for step in order if step[0] != "choose"))
    _LAST_WINDOWED = (weakref.ref(instance), _EXPANSIONS, windows, result)
    return result


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


def _walk(
    instance: Instance,
    shape: _Shape,
    windows: tuple[Window, ...] = (),
) -> _Walked:
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
    chosen = _Choices(windows)
    choices = _steps(instance, shape, mode, route, board, windows, chosen)

    while True:
        if placed == activities and all(taken >= 0 for taken in route):
            return "found", deepest, tuple(applied)
        if placed > deepest[0]:
            deepest = (placed, board.occupied())

        step = next(choices, None)
        if step is not None:
            if step[0] == "choose":
                chosen.choose(step[1], step[2])
            else:
                was_open = tuple(w.is_open(mode, route) for w in windows)
                _apply(instance, shape, step, mode, route, board)
                if windows and _breaks(windows, chosen, step, was_open, shape, mode, route, board):
                    _undo(instance, shape, step, mode, route, board)
                    continue
            # Packed inline rather than through a helper: this runs once per
            # state, and a call per state was measurably half the walk's time
            # on the widest environment.
            packed = (taken + 1 for taken in (*mode, *route))
            key = bytes(packed) if narrow else array("H", packed).tobytes()
            if windows:
                key += array("l", chosen.picked).tobytes()
            if key in seen:
                if step[0] == "choose":
                    chosen.unchoose(step[1])
                else:
                    _undo(instance, shape, step, mode, route, board)
                continue
            seen.add(key)
            budget -= 1
            if budget <= 0:
                return "capped", deepest, ()
            placed += step[0] == "place"
            stack.append(choices)
            applied.append(step)
            choices = _steps(instance, shape, mode, route, board, windows, chosen)
            continue

        if not stack:
            return "exhausted", deepest, ()
        undone = applied.pop()
        placed -= undone[0] == "place"
        if undone[0] == "choose":
            chosen.unchoose(undone[1])
        else:
            _undo(instance, shape, undone, mode, route, board)
        choices = stack.pop()


def _steps(
    instance: Instance,
    shape: _Shape,
    mode: list[int],
    route: list[int],
    board: _Board,
    windows: tuple[Window, ...] = (),
    picks: _Choices | None = None,
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

    With windows, every window chooses what it keeps before anything is placed or
    moved, one window at a time (`_options`): the first dive takes every first choice,
    and only a dead end comes back for the next.
    """
    if windows and picks is not None:
        window = picks.next_window()
        if window >= 0:
            yield from _options(instance, windows, window, picks)
            return
        # A spot held since before the plan (§6.12) is held from its start: before
        # anything else is put anywhere, as in every schedule -- or the walk could use
        # the spot first and hold it after, an order no schedule follows.
        for index, act in enumerate(instance.activities):
            if mode[index] < 0 and act.boundary is not None and act.boundary.kind == "held":
                for candidate in range(len(act.modes)):
                    if all(board.free(spot) for spot in shape.spots[index][candidate]):
                        yield ("place", index, candidate)
                return
    pins = picks.pins if picks is not None else {}
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
        pinned = pins.get(index)
        for candidate in range(len(instance.activities[index].modes)):
            if settled and candidate not in settled:
                continue
            if pinned is not None and candidate != pinned:
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
        if any(mode[gate] < 0 for gate in shape.gates[index]):
            continue  # it waits for a branch's condition, not yet produced
        settled = {
            instance.arcs[other].options[route[other]].dst_mode_index
            for other in shape.incoming[arc.dst_activity]
            if route[other] >= 0
        }
        # A choice made for a window holds what it names to one mode, and a move that
        # lands it in another leaves it an activity nothing can place.
        bound = pins.get(arc.dst_activity)
        # Two routes that differ only by the arm that drives them are one move here:
        # nothing in a walk with time erased is held by an arm, so trying the second
        # would only walk the first again under another name.
        offered: set[tuple[int, str, str]] = set()
        for option_index, option in enumerate(arc.options):
            if option.src_mode_index != chosen:
                continue
            if settled and option.dst_mode_index not in settled:
                continue
            if bound is not None and option.dst_mode_index != bound:
                continue
            same = (option.dst_mode_index, option.from_spot, option.to_spot)
            if same in offered:
                continue
            offered.add(same)
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
