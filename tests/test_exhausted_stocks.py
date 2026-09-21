"""A stock nothing can refill, with more still to draw than it holds.

Monotone: such a level only falls, so "it never goes below zero" is exactly
"what is left covers what is still to be drawn" -- one comparison, settled
before any solve. These tests hold two things at once, as `test_crowded_outputs`
does: that the diagnostic says so, and that the solver agrees when asked --
because the value of the check is entirely that it is the *same* answer,
reached by counting instead of by searching.
"""

from __future__ import annotations

import pytest

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    Instance,
    RefillCandidate,
    RefillOption,
    report_exhausted_stocks,
)
from ofplang.schedule.scheduler.model import Device, Environment, Mode
from ofplang.schedule.scheduler.status import Fixation


def _lab(capacity: int, refillable: bool) -> Environment:
    devices = {
        "lab": Device("lab", frozenset({"bay0", "bay1", "bay2", "bay3"}), {"tips": capacity}),
        "hand": Device("hand", frozenset()),
    }
    return Environment("second", devices, (), {}, {})


def _drawing(draws: int, amount: int, capacity: int = 20, refills: int = 0) -> Instance:
    """`draws` activities, each taking `amount` from `lab.tips`."""
    activities = tuple(
        ActivityInstance(
            (f"take{k}",),
            "work",
            (Mode("m", ("lab",), 1, {}, {"o": f"lab.bay{k}"}, consumption={"lab.tips": amount}),),
        )
        for k in range(draws)
    )
    return Instance(
        _lab(capacity, bool(refills)),
        "second",
        activities,
        (),
        (),
        replenishments=tuple(
            RefillCandidate(f"r{k}", "lab", 0, (RefillOption("hand", 1),), ("tips",))
            for k in range(refills)
        ),
    )


def _stocked(levels) -> Fixation:
    return Fixation(now=0, activities={}, arcs={}, levels=levels)


def _codes(instance: Instance, levels) -> list[str]:
    diags = Diagnostics()
    report_exhausted_stocks(instance, _stocked(levels), diags)
    return [d.code for d in diags.items]


def _solved(instance: Instance, levels) -> str:
    """What the solver makes of it. ⚠ The levels have to reach it: with none
    stated it builds no reservoir at all and every stock is unbounded."""
    return solve(instance, fixation=_stocked(levels), max_time_seconds=20).outcome


@pytest.mark.parametrize("draws,amount,left", [(2, 3, 5), (4, 2, 7), (1, 9, 8)])
def test_more_to_draw_than_is_left_is_reported(draws, amount, left):
    instance = _drawing(draws, amount)
    assert _codes(instance, {("lab", "tips"): left}) == ["stock_cannot_last"]
    assert _solved(instance, {("lab", "tips"): left}) == "infeasible"


@pytest.mark.parametrize("draws,amount,left", [(2, 3, 6), (4, 2, 8), (1, 9, 9)])
def test_enough_to_go_round_says_nothing(draws, amount, left):
    instance = _drawing(draws, amount)
    assert _codes(instance, {("lab", "tips"): left}) == []
    assert _solved(instance, {("lab", "tips"): left}) in ("optimal", "feasible")


def test_a_stock_something_can_refill_is_left_alone():
    # 🔴 The comparison is only valid while the level falls. Where a refill can
    # reach the stock it may rise, and whether it lasts depends on when the
    # refills go -- which is a question about the schedule, not arithmetic.
    instance = _drawing(draws=4, amount=3, capacity=20, refills=1)
    assert _codes(instance, {("lab", "tips"): 1}) == []


def test_no_levels_means_no_reservoir_and_nothing_to_say():
    # A document that states no levels gets no reservoir at all (§4.7), so there
    # is nothing here to be short of.
    instance = _drawing(draws=4, amount=3)
    assert _codes(instance, {}) == []


def test_the_cheapest_mode_is_what_counts():
    # 🔴 An activity offering several modes may draw different amounts, and the
    # schedule picks. Counting anything but the smallest would refuse instances
    # that run perfectly well -- here 2 + 2 fits in 5 and 6 + 6 would not.
    activities = tuple(
        ActivityInstance(
            (f"take{k}",),
            "work",
            (
                Mode("thirsty", ("lab",), 1, {}, {"o": f"lab.bay{k}"}, consumption={"lab.tips": 6}),
                Mode("frugal", ("lab",), 1, {}, {"o": f"lab.bay{k}"}, consumption={"lab.tips": 2}),
            ),
        )
        for k in range(2)
    )
    instance = Instance(_lab(20, False), "second", activities, (), ())
    assert _codes(instance, {("lab", "tips"): 5}) == []
    assert _solved(instance, {("lab", "tips"): 5}) in ("optimal", "feasible")
    # And it still reports where even the cheapest way does not fit.
    assert _codes(instance, {("lab", "tips"): 3}) == ["stock_cannot_last"]


def test_what_already_ran_is_not_counted_again():
    # A fixed activity's draw is spent and already folded into the levels at
    # `now` (§4.7.2), so counting it again would refuse a run that is fine.
    from ofplang.schedule.scheduler.status import ActivityFixation

    instance = _drawing(draws=2, amount=3)
    diags = Diagnostics()
    report_exhausted_stocks(
        instance,
        Fixation(
            now=5,
            activities={0: ActivityFixation("completed", 0, 1, 0)},
            arcs={},
            levels={("lab", "tips"): 3},
        ),
        diags,
    )
    assert [d.code for d in diags.items] == []
