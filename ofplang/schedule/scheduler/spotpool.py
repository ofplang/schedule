"""Handing out the bays of a collapsed spot class (SPECIFICATIONS.md §10.4).

Where a class of interchangeable spots is treated as one resource of capacity
`len(members)`, the solve decides *when* each thing happens and that no instant
asks for more bays than there are; this decides *which* bay each thing gets. It is
the spot counterpart of `cpsat._assign_pooled_transporters`, and harder for one
reason: **material stays put**.

## The chain

A transporter's occupancies are one interval per move and independent of each
other, so they can be handed out one at a time. A spot's cannot: the occupancies
that describe one stay of one Object must all name the **same** bay. Three kinds
of occupancy hold a spot (see `cpsat.solve`):

- an activity's own interval `[s_i, e_i]`, once per distinct spot its mode binds;
- a move's **source** hold `[e_src, b_r]`, on the spot it left from;
- a move's **destination** hold `[a_r, s_dst]`, on the spot it arrived at.

and the route-selection constraints tie a move's source hold to the spot its
source activity's mode bound, and its destination hold to the spot its
destination activity's mode bound. **That tie is the chain**, and it is read off
the instance rather than guessed from the times: two occupancies that merely touch
in time are not one stay, and grouping by contact would fuse a bay's consecutive
tenants into a single chain that no longer has to be one bay.

So a chain is

    {an activity's interval on one spot} + {the holds of the moves that leave or
    arrive there}

which is **one activity's residency** -- an arc ties each of its sides to the
activity at that end and does not tie the two sides to each other. A chain would
reach across an arc only where a move begins and ends on the same spot, and such a
move takes no time (§5.4), which `symmetry.aggregatable_spots` refuses. A relay is
zero-duration too, and is itself the activity rather than a bridge between two.

The chain's items meet at their endpoints -- a delivery ends where the activity
starts, a departure starts where it ends -- so their union is a single interval.
That is what makes the colouring below correct, so it is checked rather than
assumed.

## Why greedy is enough

The chains of one class are intervals, and a set of intervals is a graph whose
chromatic number equals its largest overlap (interval graphs are perfect, and a
clique of intervals is a set covering one instant). The cumulative bounded that
overlap by the number of bays, so a colouring exists; taking the chains in order of
start and giving each the lowest-named free bay finds one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Occupancy:
    """One interval that holds one spot.

    `kind` and `index` say what put it there, which is what the caller needs in
    order to write the assignment back: `activity` names an activity, `arc_from`
    and `arc_to` the two sides of a move."""

    spot: str
    start: int
    end: int
    kind: str  # "activity" | "arc_from" | "arc_to"
    index: int


@dataclass(frozen=True)
class Chain:
    """One Object's stay in one bay: the occupancies that must share it, and the
    interval they cover together."""

    items: tuple[Occupancy, ...]
    start: int
    end: int


class NotOneStay(Exception):
    """A chain's occupancies do not cover one interval.

    Raised rather than worked around: the colouring is only correct for intervals,
    so a chain with a gap in it would be handed a bay it does not hold throughout.
    Reaching this means the tie between a move's hold and its activity's spot is
    not what this module believes, and the guards in
    `symmetry.aggregatable_spots` are what keep that true."""


class NotColourable(Exception):
    """The chains of a class cannot be handed out over its bays.

    Reaching it means the capacity resource and the non-overlaps it replaced
    disagree, so the schedule on offer is one no assignment of bays can realise --
    and handing it out would be worse than failing."""


def occupancies(
    instance,
    members: frozenset[str],
    mode_of,
    option_of,
    activity_times,
    arc_times,
) -> list[Occupancy]:
    """Every interval that holds a member of this class, in the schedule described
    by `mode_of` / `option_of` (per activity / per arc) and the times.

    `activity_times[i]` and `arc_times[r]` are `(start, end)` pairs. A cancelled
    activity holds nothing (§6.2) and is left out by the caller passing no mode for
    it."""
    found: list[Occupancy] = []
    for i, act in enumerate(instance.activities):
        mode = mode_of(i)
        if mode is None:
            continue
        start, end = activity_times[i]
        bound = set(mode.input_spots.values()) | set(mode.output_spots.values())
        for spot in sorted(bound & members):
            found.append(Occupancy(spot, start, end, "activity", i))
        del act
    for r, arc in enumerate(instance.arcs):
        option = option_of(r)
        if option is None:
            continue
        begin, finish = arc_times[r]
        if option.from_spot in members:
            # The source spot is held from when the source activity finished until
            # the move ends (`cpsat.solve`).
            found.append(
                Occupancy(
                    option.from_spot,
                    activity_times[arc.src_activity][1],
                    finish,
                    "arc_from",
                    r,
                )
            )
        if option.to_spot in members:
            found.append(
                Occupancy(
                    option.to_spot,
                    begin,
                    activity_times[arc.dst_activity][0],
                    "arc_to",
                    r,
                )
            )
    return found


def build_chains(instance, items: list[Occupancy]) -> list[Chain]:
    """Group occupancies into chains: one per stay, read off the instance.

    A move's source hold joins its source activity's occupancy of the spot it left
    from, and its destination hold joins its destination activity's occupancy of
    the spot it arrived at. Nothing else joins anything, which is why a chain is
    one activity's residency."""
    # An activity's occupancy is identified by (activity, spot): one per distinct
    # spot its mode binds, which is how `cpsat.solve` feeds them to the resource.
    activity_item: dict[tuple[int, str], int] = {}
    for position, item in enumerate(items):
        if item.kind == "activity":
            activity_item[(item.index, item.spot)] = position

    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for position, item in enumerate(items):
        if item.kind == "arc_from":
            anchor = activity_item.get((instance.arcs[item.index].src_activity, item.spot))
        elif item.kind == "arc_to":
            anchor = activity_item.get((instance.arcs[item.index].dst_activity, item.spot))
        else:
            continue
        # Absent only where the activity holds nothing there -- a cancelled
        # activity, or a boundary node the caller left out. Then the hold stands
        # alone, which is what it does in the non-overlap too.
        if anchor is not None:
            union(position, anchor)

    grouped: dict[int, list[Occupancy]] = {}
    for position, item in enumerate(items):
        grouped.setdefault(find(position), []).append(item)
    return [_close(group) for group in grouped.values()]


