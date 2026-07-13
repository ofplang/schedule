#!/usr/bin/env python3
"""Generate a parametric "plate batch" as an ofplang v0 workflow YAML plus its
matching execution environment.

Ported (structure only, no benchmarking) from ofp-scheduler's
`examples/basic_workflow_demo.py`: a single source fans out to `--branches`
branches, each running a chain of stages repeated `--repeats` times, and a single
sink gathers them back —

    source ={plate_b}=> [peal -> dispense -> seal -> thermal_cycle -> rotate] x repeats =>{plate_b}= sink

The repeated structure is expressed with **nested composites** rather than one
flat body: `repeat_unit` is the five-stage chain (one plate in/out), `branch`
chains `--repeats` of those, and `main` fans a `branch` per port out of the
source and gathers them into the sink. The scheduler flattens this back to the
same atomic graph (so the schedule is identical), while the workflow stays small
and its intent is explicit. Plan node paths are hierarchical, e.g. `b1/rep1/peal`.

The source has one output port per branch and the sink one input port per branch,
so their signatures — and the loader's spots — scale with the branch count. The
environment therefore cannot be a single fixed file; this script emits a matching
`env.yaml` alongside the `workflow.yaml`. The single-device stages
(peal/dispense/seal/rotate) are contended across branches, while thermal_cycle has
a `--thermal-cycler-pool`-device pool (default 2, an environment-only knob) the scheduler
spreads parallel branches over via mode selection. Stages are `elidable_iso` (a
single `plate` port passes through).

Usage:
    python examples/gen_plate_batch.py --branches 2 --repeats 2 --out-dir examples/outputs
    python examples/gen_plate_batch.py --branches 3 --repeats 2   # both docs to stdout
"""

from __future__ import annotations

import argparse
import os
import sys

import yaml

# Per-repeat stage chain; each stage's process definition name equals the list
# entry. Node id base (`thermal_cycle` nodes are named `thermal`), executing
# device, and processing duration:
_STAGES = ["peal", "dispense", "seal", "thermal_cycle", "rotate"]
_NODE_BASE = {"thermal_cycle": "thermal"}
_STAGE_DEVICE = {"peal": "peal", "dispense": "dispense", "seal": "seal", "rotate": "rotate"}
_STAGE_DURATION = {"peal": 1, "dispense": 3, "seal": 1, "thermal_cycle": 10, "rotate": 1}
# Default thermal_cycle pool size (parallel via mode selection); override with
# --thermal-cycler-pool. The pool lives entirely in the environment, so the workflow is
# unaffected by it.
_DEFAULT_THERMAL_CYCLER_POOL = 2


def _plate_ports(*names: str) -> dict:
    return {name: {"type": "Plate", "phase": "data"} for name in names}


# --------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------


def build_workflow(branches: int, repeats: int) -> dict:
    """Build the v0 workflow with nested composites: `repeat_unit` (the five-stage
    chain), `branch` (`repeats` of those chained), and `main` (a `branch` per
    source port, gathered into the sink)."""
    if branches < 1 or repeats < 1:
        raise ValueError("branches and repeats must be >= 1")

    branch_ports = [f"plate_{b}" for b in range(1, branches + 1)]
    processes: dict = {
        # Source creates one plate per branch; sink consumes them all.
        "source": {
            "kind": "atomic",
            "outputs": _plate_ports(*branch_ports),
            "objects": {"create": [f"outputs.{p}" for p in branch_ports]},
        },
        "sink": {
            "kind": "atomic",
            "inputs": _plate_ports(*branch_ports),
            "objects": {"consume": [f"inputs.{p}" for p in branch_ports]},
        },
    }
    # Stages pass the plate through unchanged: elidable_iso on a single `plate`
    # port (v0 infers the same-name identity map, so no `objects` section).
    for stage in _STAGES:
        processes[stage] = {
            "kind": "atomic",
            "traits": ["elidable_iso"],
            "inputs": _plate_ports("plate"),
            "outputs": _plate_ports("plate"),
        }

    def _chain(node_specs: list[tuple[str, str]]) -> dict:
        """A composite that threads one `plate` through the given (node id,
        process) list in order: the first reads the composite input, each next
        reads its predecessor, and the last is returned."""
        nodes: list[dict] = []
        src = "inputs.plate"
        for node_id, process in node_specs:
            nodes.append({"id": node_id, "process": process, "state": {"plate": {"from": src}}})
            src = f"{node_id}.plate"
        return {
            "kind": "composite",
            "inputs": _plate_ports("plate"),
            "outputs": _plate_ports("plate"),
            "body": {"nodes": nodes, "returns": {"plate": {"from": src}}},
        }

    # repeat_unit: peal -> dispense -> seal -> thermal -> rotate (thermal_cycle's
    # node is named `thermal`, per _NODE_BASE).
    processes["repeat_unit"] = _chain([(_NODE_BASE.get(s, s), s) for s in _STAGES])
    # branch: repeat_unit x repeats, chained plate-to-plate.
    processes["branch"] = _chain([(f"rep{r}", "repeat_unit") for r in range(1, repeats + 1)])

    # main: source fans a branch per port; the sink gathers each branch's output.
    main_nodes: list[dict] = [{"id": "source", "process": "source"}]
    for b in range(1, branches + 1):
        main_nodes.append({"id": f"b{b}", "process": "branch", "state": {"plate": {"from": f"source.plate_{b}"}}})
    sink_state = {f"plate_{b}": {"from": f"b{b}.plate"} for b in range(1, branches + 1)}
    main_nodes.append({"id": "sink", "process": "sink", "state": sink_state})
    processes["main"] = {
        "kind": "composite",
        "inputs": {},
        "outputs": {},
        "body": {"nodes": main_nodes, "returns": {}},
    }

    return {
        "spec_version": "0.0",
        "types": {"Plate": {"domain": "object"}},
        "processes": processes,
        "entry": "main",
    }


