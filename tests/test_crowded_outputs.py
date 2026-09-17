"""Finished products, and whether there is anywhere to put them.

A boundary `output` node holds its spots until the makespan (FORMULATION §J5), so
no two finished products can rest in the same place. More products than places is
therefore unschedulable by counting, and these tests hold two things at once: that
the diagnostic says so, and that the solver agrees when asked -- because the value
of the check is entirely that it is the *same* answer, arrived at without the
twelve minutes the solver spends not arriving at it.
"""

from __future__ import annotations

import pytest

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    ArcInstance,
    BoundaryInfo,
    Instance,
    TransportOption,
    report_crowded_outputs,
)
from ofplang.schedule.scheduler.model import Arc, Endpoint, Environment, Mode

_ENV = Environment("second", {}, (), {}, {})


def _built(jobs: int, places: list[list[str]], make_duration: int = 1) -> Instance:
    """`jobs` plates, each made on its own machine and then a final output resting
    in one of the spots `places[job]` offers."""
    activities: list[ActivityInstance] = []
    arcs: list[ArcInstance] = []
    for job in range(jobs):
        activities.append(
            ActivityInstance(
                (f"make{job}",),
                "make",
                (Mode("m", (f"mk{job}",), make_duration, {}, {"o": f"mk{job}.p"}),),
            )
        )
        activities.append(
            ActivityInstance(
                (),
                "",
                tuple(Mode(spot, (), 0, {"i": spot}, {}) for spot in places[job]),
                boundary=BoundaryInfo(kind="output"),
            )
        )
        arcs.append(
            ArcInstance(
                Arc(Endpoint((f"make{job}",), "o"), Endpoint((), "out")),
                2 * job,
                2 * job + 1,
                tuple(
                    TransportOption(0, k, "arm0", f"mk{job}.p", spot, 1)
                    for k, spot in enumerate(places[job])
                ),
            )
        )
    return Instance(_ENV, "second", tuple(activities), tuple(arcs), ())


def _codes(instance: Instance) -> list[str]:
    diags = Diagnostics()
    report_crowded_outputs(instance, diags)
    return [d.code for d in diags.items]


def _shelf(jobs: int, bays: int) -> Instance:
    return _built(jobs, [[f"shelf.bay{k}" for k in range(bays)] for _ in range(jobs)])


@pytest.mark.parametrize("jobs,bays", [(2, 1), (3, 2), (4, 2), (5, 2)])
def test_more_products_than_places_is_reported(jobs, bays):
    assert _codes(_shelf(jobs, bays)) == ["final_outputs_crowded"]


@pytest.mark.parametrize("jobs,bays", [(1, 1), (2, 2), (3, 3), (2, 5)])
def test_enough_places_says_nothing(jobs, bays):
    assert _codes(_shelf(jobs, bays)) == []


@pytest.mark.parametrize("jobs,bays", [(2, 1), (3, 2)])
def test_the_solver_agrees_that_it_cannot_be_done(jobs, bays):
    # The whole value of the check is that it is the solver's own answer, reached
    # by counting instead of by searching.
    assert solve(_shelf(jobs, bays), max_time_seconds=20).outcome == "infeasible"


@pytest.mark.parametrize("jobs,bays", [(2, 2), (3, 3)])
def test_the_solver_agrees_that_it_can(jobs, bays):
    assert solve(_shelf(jobs, bays), max_time_seconds=20).outcome == "optimal"


def test_a_product_that_takes_no_time_to_make_still_needs_a_place():
    # The one theoretical escape would be a residency of zero length, which cannot
    # happen: the delivery holds the bay from when it sets off until the product
    # starts resting, and that is what overlaps.
    crowded = _built(2, [["shelf.bay0"], ["shelf.bay0"]], make_duration=0)
    assert _codes(crowded) == ["final_outputs_crowded"]
    assert solve(crowded, max_time_seconds=20).outcome == "infeasible"


def test_places_are_matched_rather_than_counted():
    # Three products and three places, so counting them says nothing is wrong --
    # but two of the three products can only go in the same single place, and the
    # third cannot take either of the other two. Nothing fits, and only trying the
    # combinations finds that out.
    crowded = _built(
        3,
        [["shelf.bay0"], ["shelf.bay0"], ["shelf.bay1", "shelf.bay2"]],
    )
    assert _codes(crowded) == ["final_outputs_crowded"]
    assert solve(crowded, max_time_seconds=20).outcome == "infeasible"


def test_one_product_is_never_crowded():
    # There is nothing to collide with, whatever the laboratory looks like.
    assert _codes(_built(1, [["shelf.bay0"]])) == []


def test_an_output_naming_no_spot_is_not_counted():
    # An output the document does not place holds nothing, so it is not competing
    # for anywhere (`metrics_rnaseq.md`, and §Activities).
    placeless = _built(2, [["shelf.bay0"], ["shelf.bay0"]])
    activities = list(placeless.activities)
    activities[3] = ActivityInstance(
        (), "", (Mode("none", (), 0, {}, {}),), boundary=BoundaryInfo(kind="output")
    )
    assert _codes(Instance(_ENV, "second", tuple(activities), placeless.arcs, ())) == []


def test_a_mode_that_places_two_products_at_once_takes_both_spots():
    # One job's output node binds every Object-bearing output it has, so choosing
    # the mode takes all of them -- which is why this searches over modes rather
    # than matching products to spots one for one.
    activities = [
        ActivityInstance(("make",), "make", (Mode("m", ("mk",), 1, {}, {"o": "mk.p"}),)),
        ActivityInstance(
            (),
            "",
            (Mode("both", (), 0, {"i": "shelf.bay0", "j": "shelf.bay1"}, {}),),
            boundary=BoundaryInfo(kind="output"),
        ),
        ActivityInstance(("make2",), "make", (Mode("m", ("mk2",), 1, {}, {"o": "mk2.p"}),)),
        ActivityInstance(
            (),
            "",
            (
                Mode("b0", (), 0, {"i": "shelf.bay0"}, {}),
                Mode("b1", (), 0, {"i": "shelf.bay1"}, {}),
            ),
            boundary=BoundaryInfo(kind="output"),
        ),
    ]
    arcs = (
        ArcInstance(
            Arc(Endpoint(("make",), "o"), Endpoint((), "out")),
            0,
            1,
            (TransportOption(0, 0, "arm0", "mk.p", "shelf.bay0", 1),),
        ),
        ArcInstance(
            Arc(Endpoint(("make2",), "o"), Endpoint((), "out")),
            2,
            3,
            (TransportOption(0, 0, "arm0", "mk2.p", "shelf.bay0", 1),),
        ),
    )
    assert _codes(Instance(_ENV, "second", tuple(activities), arcs, ())) == [
        "final_outputs_crowded"
    ]
