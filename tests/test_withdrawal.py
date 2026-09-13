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
from ofplang.schedule.scheduler.api import _frozen_holds, _holds_of
from tests.schedutil import with_spare_heater_stage

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _simple():
    return (
        yaml.safe_load((EXAMPLES / "simple.workflow.yaml").read_text(encoding="utf-8")),
        yaml.safe_load((EXAMPLES / "simple.env.yaml").read_text(encoding="utf-8")),
    )


def _roomy():
    """`simple`, but with a second spot on each station and a mode that uses it.

    🔴 The isolation tests need somewhere else to work. A stopped job's material is
    where it is, and a failed activity claims every spot it touched (nothing says which
    of them the material is on) -- so in `simple.env.yaml`, where each station owns its
    single `core`, one job failing really does leave the other with nowhere to go. That
    is the truth, and it is pinned by its own test below; what these want to show is
    that *the rest is replanned*, which needs a laboratory that has room for it. The
    sibling runner's own failure-isolation tests use a two-tray oven for the same
    reason.
    """
    workflow, env = _simple()
    env = copy.deepcopy(env)
    for device in env["devices"]:
        device["spots"] = [*device["spots"], "spare"]
    for name, station in (("source", "station_0"), ("target", "station_1")):
        (mode,) = env["processes"][name]["modes"]
        spare = copy.deepcopy(mode)
        spare["id"] = "spare"
        for key in ("input_spots", "output_spots"):
            if key in spare:
                spare[key] = dict.fromkeys(spare[key], f"{station}.spare")
        env["processes"][name]["modes"] = [mode, spare]
    env["transports"] = [
        {
            "transporter": "transport",
            "from": f"station_0.{a}",
            "to": f"station_1.{b}",
            "duration": 1,
        }
        for a in ("core", "spare")
        for b in ("core", "spare")
    ]
    return workflow, env


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
    workflow, env = _roomy()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=4)

    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert report.ok, [d.code for d in report.diagnostics]

    # job1 keeps what happened and gains nothing new.
    assert {a.get("status") for a in _of(report.plan, "job1")} == {"failed", "cancelled"}
    # job2's work is still to be done.
    assert all(a.get("status", "pending") == "pending" for a in _of(report.plan, "job2"))
    assert report.makespan > 4


def test_cancelled_work_takes_no_time_and_no_machine():
    """A stopped job's remaining work is pinned to a zero-length interval at `now`:
    it holds no spot and no device, so it cannot be why anything else waits."""
    workflow, env = _roomy()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=4)

    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert report.ok, [d.code for d in report.diagnostics]
    cancelled = [a for a in _of(report.plan, "job1") if a.get("status") == "cancelled"]
    assert cancelled
    assert all(a["start"] == a["end"] == 4 for a in cancelled)


def test_a_stopped_job_is_not_held_to_its_promise():
    """A job that will never complete cannot be made to finish by the time it was
    promised; holding it there would make every plan past a failure infeasible."""
    workflow, env = _roomy()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    promised = {e["id"]: e["bound"] for e in plan["jobs"]}["job1"]
    # It failed well past its own promise.
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=promised + 20)

    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert report.ok, [d.code for d in report.diagnostics]


def test_isolation_needs_somewhere_else_to_work():
    """🔴 The other half of the headline, and the reason the tests above need a roomier
    laboratory: **a failure isolates only where there is room to carry on.**

    `simple.env.yaml` gives each station one spot. A failed activity claims every spot
    it touched -- it applied no material effect, so what it was carrying is at one of
    them and nothing says which -- so `job1` failing on `station_0.core` leaves `job2`
    nowhere to make its sample. That is not a modelling artefact: it is a plate on the
    only bench, and a plan that sent `job2` there could not be run.

    The scheduler used to produce that plan, because it was told nothing and a
    completed activity's interval has ended. Now it derives what the stopped job is
    holding from the history the document already carries, refuses, and **names the job
    whose removal would let the rest be planned** -- which is a far better answer than
    a schedule nobody can execute.
    """
    workflow, env = _simple()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=4)

    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert not report.ok
    codes = [d.code for d in report.diagnostics]
    assert "infeasible" in codes and "jobs_not_plannable_together" in codes
    culprits = [d.message for d in report.diagnostics if d.code == "jobs_not_plannable_together"]
    assert any("'job1'" in message for message in culprits), culprits

    # Nothing was declared: the document says only what happened, and the hold follows.
    assert "occupied" not in status


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
    document = _held("station_1.core", 40)
    document["jobs"] = [{"id": "job1"}, {"id": "job2"}]

    report = schedule_jobs(_jobs(workflow), env, document_path=document)
    assert report.ok, [d.code for d in report.diagnostics]
    # It is not an activity: nothing in the plan reports it as work.
    assert all(a["kind"] != "held" for a in report.plan["activities"])
    assert report.plan["occupied"] == document["occupied"]

    again = schedule_jobs(_jobs(workflow), env, document_path=report.plan)
    assert again.ok, [d.code for d in again.diagnostics]


