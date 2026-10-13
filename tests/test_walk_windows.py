"""The mobility walk keeps undecided regions' reservations (design.md D68, stage 1).

A first plan with an undecided branch or loop keeps the region's spots from its opening
to its exit. The walk, which orders the work with time erased, keeps them too when it
is handed the regions' windows: while a window is open nothing that is not the region's
own is put on or moved onto its spots, a window does not open over something that is
not its own, and two windows on one spot, neither inside the other, are not open at
once. The order it finds then lays out into a schedule the read-back accepts.
"""

from __future__ import annotations

import copy

from ofplang.schedule import schedule
from ofplang.schedule.scheduler import api, greedy, mobility
from ofplang.schedule.scheduler.plancheck import check_schedule
from ofplang.schedule.scheduler.status import PreviousPlan, PreviousStep
from tests.test_reservations import _cups


def _instance(n: int = 3):
    """The instance the solver is handed for `inspect_then_wash` with `n` cups."""
    workflow, env, document = _cups(n)
    seen = {}
    real = api._solver()

    def capture(instance, **kwargs):
        seen["instance"] = instance
        return real(instance, **kwargs)

    keep = api._solver
    api._solver = lambda: capture
    try:
        schedule(copy.deepcopy(workflow), copy.deepcopy(env), document_path=copy.deepcopy(document),
                 random_seed=0, check_arms=False)
    finally:
        api._solver = keep
    return seen["instance"]


def _laid_out(instance, order):
    """`order` laid out as a schedule that keeps the windows, or None."""
    implicit = {i: c for kind, i, c in order
                if kind == "place" and instance.activities[i].boundary is not None}
    steps = tuple(
        PreviousStep(kind, i, c, float(n), float(n)) for n, (kind, i, c) in enumerate(order)
        if not (kind == "place" and instance.activities[i].boundary is not None)
    )
    return greedy._replay_previous(
        instance, PreviousPlan(steps, implicit), {}, greedy._history(instance, None, 0), 0, None
    )


def test_an_order_that_keeps_the_windows_lays_out_into_a_schedule():
    instance = _instance(3)
    assert instance.reservations
    windows, pins = mobility.windows_of(instance)
    order = mobility.find_order(instance, windows, pins)
    assert order is not None
    solution = _laid_out(instance, order)
    assert solution is not None
    assert check_schedule(instance, solution, None) == []


def test_without_the_windows_the_walk_opens_one_cups_window_over_another():
    # Every cup's branch keeps the sink; the plain walk decides the second cup while the
    # first is still being washed, and that order is no schedule that keeps both.
    instance = _instance(3)
    order = mobility.find_order(instance)
    assert order is not None
    assert _laid_out(instance, order) is None


def test_the_assumed_outcome_runs_on_the_first_candidate():
    instance = _instance(3)
    windows, pins = mobility.windows_of(instance)
    assert pins
    order = mobility.find_order(instance, windows, pins)
    placed = {i: c for kind, i, c in order if kind == "place"}
    assert all(placed[i] == m for i, m in pins.items())


def test_no_windows_is_the_walk_it_always_was():
    instance = _instance(3)
    plain = mobility.find_order(instance)
    assert mobility.find_order(instance, (), None) == plain
