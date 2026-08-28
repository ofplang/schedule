"""How a planned refill's amount is settled (§4.7.1, FORMULATION §11).

The amounts are not read off the solver. The reservoir has no level *variable* to
write "fills to capacity" against, so the model leaves each amount free and
`_refill_results` settles it afterwards by replaying the stock's events. That makes
the plan a **second computation over the same answer**, and the two have to agree
about one thing in particular: what happens when a refill lands at the very instant
a draw starts.

The refill goes in **before** the draw (§4.7); what has to agree is *where the level
is read*. §4.7 reads it after the whole instant, `plancheck` replays a finished plan
that way deliberately (see its module docstring), and the reservoir is written
against the same point. So the settling here must fill to the level the instant
leaves behind. Filling to capacity first and subtracting the simultaneous draw after
instead finds no room in a stock that is momentarily full, drops the refill the
solver chose, and produces a plan whose stock later goes negative. That is not a
hypothetical: it made every instance needing two or more refills unplannable.

These tests drive the settling directly, because the situation is easy to state and
expensive to provoke through a solve.
"""

from __future__ import annotations

from ofplang.schedule.scheduler.cpsat import Mode, ProcessingResult, _refill_results, _RefillVars
from ofplang.schedule.scheduler.instance import Instance, RefillCandidate, RefillOption
from ofplang.schedule.scheduler.model import Device, Environment
from ofplang.schedule.scheduler.status import Fixation

DEVICE, RESOURCE = "reader", "reagent"
QUALIFIED = f"{DEVICE}.{RESOURCE}"


class _Solved:
    """A solver whose variables are their own values.

    `_refill_results` only ever calls `Value`, so putting plain integers where the
    IntVars would be is enough, and it keeps each case readable as the arithmetic
    it is about.
    """

    @staticmethod
    def Value(x):          # noqa: N802 - mirrors CpSolver's method name
        return x


def _instance(capacity: int, n_candidates: int) -> Instance:
    env = Environment(
        time_unit="second",
        devices={DEVICE: Device(id=DEVICE, spots=frozenset({"stage"}),
                                resources={RESOURCE: capacity})},
        transporters=(),
        transports={},
        processes={},
        replenishers=("dispenser",),
        replenishments={("dispenser", DEVICE): 4},
    )
    candidates = tuple(
        RefillCandidate(f"replenishment_{i}", DEVICE, i,
                        (RefillOption("dispenser", 4),), (RESOURCE,))
        for i in range(n_candidates)
    )
    return Instance(env=env, time_unit="second", activities=(), arcs=(),
                    precedence=(), replenishments=candidates)


def _draw(start: int, amount: int, index: int = 0) -> ProcessingResult:
    mode = Mode(id="m", devices=(DEVICE,), duration=1, input_spots={},
                output_spots={}, consumption={QUALIFIED: amount})
    return ProcessingResult(activity=index, node=(f"Assay{index}",), process="assay",
                            mode=mode, start=start, end=start + 1)


def _refills(*ends: int) -> dict[str, _RefillVars]:
    """One selected candidate per end time, each free to add up to the capacity."""
    return {
        f"replenishment_{i}": _RefillVars(present=1, start=end - 4, end=end,
                                          amounts={RESOURCE: 0})
        for i, end in enumerate(ends)
    }


def _settle(capacity, start_level, draws, refill_ends):
    results = _refill_results(
        _Solved(),
        _instance(capacity, len(refill_ends)),
        Fixation(now=0, activities={}, arcs={},
                 levels={(DEVICE, RESOURCE): start_level}),
        _refills(*refill_ends),
        [_draw(t, a, i) for i, (t, a) in enumerate(draws)],
    )
    return {r.id: r.amounts.get(RESOURCE, 0) for r in results}


def _replay(capacity, start_level, draws, settled, refill_ends):
    """The level after every instant, read the way the reservoir reads it."""
    events: dict[int, int] = {}
    for t, amount in draws:
        events[t] = events.get(t, 0) - amount
    for i, end in enumerate(refill_ends):
        events[end] = events.get(end, 0) + settled.get(f"replenishment_{i}", 0)
    level, trace = start_level, []
    for t in sorted(events):
        level += events[t]
        trace.append((t, level))
        assert 0 <= level <= capacity, f"level {level} at t={t} outside [0,{capacity}]"
    return trace


def test_a_refill_landing_on_a_full_stock_at_a_draws_instant_still_fills():
    # capacity 3, starts full, nothing drawn before t=40. The refill ends at 40,
    # exactly when a draw starts, so at that instant the stock is still full.
    # Filling "to capacity" before subtracting that draw finds no room, drops the
    # refill -- and the later draw at 100 then has nothing to run on. Reading the
    # instant as a whole, the refill is what keeps the stock at 3 across it.
    draws, ends = [(40, 3), (100, 3)], [40]
    settled = _settle(3, 3, draws, ends)
    assert settled == {"replenishment_0": 3}
    assert _replay(3, 3, draws, settled, ends) == [(40, 3), (100, 0)]


def test_a_refill_never_adds_more_than_the_capacity():
    # The model bounds each amount by the capacity, so the settled figure must not
    # exceed it however large the simultaneous draw is -- otherwise the plan would
    # report a fill the solver never proved.
    settled = _settle(3, 0, [(20, 3)], [20])
    assert settled == {"replenishment_0": 3}


def test_a_refill_that_genuinely_adds_nothing_is_dropped():
    # Full, and nothing drawn at the instant it lands: there is no room, and a
    # refill holding two machines to change no level is not worth reporting.
    assert _settle(3, 3, [(50, 3)], [20]) == {}


def test_consecutive_refills_each_take_only_the_room_they_have():
    # Two refills, the second landing when the stock is already part-filled.
    settled = _settle(6, 0, [(10, 4), (30, 4)], [5, 25])
    assert settled == {"replenishment_0": 6, "replenishment_1": 4}
    assert _replay(6, 0, [(10, 4), (30, 4)], settled, [5, 25]) == [
        (5, 6), (10, 2), (25, 6), (30, 2),
    ]


def test_three_refills_for_four_draws_all_survive():
    # The shape that was unplannable: every refill is needed, and the last two land
    # at the instant of a draw. Nothing may be rounded away.
    draws = [(3, 3), (27, 3), (78, 3), (156, 3)]
    ends = [27, 45, 78]
    settled = _settle(3, 3, draws, ends)
    assert settled == {"replenishment_0": 3, "replenishment_1": 3, "replenishment_2": 3}
    assert _replay(3, 3, draws, settled, ends) == [(3, 0), (27, 0), (45, 3), (78, 3), (156, 0)]
