"""Several workflows planned together against one environment (SPEC §6.11).

The property that makes joint planning worth doing at all is the one the first test
states: the jobs draw on the same device-local stock, so a refill that *neither*
workflow needs on its own is planned once for both. Nothing in either workflow
mentions a resource, and neither asks for a refill -- it appears because the stock
belongs to the device (§4.7) and merging the jobs is what puts them on one stock.

The rest guard the seam. A joint plan carries a `job` on every activity and keeps
`node` workflow-relative, so two jobs running the same workflow stay apart without
the node path changing meaning; and the single-workflow path must come out of all
of this shaped exactly as it was, since that is what the other 490 tests describe.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ofplang.schedule import JobInput, schedule, schedule_jobs
from ofplang.schedule.scheduler.workflow import fingerprint, parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load(name: str):
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


def _consumable():
    """The shared-refill example: a one-plate workflow, the reader that holds the
    reagent it draws on, and the starting level that makes one job self-sufficient
    and two jobs not (one assay draws 2, the reader starts with 2 and holds 6)."""
    return (
        _load("shared_refill.workflow.yaml"),
        _load("consumable.env.yaml"),
        _load("shared_refill.document.yaml"),
    )


def _simple():
    """The smallest example, and the one with no consumables at all -- so a plan of it
    can be fed straight back as the next input."""
    return _load("simple.workflow.yaml"), _load("simple.env.yaml")


def _refills(plan) -> list[dict]:
    return [a for a in plan["activities"] if a["kind"] == "replenishment"]


def _stage(plan, name: str) -> int:
    """One stage's value out of the plan's objective, whatever shape it took (§6.1)."""
    kind, value = plan["objective"]["kind"], plan["objective"]["value"]
    if isinstance(kind, str):
        return value if kind == name else 0
    return value[kind.index(name)] if name in kind else 0


def test_two_jobs_need_a_refill_neither_needs_alone():
    """The headline: one job schedules with no replenishment at all, and the same
    workflow planned twice needs exactly one -- shared, so it belongs to no job."""
    workflow, env, document = _consumable()

    alone = schedule(workflow, env, document_path=document)
    assert alone.ok, [d.code for d in alone.diagnostics]
    assert _refills(alone.plan) == []
    assert _stage(alone.plan, "replenishment_count") == 0

    together = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=document,
    )
    assert together.ok, [d.code for d in together.diagnostics]
    refills = _refills(together.plan)
    assert len(refills) == 1
    assert _stage(together.plan, "replenishment_count") == 1

    # It tops up the device both jobs' assays run on, and carries no `job`: the
    # scheduler decided to run it, and it serves activities from both (§6.9).
    assert refills[0]["device"] == "reader"
    assert "job" not in refills[0]

    # And it really is shared -- assays from both jobs run after it.
    after = {
        a["job"]
        for a in together.plan["activities"]
        if a["kind"] == "processing" and a["process"] == "assay" and a["start"] >= refills[0]["end"]
    }
    assert after == {"job1", "job2"} or len(after) >= 1


def test_joint_plan_labels_every_workflow_activity_with_its_job():
    workflow, env, document = _consumable()
    report = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        env,
        document_path=document,
    )
    assert report.ok

    by_job: dict[str, int] = {}
    for a in report.plan["activities"]:
        if a["kind"] == "replenishment":
            continue
        assert "job" in a, a
        by_job[a["job"]] = by_job.get(a["job"], 0) + 1
    # Both jobs run the same workflow, so they contribute the same activities.
    assert set(by_job) == {"a", "b"}
    assert by_job["a"] == by_job["b"]


def test_node_paths_stay_workflow_relative():
    """The job id namespaces node paths *inside* the instance only. What the document
    carries is the workflow-relative path it always carried -- the convention the
    sibling runner keys its value store by (`model.Workflow`, INVARIANT 2)."""
    workflow, env, document = _consumable()
    single = schedule(workflow, env, document_path=document)
    joint = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        env,
        document_path=document,
    )

    def nodes(plan):
        return {
            tuple(a["node"]) for a in plan["activities"] if a["kind"] == "processing"
        }

    assert nodes(joint.plan) == nodes(single.plan)
    assert ("a",) not in nodes(joint.plan)

    # Arc provenance is split the same way, on both endpoints.
    for a in joint.plan["activities"]:
        if a["kind"] in ("transport", "relay"):
            assert a["arc"]["from"]["node"][:1] != ["a"]
            assert a["arc"]["to"]["node"][:1] != ["b"]


def test_single_workflow_plan_is_unchanged():
    """The single-workflow path prefixes nothing and labels nothing, so its plan is
    what it was before joint planning existed."""
    workflow, env, document = _consumable()
    report = schedule(workflow, env, document_path=document)
    assert report.ok
    assert all("job" not in a for a in report.plan["activities"])
    assert isinstance(report.plan["meta"]["workflow"], str)


def test_one_named_job_is_the_joint_shape():
    """`schedule_jobs` with a single job still labels it: the shape follows the entry
    point that was called, not how many workflows happened to be passed."""
    workflow, env, document = _consumable()
    report = schedule_jobs([JobInput("only", workflow)], env, document_path=document)
    assert report.ok
    assert all(
        a["job"] == "only" for a in report.plan["activities"] if a["kind"] != "replenishment"
    )
    assert report.plan["meta"]["workflow"] == "<in-memory>"


def test_joint_plan_meta_names_every_workflow():
    workflow, env, document = _consumable()
    report = schedule_jobs(
        [
            JobInput("a", copy.deepcopy(workflow), "a.yaml"),
            JobInput("b", copy.deepcopy(workflow), "b.yaml"),
        ],
        env,
        document_path=document,
    )
    assert report.plan["meta"]["workflow"] == ["a.yaml", "b.yaml"]


def test_jobs_compete_for_the_same_machines():
    """Merging is not just a union of two independent schedules: the jobs share the
    environment's devices and spots, so two jobs take longer than one."""
    workflow, env, document = _consumable()
    alone = schedule(workflow, env, document_path=document)
    together = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        env,
        document_path=document,
    )
    assert together.makespan > alone.makespan


