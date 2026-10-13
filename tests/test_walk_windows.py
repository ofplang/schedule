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
    order = mobility.find_order(instance, mobility.windows_of(instance))
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


def test_each_assumed_outcome_runs_on_one_of_its_candidates():
    # The read-back finds every region's candidate by the modes its assumed outcome
    # was placed in: there is one for each.
    from ofplang.schedule.scheduler.region import kept_reservations

    instance = _instance(3)
    order = mobility.find_order(instance, mobility.windows_of(instance))
    solution = _laid_out(instance, order)
    kept = kept_reservations(instance, solution, 0)
    assert kept is not None and len(kept) == len(instance.reservations)


def test_no_windows_is_the_walk_it_always_was():
    instance = _instance(3)
    plain = mobility.find_order(instance)
    assert mobility.find_order(instance, ()) == plain


def _jobs(workflow, env, roster):
    from ofplang.schedule import JobInput, schedule_jobs

    jobs = [JobInput(j["id"], copy.deepcopy(workflow)) for j in roster["jobs"]]
    return schedule_jobs(jobs, copy.deepcopy(env), document_path=copy.deepcopy(roster),
                         planner="greedy", check_arms=False)


def test_the_constructed_planner_plans_first_plans_that_keep_spots():
    # The windowed walk laid out is the constructed planner's first plan wherever an
    # undecided region keeps spots (design.md D68): a map of cups, two loops on the
    # only sink, two cups through a branch inside a branch.
    from tests.test_reservations import _CUPS_ENV, _WASHES_AGAIN, _two_rinses

    workflow, env, document = _cups(5)
    reports = [schedule(copy.deepcopy(workflow), copy.deepcopy(env),
                        document_path=copy.deepcopy(document), planner="greedy",
                        check_arms=False)]
    jobs, env, roster = _two_rinses()
    reports.append(_jobs(jobs[0].workflow, env, roster))
    cups = {"activities": [], "jobs": [
        {"id": j, "release": 0,
         "interface": {"inputs": {"cup": f"tray.{j}"}, "outputs": {"cup": f"rack.{j}"}}}
        for j in "ab"]}
    reports.append(_jobs(_WASHES_AGAIN, _CUPS_ENV, cups))
    for report in reports:
        assert report.ok, [d.code for d in report.diagnostics]
        codes = [d.code for d in report.diagnostics]
        assert "plan_constructed" in codes and "planner_unsupported" not in codes
        assert [a for a in report.plan["activities"] if a["kind"] == "reservation"]


def _rinse_with_two_readers():
    """The rinse loop, with a second reader beside the first."""
    from tests.test_loops import ENV

    env = copy.deepcopy(ENV)
    env["devices"].append({"id": "reader2", "spots": ["slot"]})
    env["transports"] += [
        {**t, "from": t["from"].replace("reader.slot", "reader2.slot"),
         "to": t["to"].replace("reader.slot", "reader2.slot")}
        for t in env["transports"] if "reader.slot" in (t["from"], t["to"])
    ]
    env["processes"]["read"]["modes"].append(
        {"devices": ["reader2"], "duration": 2, "input_spots": {"plate": "reader2.slot"},
         "output_spots": {"plate": "reader2.slot"}})
    return env


def test_a_loop_keeps_the_reader_it_chose_not_every_reader_it_could():
    # The first reader is held for the whole run. The loop keeps the spots of the modes
    # its invocation runs in -- the second reader's, here -- as the solver reads it;
    # keeping every reader it could use would leave it no window to open (design.md D68
    # §7). Planned by the constructed planner as short as by the solver.
    from tests.test_loops import DOCUMENT, RINSE

    document = {**copy.deepcopy(DOCUMENT), "occupied": [{"spot": "reader.slot", "since": 0}]}
    solved = schedule(copy.deepcopy(RINSE), _rinse_with_two_readers(),
                      document_path=copy.deepcopy(document), random_seed=0, check_arms=False)
    built = schedule(copy.deepcopy(RINSE), _rinse_with_two_readers(),
                     document_path=copy.deepcopy(document), planner="greedy", check_arms=False)
    assert solved.ok and built.ok, [d.code for d in built.diagnostics]
    assert built.makespan == solved.makespan
    reads = [a for a in built.plan["activities"]
             if a["kind"] == "processing" and a["node"][-1] == "Read"]
    assert reads and all(a["input_spots"]["plate"] == "reader2.slot" for a in reads)


