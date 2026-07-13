"""Minimal v0 workflow reader for the scheduler.

The scheduler reads the workflow itself (decision D17) instead of depending on
`ofplang.validate`, extracting only what scheduling needs: which processes are
atomic, each port's Object-bearing-ness (§5), and the expanded node graph
(processing activities with node paths, Object-bearing arcs, and precedence).
The workflow is assumed to be valid v0; this reader only diagnoses the parts the
scheduler cannot handle (structured nodes, nested composites, a missing entry).

Binding semantics follow §11: a `state` binding carries an Object-bearing linear
input (so it is a transport arc), while a `bind` binding is Pure Data (a
precedence dependency only).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.scheduler.model import (
    Arc,
    AtomicProcess,
    Endpoint,
    NodeInvocation,
    Port,
    Workflow,
)
from ofplang.schedule.validation import errors

# v0 built-in primitive Data types (no Object slots, §7.1).
_PRIMITIVES = {"Bool", "Int", "Float", "String"}
# Structured node kinds — all out of scope for the scheduler (D6, §17-20).
_STRUCTURED_KINDS = {"map", "fold", "do_while", "branch"}


def parse_workflow(source) -> tuple[Workflow | None, Diagnostics]:
    """Parse the v0 workflow at `source` into a schedulable `Workflow`.

    Returns `(workflow, diagnostics)`; the workflow is None when a blocking
    diagnostic (unparseable document or no entry) is raised.
    """
    diags = Diagnostics()
    data = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        diags.error(errors.WRONG_TYPE, "workflow must be a mapping")
        return None, diags

    # Type domains drive Object-bearing detection; `processes` holds the defs.
    domains = {
        name: (spec.get("domain") if isinstance(spec, dict) else None)
        for name, spec in (data.get("types") or {}).items()
    }
    procs = data.get("processes") or {}

    # Atomic process signatures (port name -> Object-bearing flag).
    atomic: dict[str, AtomicProcess] = {}
    for name, proc in procs.items():
        if isinstance(proc, dict) and proc.get("kind") == "atomic":
            atomic[name] = _atomic_signature(name, proc, domains)

    entry = data.get("entry") or ("main" if "main" in procs else None)
    if entry is None or entry not in procs:
        diags.error(errors.NO_ENTRY_PROCESS, "workflow has no entry process")
        return None, diags

    entry_proc = procs[entry]
    if entry_proc.get("kind") != "composite":
        # A degenerate single-atomic entry: one activity, no arcs.
        if entry in atomic:
            return Workflow((NodeInvocation((entry,), entry),), (), (), {entry: atomic[entry]}), diags
        diags.error(errors.UNSUPPORTED_FEATURE, f"entry process {entry!r} is not a composite")
        return None, diags

    activities, arcs, precedence, used = _expand_body(entry_proc, procs, atomic, diags)
    return Workflow(tuple(activities), tuple(arcs), tuple(precedence), used), diags


def _atomic_signature(name: str, proc: dict, domains: dict[str, str | None]) -> AtomicProcess:
    def ports(section: str) -> tuple[Port, ...]:
        return tuple(
            Port(port_name, _object_bearing(spec.get("type", ""), domains))
            for port_name, spec in (proc.get(section) or {}).items()
        )

    return AtomicProcess(name, ports("inputs"), ports("outputs"))


def _object_bearing(type_expr: str, domains: dict[str, str | None]) -> bool:
    """True iff a value of this type carries an Object slot (§5.2): an Object
    nominal type, or an Array (possibly nested) whose element type does."""
    t = type_expr.strip()
    if t.startswith("Array<") and t.endswith(">"):
        return _object_bearing(t[len("Array<") : -1], domains)
    if t in _PRIMITIVES:
        return False
    return domains.get(t) == "object"


def _expand_body(entry_proc, procs, atomic, diags):
    """Expand the entry composite's body into atomic activities plus arcs and
    precedence. Nested composites are not expanded yet (diagnosed as unsupported);
    the initial fixtures are single-level."""
    activities: list[NodeInvocation] = []
    arcs: list[Arc] = []
    precedence: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    used: dict[str, AtomicProcess] = {}

    body = entry_proc.get("body") or {}
    for node in body.get("nodes") or []:
        node_id = node.get("id")
        if node.get("kind") in _STRUCTURED_KINDS:
            diags.error(
                errors.UNSUPPORTED_FEATURE,
                f"structured node {node_id!r} (kind {node.get('kind')!r}) is out of scope",
            )
            continue
        pname = node.get("process")
        if pname not in procs:
            diags.error(errors.PROCESS_NOT_DEFINED, f"node {node_id!r} invokes undefined process {pname!r}")
            continue
        if procs[pname].get("kind") == "composite":
            diags.error(
                errors.UNSUPPORTED_FEATURE,
                f"nested composite invocation ({node_id!r} -> {pname!r}) is not supported yet",
            )
            continue

        path = (node_id,)
        activities.append(NodeInvocation(path, pname))
        used[pname] = atomic[pname]

        # `state` = Object-bearing arc + precedence; `bind` = precedence only.
        for section in ("state", "bind"):
            for port, binding in (node.get(section) or {}).items():
                src = _source_ref(binding)
                if src is None:
                    continue  # a literal `value`, or a composite `inputs.*` source
                src_node, src_port = src
                precedence.append(((src_node,), path))
                if section == "state":
                    arcs.append(Arc(Endpoint((src_node,), src_port), Endpoint(path, port)))

    return activities, arcs, precedence, used


def _source_ref(binding) -> tuple[str, str] | None:
    """Resolve a binding's `from: <node>.<port>` to (node, port). Returns None for
    a literal value or a composite-input source (`inputs.*`), which is not a
    node-to-node arc within the entry body."""
    if not isinstance(binding, dict):
        return None
    frm = binding.get("from")
    if not isinstance(frm, str) or "." not in frm:
        return None
    left, right = frm.split(".", 1)
    if left == "inputs":
        return None
    return left, right