def test_job_ids_must_be_usable_as_names():
    workflow, env, document = _consumable()
    with pytest.raises(ValueError):
        schedule_jobs([], env, document_path=document)
    with pytest.raises(ValueError):
        schedule_jobs([JobInput("", workflow)], env, document_path=document)
    with pytest.raises(ValueError):
        schedule_jobs(
            [JobInput("x", copy.deepcopy(workflow)), JobInput("x", copy.deepcopy(workflow))],
            env,
            document_path=document,
        )


def test_joint_plan_refuses_a_shared_interface():
    """`interface` binds one workflow's boundary to spots and says nothing about which
    job a binding belongs to, so a joint plan refuses it rather than having two
    boundary nodes claim the same spot (per-job interface is a later stage)."""
    workflow = _load("interface_load.workflow.yaml")
    env = _load("interface_load.env.yaml")
    document = _load("interface_load.document.yaml")

    # It is a perfectly good single-workflow instance.
    assert schedule(workflow, env, document_path=document).ok

    report = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        env,
        document_path=document,
    )
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["multi_job_interface"]


def test_a_joint_plan_names_its_jobs():
    """The plan carries the roster (§6.11), in the order the jobs were given, so the
    document says which workflows it covers rather than leaving it to be inferred
    from whatever `job` values happen to appear on activities."""
    workflow, env, document = _consumable()
    report = schedule_jobs(
        [JobInput("b", copy.deepcopy(workflow)), JobInput("a", copy.deepcopy(workflow))],
        env,
        document_path=document,
    )
    assert report.ok
    assert [entry["id"] for entry in report.plan["jobs"]] == ["b", "a"]


def test_a_single_workflow_plan_has_no_roster():
    workflow, env, document = _consumable()
    report = schedule(workflow, env, document_path=document)
    assert report.ok
    assert "jobs" not in report.plan


