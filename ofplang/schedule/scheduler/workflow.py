"""Minimal v0 workflow reader for the scheduler.

The scheduler reads the workflow itself (decision D17) instead of depending on
`ofplang.validate`, extracting only what scheduling needs: which processes are
atomic, each port's Object-bearing-ness (§5), and the expanded node graph
(processing activities with node paths, Object-bearing arcs, and precedence).
Composite invocations — including nested ones — are flattened by splicing
dataflow across the composite boundary (see `_Expander`). The workflow is assumed
to be valid v0; this reader only diagnoses the parts the scheduler cannot handle
(structured nodes, recursive composite definitions, a missing entry).

Binding semantics follow §11: a `state` binding carries an Object-bearing linear
input (so it is a transport arc), while a `bind` binding is Pure Data (a
precedence dependency only).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.scheduler.model import (
    Arc,
    AtomicProcess,
    CompositeIO,
    Endpoint,
    NodeInvocation,
    NodePath,
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
        # A degenerate single-atomic entry: one activity, no arcs. Its own
        # Object-bearing ports are the workflow's boundary connections.
        if entry in atomic:
            sig = atomic[entry]
            path = (entry,)
            entry_inputs = {p.name: Endpoint(path, p.name) for p in sig.inputs if p.object_bearing}
            exit_outputs = {p.name: Endpoint(path, p.name) for p in sig.outputs if p.object_bearing}
            in_ports = {p.name: p.object_bearing for p in sig.inputs}
            out_ports = {p.name: p.object_bearing for p in sig.outputs}
            return (
                Workflow(
                    (NodeInvocation(path, entry),), (), (), {entry: sig},
                    entry_inputs, exit_outputs, in_ports, out_ports,
                ),
                diags,
            )
        diags.error(errors.UNSUPPORTED_FEATURE, f"entry process {entry!r} is not a composite")
        return None, diags

    (
        activities, arcs, precedence, used, entry_inputs, exit_outputs,
        data_arcs, data_entry_inputs, data_literals, composites,
    ) = _expand_body(entry, entry_proc, procs, atomic, diags)
    # The entry composite's declared ports, tagged Object-bearing (for classifying
    # `interface` bindings). Values are `{type, phase}` specs like an atomic's.
    in_ports = {n: _object_bearing((s or {}).get("type", ""), domains) for n, s in (entry_proc.get("inputs") or {}).items()}
    out_ports = {n: _object_bearing((s or {}).get("type", ""), domains) for n, s in (entry_proc.get("outputs") or {}).items()}
    return (
        Workflow(
            tuple(activities), tuple(arcs), tuple(precedence), used,
            entry_inputs, exit_outputs, in_ports, out_ports,
            # Pure Data port-level dataflow for the runner (D26-0); the scheduler
            # does not read these, so the plan is unaffected.
            data_arcs=tuple(data_arcs), data_entry_inputs=data_entry_inputs,
            data_literals=data_literals,
            # Nested composite invocation boundaries for the runner's contract checks
            # (D34); value-independent, so the plan is unaffected.
            composites=composites,
        ),
        diags,
    )


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


def _expand_body(entry_name, entry_proc, procs, atomic, diags):
    """Flatten the entry composite into atomic activities, Object-bearing arcs, and
    precedence edges, following nested composites (see `_Expander`)."""
    exp = _Expander(procs, atomic, diags)
    # The entry's own inputs are the workflow's boundary inputs: seed the entry
    # scope so `inputs.X` resolves to an `_EntryInput(X)` marker, which propagates
    # into nested composites and is recorded at the atomic that consumes it.
    entry_env = {name: _EntryInput(name) for name in (entry_proc.get("inputs") or {})}
    exp.expand(entry_proc, (), entry_env, (entry_name,))

    # The entry's `returns` are the workflow's boundary outputs: resolve each to the
    # atomic that produces it (an entry input returned verbatim resolves to an
    # `_EntryInput` marker — a pass-through, left out of `exit_outputs`).
    siblings = _body_nodes(entry_proc)
    for out_name, source in _returns(entry_proc).items():
        producer = exp._resolve(_parse_ref(source), (), entry_env, siblings, (entry_name,))
        if isinstance(producer, _Producer):
            exp.exit_outputs[out_name] = Endpoint(producer.path, producer.port)

    # Pure Data fan-in can occasionally add the same precedence edge twice; keep
    # each edge once, in first-seen order for a deterministic activity ordering.
    seen: set[tuple[NodePath, NodePath]] = set()
    precedence: list[tuple[NodePath, NodePath]] = []
    for edge in exp.precedence:
        if edge not in seen:
            seen.add(edge)
            precedence.append(edge)
    return (
        exp.activities, exp.arcs, precedence, exp.used, exp.entry_inputs, exp.exit_outputs,
        # Pure Data port-level dataflow and static literals, for the runner only (D26-0).
        exp.data_arcs, exp.data_entry_inputs, exp.data_literals,
        # Nested composite invocation boundaries, for the runner's contract checks (D34).
        exp.composites,
    )


@dataclass(frozen=True)
class _Producer:
    """The concrete atomic output that ultimately feeds a reference, after
    resolving through any composite boundaries (a `returns` map, or a composite's
    own input)."""

    path: NodePath
    port: str


@dataclass(frozen=True)
class _EntryInput:
    """A reference that resolves to one of the *workflow's* own entry inputs, i.e.
    a `main`-level input port with no in-body producer. Carried (instead of a
    `_Producer`) so the atomic that ultimately consumes it can be recorded as a
    boundary connection (`Workflow.entry_inputs`); the name survives nesting because
    the marker propagates through composite input environments."""

    name: str


@dataclass(frozen=True)
class _Literal:
    """A binding to a static literal value (`bind: {port: {value: ...}}`, §11), which
    has no in-body producer. Carried like `_EntryInput` so the atomic that ultimately
    consumes it can be recorded (`Workflow.data_literals`); the value survives nesting
    because the marker propagates through composite input environments. Literals are
    Pure Data (`bind` only) and, like `data_arcs`, are recorded for the sibling
    `ofplang-run` runner alone -- the scheduler never reads them."""

    value: object


class _Expander:
    """Flattens the entry composite into atomic activities, splicing dataflow
    across composite boundaries.

    Node bindings and returns use body dataflow references (v0 §2.6.1): `inputs.X`
    names an input port of the current composite, `Node.Y` an output of a direct
    child. Flattening a nested composite therefore means:

    - inward — a child's `inputs.X` reference resolves to whatever the enclosing
      invocation bound to X;
    - outward — an enclosing `Child.Y` reference resolves through the child's
      `returns[Y]` to the atomic that actually produces the value.

    Both directions are handled by `_resolve`, which walks these boundaries down to
    the producing atomic. Only atomic invocations become activities; a `state`
    binding on an atomic input yields an Object-bearing transport arc, a `bind`
    binding only a precedence edge (v0 §11)."""

    def __init__(self, procs: dict, atomic: dict[str, AtomicProcess], diags: Diagnostics) -> None:
        self.procs = procs
        self.atomic = atomic
        self.diags = diags
        self.activities: list[NodeInvocation] = []
        self.arcs: list[Arc] = []
        self.precedence: list[tuple[NodePath, NodePath]] = []
        self.used: dict[str, AtomicProcess] = {}
        # Pure Data (`bind`) port-level dataflow, recorded for the sibling
        # `ofplang-run` runner only (D26-0; see `model.Workflow.data_arcs`). The
        # scheduler itself never reads these -- they are the Pure Data mirror of
        # `arcs` / `entry_inputs`, capturing the output-port -> input-port mapping
        # that a node-level `precedence` edge would otherwise throw away, so the
        # runner can route Pure Data *values* along it. Populating them must not
        # change the plan the solver produces.
        self.data_arcs: list[Arc] = []
        self.data_entry_inputs: dict[str, Endpoint] = {}
        # Static literal bindings (`bind: {port: {value: ...}}`, §11) keyed by the
        # consuming atomic input endpoint. Recorded for the runner's value layer only
        # (like data_arcs); the scheduler never reads them.
        self.data_literals: dict[Endpoint, object] = {}
        # Boundary connections (SPEC §6.8): main input port -> consuming atomic
        # endpoint (recorded from `state` bindings that resolve to an entry input),
        # and main output port -> producing atomic endpoint (from the entry's
        # `returns`). Only Object-bearing ports land here (state = Object-bearing).
        self.entry_inputs: dict[str, Endpoint] = {}
        self.exit_outputs: dict[str, Endpoint] = {}
        # Nested composite invocation boundaries (D34), keyed by the composite's node
        # path -> CompositeIO. Recorded for the runner's composite contract checks
        # only; the scheduler never reads them (like data_arcs). The entry composite
        # `()` is omitted (the runner checks it via its whole-workflow handles, D33).
        self.composites: dict[NodePath, CompositeIO] = {}

    def expand(self, comp: dict, prefix: NodePath, inputs_env: dict, stack: tuple[str, ...]) -> None:
        """Expand one composite `comp` whose body node paths are prefixed by
        `prefix`. `inputs_env` maps this composite's input ports to their producer
        (resolved in the enclosing scope); `stack` is the chain of composite process
        names currently open, used to catch recursive definitions."""
        siblings = _body_nodes(comp)
        for node in siblings.values():
            self._expand_node(node, prefix, inputs_env, siblings, stack)

    def _expand_node(self, node: dict, prefix: NodePath, inputs_env: dict, siblings: dict, stack: tuple[str, ...]) -> None:
        node_id = node.get("id")
        path = prefix + (node_id,)
        kind = node.get("kind")
        # Structured nodes stay out of scope (D6): they reshape dataflow in ways the
        # flat scheduler graph cannot represent.
        if kind in _STRUCTURED_KINDS:
            self.diags.error(errors.UNSUPPORTED_FEATURE, f"structured node {node_id!r} (kind {kind!r}) is out of scope")
            return
        pname = node.get("process")
        if pname not in self.procs:
            self.diags.error(errors.PROCESS_NOT_DEFINED, f"node {node_id!r} invokes undefined process {pname!r}")
            return

        child_kind = self.procs[pname].get("kind")
        if child_kind == "atomic":
            # An atomic invocation is a real activity; wire each bound input to its
            # producer (`state` -> Object arc + precedence, `bind` -> precedence).
            self.activities.append(NodeInvocation(path, pname))
            self.used[pname] = self.atomic[pname]
            for section in ("state", "bind"):
                for port, binding in (node.get(section) or {}).items():
                    producer = self._resolve(_parse_ref(binding), prefix, inputs_env, siblings, stack)
                    if producer is None:
                        continue  # an unconnected workflow input
                    if isinstance(producer, _Literal):
                        # A Pure Data static literal: no producer, no precedence, no
                        # arc. Recorded port-level for the runner's value layer only
                        # (like data_arcs); the scheduler never reads it. Literals are
                        # Pure Data, so this happens under `bind` (a literal wrongly on
                        # `state` is a validation error, caught upstream; ignore here).
                        if section == "bind":
                            self.data_literals[Endpoint(path, port)] = producer.value
                        continue
                    if isinstance(producer, _EntryInput):
                        # A workflow entry input: no in-body producer, so no arc /
                        # precedence. A `state` (Object-bearing) binding records the
                        # boundary connection; a `bind` (Pure Data) one carries no spot
                        # but its port-level boundary is recorded for the runner (D26-0)
                        # so it can seed the value that enters here.
                        if section == "state":
                            self.entry_inputs[producer.name] = Endpoint(path, port)
                        else:  # bind: Pure Data entry input consumed at this atomic.
                            self.data_entry_inputs[producer.name] = Endpoint(path, port)
                        continue
                    self.precedence.append((producer.path, path))
                    if section == "state":
                        self.arcs.append(Arc(Endpoint(producer.path, producer.port), Endpoint(path, port)))
                    else:
                        # A `bind` is Pure Data: a precedence edge for the solver (added
                        # above), plus the port-level arc for the runner's value routing
                        # (D26-0). The scheduler does not read `data_arcs`.
                        self.data_arcs.append(Arc(Endpoint(producer.path, producer.port), Endpoint(path, port)))
        elif child_kind == "composite":
            # A composite invocation is structural: resolve its input bindings here,
            # then expand its body one level deeper with those producers in scope.
            if pname in stack:
                self.diags.error(errors.RECURSIVE_COMPOSITE, f"composite {pname!r} is recursively defined (via node {node_id!r})")
                return
            child_env = self._resolve_inputs(node, prefix, inputs_env, siblings, stack)
            self.expand(self.procs[pname], path, child_env, stack + (pname,))
            # Record this composite invocation's value-layer boundary for the runner's
            # contract checks (D34): its inputs (from `child_env`) and its outputs (its
            # `returns`, resolved to producing atomics in its own scope). Value-
            # independent metadata; the scheduler never reads it.
            self._record_composite(pname, path, child_env, stack)
        else:
            self.diags.error(errors.UNSUPPORTED_FEATURE, f"node {node_id!r} invokes process {pname!r} of unsupported kind {child_kind!r}")

    def _resolve_inputs(self, node: dict, prefix: NodePath, inputs_env: dict, siblings: dict, stack: tuple[str, ...]) -> dict:
        """Resolve every input binding of a composite invocation to its producer, so
        the child body's `inputs.*` references can be resolved against it."""
        env: dict[str, _Producer | None] = {}
        for section in ("state", "bind"):
            for port, binding in (node.get(section) or {}).items():
                env[port] = self._resolve(_parse_ref(binding), prefix, inputs_env, siblings, stack)
        return env

    def _record_composite(self, pname: str, path: NodePath, child_env: dict, stack: tuple[str, ...]) -> None:
        """Record a composite invocation's value-layer boundary (D34): each input port
        -> its source (from `child_env`), and each output port -> its source (its
        `returns` resolved to the producing atomic in the composite's own scope). A
        source is a producing atomic / boundary `Endpoint` or a static literal value."""
        cproc = self.procs[pname]
        inputs: dict[str, Endpoint] = {}
        input_literals: dict[str, object] = {}
        for port, producer in child_env.items():
            self._place_source(producer, port, inputs, input_literals)
        outputs: dict[str, Endpoint] = {}
        output_literals: dict[str, object] = {}
        for out_port, source in _returns(cproc).items():
            producer = self._resolve(_parse_ref(source), path, child_env, _body_nodes(cproc), stack + (pname,))
            self._place_source(producer, out_port, outputs, output_literals)
        self.composites[path] = CompositeIO(
            process=pname,
            inputs=inputs,
            input_literals=input_literals,
            outputs=outputs,
            output_literals=output_literals,
        )

    @staticmethod
    def _place_source(producer, port: str, endpoints: dict, literals: dict) -> None:
        """Classify a resolved producer into a value source: a producing atomic
        `Endpoint`, the workflow boundary `Endpoint((), name)` for an entry input, or a
        static literal value. An unconnected source (None) records nothing -- no value
        flows into that port, so a contract referencing it never becomes ready."""
        if isinstance(producer, _Producer):
            endpoints[port] = Endpoint(producer.path, producer.port)
        elif isinstance(producer, _EntryInput):
            endpoints[port] = Endpoint((), producer.name)  # boundary value-store key
        elif isinstance(producer, _Literal):
            literals[port] = producer.value

    def _resolve(self, ref, prefix: NodePath, inputs_env: dict, siblings: dict, stack: tuple[str, ...]):
        """Resolve a body dataflow reference to the atomic that produces it (a
        `_Producer`), a boundary marker (`_EntryInput` / `_Literal`), or None for an
        unconnected source."""
        if ref is None:
            return None
        kind, left, right = ref
        if kind == "literal":
            # A static literal (`value`): carried to the consuming atomic (like an
            # entry input) so the runner can seed it. `left` holds the literal value.
            return _Literal(left)
        if kind == "input":
            # `inputs.X` -> whatever the enclosing invocation bound to port X.
            return inputs_env.get(left)

        # `Node.Y` -> an output of a direct child of the current body.
        child = siblings.get(left)
        if child is None:
            return None  # dangling reference; a valid v0 workflow has none
        pname = child.get("process")
        cproc = self.procs.get(pname)
        if cproc is None:
            return None
        if cproc.get("kind") == "atomic":
            return _Producer(prefix + (left,), right)
        if cproc.get("kind") == "composite":
            if pname in stack:
                self.diags.error(errors.RECURSIVE_COMPOSITE, f"composite {pname!r} is recursively defined (via node {left!r})")
                return None
            # Follow the child composite's `returns[Y]` to the real producer, resolved
            # in the child's own scope (its inputs resolved here, body prefixed by Node).
            child_env = self._resolve_inputs(child, prefix, inputs_env, siblings, stack)
            returns = _returns(cproc)
            return self._resolve(_parse_ref(returns.get(right)), prefix + (left,), child_env, _body_nodes(cproc), stack + (pname,))
        return None  # a structured or unknown child output cannot be a scheduler source


