"""The basic-workflow generator: it emits both a v0 workflow and a matching
environment (their source/sink port count and the loader's spots scale with the
branch count). These tests exercise several sizes and the committed sample.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from ofplang.schedule import schedule

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
OUTPUTS = EXAMPLES / "outputs"
_STAGES = ["peal", "dispense", "seal", "thermal_cycle", "rotate"]


def _generator():
    spec = importlib.util.spec_from_file_location("gen_plate_batch", EXAMPLES / "gen_plate_batch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schedule(gen, branches: int, repeats: int, tmp_path: Path):
    wf = tmp_path / "wf.yaml"
    env = tmp_path / "env.yaml"
    wf.write_text(yaml.safe_dump(gen.build_workflow(branches, repeats)), encoding="utf-8")
    env.write_text(yaml.safe_dump(gen.build_env(branches)), encoding="utf-8")
    return schedule(wf, env)


def test_generated_2x2_schedules_with_thermal_parallelism(tmp_path):
    report = _schedule(_generator(), 2, 2, tmp_path)
    assert report.outcome == "optimal"
    # The nested composites flatten to the same atomic graph: 1 source + 1 sink +
    # 2 branches x 2 repeats x 5 stages = 22 processing, plus 11 arcs/transports
    # per branch = 22 -> 44 activities.
    assert len(report.plan["activities"]) == 44
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
        report = _schedule(gen, branches, repeats, tmp_path)
        assert report.outcome in ("optimal", "feasible"), (branches, repeats)


def test_committed_sample_schedules():
    report = schedule(OUTPUTS / "plate_batch.workflow.yaml", OUTPUTS / "plate_batch.env.yaml")
    assert report.outcome == "optimal"


def test_single_source_and_sink_with_per_branch_ports():
    doc = _generator().build_workflow(2, 2)
    source, sink = doc["processes"]["source"], doc["processes"]["sink"]
    assert set(source["outputs"]) == {"plate_1", "plate_2"}
    assert source["objects"] == {"create": ["outputs.plate_1", "outputs.plate_2"]}
    assert set(sink["inputs"]) == {"plate_1", "plate_2"}
    assert sink["objects"] == {"consume": ["inputs.plate_1", "inputs.plate_2"]}
    # Exactly one source node and one sink node in the body.
    node_ids = [n["id"] for n in doc["processes"]["main"]["body"]["nodes"]]
    assert node_ids.count("source") == 1 and node_ids.count("sink") == 1


def test_thermal_cycler_pool_is_configurable(tmp_path):
    gen = _generator()
    # The pool lives only in the environment (device count + thermal_cycle modes).
    for pool in (1, 3):
        env = gen.build_env(2, thermal_cycler_pool=pool)
        devices = [d["id"] for d in env["devices"] if d["id"].startswith("thermal_cycle_")]
        assert len(devices) == pool
        assert len(env["processes"]["thermal_cycle"]["modes"]) == pool
    # The workflow does not take a pool argument; the same workflow schedules
    # against any pool size.
    wf = gen.build_workflow(2, 2)
    (tmp_path / "wf.yaml").write_text(yaml.safe_dump(wf), encoding="utf-8")
    for pool in (1, 3):
        env_path = tmp_path / f"env{pool}.yaml"
        env_path.write_text(yaml.safe_dump(gen.build_env(2, thermal_cycler_pool=pool)), encoding="utf-8")
        assert schedule(tmp_path / "wf.yaml", env_path).outcome == "optimal"


def test_stages_are_elidable_iso():
    doc = _generator().build_workflow(1, 1)
    for stage in _STAGES:
        proc = doc["processes"][stage]
        assert proc.get("traits") == ["elidable_iso"]
        assert set(proc["inputs"]) == {"plate"} and set(proc["outputs"]) == {"plate"}
        assert "objects" not in proc


def test_nested_composite_structure():
    # The repeated structure is expressed with nested composites, not one flat
    # body: repeat_unit (the five-stage chain), branch (repeats of it), main.
    doc = _generator().build_workflow(2, 2)
    procs = doc["processes"]
    assert procs["repeat_unit"]["kind"] == "composite"
    assert procs["branch"]["kind"] == "composite"

    # repeat_unit threads one plate through the five stages and returns the last.
    ru = procs["repeat_unit"]["body"]
    assert [n["id"] for n in ru["nodes"]] == ["peal", "dispense", "seal", "thermal", "rotate"]
    assert ru["nodes"][0]["state"] == {"plate": {"from": "inputs.plate"}}
    assert ru["nodes"][1]["state"] == {"plate": {"from": "peal.plate"}}
    assert ru["returns"] == {"plate": {"from": "rotate.plate"}}

    # branch chains `repeats` repeat_unit invocations plate-to-plate.
    br = procs["branch"]["body"]
    assert [n["id"] for n in br["nodes"]] == ["rep1", "rep2"]
    assert all(n["process"] == "repeat_unit" for n in br["nodes"])
    assert br["nodes"][1]["state"] == {"plate": {"from": "rep1.plate"}}
    assert br["returns"] == {"plate": {"from": "rep2.plate"}}

    # main invokes one branch per source port and gathers them into the sink.
    main_nodes = procs["main"]["body"]["nodes"]
    assert [n["id"] for n in main_nodes] == ["source", "b1", "b2", "sink"]
    assert main_nodes[1]["state"] == {"plate": {"from": "source.plate_1"}}
    assert main_nodes[3]["state"] == {
        "plate_1": {"from": "b1.plate"},
        "plate_2": {"from": "b2.plate"},
    }
