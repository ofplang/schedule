"""Objects that cannot all reach the end, and Objects that can.

The check this exercises refuses an instance outright, so the test that matters
most is not that it fires -- it is that it **does not** fire on anything a
schedule exists for. Every fixture here is therefore held against the solver as
well: where the walk refuses, CP-SAT is asked to prove `infeasible`, and where
the walk is silent, CP-SAT is asked to produce a schedule. The value of the check
is entirely that it is the *same* answer reached without the search, so the two
have to be seen agreeing rather than asserted to agree.
"""

from __future__ import annotations

import pytest

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.scheduler import mobility
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    ArcInstance,
    BoundaryInfo,
    Instance,
    TransportOption,
)
from ofplang.schedule.scheduler.model import Arc, Endpoint, Environment, Mode

_ENV = Environment("second", {}, (), {}, {})


def _codes(instance: Instance) -> list[str]:
    diags = Diagnostics()
    mobility.report_deadlocked_objects(instance, diags)
    return [d.code for d in diags.items]


# ---------------------------------------------------------------------------
# The ping-pong trap: the RNA-seq minimal laboratory, shrunk.
#
# One chain per Object, alternating between two bays, entering from a bay of its
# own and coming to rest wherever it can reach. The two bays are all there is, so
# an Object that has started can never leave them -- which is what makes the
# laboratory a trap rather than merely a narrow one.
# ---------------------------------------------------------------------------


def _pingpong(objects: int, steps: int) -> Instance:
    bays = ("lab.a", "lab.b")
    activities: list[ActivityInstance] = []
    arcs: list[ArcInstance] = []
    for job in range(objects):
        first = len(activities)
        # The entry: material waiting in a place of its own, as a document's
        # `interface.inputs` puts it.
        entry = f"gate.s{job}"
        activities.append(
            ActivityInstance(
                (),
                "",
                (Mode("in", (), 0, {}, {"o": entry}),),
                boundary=BoundaryInfo(kind="input"),
            )
        )
        for step in range(steps):
            spot = bays[step % 2]
            activities.append(
                ActivityInstance(
                    (f"j{job}s{step}",),
                    "work",
                    (Mode("m", (spot.split(".")[0],), 1, {"i": spot}, {"o": spot}),),
                )
            )
        # The finished product rests in whichever bay it can be got to.
        activities.append(
            ActivityInstance(
                (),
                "",
                tuple(Mode(bay, (), 0, {"i": bay}, {}) for bay in bays),
                boundary=BoundaryInfo(kind="output"),
            )
        )
        # Entry into the first bay, then bay to bay, then out to the resting place.
        arcs.append(
            ArcInstance(
                Arc(Endpoint((), "in"), Endpoint((f"j{job}s0",), "i")),
                first,
                first + 1,
                (TransportOption(0, 0, "arm", entry, bays[0], 1),),
            )
        )
        for step in range(steps - 1):
            arcs.append(
                ArcInstance(
                    Arc(Endpoint((f"j{job}s{step}",), "o"), Endpoint((f"j{job}s{step + 1}",), "i")),
                    first + 1 + step,
                    first + 2 + step,
                    (
                        TransportOption(
                            0, 0, "arm", bays[step % 2], bays[(step + 1) % 2], 1
                        ),
                    ),
                )
            )
        last = first + steps
        arcs.append(
            ArcInstance(
                Arc(Endpoint((f"j{job}s{steps - 1}",), "o"), Endpoint((), "out")),
                last,
                last + 1,
                tuple(
                    TransportOption(0, k, "arm", bays[(steps - 1) % 2], bay, 1)
                    for k, bay in enumerate(bays)
                ),
            )
        )
    return Instance(_ENV, "second", tuple(activities), tuple(arcs), ())


def test_one_object_can_ping_pong_all_it_likes():
    # The control, and it is the point of having one: a check that refused
    # everything would prove nothing by refusing the two-Object case.
    instance = _pingpong(objects=1, steps=7)
    assert _codes(instance) == []
    assert solve(instance, max_time_seconds=20).outcome == "optimal"


