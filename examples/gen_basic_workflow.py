#!/usr/bin/env python3
"""Generate a parametric "basic workflow" as an ofplang v0 workflow YAML.

Ported (structure only, no benchmarking) from ofp-scheduler's
`examples/basic_workflow_demo.py`: a set of independent branches, each running a
chain of stages repeated `repeats` times —

    source -> [peal -> dispense -> seal -> thermal_cycle -> rotate] x repeats -> sink

Every stage invokes a fixed-signature atomic process (`plate_in` / `plate_out`),
so one shared execution environment (examples/basic_workflow.env.yaml) works for
any branch/repeat count. The stages are `elidable_iso` (the plate passes through
with its identity preserved), while the source creates the plate and the sink
consumes it. Branches contend on the shared single-device stages
(peal/dispense/seal/rotate and the loader); the environment gives thermal_cycle a
small device pool, so the scheduler can run those steps in parallel via mode
selection.

Usage:
    python examples/gen_basic_workflow.py --branches 2 --repeats 2 -o out.yaml
    python examples/gen_basic_workflow.py            # 2x2 to stdout
"""

from __future__ import annotations

import argparse
import sys

import yaml

# Per-repeat stage chain and each stage's process definition name. The node id
# base is the process name (kept short); `thermal_cycle` nodes are named `thermal`.
_STAGES = ["peal", "dispense", "seal", "thermal_cycle", "rotate"]
_NODE_BASE = {"thermal_cycle": "thermal"}


def _atomic(inputs: list[str], outputs: list[str]) -> dict:
    """An atomic Plate process: consumes every input, creates every output.
    Used for the pure source (creates a plate) and sink (consumes it)."""
    proc: dict = {"kind": "atomic"}
    if inputs:
        proc["inputs"] = {name: {"type": "Plate", "phase": "data"} for name in inputs}
    if outputs:
        proc["outputs"] = {name: {"type": "Plate", "phase": "data"} for name in outputs}
    objects: dict = {}
    if inputs:
        objects["consume"] = [f"inputs.{name}" for name in inputs]
    if outputs:
        objects["create"] = [f"outputs.{name}" for name in outputs]
    proc["objects"] = objects
    return proc


def _iso_stage() -> dict:
    """A 1-in/1-out stage that passes the same plate through unchanged, i.e.
    `elidable_iso` (identity-preserving), not consume+create. The ports are named
    plate_in/plate_out rather than sharing one name, so the identity map is
    written explicitly (elidable_iso's implicit same-name inference does not apply
    to differently-named ports)."""
    return {
        "kind": "atomic",
        "traits": ["elidable_iso"],
        "inputs": {"plate_in": {"type": "Plate", "phase": "data"}},
        "outputs": {"plate_out": {"type": "Plate", "phase": "data"}},
        "objects": {"map": {"outputs.plate_out": "inputs.plate_in"}},
    }


def build_workflow(branches: int, repeats: int) -> dict:
    """Build the v0 workflow document for `branches` chains of length `repeats`."""
    if branches < 1 or repeats < 1:
        raise ValueError("branches and repeats must be >= 1")

    # Fixed process definitions (independent of branches/repeats).
    processes: dict = {
        "source": _atomic([], ["plate_out"]),
        "sink": _atomic(["plate_in"], []),
    }
    for stage in _STAGES:
        processes[stage] = _iso_stage()

    # Composite body: one independent chain per branch.
    nodes: list[dict] = []
    for b in range(1, branches + 1):
        nodes.append({"id": f"source_b{b}", "process": "source"})
        prev = f"source_b{b}"
        for r in range(1, repeats + 1):
            for stage in _STAGES:
                base = _NODE_BASE.get(stage, stage)
                node_id = f"{base}_b{b}_r{r}"
                nodes.append(
                    {
                        "id": node_id,
                        "process": stage,
                        "state": {"plate_in": {"from": f"{prev}.plate_out"}},
                    }
                )
                prev = node_id
        nodes.append(
            {
                "id": f"sink_b{b}",
                "process": "sink",
                "state": {"plate_in": {"from": f"{prev}.plate_out"}},
            }
        )

    processes["main"] = {
        "kind": "composite",
        "inputs": {},
        "outputs": {},
        "body": {"nodes": nodes, "returns": {}},
    }

    return {
        "spec_version": "0.0",
        "types": {"Plate": {"domain": "object"}},
        "processes": processes,
        "entry": "main",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a basic-workflow v0 YAML.")
    parser.add_argument("--branches", type=int, default=2, help="number of parallel branches")
    parser.add_argument("--repeats", type=int, default=2, help="stage-chain repeats per branch")
    parser.add_argument("-o", "--out", metavar="FILE", help="write here (default: stdout)")
    args = parser.parse_args(argv)

    doc = build_workflow(args.branches, args.repeats)
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