# --------------------------------------------------------------------------
# Environment (matches the workflow's branch count)
# --------------------------------------------------------------------------


def build_env(branches: int, thermal_cycler_pool: int = _DEFAULT_THERMAL_CYCLER_POOL) -> dict:
    """Build the execution environment for `branches` branches with a
    `thermal_cycler_pool`-device thermal_cycle pool. The loader (used by source/sink)
    gets one spot per branch; the single-device stages are fixed."""
    if branches < 1:
        raise ValueError("branches must be >= 1")
    if thermal_cycler_pool < 1:
        raise ValueError("thermal_cycler_pool must be >= 1")

    loader_spots = [f"s{b}" for b in range(1, branches + 1)]
    devices = [{"id": "loader", "spots": loader_spots}]
    for name in ("peal", "dispense", "seal", "rotate"):
        devices.append({"id": name, "spots": ["core"]})
    for k in range(1, thermal_cycler_pool + 1):
        devices.append({"id": f"thermal_cycle_{k}", "spots": ["core"]})

    def move(frm: str, to: str) -> dict:
        return {"transporter": "transport", "from": frm, "to": to, "duration": 1}

    transports: list[dict] = []
    for b in range(1, branches + 1):
        transports.append(move(f"loader.s{b}", "peal.core"))    # source -> first stage
        transports.append(move("rotate.core", f"loader.s{b}"))  # last stage -> sink
    transports += [move("peal.core", "dispense.core"), move("dispense.core", "seal.core")]
    for k in range(1, thermal_cycler_pool + 1):
        transports.append(move("seal.core", f"thermal_cycle_{k}.core"))
        transports.append(move(f"thermal_cycle_{k}.core", "rotate.core"))
    transports.append(move("rotate.core", "peal.core"))         # next repeat

    def stage_mode(device: str, duration: int) -> dict:
        return {
            "devices": [device],
            "duration": duration,
            "input_spots": {"plate": f"{device}.core"},
            "output_spots": {"plate": f"{device}.core"},
        }

    processes: dict = {
        "source": {
            "modes": [
                {
                    "devices": ["loader"],
                    "duration": 1,
                    "output_spots": {f"plate_{b}": f"loader.s{b}" for b in range(1, branches + 1)},
                }
            ]
        },
        "sink": {
            "modes": [
                {
                    "devices": ["loader"],
                    "duration": 1,
                    "input_spots": {f"plate_{b}": f"loader.s{b}" for b in range(1, branches + 1)},
                }
            ]
        },
    }
    for stage in ("peal", "dispense", "seal", "rotate"):
        processes[stage] = {"modes": [stage_mode(_STAGE_DEVICE[stage], _STAGE_DURATION[stage])]}
    processes["thermal_cycle"] = {
        "modes": [stage_mode(f"thermal_cycle_{k}", _STAGE_DURATION["thermal_cycle"]) for k in range(1, thermal_cycler_pool + 1)]
    }

    return {
        "time": {"unit": "second"},
        "devices": devices,
        "transporters": [{"id": "transport"}],
        "transports": transports,
        "processes": processes,
        "objective": {"kind": "makespan"},
    }


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a basic-workflow v0 YAML and its environment.")
    parser.add_argument("--branches", type=int, default=2, help="number of parallel branches")
    parser.add_argument("--repeats", type=int, default=2, help="stage-chain repeats per branch")
    parser.add_argument("--thermal-cycler-pool", type=int, default=_DEFAULT_THERMAL_CYCLER_POOL, help="thermal_cycle device count (environment only)")
    parser.add_argument("--out-dir", metavar="DIR", help="write <name>.workflow.yaml and <name>.env.yaml here (default: stdout)")
    parser.add_argument("--name", default="plate_batch", help="base file name when --out-dir is given")
    args = parser.parse_args(argv)

    workflow = build_workflow(args.branches, args.repeats)
    env = build_env(args.branches, args.thermal_cycler_pool)

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        wf_path = os.path.join(args.out_dir, f"{args.name}.workflow.yaml")
        env_path = os.path.join(args.out_dir, f"{args.name}.env.yaml")
        with open(wf_path, "w", encoding="utf-8") as f:
            f.write(_dump(workflow))
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(_dump(env))
        print(f"wrote {wf_path} and {env_path}", file=sys.stderr)
    else:
        sys.stdout.write("# === workflow ===\n" + _dump(workflow))
        sys.stdout.write("# === environment ===\n" + _dump(env))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