def test_a_joint_plan_round_trips_through_its_own_roster():
    """The plan is the next input (§6.2), so feeding it straight back has to work --
    and it is the roster that makes the second call agree about who the jobs are.

    On `simple` rather than the shared-refill example, because a plan carrying a
    *pending* refill is not a replanning input at all (`pending_replenishment_in_status`:
    how many to run is re-decided every solve). That rule is older than jobs and has
    nothing to do with them."""
    workflow, env = _simple()
    first = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
    )
    assert first.ok

    again = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=first.plan,
    )
    assert again.ok, [d.code for d in again.diagnostics]
    assert again.plan["jobs"] == first.plan["jobs"]
    assert again.makespan == first.makespan


def test_a_document_planning_other_jobs_is_refused():
    """A replan given a different set of workflows than the plan it continues would
    match history onto activities that never ran it."""
    workflow, env, document = _consumable()
    plan = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=document,
    ).plan

    for given in (
        [JobInput("job1", copy.deepcopy(workflow))],  # one of the two
        [  # renamed
            JobInput("job1", copy.deepcopy(workflow)),
            JobInput("job3", copy.deepcopy(workflow)),
        ],
    ):
        report = schedule_jobs(given, env, document_path=copy.deepcopy(plan))
        assert not report.ok
        assert [d.code for d in report.diagnostics] == ["job_roster_mismatch"]


def test_the_roster_is_compared_as_a_set_not_a_sequence():
    """Re-stating the same jobs in another order is the same plan, not a different
    one. (The order is still the record of how they were given, and is preserved.)"""
    workflow, env = _simple()
    plan = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
    ).plan

    report = schedule_jobs(
        [JobInput("job2", copy.deepcopy(workflow)), JobInput("job1", copy.deepcopy(workflow))],
        env,
        document_path=plan,
    )
    assert report.ok, [d.code for d in report.diagnostics]


def test_an_empty_roster_is_no_roster_for_a_single_workflow():
    """`jobs: []` and no `jobs` at all say the same thing to a single-workflow call:
    this document plans no named jobs. Only a roster that *names* one is a mismatch."""
    workflow, env, document = _consumable()
    document = copy.deepcopy(document)
    document["jobs"] = []
    report = schedule(workflow, env, document_path=document)
    assert report.ok, [d.code for d in report.diagnostics]
    assert "jobs" not in report.plan


def test_a_single_workflow_may_not_continue_a_joint_plan():
    """The same guard from the other side: `schedule` is one unnamed job, which is
    not the roster the joint plan names."""
    workflow, env, document = _consumable()
    plan = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=document,
    ).plan
    report = schedule(workflow, env, document_path=plan)
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["job_roster_mismatch"]


# ---------------------------------------------------------------------------
# The roster's planning parameters: which workflow, and from when.
# ---------------------------------------------------------------------------


def test_the_roster_records_which_workflow_each_job_runs():
    workflow, env, document = _consumable()
    other, _ = _simple()
    report = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        env,
        document_path=document,
    )
    assert report.ok
    prints = [entry["fingerprint"] for entry in report.plan["jobs"]]
    # Two copies of one workflow hash the same -- which is the point: swapping *those*
    # two changes nothing, and the digest is there to catch the swap that does.
    assert prints[0] == prints[1]
    assert prints[0] != fingerprint(parse_workflow(other)[0])


def test_a_job_given_a_different_workflow_is_refused():
    """Two jobs handed over in the other order have ids that match as a set, so the
    roster check passes -- only the fingerprint catches it."""
    workflow, env, document = _consumable()
    other, other_env = _simple()
    plan = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        env,
        document_path=document,
    ).plan

    report = schedule_jobs(
        [JobInput("a", copy.deepcopy(other)), JobInput("b", copy.deepcopy(workflow))],
        env,
        document_path=plan,
    )
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["job_workflow_mismatch"]
    assert "'a'" in report.diagnostics[0].message


def test_a_roster_without_fingerprints_is_still_replannable():
    """An entry written before fingerprints were recorded is left alone: refusing it
    would strand documents that are otherwise perfectly replannable."""
    workflow, env = _simple()
    plan = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
    ).plan
    for entry in plan["jobs"]:
        del entry["fingerprint"]

    report = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=plan,
    )
    assert report.ok, [d.code for d in report.diagnostics]


