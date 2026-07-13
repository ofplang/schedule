"""The basic-workflow generator + its shared environment.

The generator (examples/gen_basic_workflow.py) emits fixed-signature processes, so
one environment (examples/basic_workflow.env.yaml) schedules any branch/repeat
count. These tests exercise that: several sizes schedule, and the committed
sample stays optimal.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from ofplang.schedule import schedule

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
ENV = EXAMPLES / "basic_workflow.env.yaml"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_basic_workflow", EXAMPLES / "gen_basic_workflow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schedule_doc(doc: dict, tmp_path: Path):
    wf = tmp_path / "wf.yaml"
    wf.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return schedule(wf, ENV)


def test_generated_2x2_schedules_with_thermal_parallelism(tmp_path):
    report = _schedule_doc(_generator().build_workflow(2, 2), tmp_path)
    assert report.outcome == "optimal"
    # 2 branches x (source + 2 repeats x 5 stages + sink) = 24 processing,
    # plus 11 arcs/transports per branch = 22 -> 46 activities.
    assert len(report.plan["activities"]) == 46
    # The two-device thermal pool is used in parallel (mode selection).
    used = {
        d
        for a in report.plan["activities"]
        if a["kind"] == "processing" and a.get("process") == "thermal_cycle"
        for d in a.get("devices", [])
    }
    assert used == {"thermal_cycle_1", "thermal_cycle_2"}


def test_generator_is_parametric(tmp_path):
    gen = _generator()
    for branches, repeats in [(1, 1), (3, 1), (2, 3)]:
        report = _schedule_doc(gen.build_workflow(branches, repeats), tmp_path)
        assert report.outcome in ("optimal", "feasible"), (branches, repeats)


def test_committed_sample_schedules():
    report = schedule(EXAMPLES / "outputs" / "basic_workflow.workflow.yaml", ENV)
    assert report.outcome == "optimal"


def test_stages_are_elidable_iso():
    doc = _generator().build_workflow(1, 1)
    for stage in ["peal", "dispense", "seal", "thermal_cycle", "rotate"]:
        proc = doc["processes"][stage]
        assert proc.get("traits") == ["elidable_iso"]
        # Same-name `plate` port in and out; no `objects` (identity map inferred).
        assert set(proc["inputs"]) == {"plate"} and set(proc["outputs"]) == {"plate"}
        assert "objects" not in proc
    # Source still creates and sink still consumes (single `plate` port).
    assert doc["processes"]["source"]["objects"] == {"create": ["outputs.plate"]}
    assert doc["processes"]["sink"]["objects"] == {"consume": ["inputs.plate"]}