def test_a_spot_is_declared_occupied_once():
    """🔴 Two entries for one spot used to come back as a bare `infeasible`.

    A spot holds one item (§4.4), so the second entry adds no claim -- but each entry
    becomes a held node, and two of them hold the one spot over the same interval, so
    the document was unschedulable and said only that no schedule exists. The refusal
    exists to turn that into an explanation.
    """
    workflow, env = _simple()
    document = _held("station_1.core", 40)
    document["occupied"] = list(document["occupied"]) + [
        {"spot": "station_1.core", "since": 60}
    ]
    report = schedule_jobs(_jobs(workflow), env, document_path=document)
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["occupied_duplicate_spot"]


def test_an_occupancy_names_no_job():
    """It says a spot is held, and nothing more. A spot can be held for reasons no
    document records -- a plate somebody left there, an earlier run's leavings -- so
    the section keeps the form that covers those as well as a stopped job's residue.
    Naming an owner is refused outright (§6.12)."""
    workflow, env = _simple()
    document = _held("station_1.core", 40)
    document["jobs"] = [{"id": "job1"}, {"id": "job2"}]
    assert schedule_jobs(_jobs(workflow), env, document_path=document).ok

    named = _held("station_1.core", 40, job="job2")
    named["jobs"] = [{"id": "job1"}, {"id": "job2"}]
    refused = schedule_jobs(_jobs(workflow), env, document_path=named)
    assert not refused.ok
    assert "unknown_key" in {d.code for d in refused.diagnostics}


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


def test_the_committed_example_costs_what_the_truth_costs():
    """`stopped_job`: the two remaining jobs share the other tray, and **nobody had to
    say so**.

    🔴 This used to be the example of what an `occupied` entry buys: delete the section
    and the plan came back at 38 instead of 57, because it baked on the tray the stopped
    job's plate was sitting on. The nineteen seconds were what the truth cost, and the
    document had to be told the truth.

    The document already contained it. Which spots a stopped job is still holding
    follows from its own history -- the bake that failed names the tray it failed on --
    so the scheduler works it out and the section has nothing left to add. Saying it
    anyway is the same claim twice (`occupied_already_derived`).
    """
    workflow = yaml.safe_load(
        (EXAMPLES / "shared_refill.workflow.yaml").read_text(encoding="utf-8")
    )
    env = yaml.safe_load((EXAMPLES / "stopped_job.env.yaml").read_text(encoding="utf-8"))
    document = yaml.safe_load(
        (EXAMPLES / "stopped_job.document.yaml").read_text(encoding="utf-8")
    )
    def jobs():
        return [JobInput(f"job{i + 1}", copy.deepcopy(workflow)) for i in range(3)]

    assert "occupied" not in document, "the example states nothing it can derive"

    report = schedule_jobs(jobs(), env, document_path=copy.deepcopy(document))
    assert report.ok, [d.code for d in report.diagnostics]
    assert report.makespan == 57
    # The remaining jobs take turns on the other tray; nobody is sent to `job2`'s.
    trays = {
        a["mode"]
        for a in report.plan["activities"]
        if a["kind"] == "processing" and a.get("node") == ["Assay"] and a["job"] != "job2"
    }
    assert trays == {"tray_2"}, trays
    # And the plan states no occupancy: there is nothing to state that the history
    # does not already say.
    assert "occupied" not in report.plan

    # Saying it as well is refused -- one claim, one place.
    stated = copy.deepcopy(document)
    stated["occupied"] = [{"spot": "oven.tray_1", "since": 10}]
    said_twice = schedule_jobs(jobs(), env, document_path=stated)
    assert not said_twice.ok
    assert [d.code for d in said_twice.diagnostics] == ["occupied_already_derived"]