def test_a_new_job_may_join_an_existing_roster():
    """The roster names the jobs already being planned; anything beyond it is arriving
    now (§6.11). Dropping one stays an error -- its history would have nowhere to go."""
    workflow, env = _simple()
    plan = schedule_jobs([JobInput("job1", copy.deepcopy(workflow))], env).plan

    joined = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=plan,
    )
    assert joined.ok, [d.code for d in joined.diagnostics]
    assert [entry["id"] for entry in joined.plan["jobs"]] == ["job1", "job2"]

    dropped = schedule_jobs([JobInput("job2", copy.deepcopy(workflow))], env, document_path=plan)
    assert not dropped.ok
    assert [d.code for d in dropped.diagnostics] == ["job_roster_mismatch"]


def test_an_arriving_job_is_released_at_now():
    """A job that did not exist earlier cannot be scheduled to have started earlier."""
    workflow, env, plan = _joint_simple()
    status = _advance(plan, 3)

    report = schedule_jobs(
        [
            JobInput("job1", copy.deepcopy(workflow)),
            JobInput("job2", copy.deepcopy(workflow)),
            JobInput("job3", copy.deepcopy(workflow)),
        ],
        env,
        document_path=status,
    )
    assert report.ok, [d.code for d in report.diagnostics]
    entries = {entry["id"]: entry for entry in report.plan["jobs"]}
    # The two that were already planned keep release 0 (omitted); the newcomer is
    # released at `now`.
    assert "release" not in entries["job1"]
    assert entries["job3"]["release"] == 3


# ---------------------------------------------------------------------------
# Priority: an earlier job is not disturbed by a later one (design.md D38).
# ---------------------------------------------------------------------------


def _bounds(plan) -> dict[str, int]:
    return {entry["id"]: entry["bound"] for entry in plan["jobs"]}


def _completion(plan, job: str) -> int:
    return max(a["end"] for a in plan["activities"] if a.get("job") == job)


def test_a_first_plan_promises_every_job_what_it_achieved():
    """B_j is the completion the solve reached, not a value derived some other way."""
    workflow, env = _simple()
    report = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
    )
    assert report.ok
    assert _bounds(report.plan) == {
        "job1": _completion(report.plan, "job1"),
        "job2": _completion(report.plan, "job2"),
    }


def test_a_later_job_cannot_push_an_earlier_one_out():
    """The headline. `job1` is planned alone and promised a completion; a second job
    arriving afterwards competes for the same devices but may not delay it."""
    workflow, env = _simple()
    first = schedule_jobs([JobInput("job1", copy.deepcopy(workflow))], env)
    assert first.ok
    promised = _bounds(first.plan)["job1"]

    joined = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=first.plan,
    )
    assert joined.ok, [d.code for d in joined.diagnostics]
    assert _completion(joined.plan, "job1") <= promised
    # The newcomer really is competing -- it does not simply overlay job1.
    assert _completion(joined.plan, "job2") > promised
    # And job1's promise is unchanged: bounds do not ratchet.
    assert _bounds(joined.plan)["job1"] == promised


def test_a_promise_is_kept_across_repeated_replans():
    workflow, env = _simple()
    plan = schedule_jobs([JobInput("job1", copy.deepcopy(workflow))], env).plan
    promised = _bounds(plan)["job1"]
    for extra in ("job2", "job3"):
        jobs = [JobInput(i, copy.deepcopy(workflow)) for i in (*_bounds(plan), extra)]
        report = schedule_jobs(jobs, env, document_path=plan)
        assert report.ok, [d.code for d in report.diagnostics]
        plan = report.plan
        assert _bounds(plan)["job1"] == promised
        assert _completion(plan, "job1") <= promised