def _close(group: list[Occupancy]) -> Chain:
    """One chain, with the check that its occupancies really cover one interval."""
    ordered = sorted(group, key=lambda item: (item.start, item.end))
    reach = ordered[0].end
    for item in ordered[1:]:
        if item.start > reach:
            raise NotOneStay(
                f"a stay on {item.spot} has a gap: "
                f"{[(i.kind, i.index, i.start, i.end) for i in ordered]}"
            )
        reach = max(reach, item.end)
    return Chain(tuple(ordered), ordered[0].start, reach)


def assign(chains: list[Chain], members: tuple[str, ...]) -> dict[int, str]:
    """Give each chain a bay -- earliest start first, to the lowest-named bay that
    is free -- keyed by the chain's position in `chains`.

    Raises `NotColourable` if none is free, which the capacity resource exists to
    prevent."""
    free_at: dict[str, int] = dict.fromkeys(members, -1)
    out: dict[int, str] = {}
    order = sorted(range(len(chains)), key=lambda k: (chains[k].start, chains[k].end))
    for k in order:
        chain = chains[k]
        bay = next((m for m in members if free_at[m] <= chain.start), None)
        if bay is None:
            raise NotColourable(
                f"no bay free at {chain.start} among {len(members)}"
            )
        free_at[bay] = chain.end
        out[k] = bay
    return out


def overlapping(
    chains: list[Chain], assignment: dict[int, str]
) -> list[tuple[Chain, Chain]]:
    """Pairs of chains given the same bay that overlap in time.

    The one thing a capacity resource does not check for the caller, so it is
    checked here rather than trusted."""
    by_bay: dict[str, list[Chain]] = {}
    for k, bay in assignment.items():
        by_bay.setdefault(bay, []).append(chains[k])
    bad: list[tuple[Chain, Chain]] = []
    for held in by_bay.values():
        held.sort(key=lambda c: (c.start, c.end))
        for earlier, later in zip(held, held[1:], strict=False):
            if earlier.end > later.start:
                bad.append((earlier, later))
    return bad