# ---------------------------------------------------------------------------
# A job does not always stop with nothing in flight.
# ---------------------------------------------------------------------------


def _consuming():
    """The one-plate workflow and a reader that holds a consumable, so a stopped job
    has abandoned work whose mode draws something."""
    return (
        yaml.safe_load(
            (EXAMPLES / "shared_refill.workflow.yaml").read_text(encoding="utf-8")
        ),
        yaml.safe_load((EXAMPLES / "consumable.env.yaml").read_text(encoding="utf-8")),
    )


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
    a spot's hold made the document infeasible.

    The hold is the one the stopped job's own failed bake leaves on its tray, derived
    from the history rather than declared -- and the job's abandoned work lands on that
    same tray, which is exactly the case at issue. It used to be set up by writing the
    tray into `occupied`; saying it as well as deriving it is now the same claim twice
    (`occupied_already_derived`), and saying it is no longer necessary.
    """
    workflow, env = _two_branch()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    status, _running = _stop_mid_flight(plan)
    failed_tray = [
        a["input_spots"]["plate"] for a in _of(status, "job1") if a.get("status") == "failed"
    ]
    assert failed_tray, status
    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert report.ok, [d.code for d in report.diagnostics]
    # And the tray really is held: nobody else is sent to bake on it.
    others = {
        spot
        for a in report.plan["activities"]
        if a.get("job") != "job1" and a.get("process") == "assay"
        for spot in (a.get("input_spots") or {}).values()
    }
    assert failed_tray[0] not in others, others


def test_cancelled_work_draws_no_consumption():
    """🔴 A stopped job's abandoned work never ran, so it drew nothing (§6.2) -- and
    its `consumption` echo (§6.3) must not say otherwise. It did: the solver modelled
    the draw as absent while the rendered plan echoed it, the levels replay believed
    the echo, and the plan's own self-check reported the document it had just produced
    as inconsistent (`plan_inventory_inconsistent`).

    Only a stopped job can put a `cancelled` activity in a plan at all, which is why
    nothing before joint planning met this.
    """
    workflow, env = _consuming()
    stock = {"inventories": {"levels": {"reader": {"reagent": 2}}}, "activities": []}
    plan = schedule_jobs(
        _jobs(workflow), env, document_path=copy.deepcopy(stock), random_seed=0
    ).plan
    status = _stop(plan, "job1", failed_node=["Make"], at=4)
    # A status reports what started; the refill that ran by then is history, and
    # anything still pending is re-derived (a `pending` refill in the input is
    # refused outright, §6.9).
    for a in status["activities"]:
        if a["kind"] == "replenishment" and a["end"] <= 4:
            a["status"] = "completed"
    status["activities"] = [a for a in status["activities"] if a.get("status")]

    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert report.ok, [d.code for d in report.diagnostics]
    cancelled = [
        a for a in report.plan["activities"]
        if a.get("status") == "cancelled" and a["kind"] == "processing"
    ]
    assert cancelled, "the stopped job should have abandoned work"
    assert all("consumption" not in a for a in cancelled)
    # ... and the work that did run still says what it drew.
    ran = [a for a in report.plan["activities"] if a.get("process") == "assay"
           and a.get("status") != "cancelled"]
    assert ran and all("consumption" in a for a in ran)


# ---------------------------------------------------------------------------
# A stopped job that had boundary material (SPEC §6.8).
# ---------------------------------------------------------------------------


def _bay(spare_stage=False):
    """Two jobs of one workflow with real boundary material: one shared loading bay,
    a rack each. Every other stopped-job fixture here uses a workflow that creates its
    material internally, so this is the only one with boundary nodes at all.

    `spare_stage` gives the heater a second stage. The example has one, so a job that
    fails there leaves the other with nowhere to heat -- true, and pinned by
    `test_isolation_needs_somewhere_else_to_work`, but not what the test that asks for
    this is about.
    """
    workflow = yaml.safe_load(
        (EXAMPLES / "interface_load.workflow.yaml").read_text(encoding="utf-8")
    )
    env = yaml.safe_load((EXAMPLES / "shared_bay.env.yaml").read_text(encoding="utf-8"))
    document = yaml.safe_load(
        (EXAMPLES / "shared_bay.document.yaml").read_text(encoding="utf-8")
    )
    if spare_stage:
        env = with_spare_heater_stage(env)
    return workflow, env, document


def test_a_stopped_job_keeps_the_history_of_its_boundary_move():
    """🔴 A job whose entry material had already been collected must still be
    replannable, and so must the laboratory around it.

    Its boundary nodes are not fixation-managed: whatever fixation one carries, the
    solver pins the input node at the job's release and the output node at the makespan
    (§6.8). The blanket cancel a stopped job's activities receive put one on them all
    the same, and the check on committed transport legs then measured the bay move
    against that zero-length interval at `now`. The move had departed at the release,
    so the leg read as departing before its own source finished: every stopped job with
    boundary material was refused as self-contradictory, and the refusal took the whole
    document with it -- the jobs that had not stopped could not be replanned either.

    A job that has *not* stopped never met this, because its boundary nodes carry no
    fixation to measure against.
    """
    workflow, env, document = _bay(spare_stage=True)
    plan = schedule_jobs(_jobs(workflow), env, document_path=document, random_seed=0).plan

    status = _stop(plan, "job1", failed_node=["Heat"], at=12)
    # `_stop` reports the failed activity as having begun at 0, which is right for the
    # workflows that start there; this one starts when its sample arrives.
    heat = [a for a in _of(status, "job1") if a["kind"] == "processing"]
    assert len(heat) == 1
    heat[0]["start"] = 2
    # The bay move actually happened -- the sample was collected, and *then* the heat
    # failed. That is the case at issue: a committed leg that departed at the job's
    # release, long before `now`.
    entry_move = [
        a
        for a in status["activities"]
        if a.get("job") == "job1"
        and a["kind"] == "transport"
        and a["arc"]["from"]["node"] == []
    ]
    assert len(entry_move) == 1
    entry_move[0].update(status="completed", start=0, end=2)

    report = schedule_jobs(_jobs(workflow), env, document_path=status)
    assert report.ok, [d.code for d in report.diagnostics]

    # The move is still history, at the time it happened.
    kept = [
        (a["start"], a["end"])
        for a in _of(report.plan, "job1")
        if a["kind"] == "transport" and a["arc"]["from"]["node"] == []
    ]
    assert kept == [(0, 2)]
    # And job2 was planned regardless, which is the whole point.
    assert any(a.get("status") is None for a in _of(report.plan, "job2"))


# ---------------------------------------------------------------------------
# What a stopped job is holding, derived (§6.12).
#
# The rules are asked of `_holds_of` directly here: each one is a sentence, and a
# whole document exercising all of them at once says which are wrong only by which
# plan comes back. Every input is a document entry -- there is nothing else to supply.
# ---------------------------------------------------------------------------


def _processing(job, node, status, start, end, *, inputs=None, outputs=None):
    entry = {"kind": "processing", "job": job, "node": [node], "status": status,
             "start": start, "end": end}
    if inputs:
        entry["input_spots"] = inputs
    if outputs:
        entry["output_spots"] = outputs
    return entry


def _transport(job, status, start, end, source, destination):
    return {"kind": "transport", "job": job, "status": status, "start": start, "end": end,
            "from_spot": source, "to_spot": destination}


def test_a_stopped_jobs_last_completed_output_is_held():
    """The plainest case: it made something and stopped, so the something is there."""
    activities = [
        _processing("job1", "Make", "completed", 0, 2, outputs={"out": "bench.slot_a"}),
        _processing("job1", "Assay", "failed", 2, 9, inputs={"plate": "bench.slot_a"}),
    ]
    assert _holds_of(activities, {}, ["job1"], 9) == {"bench.slot_a": 9}


def test_a_spot_the_job_emptied_is_not_held():
    """Ownership is not enough: a job can carry its own plate away. The trajectory is
    what settles it, and a transport releases the spot it departed."""
    activities = [
        _processing("job1", "Make", "completed", 0, 2, outputs={"out": "bench.slot_a"}),
        _transport("job1", "completed", 2, 3, "bench.slot_a", "oven.tray_1"),
        _processing("job1", "Assay", "failed", 3, 9, inputs={"plate": "oven.tray_1"}),
    ]
    assert _holds_of(activities, {}, ["job1"], 9) == {"oven.tray_1": 9}


def test_a_spot_another_job_has_since_used_is_not_claimed():
    """🔴 Ownership, not acquaintance. Two jobs of one workflow use the same bench slot
    one after the other, so "every spot this job ever touched" would claim the plate the
    next job has just made -- and make that job unplannable."""
    activities = [
        _processing("job1", "Make", "completed", 0, 2, outputs={"out": "bench.slot_a"}),
        _transport("job1", "completed", 2, 3, "bench.slot_a", "oven.tray_1"),
        _processing("job1", "Assay", "failed", 3, 9, inputs={"plate": "oven.tray_1"}),
        # job2 came along afterwards and made its own plate on the freed slot.
        _processing("job2", "Make", "completed", 4, 6, outputs={"out": "bench.slot_a"}),
    ]
    assert _holds_of(activities, {}, ["job1"], 9) == {"oven.tray_1": 9}


def test_a_spot_a_running_activity_holds_is_not_residue_yet():
    """§6.12 is for what the plan does not otherwise account for, and a running activity
    accounts for its spots perfectly well. It becomes residue the moment that operation
    comes off -- which is why this is derived every solve rather than settled once."""
    activities = [
        _processing("job1", "AssayA", "failed", 0, 9, inputs={"plate": "oven.tray_1"}),
        _processing("job1", "AssayB", "running", 2, 20, inputs={"plate": "oven.tray_2"}),
    ]
    assert _holds_of(activities, {}, ["job1"], 9) == {"oven.tray_1": 9}


def test_a_failed_transport_claims_both_ends():
    """It applied no material effect, so what it was carrying is at one of the spots it
    touched and nothing says which. Over-claiming costs a slower plan; under-claiming
    puts a plate where a plate already is."""
    activities = [
        _processing("job1", "Make", "completed", 0, 2, outputs={"out": "bench.slot_a"}),
        _transport("job1", "failed", 2, 5, "bench.slot_a", "oven.tray_1"),
    ]
    assert _holds_of(activities, {}, ["job1"], 5) == {"bench.slot_a": 5, "oven.tray_1": 5}


def test_a_spot_the_staying_job_still_derives_is_not_written():
    """🔴 The withdrawal writes down only what nobody will be able to derive once the
    job has gone -- and a spot two stopped jobs both claim does not qualify.

    A failed transport claims both its ends, and the other end may be where another
    job's history left something. Write it, and from the next replan on the document
    holds that spot twice: once as the entry, once from the job that is still here to
    derive it. That is refused (`occupied_already_derived`) -- and because the entry
    rides in the echo, the refusal is not one bad call but every call after it.
    """
    activities = [
        # job2 is stopped and staying; the tray is its plate.
        _processing("job2", "Make", "completed", 0, 6, outputs={"out": "oven.tray_1"}),
        _processing("job2", "Assay", "failed", 6, 9, inputs={"plate": "oven.tray_1"}),
        # job1 is leaving. Its own bench slot is its alone; the failed move claims the
        # tray as well, because nothing says which end its plate is at.
        _processing("job1", "Make", "completed", 0, 2, outputs={"out": "bench.slot_a"}),
        _transport("job1", "failed", 2, 5, "bench.slot_a", "oven.tray_1"),
    ]
    # Both claim the tray, which is what makes this the case worth refusing to write.
    assert "oven.tray_1" in _holds_of(activities, {}, ["job1"], 9)
    staying = _holds_of(activities, {}, ["job2"], 9)
    assert "oven.tray_1" in staying

    written = _frozen_holds(["job1"], {}, activities, None, 9, derived=set(staying))
    assert [entry["spot"] for entry in written] == ["bench.slot_a"]

    # Without being told what stays derivable it writes the tray as well -- the bug
    # this guards, kept here so the guard is measured rather than assumed.
    assert "oven.tray_1" in {
        entry["spot"] for entry in _frozen_holds(["job1"], {}, activities, None, 9)
    }


def test_a_stopped_job_keeps_its_roster_entry_but_not_its_promise():
    """🔴 The solve already drops a stopped job's deadline -- a promise it can never
    reach would make every plan past a failure infeasible -- so restating it here
    changes no schedule. What it would change is the document, which would go on
    reporting a completion this job will not reach: a lie a reader has no way to
    detect. And under a caller that echoes the plan back, it would be restated for the
    rest of the run."""
    workflow, env = _roomy()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    assert all("bound" in entry for entry in plan["jobs"])
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=40)

    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert report.ok, [d.code for d in report.diagnostics]
    entries = {entry["id"]: entry for entry in report.plan["jobs"]}
    # Still on the roster: something of it is still in the laboratory.
    assert set(entries) == {"job1", "job2"}
    assert "bound" not in entries["job1"]
    assert entries["job2"]["bound"] is not None


def test_entry_material_nobody_collected_is_held():
    """It is *there*, given, from the job's release (§6.8) -- and until the move that
    collects it, no activity has touched it. Only the roster says where it is."""
    entries = {"job1": {"id": "job1", "release": 4,
                        "interface": {"inputs": {"sample": "loader.stage"}}}}
    activities = [_processing("job1", "Heat", "cancelled", 9, 9)]
    assert _holds_of(activities, entries, ["job1"], 9) == {"loader.stage": 4}
    # Not before its release: there is nothing on the bay yet.
    assert _holds_of(activities, entries, ["job1"], 2) == {}


def test_the_moment_is_when_it_was_left_there():
    """Not when the claim is made. It changes no plan -- a hold runs from
    `max(since, now)` either way -- but it is what a withdrawal writes down, and by then
    the history that would have said so is gone."""
    activities = [
        _processing("job1", "Make", "completed", 0, 2, outputs={"out": "bench.slot_a"}),
        _processing("job1", "Drop", "cancelled", 40, 40),
    ]
    assert _holds_of(activities, {}, ["job1"], 40) == {"bench.slot_a": 2}


# ---------------------------------------------------------------------------
# A document that disagrees with itself (§6.12, §6.11).
#
# Neither of these is about the schedule being hard: both are the document stating
# something its own history denies. Left unchecked they come back as `infeasible`, and
# the reader has nothing to go on.
# ---------------------------------------------------------------------------


def test_an_occupancy_on_a_spot_in_use_is_refused():
    """§6.12 is for a hold the plan does **not otherwise account for**, and a running
    activity accounts for its spots perfectly well: the two describe the one spot over
    overlapping intervals, and nothing can satisfy both."""
    workflow, env = _two_branch()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    status, _running = _stop_mid_flight(plan)
    in_use = [
        spot
        for a in _of(status, "job1")
        if a.get("status") == "running"
        for spot in (a.get("input_spots") or {}).values()
    ]
    assert in_use, status
    status["occupied"] = [{"spot": in_use[0], "since": 0}]

    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["occupied_spot_in_use"]
    assert in_use[0] in report.diagnostics[0].message


def test_a_release_later_than_the_jobs_own_history_is_refused():
    """A release holds back work that has **not run** (§J1): history is pinned by what
    happened, and re-holding it would make the past infeasible rather than say anything
    about the future. So a release after a started activity constrains nothing -- it is
    a stated intention the document's own activities deny."""
    workflow, env = _roomy()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    status = _stop(plan, "job1", failed_node=["SampleSource"], at=4)
    started = min(a["start"] for a in _of(status, "job1") if a.get("status") == "failed")
    for entry in status["jobs"]:
        if entry["id"] == "job1":
            entry["release"] = started + 5

    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["release_after_history"]
    assert "'job1'" in report.diagnostics[0].message


def test_a_release_a_job_has_not_reached_is_ordinary():
    """The check is about contradiction, not about releases. A job whose work has not
    started is exactly what a release is for."""
    workflow, env = _roomy()
    plan = schedule_jobs(_jobs(workflow), env, random_seed=0).plan
    status = copy.deepcopy(plan)
    status["now"] = 0
    status["activities"] = []
    for entry in status["jobs"]:
        if entry["id"] == "job2":
            entry["release"] = 50

    report = schedule_jobs(_jobs(workflow), env, document_path=status, random_seed=0)
    assert report.ok, [d.code for d in report.diagnostics]
    assert min(a["start"] for a in _of(report.plan, "job2")) >= 50