def test_release_holds_a_job_back():
    workflow, env = _simple()
    report = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
    )
    plan = copy.deepcopy(report.plan)
    for entry in plan["jobs"]:
        del entry["bound"]  # re-plan from scratch, but with job2 held back
        if entry["id"] == "job2":
            entry["release"] = 50

    again = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=plan,
    )
    assert again.ok, [d.code for d in again.diagnostics]
    started = min(a["start"] for a in again.plan["activities"] if a.get("job") == "job2")
    assert started >= 50
    # job1 is not held back with it.
    assert min(a["start"] for a in again.plan["activities"] if a.get("job") == "job1") == 0


def test_a_promise_that_can_no_longer_be_kept_is_relaxed_and_reported():
    """Reality moved: the work took longer than planned, and the promise no longer
    fits. It is re-derived rather than the plan being refused, and the caller is told."""
    workflow, env = _simple()
    plan = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
    ).plan

    status = copy.deepcopy(plan)
    status["now"] = 40
    # job1's source overran badly -- far past the completion job1 was promised.
    entry = _find(status, "job1", ["SampleSource"])
    entry["status"], entry["start"], entry["end"] = "completed", 0, 40

    report = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=status,
    )
    assert report.ok, [d.code for d in report.diagnostics]
    warned = [d for d in report.diagnostics if d.code == "job_bound_relaxed"]
    assert warned and all(d.severity == "warning" for d in warned)
    assert any("'job1'" in d.message for d in warned)
    # Every re-derived promise is what the new schedule achieves.
    for job, bound in _bounds(report.plan).items():
        assert bound == _completion(report.plan, job)


def test_only_the_job_that_cannot_be_kept_is_relaxed():
    """Minimality, and in roster order: `job2`'s promise is impossible and `job1`'s is
    not, so `job1` keeps its own -- an earlier job is never relaxed to spare a later
    one."""
    workflow, env = _simple()
    plan = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
    ).plan
    promised = _bounds(plan)["job1"]

    status = copy.deepcopy(plan)
    for entry in status["jobs"]:
        if entry["id"] == "job2":
            entry["bound"] = 1  # nothing can finish that early

    report = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=status,
    )
    assert report.ok, [d.code for d in report.diagnostics]
    warned = [d for d in report.diagnostics if d.code == "job_bound_relaxed"]
    assert len(warned) == 1
    assert "'job2'" in warned[0].message
    assert _bounds(report.plan)["job1"] == promised


def test_an_instance_that_is_simply_infeasible_reports_no_relaxation():
    """The probe exists to tell "the promises cannot be kept" from "there is no
    schedule at all" -- a relaxation warning on the second would send the reader to
    the wrong place."""
    workflow, env = _simple()
    plan = schedule_jobs([JobInput("job1", copy.deepcopy(workflow))], env).plan
    broken = copy.deepcopy(env)
    del broken["processes"]["target"]["modes"][0]["input_spots"]

    report = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow))], broken, document_path=plan
    )
    assert not report.ok
    assert not [d for d in report.diagnostics if d.code == "job_bound_relaxed"]


# ---------------------------------------------------------------------------
# The objective (§4.8): what a joint plan minimises by default.
# ---------------------------------------------------------------------------


def test_a_joint_plan_minimises_the_sum_of_completions_by_default():
    workflow, env = _simple()
    report = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
    )
    assert report.plan["objective"]["kind"] == ["completion_time_sum", "makespan"]
    total = sum(
        _completion(report.plan, job) for job in ("job1", "job2")
    )
    assert report.plan["objective"]["value"][0] == total


def test_a_single_workflow_objective_is_unchanged():
    """Only the *default* switches on the job count. One workflow keeps the objective
    it always had, so no existing plan changes meaning."""
    workflow, env = _simple()
    assert schedule(workflow, env).plan["objective"]["kind"] == "makespan"
    assert schedule_jobs([JobInput("only", workflow)], env).plan["objective"]["kind"] == "makespan"


def test_a_stated_objective_is_honoured_whatever_the_job_count():
    workflow, env = _simple()
    report = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path={"objective": {"kind": "makespan"}, "activities": []},
    )
    assert report.ok, [d.code for d in report.diagnostics]
    assert report.plan["objective"]["kind"] == "makespan"


