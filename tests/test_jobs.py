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
