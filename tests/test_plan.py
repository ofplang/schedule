"""The rendered execution plan must itself be a valid execution document."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ofplang.schedule import validate_document
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.plan import render_plan, to_yaml
from ofplang.schedule.scheduler.plancheck import check_plan_inventories
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
OUTPUTS = EXAMPLES / "outputs"


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


def test_simple_plan_is_valid_document(tmp_path):
    doc = _plan("simple")
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


# The plan YAML committed under outputs/ for each example (a saved solve result)
# must be a valid execution document (§9.2), so the tracked artifacts stay honest.
_COMMITTED_PLANS = [
    "simple",
    "reformatter",
    "two_arms",
    "plate_batch",
    "consumable",
    "storage",
    # A joint plan of two jobs (§6.11): its activities carry `job`, so it also pins
    # that the validator accepts one.
    "shared_refill",
    # A joint plan whose jobs carry their own boundary (§6.8 per job).
    "shared_bay",
]


@pytest.mark.parametrize("name", _COMMITTED_PLANS)
def test_committed_plan_is_valid_document(name):
    path = OUTPUTS / f"{name}.plan.yaml"
    assert path.is_file(), f"missing committed plan: {path}"
    result = validate_document(path)
    assert result.ok, [(d.code, d.path) for d in result.errors]


def test_the_committed_consumable_plan_is_executable():
    """Valid is not enough for a plan that consumes: it also has to be *runnable*.

    A document can satisfy §9.2 in every respect and still take more out of a stock
    than it puts in -- that is exactly what 0.2.0 and 0.2.1 emitted, and validating
    the shape would never have said so. The committed artifact is the place to pin
    it, because it is the one plan a reader is invited to trust.
    """
    plan = yaml.safe_load((OUTPUTS / "consumable.plan.yaml").read_text(encoding="utf-8"))
    env, result = load_environment(EXAMPLES / "consumable.env.yaml")
    assert env is not None, [d.code for d in result.diagnostics]
    assert check_plan_inventories(plan, env, plan.get("inventories")) == []


def test_the_committed_consumable_plan_keeps_its_refill_off_the_reader():
    """A refill holds the device it fills, so it cannot overlap an assay on it. The
    committed chart draws that as a ghost bar on the reader's lane; this is the same
    claim, checked."""
    plan = yaml.safe_load((OUTPUTS / "consumable.plan.yaml").read_text(encoding="utf-8"))
    refills = [a for a in plan["activities"] if a["kind"] == "replenishment"]
    assert refills, "the committed plan should carry a refill"
    on_reader = [
        a
        for a in plan["activities"]
        if a.get("kind") == "processing" and "reader" in (a.get("devices") or [])
    ]
    assert on_reader
    for refill in refills:
        assert refill["device"] == "reader"
        for other in on_reader:
            assert refill["end"] <= other["start"] or other["end"] <= refill["start"]