def test_a_candidate_whose_spots_cannot_be_kept_is_passed_over_for_the_next():
    # The walk chooses among a branch's candidates as the solver does: one that keeps a
    # spot held for the whole run can never open, and the next one is taken; with that
    # one alone there is no way through at all.
    from dataclasses import replace as replaced

    workflow, env, document = _cups(3)
    document = {**document, "occupied": [{"spot": "tub.bath", "since": 0}]}
    env = copy.deepcopy(env)
    if not any(d["id"] == "tub" for d in env["devices"]):
        env["devices"].append({"id": "tub", "spots": ["bath"]})
    seen = {}
    real = api._solver()

    def capture(instance, **kwargs):
        seen["instance"] = instance
        return real(instance, **kwargs)

    keep = api._solver
    api._solver = lambda: capture
    try:
        schedule(copy.deepcopy(workflow), env, document_path=document, random_seed=0,
                 check_arms=False)
    finally:
        api._solver = keep
    instance = seen["instance"]
    windows = list(mobility.windows_of(instance))
    spots, pins = windows[0].candidates[0]
    blocked = (spots | {"tub.bath"}, pins)
    first_blocked = (*windows[:0], replaced(windows[0], candidates=(blocked, (spots, pins))),
                     *windows[1:])
    outcome, order = mobility.walk_with_windows(instance, tuple(first_blocked))
    assert outcome == "found" and order
    only_blocked = (replaced(windows[0], candidates=(blocked,)), *windows[1:])
    outcome, _order = mobility.walk_with_windows(instance, tuple(only_blocked))
    assert outcome == "exhausted"


def _two_sinks():
    """The cups' environment with a second sink beside the first, washing alike."""
    from tests.test_reservations import _CUPS_ENV

    env = copy.deepcopy(_CUPS_ENV)
    env["devices"].append({"id": "sink2", "spots": ["basin"]})
    places = ["tray.a", "scope.stage", "sink.basin", "sink2.basin", "buffer.pad", "rack.a"]
    env["transports"] = [{"transporter": "arm", "from": a, "to": b, "duration": 1}
                         for a in places for b in places if a != b]
    env["processes"]["wash"]["modes"].append(
        {"devices": ["sink2"], "duration": 5, "input_spots": {"cup": "sink2.basin"},
         "output_spots": {"cup": "sink2.basin"}})
    return env


def test_a_branch_whose_first_sink_is_held_is_planned_on_the_other():
    # The first sink is held for the whole run. The branch's candidate on the second
    # sink is the first one's image -- the outcome that polishes uses neither sink, and
    # a swap of two machines it never touches is no change to it -- so the solver and
    # the constructed planner both plan it there (design.md D68, Q8).
    from tests.test_reservations import _DECIDES

    document = {"interface": {"inputs": {"cup": "tray.a"}, "outputs": {"cup": "rack.a"}},
                "occupied": [{"spot": "sink.basin", "since": 0}], "activities": []}
    solved = schedule(copy.deepcopy(_DECIDES), _two_sinks(), document_path=copy.deepcopy(document),
                      random_seed=0, check_arms=False)
    built = schedule(copy.deepcopy(_DECIDES), _two_sinks(), document_path=copy.deepcopy(document),
                     planner="greedy", check_arms=False)
    assert solved.ok and built.ok, [d.code for d in solved.diagnostics + built.diagnostics]
    assert built.makespan == solved.makespan
    kept = [a for a in built.plan["activities"] if a["kind"] == "reservation"]
    assert kept and all("sink.basin" not in a["spots"] and "sink2.basin" in a["spots"]
                        for a in kept)
