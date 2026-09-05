"""A job that stops, and the material it leaves behind (SPEC §6.2, §6.11, §6.12).

v0 stops a run on any failure, and for one workflow that is the whole story. For
several jobs sharing a laboratory it is not: a plate cracking in one run says nothing
about the other three, and refusing to replan any of them is not a safety property but
a limitation. So a terminal status stops the **job** it belongs to.

What that leaves behind is the harder half. The scheduler knows a spot is taken only
while some activity's interval covers it, and a stopped job's last activity has ended,
so its material is invisible -- the plan would send another job to a place that is
physically full. `occupied` is how a document says otherwise, and the tests below are
mostly about that: the modelling is easy to get subtly wrong, and it was (holding to
the makespan rather than the horizon reported a makespan for a run that had finished).
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ofplang.schedule import JobInput, schedule, schedule_jobs

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _simple():
    return (
        yaml.safe_load((EXAMPLES / "simple.workflow.yaml").read_text(encoding="utf-8")),
        yaml.safe_load((EXAMPLES / "simple.env.yaml").read_text(encoding="utf-8")),
    )


def _jobs(workflow, n=2):
    return [JobInput(f"job{i + 1}", copy.deepcopy(workflow)) for i in range(n)]


def _stop(plan, job, *, failed_node, at):
    """The status a run reports when `job` has failed: the activity that ended
    abnormally is `failed`, and its remaining work `cancelled` (§6.2)."""
    status = copy.deepcopy(plan)
    status["now"] = at
    for a in status["activities"]:
        if a.get("job") != job:
            continue
        if a["kind"] == "processing" and a.get("node") == failed_node:
            a["status"], a["start"], a["end"] = "failed", 0, at
        else:
            a["status"], a["start"], a["end"] = "cancelled", at, at
    return status


def _of(plan, job):
    return [a for a in plan["activities"] if a.get("job") == job]


# ---------------------------------------------------------------------------
# A terminal status stops one job, not the plan.
# ---------------------------------------------------------------------------


def test_one_job_failing_does_not_stop_the_others():
    """The headline. `job1` fails; `job2` is untouched work and is replanned."""
    workflow, env = _simple()
    plan = schedule_jobs(_jobs(workflow), env).plan
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=4)

    report = schedule_jobs(_jobs(workflow), env, document_path=status)
    assert report.ok, [d.code for d in report.diagnostics]

    # job1 keeps what happened and gains nothing new.
    assert {a.get("status") for a in _of(report.plan, "job1")} == {"failed", "cancelled"}
    # job2's work is still to be done.
    assert all(a.get("status", "pending") == "pending" for a in _of(report.plan, "job2"))
    assert report.makespan > 4


def test_cancelled_work_takes_no_time_and_no_machine():
    """A stopped job's remaining work is pinned to a zero-length interval at `now`:
    it holds no spot and no device, so it cannot be why anything else waits."""
    workflow, env = _simple()
    plan = schedule_jobs(_jobs(workflow), env).plan
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=4)

    report = schedule_jobs(_jobs(workflow), env, document_path=status)
    cancelled = [a for a in _of(report.plan, "job1") if a.get("status") == "cancelled"]
    assert cancelled
    assert all(a["start"] == a["end"] == 4 for a in cancelled)


def test_a_stopped_job_is_not_held_to_its_promise():
    """A job that will never complete cannot be made to finish by the time it was
    promised; holding it there would make every plan past a failure infeasible."""
    workflow, env = _simple()
    plan = schedule_jobs(_jobs(workflow), env).plan
    promised = {e["id"]: e["bound"] for e in plan["jobs"]}["job1"]
    # It failed well past its own promise.
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=promised + 20)

    report = schedule_jobs(_jobs(workflow), env, document_path=status)
    assert report.ok, [d.code for d in report.diagnostics]


def test_every_job_stopping_is_still_the_end_of_the_run():
    workflow, env = _simple()
    plan = schedule_jobs(_jobs(workflow), env).plan
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=4)
    for a in status["activities"]:
        if a.get("job") == "job2":
            a["status"], a["start"], a["end"] = "cancelled", 4, 4

    report = schedule_jobs(_jobs(workflow), env, document_path=status)
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["terminal_status_not_replannable"]


def test_a_single_workflow_is_one_job_and_stops_as_it_always_did():
    """The rule is per job, and one workflow is one job -- so this is unchanged."""
    workflow, env = _simple()
    report = schedule(
        workflow,
        env,
        document_path={
            "now": 4,
            "activities": [
                {
                    "kind": "processing",
                    "status": "failed",
                    "start": 0,
                    "end": 4,
                    "process": "source",
                    "mode": "0",
                    "node": ["SampleSource"],
                }
            ],
        },
    )
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["terminal_status_not_replannable"]


# ---------------------------------------------------------------------------
# `occupied`: a spot held by something the plan does not otherwise account for.
# ---------------------------------------------------------------------------


def _held(spot, since, **extra):
    return {"occupied": [{"spot": spot, "since": since, **extra}], "activities": []}


def test_an_occupied_spot_cannot_be_used_after_it_is_taken():
    workflow, env = _simple()
    assert schedule(copy.deepcopy(workflow), env).makespan == 5

    # Taken from the start: the target has nowhere to run at all.
    blocked = schedule(
        copy.deepcopy(workflow), env, document_path=_held("station_1.core", 0)
    )
    assert not blocked.ok
    assert blocked.outcome == "infeasible"

    # Taken later: the work has to be done before then. The target cannot finish
    # before 5 (source 2, move 1, target 2), so 5 is exactly the boundary -- it fits
    # at 5 and does not at 4, which is the constraint biting rather than a coincidence.
    fits = schedule(copy.deepcopy(workflow), env, document_path=_held("station_1.core", 5))
    assert fits.ok, [d.code for d in fits.diagnostics]
    target = [a for a in fits.plan["activities"] if a.get("node") == ["SampleTarget"]]
    assert target and target[0]["end"] <= 5

    too_soon = schedule(
        copy.deepcopy(workflow), env, document_path=_held("station_1.core", 4)
    )
    assert not too_soon.ok
    assert too_soon.outcome == "infeasible"


def test_an_occupancy_does_not_become_the_makespan():
    """It ends at the horizon, not at the makespan. Tying it to `c_max` forced
    `c_max >= since` and reported a makespan for a run that had long finished."""
    workflow, env = _simple()
    report = schedule(
        copy.deepcopy(workflow), env, document_path=_held("station_1.core", 500)
    )
    assert report.ok
    assert report.makespan == 5  # what the work takes, not when the spot was taken


def _oven():
    """One workflow, and a two-tray oven -- so a held tray still leaves somewhere to
    work, which `simple` (one spot per device) does not."""
    return (
        yaml.safe_load(
            (EXAMPLES / "shared_refill.workflow.yaml").read_text(encoding="utf-8")
        ),
        yaml.safe_load((EXAMPLES / "stopped_job.env.yaml").read_text(encoding="utf-8")),
    )


def test_an_occupancy_stated_from_the_past_holds_from_now():
    """🔴 A `since` before `now` constrains nothing -- pending work starts at or after
    `now` and reported work is pinned by its history -- so pinning the hold there could
    only collide with that history and refuse the document. And the document refused is
    the ordinary one: a stopped job's material is described twice over, once by the
    activity that put it there and once by this section.

    Measured here as the thing that matters: every `since` up to `now` gives the *same*
    plan, so nothing is lost by declining to re-litigate the past."""
    workflow, env = _oven()

    def planned(since):
        document = _held("oven.tray_1", since)
        document["now"] = 20
        report = schedule(copy.deepcopy(workflow), env, document_path=document)
        assert report.ok, [d.code for d in report.diagnostics]
        return report.plan["activities"]

    reference = planned(20)
    for since in (0, 5, 12, 19):
        assert planned(since) == reference


def test_an_occupancy_keeps_the_date_it_was_given():
    """It holds from `now`, but the plan echoes what the document said: when the spot
    was taken is a fact about the run, and this section is where it is recorded."""
    workflow, env = _oven()
    document = _held("oven.tray_1", 3)
    document["now"] = 20
    report = schedule(copy.deepcopy(workflow), env, document_path=document)
    assert report.ok
    assert report.plan["occupied"] == [{"spot": "oven.tray_1", "since": 3}]


def test_an_occupancy_belongs_to_no_work_and_round_trips():
    workflow, env = _simple()
    document = _held("station_1.core", 40, job="job2")
    document["jobs"] = [{"id": "job1"}, {"id": "job2"}]

    report = schedule_jobs(_jobs(workflow), env, document_path=document)
    assert report.ok, [d.code for d in report.diagnostics]
    # It is not an activity: nothing in the plan reports it as work.
    assert all(a["kind"] != "held" for a in report.plan["activities"])
    assert report.plan["occupied"] == document["occupied"]

    again = schedule_jobs(_jobs(workflow), env, document_path=report.plan)
    assert again.ok, [d.code for d in again.diagnostics]


def test_an_occupancy_may_name_a_job_or_not():
    """Naming the job that left it is traceability, not provenance: nobody may know,
    and the spot is taken either way."""
    workflow, env = _simple()
    document = _held("station_1.core", 40)
    document["jobs"] = [{"id": "job1"}, {"id": "job2"}]
    assert schedule_jobs(_jobs(workflow), env, document_path=document).ok


# ---------------------------------------------------------------------------
# When nothing can be planned: which job is why (§6.11).
# ---------------------------------------------------------------------------


def test_the_job_that_makes_a_plan_impossible_is_named():
    """`job2` is asked to start on a spot that is already taken for good, so nothing
    can be planned -- and saying which job beats a bare `infeasible`."""
    workflow, env = _simple()
    document = {
        "now": 0,
        "jobs": [{"id": "job1"}, {"id": "job2", "release": 100}],
        # Held from 0 for good: any job wanting station_1.core is impossible, and both
        # of these do -- so neither alone accounts for it.
        "occupied": [{"spot": "station_1.core", "since": 0}],
        "activities": [],
    }
    report = schedule_jobs(_jobs(workflow), env, document_path=document)
    assert not report.ok
    codes = [d.code for d in report.diagnostics]
    assert "infeasible" in codes
    assert "jobs_not_plannable_together" in codes
    named = [d for d in report.diagnostics if d.code == "jobs_not_plannable_together"]
    assert "no single job accounts for this" in named[0].message


def test_a_single_culprit_is_named_and_not_removed():
    """One job is impossible on its own; the rest could be planned without it. The
    scheduler says so and plans nothing -- discarding the work is not its decision."""
    workflow, env = _simple()
    # job2 is released after the spot it needs is taken for good; job1 finishes first.
    document = {
        "now": 0,
        "jobs": [{"id": "job1"}, {"id": "job2", "release": 60}],
        "occupied": [{"spot": "station_1.core", "since": 50}],
        "activities": [],
    }
    report = schedule_jobs(_jobs(workflow), env, document_path=document)
    assert not report.ok
    named = [d for d in report.diagnostics if d.code == "jobs_not_plannable_together"]
    assert len(named) == 1
    assert "'job2'" in named[0].message
    assert "not being removed" in named[0].message
    assert report.plan is None


def test_one_workflow_is_never_diagnosed_this_way():
    """With nothing to take out, there is nothing to say."""
    workflow, env = _simple()
    report = schedule(
        copy.deepcopy(workflow), env, document_path=_held("station_1.core", 0)
    )
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["infeasible"]


def test_the_committed_example_shows_what_the_occupancy_costs():
    """`stopped_job`: with the tray declared taken the two remaining jobs share the
    other one; without it the plan is shorter and puts a plate where one already is."""
    workflow = yaml.safe_load(
        (EXAMPLES / "shared_refill.workflow.yaml").read_text(encoding="utf-8")
    )
    env = yaml.safe_load((EXAMPLES / "stopped_job.env.yaml").read_text(encoding="utf-8"))
    document = yaml.safe_load(
        (EXAMPLES / "stopped_job.document.yaml").read_text(encoding="utf-8")
    )
    jobs = [JobInput(f"job{i + 1}", copy.deepcopy(workflow)) for i in range(3)]

    honest = schedule_jobs(jobs, env, document_path=copy.deepcopy(document))
    assert honest.ok, [d.code for d in honest.diagnostics]
    assert honest.makespan == 57

    blind = copy.deepcopy(document)
    del blind["occupied"]
    shorter = schedule_jobs(jobs, env, document_path=blind)
    assert shorter.ok
    assert shorter.makespan == 38
    # And it is shorter because it bakes on the tray that is already holding a plate.
    trays = {
        a["mode"]
        for a in shorter.plan["activities"]
        if a["kind"] == "processing" and a.get("node") == ["Assay"] and a["job"] != "job2"
    }
    assert "tray_1" in trays


# ---------------------------------------------------------------------------
# A job does not always stop with nothing in flight.
# ---------------------------------------------------------------------------


def _two_branch():
    """A workflow whose job has two independent branches, and the two-tray oven. A
    linear job can never fail with its own work still running -- whatever fails is
    the only thing that was going."""
    return (
        yaml.safe_load(
            (Path(__file__).parent / "fixtures" / "two_branch.workflow.yaml")
            .read_text(encoding="utf-8")
        ),
        yaml.safe_load((EXAMPLES / "stopped_job.env.yaml").read_text(encoding="utf-8")),
    )


def _stop_mid_flight(plan, held_spot=None, held_since=None):
    """`job1` has stopped: one of its bakes failed, while its *other* bake is still on
    the oven. `now` is the moment the first one gave out. Returns the status and the
    moment the still-running bake is due to come off.

    `held_spot` is named by the caller rather than hard-coded, because which tray each
    bake lands on is the solver's choice and need not be the same from run to run."""
    status = copy.deepcopy(plan)
    bakes = [a for a in _of(status, "job1") if a.get("process") == "assay"]
    assert len(bakes) == 2, bakes
    running, failed = sorted(bakes, key=lambda a: -a["end"])
    at = failed["end"]
    assert running["start"] <= at < running["end"], (running, failed)
    status["now"] = at
    for a in status["activities"]:
        if a.get("job") != "job1":
            a.pop("status", None)
            continue
        if a is failed:
            a["status"] = "failed"
        elif a is running:
            a["status"] = "running"
        elif a["end"] <= at:
            a["status"] = "completed"
        else:
            a.pop("status", None)
    status["activities"] = [
        a for a in status["activities"] if a.get("job") != "job1" or "status" in a
    ]
    if held_since is not None:
        status["occupied"] = [{"spot": held_spot, "since": held_since}]
    return status, running["end"]