@pytest.mark.parametrize("steps", [3, 5, 7])
def test_two_objects_in_two_bays_cannot_both_finish(steps):
    # Whichever enters second finds the other standing in the bay it needs, and
    # the occupant cannot move either, because the bay it would move into is the
    # one the newcomer is in. There is no swap in this model.
    instance = _pingpong(objects=2, steps=steps)
    assert _codes(instance) == ["objects_deadlocked"]
    assert solve(instance, max_time_seconds=30).outcome == "infeasible"


def test_the_message_names_the_bays_that_were_full():
    diags = Diagnostics()
    mobility.report_deadlocked_objects(_pingpong(objects=2, steps=5), diags)
    message = diags.items[0].message
    assert "lab.a" in message and "lab.b" in message


# ---------------------------------------------------------------------------
# The ring: the benchmark's plate-batch grid, shrunk.
#
# Stage spots in a cycle, one place each, with every Object entering from and
# returning to a loader spot of its own. The forward pass of the greedy stands
# still on this shape (report section 46.4) -- and it is schedulable, which is
# exactly why the check has to stay silent on it.
# ---------------------------------------------------------------------------


def _ring(objects: int, stages: int, laps: int) -> Instance:
    stage_spots = [f"st{k}.core" for k in range(stages)]
    activities: list[ActivityInstance] = []
    arcs: list[ArcInstance] = []
    for job in range(objects):
        home = f"loader.s{job}"
        first = len(activities)
        activities.append(
            ActivityInstance(
                (), "", (Mode("in", (), 0, {}, {"o": home}),), boundary=BoundaryInfo(kind="input")
            )
        )
        route = [stage_spots[k % stages] for k in range(stages * laps)]
        for step, spot in enumerate(route):
            activities.append(
                ActivityInstance(
                    (f"j{job}s{step}",),
                    "stage",
                    (Mode("m", (spot.split(".")[0],), 1, {"i": spot}, {"o": spot}),),
                )
            )
        # Home again, and the loader spot is the Object's own, so parking there
        # takes nothing from anybody.
        activities.append(
            ActivityInstance(
                (),
                "",
                (Mode("home", (), 0, {"i": home}, {}),),
                boundary=BoundaryInfo(kind="output"),
            )
        )
        hops = [home, *route, home]
        for step in range(len(hops) - 1):
            src = first + step
            arcs.append(
                ArcInstance(
                    Arc(Endpoint((f"j{job}s{step}",), "o"), Endpoint((f"j{job}s{step + 1}",), "i")),
                    src,
                    src + 1,
                    (TransportOption(0, 0, "arm", hops[step], hops[step + 1], 1),),
                )
            )
    return Instance(_ENV, "second", tuple(activities), tuple(arcs), ())


@pytest.mark.parametrize("objects,stages,laps", [(2, 3, 2), (3, 3, 2), (4, 3, 1)])
def test_a_ring_with_more_objects_than_places_is_not_refused(objects, stages, laps):
    # More Objects than the ring has room for is not a deadlock: the ones that do
    # not fit wait at home, which is a place of their own. Refusing here would be
    # refusing a plan the laboratory can run, which is the failure that matters.
    instance = _ring(objects, stages, laps)
    assert _codes(instance) == []
    assert solve(instance, max_time_seconds=30).outcome in ("optimal", "feasible")


# ---------------------------------------------------------------------------
# Material that never lets go of its spot.
# ---------------------------------------------------------------------------


def _one_bay_with_leftovers() -> Instance:
    """One bay, one job that needs it, and a `held` node saying something is
    already standing there for the rest of the run (SPEC §6.12)."""
    return Instance(
        _ENV,
        "second",
        (
            ActivityInstance(
                (),
                "",
                (Mode("held", (), 0, {"i": "lab.a"}, {}),),
                boundary=BoundaryInfo("held", since=0),
            ),
            ActivityInstance(
                (), "", (Mode("in", (), 0, {}, {"o": "gate.s0"}),), boundary=BoundaryInfo("input")
            ),
            ActivityInstance(
                ("work",), "work", (Mode("m", ("lab",), 1, {"i": "lab.a"}, {"o": "lab.a"}),)
            ),
        ),
        (
            ArcInstance(
                Arc(Endpoint((), "in"), Endpoint(("work",), "i")),
                1,
                2,
                (TransportOption(0, 0, "arm", "gate.s0", "lab.a", 1),),
            ),
        ),
        (),
    )