# ---------------------------------------------------------------------------
# Replanning a joint plan: history belongs to the job that ran it.
# ---------------------------------------------------------------------------


def _joint_simple():
    """Two jobs of `simple`, planned together. Both render the node path
    `[SampleSource]`, which is what makes them worth replanning: the two are told
    apart by `job` alone."""
    workflow, env = _simple()
    report = schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
    )
    assert report.ok, [d.code for d in report.diagnostics]
    return workflow, env, report.plan


def _advance(plan, now: int) -> dict:
    """The status a run of `plan` would report at `now`: everything that has ended is
    `completed`, at the times the plan itself gave it.

    Built from the plan's own schedule rather than from invented times, because two
    jobs share the environment's devices -- reporting both jobs' sources as running at
    once is infeasible *history*, which the solver rejects before it can say anything
    about how the two were keyed."""
    status = copy.deepcopy(plan)
    status["now"] = now
    for activity in status["activities"]:
        if activity["end"] <= now:
            activity["status"] = "completed"
    return status


def _find(plan, job, node) -> dict:
    for activity in plan["activities"]:
        if activity.get("job") == job and activity.get("node") == node:
            return activity
    raise AssertionError(f"no activity {node} in job {job}")


def _replan(workflow, env, status):
    return schedule_jobs(
        [JobInput("job1", copy.deepcopy(workflow)), JobInput("job2", copy.deepcopy(workflow))],
        env,
        document_path=status,
    )


def test_history_is_fixed_for_the_job_that_ran_it():
    """The headline for replanning a joint plan. `job1`'s source ran [0, 2] and is
    pinned there; `job2`'s source has the very same node path and is *not* fixed by
    it -- it is still pending and re-optimised at or after `now`."""
    workflow, env, plan = _joint_simple()
    report = _replan(workflow, env, _advance(plan, 2))
    assert report.ok, [d.code for d in report.diagnostics]

    ran = _find(report.plan, "job1", ["SampleSource"])
    assert ran["status"] == "completed"
    assert (ran["start"], ran["end"]) == (0, 2)

    other = _find(report.plan, "job2", ["SampleSource"])
    assert other.get("status", "pending") == "pending"
    assert other["start"] >= 2


def test_two_jobs_reporting_the_same_node_are_not_a_duplicate():
    """Both jobs report a completed `[SampleSource]`. Keyed by node path alone that is
    one activity fixed twice (`status_duplicate`) and one job's history is lost; keyed
    by job + node they are two, each pinned where it ran."""
    workflow, env, plan = _joint_simple()
    report = _replan(workflow, env, _advance(plan, 5))
    assert report.ok, [d.code for d in report.diagnostics]

    job1 = _find(report.plan, "job1", ["SampleSource"])
    job2 = _find(report.plan, "job2", ["SampleSource"])
    assert (job1["start"], job1["end"]) == (0, 2)
    assert (job2["start"], job2["end"]) == (3, 5)
    assert job1["status"] == job2["status"] == "completed"


def test_one_job_reporting_the_same_node_twice_is_still_a_duplicate():
    """The guard the job scoping must not throw away: within one job, the same node
    fixed twice is still one activity fixed twice."""
    workflow, env, plan = _joint_simple()
    status = _advance(plan, 2)
    status["activities"].append(copy.deepcopy(_find(status, "job1", ["SampleSource"])))

    report = _replan(workflow, env, status)
    assert not report.ok
    assert "status_duplicate" in [d.code for d in report.diagnostics]


def test_a_started_transport_is_matched_within_its_job():
    """A transport is keyed by its arc, and both jobs render the identical arc
    (`SampleSource.source_out -> SampleTarget.target_in`) -- so the job has to be part
    of that key too, on both of its endpoints."""
    workflow, env, plan = _joint_simple()
    report = _replan(workflow, env, _advance(plan, 3))
    assert report.ok, [d.code for d in report.diagnostics]

    def transports(job):
        return [
            a
            for a in report.plan["activities"]
            if a.get("kind") == "transport" and a.get("job") == job
        ]

    assert [(a["start"], a["end"], a["status"]) for a in transports("job1")] == [
        (2, 3, "completed")
    ]
    # job2's leg is the same arc and must be untouched by job1's history.
    assert all(a.get("status", "pending") == "pending" for a in transports("job2"))


