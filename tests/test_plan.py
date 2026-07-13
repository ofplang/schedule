"""The rendered execution plan must itself be a valid execution document."""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule import validate_document
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.plan import render_plan, to_yaml
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _plan(name):
    wf, _ = parse_workflow(EXAMPLES / f"{name}.workflow.yaml")
    env, _ = load_environment(EXAMPLES / f"{name}.env.yaml")
    inst, _ = build_instance(wf, env)
    sol = solve(inst)
    return render_plan(inst, sol, workflow=f"{name}.workflow.yaml", environment=f"{name}.env.yaml")


def _assert_valid_document(doc, tmp_path):
    out = tmp_path / "plan.yaml"
    out.write_text(to_yaml(doc), encoding="utf-8")
    result = validate_document(out)
    assert result.ok, [(d.code, d.path) for d in result.errors]


def test_job_sample_plan_is_valid_document(tmp_path):
    doc = _plan("job_sample")
    assert doc["outcome"] == "optimal"
    assert doc["objective"] == {"kind": "makespan", "value": 5}
    kinds = [a["kind"] for a in doc["activities"]]
    assert kinds.count("processing") == 2 and kinds.count("transport") == 1
    _assert_valid_document(doc, tmp_path)


def test_reformatter_plan_is_valid_document(tmp_path):
    doc = _plan("reformatter")
    assert doc["outcome"] == "optimal"
    assert len(doc["activities"]) == 8 + 12
    _assert_valid_document(doc, tmp_path)
