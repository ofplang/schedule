"""Handing out the bays of a spot class (scheduler/spotpool.py).

Nothing in the model is collapsed yet; this pins the logic the collapse will
depend on. The claims are: a stay is grouped from the instance rather than from
the clock, a stay covers one interval, the greedy colouring never puts two
overlapping stays in one bay, and it refuses rather than guesses when it cannot.

The last of those matters most. A capacity resource counts demand and does not
check that the demand can be handed out, so this is the only place that does.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ofplang.schedule.scheduler.spotpool import (
    NotColourable,
    NotOneStay,
    Occupancy,
    assign,
    build_chains,
    occupancies,
    overlapping,
)

BAYS = ("bench.a", "bench.b")


# --- a stand-in instance, so the grouping can be tested on its own --------


@dataclass(frozen=True)
class _Mode:
    input_spots: dict
    output_spots: dict


@dataclass(frozen=True)
class _Act:
    modes: tuple


@dataclass(frozen=True)
class _ArcEnds:
    src_activity: int
    dst_activity: int


@dataclass(frozen=True)
class _Instance:
    activities: tuple
    arcs: tuple


def _instance(n_activities: int, arcs: list[tuple[int, int]]) -> _Instance:
    return _Instance(
        tuple(_Act(()) for _ in range(n_activities)),
        tuple(_ArcEnds(src, dst) for src, dst in arcs),
    )


# --- grouping -------------------------------------------------------------


def test_an_activity_and_the_move_that_leaves_it_are_one_stay():
    instance = _instance(2, [(0, 1)])
    items = [
        Occupancy("bench.a", 0, 5, "activity", 0),
        Occupancy("bench.a", 5, 8, "arc_from", 0),
    ]
    (chain,) = build_chains(instance, items)
    assert (chain.start, chain.end) == (0, 8)
    assert {i.kind for i in chain.items} == {"activity", "arc_from"}


def test_a_delivery_and_the_activity_it_feeds_are_one_stay():
    instance = _instance(2, [(0, 1)])
    items = [
        Occupancy("bench.a", 3, 6, "arc_to", 0),
        Occupancy("bench.a", 6, 9, "activity", 1),
    ]
    (chain,) = build_chains(instance, items)
    assert (chain.start, chain.end) == (3, 9)


def test_the_two_sides_of_one_move_are_not_one_stay():
    # The point the whole design rests on: an arc ties each side to the activity at
    # that end, and does not tie the two sides to each other. Here the move leaves
    # bay a and arrives at bay b, so it is two stays on two bays -- and it would be
    # one only if it began and ended on the same spot, which takes no time and is
    # refused before the collapse.
    instance = _instance(2, [(0, 1)])
    items = [
        Occupancy("bench.a", 0, 4, "activity", 0),
        Occupancy("bench.a", 4, 6, "arc_from", 0),
        Occupancy("bench.b", 4, 7, "arc_to", 0),
        Occupancy("bench.b", 7, 9, "activity", 1),
    ]
    chains = build_chains(instance, items)
    assert sorted((c.start, c.end) for c in chains) == [(0, 6), (4, 9)]


def test_two_tenants_of_one_bay_that_merely_touch_are_two_stays():
    # 🔴 Why the grouping is read off the instance and not off the clock. These two
    # stays meet at 5, and grouping by contact would fuse them into one -- which is
    # not wrong about *this* schedule but is wrong about what has to share a bay,
    # and over-constrains the colouring.
    instance = _instance(2, [])
    items = [
        Occupancy("bench.a", 0, 5, "activity", 0),
        Occupancy("bench.a", 5, 9, "activity", 1),
    ]
    chains = build_chains(instance, items)
    assert sorted((c.start, c.end) for c in chains) == [(0, 5), (5, 9)]


def test_a_stay_with_a_gap_is_refused():
    # The colouring is only right for intervals, so a stay that is not one is a
    # defect to report rather than a bay to hand out.
    instance = _instance(1, [(0, 0)])
    items = [
        Occupancy("bench.a", 0, 2, "activity", 0),
        Occupancy("bench.a", 7, 9, "arc_from", 0),
    ]
    with pytest.raises(NotOneStay):
        build_chains(instance, items)


def test_a_hold_whose_activity_holds_nothing_stands_alone():
    # A cancelled activity holds nothing (§6.2), so the move's hold has no
    # occupancy to join -- which is what it does in the non-overlap too.
    instance = _instance(2, [(0, 1)])
    items = [Occupancy("bench.a", 4, 6, "arc_from", 0)]
    (chain,) = build_chains(instance, items)
    assert (chain.start, chain.end) == (4, 6)


# --- colouring ------------------------------------------------------------


def _chains(instance, items):
    return build_chains(instance, items)


def test_stays_that_do_not_overlap_share_one_bay():
    instance = _instance(2, [])
    chains = _chains(
        instance,
        [
            Occupancy("bench.a", 0, 5, "activity", 0),
            Occupancy("bench.a", 5, 9, "activity", 1),
        ],
    )
    given = assign(chains, BAYS)
    assert len(set(given.values())) == 1
    assert overlapping(chains, given) == []


def test_stays_that_overlap_get_different_bays():
    instance = _instance(2, [])
    chains = _chains(
        instance,
        [
            Occupancy("bench.a", 0, 5, "activity", 0),
            Occupancy("bench.a", 3, 9, "activity", 1),
        ],
    )
    given = assign(chains, BAYS)
    assert len(set(given.values())) == 2
    assert overlapping(chains, given) == []


def test_touching_at_an_endpoint_is_not_overlapping():
    # The same rule the non-overlap uses: a bay is free the instant it is left.
    instance = _instance(3, [])
    chains = _chains(
        instance,
        [
            Occupancy("bench.a", 0, 5, "activity", 0),
            Occupancy("bench.a", 5, 10, "activity", 1),
            Occupancy("bench.a", 10, 15, "activity", 2),
        ],
    )
    given = assign(chains, BAYS)
    assert len(set(given.values())) == 1


def test_more_overlapping_stays_than_bays_is_refused():
    # Unreachable once the capacity resource is in place, which is exactly why it
    # is a refusal and not a fallback: handing out a plan with two Objects in one
    # bay would be worse than failing.
    instance = _instance(3, [])
    chains = _chains(
        instance,
        [
            Occupancy("bench.a", 0, 9, "activity", 0),
            Occupancy("bench.a", 1, 9, "activity", 1),
            Occupancy("bench.a", 2, 9, "activity", 2),
        ],
    )
    with pytest.raises(NotColourable):
        assign(chains, BAYS)


def test_the_assignment_is_deterministic():
    # Two runs of the same schedule name the same bays: the plan a laboratory is
    # handed should not depend on dictionary order.
    instance = _instance(4, [])
    items = [
        Occupancy("bench.a", start, start + 4, "activity", i)
        for i, start in enumerate((0, 2, 8, 9))
    ]
    first = assign(_chains(instance, items), BAYS)
    second = assign(_chains(instance, items), BAYS)
    assert [first[k] for k in sorted(first)] == [second[k] for k in sorted(second)]


def test_overlapping_finds_a_bad_assignment():
    # The checker has to be able to fail, or it is not checking anything.
    instance = _instance(2, [])
    chains = _chains(
        instance,
        [
            Occupancy("bench.a", 0, 5, "activity", 0),
            Occupancy("bench.a", 3, 9, "activity", 1),
        ],
    )
    forced = {0: "bench.a", 1: "bench.a"}
    assert len(overlapping(chains, forced)) == 1


# --- reading the occupancies off a schedule -------------------------------


def test_occupancies_are_read_from_the_chosen_modes_and_routes():
    @dataclass(frozen=True)
    class Option:
        from_spot: str
        to_spot: str

    instance = _Instance(
        (
            _Act((_Mode({}, {"o": "bench.a"}),)),
            _Act((_Mode({"i": "bench.b"}, {}),)),
        ),
        (_ArcEnds(0, 1),),
    )
    items = occupancies(
        instance,
        frozenset(BAYS),
        lambda i: instance.activities[i].modes[0],
        lambda r: Option("bench.a", "bench.b"),
        {0: (0, 4), 1: (7, 9)},
        {0: (4, 7)},
    )
    assert sorted((i.spot, i.start, i.end, i.kind) for i in items) == [
        ("bench.a", 0, 4, "activity"),
        ("bench.a", 4, 7, "arc_from"),
        ("bench.b", 4, 7, "arc_to"),
        ("bench.b", 7, 9, "activity"),
    ]