def test_leftovers_against_ordinary_work_is_a_gap_the_walk_leaves_open():
    """🔴 A limitation, recorded rather than papered over.

    The instance is infeasible and the solver says so: one bay, work that needs
    it, and a `held` node saying something is already standing there for the rest
    of the run (§6.12). The walk stays quiet anyway, because a held occupancy is
    pinned in *time* -- `since` or `now`, whichever is later, through to the
    horizon -- and the walk has no clock, so it is free to place the leftover
    material last and find the bay empty.

    Pinning it from the beginning instead would be **stricter than the model** in
    two ways: a `since` later than `now` leaves the bay usable before it, and a
    fixed activity may have used the bay in the past. Stricter is the one
    direction this must never be, so the gap stays. Silence claims nothing, which
    is what makes leaving it safe.
    """
    instance = _one_bay_with_leftovers()
    assert _codes(instance) == []
    assert solve(instance, max_time_seconds=20).outcome == "infeasible"


def _leftovers_on_the_only_resting_place() -> Instance:
    """The shape a withdrawal produces: material left behind on one bay, and a job
    whose finished product can only come to rest on that same bay."""
    return Instance(
        _ENV,
        "second",
        (
            ActivityInstance(
                (),
                "",
                (Mode("held", (), 0, {"i": "rack.a"}, {}),),
                boundary=BoundaryInfo("held", since=0),
            ),
            ActivityInstance(
                (), "", (Mode("in", (), 0, {}, {"o": "gate.s0"}),), boundary=BoundaryInfo("input")
            ),
            ActivityInstance(
                ("work",), "work", (Mode("m", ("lab",), 1, {"i": "lab.a"}, {"o": "lab.a"}),)
            ),
            ActivityInstance(
                (), "", (Mode("rack", (), 0, {"i": "rack.a"}, {}),), boundary=BoundaryInfo("output")
            ),
        ),
        (
            ArcInstance(
                Arc(Endpoint((), "in"), Endpoint(("work",), "i")),
                1,
                2,
                (TransportOption(0, 0, "arm", "gate.s0", "lab.a", 1),),
            ),
            ArcInstance(
                Arc(Endpoint(("work",), "o"), Endpoint((), "out")),
                2,
                3,
                (TransportOption(0, 0, "arm", "lab.a", "rack.a", 1),),
            ),
        ),
        (),
    )


def test_two_occupancies_that_both_outlast_the_run_cannot_share_a_spot():
    # 🔴 This is the one `final_outputs_crowded` cannot see: it counts finished
    # products against each other, and the thing in the way here is not a product
    # but material a departed job left behind (§6.12). Both hold to the end, so
    # no order of the work saves it -- which is why the walk catches it while the
    # leftovers-against-work case above escapes.
    instance = _leftovers_on_the_only_resting_place()
    assert _codes(instance) == ["objects_deadlocked"]
    assert solve(instance, max_time_seconds=20).outcome == "infeasible"


# ---------------------------------------------------------------------------
# What silence has to mean.
# ---------------------------------------------------------------------------


def test_a_walk_that_is_stopped_early_claims_nothing(monkeypatch):
    # The cap exists so an instance too large to settle ends in silence rather
    # than in a long wait. Silence must therefore never be read as a refusal --
    # and the way to be sure is to cap a walk that would otherwise refuse.
    instance = _pingpong(objects=2, steps=7)
    assert _codes(instance) == ["objects_deadlocked"]
    monkeypatch.setattr(mobility, "_EXPANSIONS", 5)
    assert _codes(instance) == []


def test_an_unreachable_arc_is_left_to_the_check_that_names_it():
    # An arc no route can serve makes the walk impossible too, but saying
    # `objects_deadlocked` about it would be the vaguer of two true answers, and
    # `report_unreachable` has already given the precise one.
    instance = _pingpong(objects=2, steps=5)
    stripped = Instance(
        instance.env,
        instance.time_unit,
        instance.activities,
        (
            ArcInstance(
                instance.arcs[0].arc,
                instance.arcs[0].src_activity,
                instance.arcs[0].dst_activity,
                (),
            ),
            *instance.arcs[1:],
        ),
        instance.precedence,
    )
    assert _codes(stripped) == []


def test_an_instance_with_nothing_in_it_says_nothing():
    assert _codes(Instance(_ENV, "second", (), (), ())) == []