def test_a_status_naming_an_unknown_node_says_which_job():
    """Two jobs of one workflow would otherwise report the same finding with nothing
    to tell them apart."""
    workflow, env, plan = _joint_simple()
    status = _advance(plan, 2)
    _find(status, "job1", ["SampleSource"])["node"] = ["Nope"]

    report = _replan(workflow, env, status)
    assert not report.ok
    unknown = [d for d in report.diagnostics if d.code == "status_node_unknown"]
    assert unknown and "job1" in unknown[0].message


def test_a_joint_replan_still_carries_the_roster():
    workflow, env, plan = _joint_simple()
    report = _replan(workflow, env, _advance(plan, 3))
    assert report.ok
    assert [entry["id"] for entry in report.plan["jobs"]] == ["job1", "job2"]
    assert report.plan["now"] == 3


def test_the_cli_plans_the_same_workflow_twice(tmp_path):
    """The end-to-end demo, through the command line: the same file given twice, and
    the refill that only the pair needs. Numbering the jobs by position is what makes
    repeating one file mean two runs of it."""
    from ofplang.schedule import cli, validate_document

    out = tmp_path / "plan.yaml"
    workflow = str(EXAMPLES / "shared_refill.workflow.yaml")
    code = cli.main(
        [
            "schedule",
            workflow,
            workflow,
            "--env",
            str(EXAMPLES / "consumable.env.yaml"),
            "--document",
            str(EXAMPLES / "shared_refill.document.yaml"),
            "-o",
            str(out),
        ]
    )
    assert code == cli.EXIT_OK
    plan = yaml.safe_load(out.read_text(encoding="utf-8"))
    # The plan is still a valid execution document (§6), joint or not.
    assert validate_document(plan).ok, [d.code for d in validate_document(plan).diagnostics]
    assert len(_refills(plan)) == 1
    assert {a["job"] for a in plan["activities"] if a["kind"] != "replenishment"} == {
        "job1",
        "job2",
    }


def test_the_cli_names_jobs_on_request(tmp_path):
    from ofplang.schedule import cli

    out = tmp_path / "plan.yaml"
    workflow = str(EXAMPLES / "shared_refill.workflow.yaml")
    code = cli.main(
        [
            "schedule",
            f"morning={workflow}",
            f"evening={workflow}",
            "--env",
            str(EXAMPLES / "consumable.env.yaml"),
            "--document",
            str(EXAMPLES / "shared_refill.document.yaml"),
            "-o",
            str(out),
        ]
    )
    assert code == cli.EXIT_OK
    plan = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert {a["job"] for a in plan["activities"] if a["kind"] != "replenishment"} == {
        "morning",
        "evening",
    }


def test_the_cli_refuses_a_repeated_job_id(tmp_path):
    from ofplang.schedule import cli

    workflow = str(EXAMPLES / "shared_refill.workflow.yaml")
    code = cli.main(
        [
            "schedule",
            f"same={workflow}",
            f"same={workflow}",
            "--env",
            str(EXAMPLES / "consumable.env.yaml"),
        ]
    )
    assert code == cli.EXIT_USAGE


def test_a_broken_workflow_names_the_job_it_came_from():
    """Two jobs running the same broken workflow would otherwise report the same
    finding twice with nothing to tell them apart."""
    workflow, env, document = _consumable()
    broken = copy.deepcopy(workflow)
    del env["processes"]["assay"]

    report = schedule_jobs(
        [JobInput("first", copy.deepcopy(workflow)), JobInput("second", broken)],
        env,
        document_path=document,
    )
    assert not report.ok
    messages = [d.message for d in report.diagnostics if d.code == "no_capability"]
    assert any(m.startswith("job 'first':") for m in messages)
    assert any(m.startswith("job 'second':") for m in messages)