def _body_nodes(comp: dict) -> dict[str, dict]:
    """The composite body's nodes keyed by id, in document order (dicts preserve
    insertion order, which fixes a deterministic activity ordering)."""
    body = comp.get("body") or {}
    result: dict[str, dict] = {}
    for node in body.get("nodes") or []:
        if isinstance(node, dict) and "id" in node:
            result[node["id"]] = node
    return result


def _returns(comp: dict) -> dict:
    body = comp.get("body") or {}
    return body.get("returns") or {}


def _parse_ref(binding):
    """Parse a binding / return source entry to a reference tuple, or None for a
    literal `value` or a malformed/absent `from`. A body dataflow reference is
    `inputs.X` (composite input) or `Node.Y` (child output), a single dot (§2.6.1);
    node ids and port names cannot contain a dot, so the first split is exact."""
    if not isinstance(binding, dict):
        return None
    frm = binding.get("from")
    if isinstance(frm, str) and "." in frm:
        left, right = frm.split(".", 1)
        if left == "inputs":
            return ("input", right, None)
        return ("node", left, right)
    # No `from`: a static literal `value` (§11) is a distinct reference so the runner
    # can seed it; anything else is an unconnected / malformed binding (None).
    if "value" in binding:
        return ("literal", binding["value"], None)
    return None
