"""A job leaving the plan: `withdraw` (design.md D42, SPEC §6.11, §6.10).

The roster is not a ledger. It is the set of jobs something of which is still in the
laboratory -- unfinished work, or material nobody has collected -- so a finished job
staying in it is not a leak but a record of the plate still on the rack. Withdrawing
is the physical act of collecting that material, said out loud, and it is asked for
rather than inferred: whether the room is actually clear is not something a document
can be read for.

🔴 **What makes it more than deleting an entry is the arithmetic.** A stock's level
is replayed, never reported (§4.7.2), so a job's history is part of what the current
levels are made of -- and taking that history out of the document would give the
stock back what the job drew. So the levels move forward to `now` and say so
(`inventories.at`, §6.10): the job's draws are spent before the baseline rather than
replayed after it, and the number the next replan starts from is the number this one
finished with. The round trip below is the test that matters.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ofplang.schedule import JobInput, schedule_jobs

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load(name):
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


def _shared_refill():
    """Two jobs over one reader, sharing the refill neither needs alone -- so the
    stock carries a draw from each, which is what makes a withdrawal's arithmetic
    visible."""
    return (
        _load("shared_refill.workflow.yaml"),
        _load("consumable.env.yaml"),
        _load("shared_refill.document.yaml"),
    )


def _jobs(workflow, *ids):
    return [JobInput(job_id, copy.deepcopy(workflow)) for job_id in ids]


def _as_status(plan, now: int):
    status = copy.deepcopy(plan)
    status["now"] = now
    for activity in status["activities"]:
        if activity["end"] <= now:
            activity["status"] = "completed"
        elif activity["start"] <= now:
            activity["status"] = "running"
    status["activities"] = [a for a in status["activities"] if a.get("status")]
    return status


def _first_plan(workflow, env, document, ids=("job1", "job2")):
    report = schedule_jobs(
        _jobs(workflow, *ids), env, document_path=copy.deepcopy(document), random_seed=0
    )
    assert report.ok, [d.code for d in report.diagnostics]
    return report.plan


def _codes(report, severity="error"):
    return [d.code for d in report.diagnostics if d.severity == severity]


# ---------------------------------------------------------------------------
# What withdrawing does.
# ---------------------------------------------------------------------------


def test_a_withdrawn_job_leaves_the_roster_and_the_history():
    """The entry goes, and so does everything the plan said about it. What stays is
    what never belonged to it: the refill carries no `job` (§6.11), because the
    scheduler ran it for whoever draws next."""
    workflow, env, document = _shared_refill()
    plan = _first_plan(workflow, env, document)
    status = _as_status(plan, 19)

    report = schedule_jobs(
        _jobs(workflow, "job2"), env, document_path=status, withdraw=["job1"], random_seed=0
    )
    assert report.ok, [d.code for d in report.diagnostics]

    assert [entry["id"] for entry in report.plan["jobs"]] == ["job2"]
    assert not [a for a in report.plan["activities"] if a.get("job") == "job1"]
    assert [a for a in report.plan["activities"] if a["kind"] == "replenishment"]
    # Said out loud: the baseline moving is not something to do quietly.
    assert "job_withdrawn" in {d.code for d in report.diagnostics}


def test_the_levels_are_carried_forward_so_the_draws_are_not_undone():
    """🔴 The whole point. `job1` drew 2 and the shared refill added 4 to a stock that
    started at 2, so the reader holds 4 when `job1` leaves. Its history goes with it,
    and the number stays: the plan states 4 as of `now` instead of 2 as of the start.

    Read the other way, this is what would have gone wrong. Drop the history and keep
    the old baseline and the stock is back to 6 -- the scheduler would plan a draw
    against reagent nobody has.
    """
    workflow, env, document = _shared_refill()
    assert document["inventories"] == {"levels": {"reader": {"reagent": 2}}}
    plan = _first_plan(workflow, env, document)

    report = schedule_jobs(
        _jobs(workflow, "job2"), env, document_path=_as_status(plan, 19),
        withdraw=["job1"], random_seed=0,
    )
    assert report.ok, [d.code for d in report.diagnostics]
    assert report.plan["inventories"] == {"levels": {"reader": {"reagent": 4}}, "at": 19}


def test_the_plan_a_job_left_is_a_status_the_next_replan_reads_the_same_way():
    """The round trip, which is where a re-baselining goes wrong if it goes wrong.

    The plan still carries history from before the moment its levels are stated for
    -- the shared refill, and `job2`'s own earlier work. Replaying that against the
    carried-forward levels would count it twice. It does not: the level the solver
    starts from is the same 4 before and after the trip, and 2 once `job2`'s own draw
    has happened.
    """
    workflow, env, document = _shared_refill()
    plan = _first_plan(workflow, env, document)
    left = schedule_jobs(
        _jobs(workflow, "job2"), env, document_path=_as_status(plan, 19),
        withdraw=["job1"], random_seed=0,
    )
    assert left.ok, [d.code for d in left.diagnostics]

    again = schedule_jobs(
        _jobs(workflow, "job2"), env, document_path=_as_status(left.plan, 19), random_seed=0
    )
    assert again.ok, [d.code for d in again.diagnostics]
    # Echoed unchanged, `at` and all: nothing withdrew this time, so nothing moved.
    assert again.plan["inventories"] == left.plan["inventories"]
    assert again.makespan == left.makespan

    later = schedule_jobs(
        _jobs(workflow, "job2"), env, document_path=_as_status(left.plan, 30), random_seed=0
    )
    assert later.ok, [d.code for d in later.diagnostics]


def test_the_jobs_that_stay_are_planned_exactly_as_they_were():
    """Withdrawing is not a replan of the others. `job2` keeps the schedule it had,
    which is what says the leaving job took only its own share with it."""
    workflow, env, document = _shared_refill()
    plan = _first_plan(workflow, env, document)
    status = _as_status(plan, 19)

    staying = schedule_jobs(
        _jobs(workflow, "job1", "job2"), env, document_path=copy.deepcopy(status), random_seed=0
    )
    left = schedule_jobs(
        _jobs(workflow, "job2"), env, document_path=copy.deepcopy(status),
        withdraw=["job1"], random_seed=0,
    )
    assert staying.ok and left.ok

    def job2_work(report):
        return [
            (a["kind"], a["start"], a["end"])
            for a in report.plan["activities"]
            if a.get("job") == "job2"
        ]

    assert job2_work(left) == job2_work(staying)
    assert left.makespan == staying.makespan


# ---------------------------------------------------------------------------
# What it refuses.
# ---------------------------------------------------------------------------


def test_withdrawing_a_job_the_roster_does_not_name_is_refused():
    """A mistyped id that withdrew nothing would leave the caller believing the
    laboratory had been cleared of a job still standing in it."""
    workflow, env, document = _shared_refill()
    plan = _first_plan(workflow, env, document)

    report = schedule_jobs(
        _jobs(workflow, "job1", "job2"), env, document_path=_as_status(plan, 19),
        withdraw=["job3"], random_seed=0,
    )
    assert not report.ok
    assert _codes(report) == ["unknown_withdrawal"]


def test_a_job_cannot_both_leave_and_be_planned():
    """Giving a workflow for a job says there is work left to do with it; withdrawing
    says there is nothing of it left at all."""
    workflow, env, document = _shared_refill()
    plan = _first_plan(workflow, env, document)

    report = schedule_jobs(
        _jobs(workflow, "job1", "job2"), env, document_path=_as_status(plan, 19),
        withdraw=["job1"], random_seed=0,
    )
    assert not report.ok
    assert _codes(report) == ["unknown_withdrawal"]


def test_a_job_with_work_left_cannot_leave():
    """Pending work is work somebody asked for, and running work is on a machine now
    -- which `occupied` cannot express, saying a *spot* is taken rather than that a
    device is busy. `job2` still has both at this moment."""
    workflow, env, document = _shared_refill()
    plan = _first_plan(workflow, env, document)

    report = schedule_jobs(
        _jobs(workflow, "job1"), env, document_path=_as_status(plan, 19),
        withdraw=["job2"], random_seed=0,
    )
    assert not report.ok
    assert _codes(report) == ["withdrawal_not_finished"]


def test_an_occupancy_outlives_the_job_that_left_the_material():
    """An occupancy is not a claim about a job, so a job leaving does not settle it.

    🔴 Withdrawing used to be refused while an entry named the leaving job, on the
    reasoning that the document was saying something of it is still here. But residue
    is declared *because* the job is there and moves into `occupied` *because* it
    leaves (design.md D42), so requiring the entry to go first ran the same material
    backwards -- and the section names no job to refuse on. What it does instead is
    outlast the job: the spot is still held afterwards, which is the whole point of
    saying so.
    """
    workflow, env, document = _shared_refill()
    plan = _first_plan(workflow, env, document)
    status = _as_status(plan, 19)
    status["occupied"] = [{"spot": "reader.slot", "since": 19}]

    report = schedule_jobs(
        _jobs(workflow, "job2"), env, document_path=status, withdraw=["job1"], random_seed=0
    )
    assert report.ok, [d.code for d in report.diagnostics]
    assert report.plan["occupied"] == [{"spot": "reader.slot", "since": 19}]


def test_withdrawing_every_job_is_refused():
    """It would have to mean either "plan nothing" or "plan one unnamed workflow",
    and it says neither."""
    workflow, env, document = _shared_refill()
    plan = _first_plan(workflow, env, document)
    status = _as_status(plan, 19)
    # Neither job is given a workflow: both are said to be leaving.
    report = schedule_jobs(
        _jobs(workflow, "job2"), env, document_path=status,
        withdraw=["job1", "job2"], random_seed=0,
    )
    assert not report.ok
    assert "withdrawal_empties_roster" in _codes(report)