def test_a_job_that_stops_with_work_still_running_does_not_stop_the_others():
    """🔴 A job can fail in one place while another of its operations is still on the
    machine -- and a running operation is never aborted (§6.2). Its abandoned work
    must be placed *after* that operation: at `now` it would sit before the thing it
    waits on, which no schedule satisfies, so the whole document went infeasible and
    took every other job in the laboratory with it."""
    workflow, env = _two_branch()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    status, running = _stop_mid_flight(plan)

    report = schedule_jobs(_jobs(workflow), env, document_path=status)
    assert report.ok, [d.code for d in report.diagnostics]
    cancelled = [a for a in report.plan["activities"] if a.get("status") == "cancelled"]
    assert cancelled
    # Abandoned where the job actually stopped: once its last operation came off,
    # not at `now` -- and as a zero-length interval, as it always was.
    assert {a["start"] for a in cancelled} == {running}
    assert all(a["start"] == a["end"] for a in cancelled)
    # And job2 was planned regardless, which is the whole point.
    assert any(
        a.get("job") == "job2" and a.get("status") is None
        for a in report.plan["activities"]
    )


def test_cancelled_work_holds_no_spot():
    """🔴 It never ran, so it takes no spot and no machine -- and being zero-length is
    not enough to arrange that. A point strictly inside another interval is still a
    point inside it, and CP-SAT refuses the pair: a cancelled activity landing inside
    a spot's `occupied` hold made the document infeasible."""
    workflow, env = _two_branch()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    # The tray the stopped job's own cancelled bake would have used: the one its
    # abandoned work lands on, which is exactly the case at issue.
    cancelled_tray = [
        a["input_spots"]["plate"]
        for a in _of(plan, "job1")
        if a.get("process") == "assay"
    ]
    status, _running = _stop_mid_flight(plan, held_spot=cancelled_tray[0], held_since=0)
    report = schedule_jobs(_jobs(workflow), env, document_path=status)
    assert report.ok, [d.code for d in report.diagnostics]
