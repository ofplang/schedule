"""What the inventory constraint assumes of CP-SAT's reservoir.

FORMULATION §11 models a stock as a reservoir whose level changes are the amounts
consumed and added. The added amounts are **decision variables**, and that is the
delicate part: `AddReservoirConstraintWithActive` accepts a `LinearExpr` there by
its type hints, while its docstring still says the change "is constant".

So this relies on behaviour the API does not document. If a future OR-Tools read a
variable change as a constant -- or as zero -- the scheduler would not fail; it
would quietly plan refills that add nothing and hand out schedules that cannot run.
These four properties are what stand between that and a loud failure, so they are
pinned here rather than left to be noticed in a plan.
"""

from __future__ import annotations

from ortools.sat.python import cp_model


def _solve(model):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 1
    return solver, solver.Solve(model)


def test_a_variable_fill_is_bounded_by_the_capacity():
    # Nothing but the reservoir's max_level stops `amount` reaching 100.
    model = cp_model.CpModel()
    amount = model.NewIntVar(0, 100, "amount")
    model.AddReservoirConstraintWithActive(
        [model.NewConstant(5)], [amount], [model.NewConstant(1)], 0, 10
    )
    model.Maximize(amount)
    solver, status = _solve(model)
    assert status == cp_model.OPTIMAL
    assert solver.Value(amount) == 10


def test_a_variable_fill_must_cover_a_later_draw():
    # Minimising would take 0 if the lower bound did not bind.
    model = cp_model.CpModel()
    when = model.NewIntVar(0, 100, "when")
    amount = model.NewIntVar(0, 10, "amount")
    model.AddReservoirConstraintWithActive(
        [when, model.NewConstant(50)],
        [amount, -7],
        [model.NewBoolVar("on"), model.NewConstant(1)],
        0,
        10,
    )
    model.Minimize(amount)
    solver, status = _solve(model)
    assert status == cp_model.OPTIMAL
    assert solver.Value(amount) == 7
    assert solver.Value(when) <= 50


def test_variable_fills_accumulate_against_the_capacity():
    # Capacity 10, draws of 6 at t=10 and t=30. The first fill is capped at 10; by
    # t=20 the level is a1-6, so the second may add at most 16-a1. Total 16 -- which
    # only holds if both variable changes are actually accumulated.
    model = cp_model.CpModel()
    a1 = model.NewIntVar(0, 100, "a1")
    a2 = model.NewIntVar(0, 100, "a2")
    times = [model.NewConstant(t) for t in (0, 10, 20, 30)]
    model.AddReservoirConstraintWithActive(
        times, [a1, -6, a2, -6], [model.NewConstant(1)] * 4, 0, 10
    )
    model.Maximize(a1 + a2)
    solver, status = _solve(model)
    assert status == cp_model.OPTIMAL
    assert solver.Value(a1) + solver.Value(a2) == 16


def test_a_draw_no_fill_can_cover_is_infeasible():
    # The failure that matters: a stock that cannot be made to reach 20 must be
    # reported as infeasible, not planned around.
    model = cp_model.CpModel()
    amount = model.NewIntVar(0, 100, "amount")
    model.AddReservoirConstraintWithActive(
        [model.NewConstant(0), model.NewConstant(10)],
        [amount, -20],
        [model.NewConstant(1)] * 2,
        0,
        10,
    )
    _, status = _solve(model)
    assert status == cp_model.INFEASIBLE


def test_changes_at_one_time_point_are_read_together():
    """The property the doubled time axis exists to work around.

    A reservoir checks its bounds *between* time points, so two changes at the same
    time point are read as one: their sum is what has to fit. Here a +2 and a -2 at
    t=5 leave a full stock at its capacity, and the level of 4 that the addition
    alone would reach is never checked.

    That is not what §4.7 says a schedule may do -- the level a refill leaves behind
    is what the device is holding when it finishes, and it has to fit. So the
    scheduler does not hand the reservoir a shared time point; see the next test.
    """
    model = cp_model.CpModel()
    amount = model.NewIntVar(0, 2, "amount")
    model.AddReservoirConstraintWithActive(
        [model.NewConstant(0), model.NewConstant(5), model.NewConstant(5)],
        [2, amount, -2],
        [model.NewConstant(1)] * 3,
        0,
        2,
    )
    model.Maximize(amount)
    solver, status = _solve(model)
    assert status == cp_model.OPTIMAL
    assert solver.Value(amount) == 2      # 2 + 2 - 2 = 2: the intermediate 4 is unseen


def test_a_doubled_axis_separates_a_completion_from_a_start():
    """Mapping a completion to `2t` and a start to `2t + 1` restores the check.

    `_add_resources` relies on this: with the two changes at distinct time points
    the reservoir checks the level between them, so a refill that would take a full
    stock past its capacity cannot be placed there -- which is what §4.7 asks for.

    The time expressions are affine over the same start/end variables, so this adds
    no variable, and the two images are disjoint (even / odd), so no order relation
    between a completion and a start is newly distinguished.
    """
    model = cp_model.CpModel()
    amount = model.NewIntVar(0, 2, "amount")
    end = model.NewIntVar(0, 100, "end")
    model.Add(end == 5)
    draw_at = model.NewConstant(5)
    model.AddReservoirConstraintWithActive(
        [model.NewConstant(-1), 2 * end, 2 * draw_at + 1],
        [2, amount, -2],
        [model.NewConstant(1)] * 3,
        0,
        2,
    )
    model.Maximize(amount)
    solver, status = _solve(model)
    assert status == cp_model.OPTIMAL
    assert solver.Value(amount) == 0      # the stock is full when the refill lands


def test_a_doubled_axis_still_lets_a_refill_feed_a_draw_at_that_instant():
    """The separation must not break the case §4.7 exists to allow: a refill ending
    exactly when the work it feeds begins does feed it. Empty stock, capacity 2, a
    refill completing at t=5 and a draw of 2 starting at t=5."""
    model = cp_model.CpModel()
    amount = model.NewIntVar(0, 2, "amount")
    end = model.NewConstant(5)
    model.AddReservoirConstraintWithActive(
        [model.NewConstant(-1), 2 * end, 2 * end + 1],
        [0, amount, -2],
        [model.NewConstant(1)] * 3,
        0,
        2,
    )
    solver, status = _solve(model)
    assert status == cp_model.OPTIMAL
    assert solver.Value(amount) == 2
