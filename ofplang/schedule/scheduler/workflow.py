"""Minimal v0 workflow reader for the scheduler.

The scheduler reads the workflow itself (decision D17) instead of depending on
`ofplang.validate`, extracting only what scheduling needs: which processes are
atomic, each port's Object-bearing-ness (§5), and the expanded node graph
(processing activities with node paths, Object-bearing arcs, and precedence).
Composite invocations — including nested ones — are flattened by splicing
dataflow across the composite boundary, and `map` / `fold` nodes are expanded into
their invocations (see `_Expander`). The workflow is assumed to be valid v0; this
reader only diagnoses the parts the scheduler cannot handle (a capability gate):
generic processes (`generic_processes`), an unexpanded `$import`, `branch` and
`do_while` nodes, an atomic process with an Object-bearing Array port, a traversal
whose length is not known before the run, recursive composite definitions, and a
missing entry.

Binding semantics follow §11, read by the port's type: a binding to an
Object-bearing input carries an Object (so it is a transport arc), one to a Pure
Data input a value (a precedence dependency only).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.core.identifiers import format_node_path
from ofplang.schedule.scheduler.model import (
    Arc,
    AtomicProcess,
    CompositeIO,
    Endpoint,
    LengthCheck,
    NodeInvocation,
    NodePath,
    Port,
    Source,
    SourceLiteral,
    SourceRef,
    SourceSeq,
    Workflow,
)
from ofplang.schedule.validation import errors

# v0 built-in primitive Data types (no Object slots, §7.1).
_PRIMITIVES = {"Bool", "Int", "Float", "String"}


def _contains_import_key(obj) -> bool:
    """True if a `$import` key appears anywhere in the document (spec 3).

    The scheduler does not resolve imports; a workflow must already be expanded.
    An unexpanded `$import` would otherwise be silently ignored, dropping the
    imported processes and mis-reading the graph."""
    if isinstance(obj, dict):
        return "$import" in obj or any(_contains_import_key(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_import_key(v) for v in obj)
    return False


def parse_workflow(source, *, interface: dict | None = None) -> tuple[Workflow | None, Diagnostics]:
    """Parse the v0 workflow into a schedulable `Workflow`.

    `source` is either a path to a workflow YAML file, or an already-loaded workflow
    document (a mapping) -- so a caller holding the document in memory (e.g. the runner
    after an in-process rewrite) need not round-trip it through a temp file.

    `interface` is the document's §6.8 section for this workflow, where one is given.
    It matters only to a workflow that traverses an Array of Objects at its boundary
    with a `map` / `fold`: the binding's list of spots is how many elements there are,
    and so how many invocations the expansion makes (design.md D57). The result is a
    function of the two -- the same workflow with a longer list is a different graph,
    and its fingerprint says so. Every other workflow reads the same with or without it.

    Returns `(workflow, diagnostics)`; the workflow is None when a blocking
    diagnostic (unparseable document or no entry) is raised.

    This reader assumes valid v0 -- validating a workflow is `ofplang-validate`'s job,
    and both CLIs run it at their front door. What it does not assume is that it was
    *given* valid v0: `--no-validate` says the caller already validated, and a caller
    holding a document in memory may not have. Two guards keep that from turning into
    something worse than a diagnostic (see `_check_readable` and the translation
    below).
    """
    diags = Diagnostics()
    if isinstance(source, dict):
        data = source
    else:
        data = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        diags.error(errors.WRONG_TYPE, "workflow must be a mapping")
        return None, diags

    # First guard: the shapes this reader would otherwise read *partially*, which is
    # worse than failing -- a workflow with fewer activities than the document says
    # schedules successfully and hides the omission.
    if not _check_readable(data, diags):
        return None, diags

    # Second guard: everything else. A shape this reader cannot use at all makes it
    # raise, and an exception escaping a function whose contract is "returns
    # diagnostics" reaches the CLI as a traceback (both CLIs catch only YAMLError).
    # The exception's own type and text go into the message: a genuine bug in the
    # reader must not be disguised as a malformed document.
    try:
        return _read_workflow(data, diags, interface)
    except (AttributeError, TypeError, KeyError) as exc:
        diags.error(
            errors.WRONG_TYPE,
            f"workflow could not be read ({type(exc).__name__}: {exc}); it is not "
            "shaped as v0 requires -- validate it first",
        )
        return None, diags


def fingerprint(workflow: Workflow) -> str:
    """A short digest of the schedulable structure, identifying *which workflow* a
    job runs (SPEC §6.11).

    A joint plan's roster names its jobs by id, and a replan is handed the workflows
    again. Nothing but this ties an id to the workflow it was planned for, so without
    it two jobs given in the other order would silently swap histories -- their ids
    match as a set, and each job would be matched against the other's workflow.

    🔴 **Exactly as strong as it needs to be.** Two copies of one workflow hash the
    same, so swapping *those* two is not detected -- and does not need to be: they are
    interchangeable by definition, which is the same fact job-symmetry breaking rests
    on. What the digest catches is the swap that changes the answer.

    The ingredients are the **spec-level structure** and nothing else: which nodes
    invoke which processes, which ports the Object-bearing arcs connect, the
    precedence, and the boundary ports. Not the raw YAML (comments, key order and
    formatting are not the workflow), and not the environment (that is shared by every
    job, so it says nothing about which job this is).

    🔴 The digest is written into documents that later runs read back, so **changing
    how it is computed is a breaking change**: a plan written by one version would
    stop being replannable by the next. Keep the ingredients spec-level, and treat any
    change to them as one.
    """
    # An arc that carries one element of an Array port adds its two element indices.
    # A whole-port arc adds nothing, so the digest of a workflow without Arrays of
    # Objects is the one it always had. In this stage no interior arc carries an index
    # (an atomic's Object-bearing Array port is refused), so the branch is for the
    # stage that plans one element by element. The boundary's element arcs are not
    # read here: they follow from the activities and the boundary ports, which are.
    def arc_entry(arc: Arc) -> tuple:
        entry: tuple = (list(arc.src.node), arc.src.port, list(arc.dst.node), arc.dst.port)
        if arc.src.index or arc.dst.index:
            entry += (list(arc.src.index), list(arc.dst.index))
        return entry

    parts = [
        sorted(((list(a.path), a.process) for a in workflow.activities), key=_typed),
        sorted((arc_entry(arc) for arc in workflow.arcs), key=_typed),
        sorted(((list(s), list(d)) for s, d in workflow.precedence), key=_typed),
        sorted(workflow.entry_input_ports.items()),
        sorted(workflow.exit_output_ports.items()),
    ]
    # A separator-free, unambiguous encoding: JSON with sorted keys, so the digest
    # depends on the structure above and not on how Python happens to repr it.
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _typed(value):
    """A sort key that orders node ids and iteration indices without comparing a str
    with an int (which Python refuses). Strings sort among themselves exactly as
    before, so the order -- and with it the digest -- of anything without an index
    is unchanged."""
    if isinstance(value, (list, tuple)):
        return tuple(_typed(element) for element in value)
    if isinstance(value, int) and not isinstance(value, bool):
        return (1, value)
    return (0, value)


def _check_readable(data: dict, diags: Diagnostics) -> bool:
    """Report the shapes that would otherwise be read partially, and say whether the
    document is worth reading at all.

    Only those: a shape this reader *cannot* read raises, and the caller translates
    that. What is caught here is where it would carry on with less than the document
    holds -- a type whose domain it cannot see (so an Object silently counts as Pure
    Data and its transport arc disappears), a body or a node list it cannot walk (so
    the workflow comes out with no activities in it), a node without an id, a binding
    or a return whose source it cannot read (so a connection silently is not one).

    Every finding is collected in one pass: these are independent positions, not one
    error and its consequences.
    """
    ok = True

    def wrong(path: str, what: str) -> None:
        nonlocal ok
        diags.error(errors.WRONG_TYPE, f"{path} must be {what}", path)
        ok = False

    types = data.get("types")
    if isinstance(types, dict):
        for tname, spec in types.items():
            if not isinstance(spec, dict):
                wrong(f"types.{tname}", "a mapping (its domain decides Object-bearing)")

    procs = data.get("processes")
    if not isinstance(procs, dict):
        return ok
    for pname, proc in procs.items():
        if not isinstance(proc, dict):
            continue  # unreadable, not partially readable: the translation reports it
        body = proc.get("body")
        if body is None:
            continue
        base = f"processes.{pname}.body"
        if not isinstance(body, dict):
            wrong(base, "a mapping")
            continue
        nodes = body.get("nodes")
        if nodes is not None and not isinstance(nodes, list):
            wrong(f"{base}.nodes", "a sequence")
        elif isinstance(nodes, list):
            for i, node in enumerate(nodes):
                npath = f"{base}.nodes[{i}]"
                if not isinstance(node, dict):
                    wrong(npath, "a mapping")
                    continue
                if not isinstance(node.get("id"), str):
                    wrong(f"{npath}.id", "a string (a node is keyed by its id)")
                for section in ("state", "bind", "each", "carry"):
                    entries = node.get(section)
                    if not isinstance(entries, dict):
                        continue  # unreadable: left to the translation
                    for port, binding in entries.items():
                        if not isinstance(binding, dict):
                            wrong(f"{npath}.{section}.{port}", "a mapping")
        returns = body.get("returns")
        if isinstance(returns, dict):
            for rname, ret in returns.items():
                if not isinstance(ret, dict):
                    wrong(f"{base}.returns.{rname}", "a mapping")
    return ok


def _read_workflow(
    data: dict, diags: Diagnostics, interface: dict | None = None
) -> tuple[Workflow | None, Diagnostics]:
    """Read a document this reader can use: the capability gate, then the flattening.

    Split out so the guards in `parse_workflow` wrap the whole of it -- including the
    capability gate, which walks `processes` and so needs the same protection.
    """
    # Capability gate: the scheduler handles a subset of valid v0. Features it
    # cannot schedule are rejected here with a clear unsupported-feature
    # diagnostic rather than silently mis-read — e.g. a generic Object port
    # would be mistaken for Pure Data and its transport arc dropped (spec 4.4).
    # This runs regardless of any front-door validation: the library is the real
    # boundary, and the runner calls this per replan tick.
    has_import = _contains_import_key(data)
    generic = [
        name
        for name, proc in (data.get("processes") or {}).items()
        if isinstance(proc, dict) and proc.get("type_params") is not None
    ]
    if has_import:
        diags.error(
            errors.UNSUPPORTED_FEATURE,
            "workflow contains a $import; it must be expanded before scheduling",
        )
    for name in generic:
        diags.error(
            errors.UNSUPPORTED_FEATURE,
            f"process {name!r} uses generic type parameters (generic_processes), "
            "which the scheduler does not support",
        )
    if has_import or generic:
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
        # A degenerate single-atomic entry: one activity, no arcs. Its own ports are
        # the workflow's boundary connections. The Object-bearing ones are the ones
        # the planner places; the Pure Data ones are recorded for the runner exactly
        # as a composite entry's are (a boundary data arc per input, the activity's
        # output for each output) -- leaving them out handed the runner a typed
        # default for every Pure Data input and dropped every Pure Data output.
        if entry in atomic:
            if not _Expander(procs, atomic, diags, domains)._plannable(entry):
                return None, diags  # an Object-bearing Array port (reported there)
            sig = atomic[entry]
            path = (entry,)
            entry_inputs = {p.name: Endpoint(path, p.name) for p in sig.inputs if p.object_bearing}
            exit_outputs = {p.name: Endpoint(path, p.name) for p in sig.outputs}
            data_inputs = [p.name for p in sig.inputs if not p.object_bearing]
            in_ports = {p.name: p.object_bearing for p in sig.inputs}
            out_ports = {p.name: p.object_bearing for p in sig.outputs}
            # As Source trees every port is said, Pure Data ones included: each input is
            # the workflow's entry input of the same name, seeded at the boundary, and
            # each output is this one activity's.
            input_sources: dict[Endpoint, Source] = {
                Endpoint(path, p.name): SourceRef((), p.name) for p in sig.inputs
            }
            output_sources: dict[str, Source] = {
                p.name: SourceRef(path, p.name) for p in sig.outputs
            }
            return (
                Workflow(
                    (NodeInvocation(path, entry),), (), (), {entry: sig},
                    entry_inputs, exit_outputs, in_ports, out_ports,
                    data_arcs=tuple(
                        Arc(Endpoint((), name), Endpoint(path, name)) for name in data_inputs
                    ),
                    data_entry_inputs={name: Endpoint(path, name) for name in data_inputs},
                    input_sources=input_sources, output_sources=output_sources,
                    # The planner's boundary: one arc per Object-bearing port, each
                    # one Object (an Array port was refused above).
                    entry_arcs=tuple(
                        Arc(Endpoint((), name), consumer) for name, consumer in entry_inputs.items()
                    ),
                    exit_arcs=tuple(
                        Arc(Endpoint(path, p.name), Endpoint((), p.name))
                        for p in sig.outputs if p.object_bearing
                    ),
                    **_boundary_ranks(entry_proc),
                ),
                diags,
            )
        diags.error(errors.UNSUPPORTED_FEATURE, f"entry process {entry!r} is not a composite")
        return None, diags

    # The entry composite's declared ports, tagged Object-bearing (for classifying
    # `interface` bindings, and -- on the output side -- for telling a Pure Data
    # pass-through return from an Object one during the flattening). Values are
    # `{type, phase}` specs like an atomic's.
    in_ports = {
        n: _object_bearing((s or {}).get("type", ""), domains)
        for n, s in (entry_proc.get("inputs") or {}).items()
    }
    out_ports = {
        n: _object_bearing((s or {}).get("type", ""), domains)
        for n, s in (entry_proc.get("outputs") or {}).items()
    }

    exp, precedence = _expand_body(
        entry, entry_proc, procs, atomic, in_ports, out_ports, interface, domains, diags
    )

    # scheduling_policies (§23) / object policies (§24) are best-effort preferences
    # this scheduler does not honor; a composite's `scheduling` section is dropped
    # when the composite is flattened. Warn (not an error, §23) once per used
    # composite that carries one, so the ignored feature is visible rather than
    # silently discarded.
    used_composites = {entry: entry_proc}
    for io in exp.composites.values():
        cp = procs.get(io.process)
        if cp is not None:
            used_composites.setdefault(io.process, cp)
    for cname, cproc in used_composites.items():
        if (cproc or {}).get("scheduling") is not None:
            diags.warning(
                errors.SCHEDULING_POLICIES_IGNORED,
                f"scheduling policies on composite {cname!r} are not supported and are ignored",
                f"processes.{cname}.scheduling",
            )
    return (
        Workflow(
            tuple(exp.activities), tuple(exp.arcs), tuple(precedence), exp.used,
            exp.entry_inputs, exp.exit_outputs, in_ports, out_ports,
            # Pure Data port-level dataflow for the runner (D26-0); the scheduler
            # does not read these, so the plan is unaffected.
            data_arcs=tuple(exp.data_arcs), data_entry_inputs=exp.data_entry_inputs,
            data_literals=exp.data_literals, exit_literals=exp.exit_literals,
            input_sources=exp.input_sources, output_sources=exp.output_sources,
            # Nested composite invocation boundaries for the runner's contract checks
            # (D34); value-independent, so the plan is unaffected.
            composites=exp.composites,
            # The Object-bearing boundary as the planner reads it, an arc per Object.
            entry_arcs=tuple(exp.entry_arcs),
            exit_arcs=tuple(exp.exit_arcs),
            # How many invocations each map / fold made, and the lengths the runner
            # has to check (D57); the scheduler does not read these either.
            iterations=exp.iterations,
            length_checks=tuple(exp.length_checks),
            **_boundary_ranks(entry_proc),
        ),
        diags,
    )


def _boundary_ranks(entry_proc: dict) -> dict:
    """Every boundary port's Array rank -- the shape its `interface` binding takes (a
    spot, or lists of spots that deep)."""
    return {
        "entry_input_ranks": {
            n: _array_rank((s or {}).get("type", ""))
            for n, s in (entry_proc.get("inputs") or {}).items()
        },
        "exit_output_ranks": {
            n: _array_rank((s or {}).get("type", ""))
            for n, s in (entry_proc.get("outputs") or {}).items()
        },
    }


def _array_rank(type_expr) -> int:
    """How deeply a type nests Arrays: 0 for `Plate`, 2 for `Array<Array<Plate>>`."""
    t = str(type_expr).strip()
    rank = 0
    while t.startswith("Array<") and t.endswith(">"):
        t = t[len("Array<") : -1].strip()
        rank += 1
    return rank


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


def _expand_body(entry_name, entry_proc, procs, atomic, in_ports, exit_object_bearing,
                 interface, domains, diags):
    """Flatten the entry composite into atomic activities, Object-bearing arcs, and
    precedence edges, following nested composites and expanding `map` / `fold`
    nodes (see `_Expander`).

    `in_ports` / `exit_object_bearing` are the entry's `{port: object_bearing}`
    tables: the first says which entry inputs are Arrays of Objects whose length the
    `interface` gives, the second tells a Pure Data pass-through return (recorded)
    from an Object one (out of scope)."""
    exp = _Expander(procs, atomic, diags, domains, interface, in_ports)
    # The entry's own inputs are the workflow's boundary inputs: seed the entry
    # scope so `inputs.X` resolves to an `_EntryInput(X)` marker, which propagates
    # into nested composites and is recorded at the atomic that consumes it.
    entry_env = {name: _EntryInput(name) for name in (entry_proc.get("inputs") or {})}
    exp.expand(entry_proc, (), entry_env, (entry_name,))

    # The entry's `returns` are the workflow's boundary outputs: resolve each to the
    # atomic that produces it. Other sources reach here too:
    #  - an entry input returned verbatim (directly, or through nested composites)
    #    resolves to an `_EntryInput` marker -- a pass-through. A Pure Data one is
    #    recorded with the boundary node `()` as its producer, which is where the
    #    runner seeds entry inputs, so the value it returns is the one that came in.
    #    An Object-bearing one stays out of `exit_outputs`: a pass-through Object has
    #    no activity to deliver it, and is out of scope (an `interface` binding of it
    #    is diagnosed in `instance`).
    #  - a nested composite that returns a literal-bound input resolves to a
    #    `_Literal`; it has no producer at all, so it is kept apart in `exit_literals`.
    #  - a `map` / `fold` output is an Array gathered from the invocations
    #    (`_Gathered`): its Objects cross the boundary one element at a time, each on
    #    an exit arc of its own, and the runner reads the whole from `output_sources`.
    siblings = _body_nodes(entry_proc)
    for out_name, source in _returns(entry_proc).items():
        value = exp._resolve(_parse_ref(source), (), entry_env, siblings, (entry_name,))
        # An undeclared port (invalid upstream) counts as Object-bearing, so that
        # nothing is recorded for it as Pure Data.
        object_bearing = exit_object_bearing.get(out_name, True)
        if isinstance(value, _Producer) and not value.index:
            exp.exit_outputs[out_name] = Endpoint(value.path, value.port)
        elif isinstance(value, _EntryInput) and not value.index:
            if object_bearing:
                continue  # an Object pass-through: out of scope, so no Source either
            exp.exit_outputs[out_name] = Endpoint((), value.name)
        elif isinstance(value, _Literal):
            exp.exit_literals[out_name] = value.value
        leaves = list(_leaves(value))
        if object_bearing:
            # An Array output any element of which is a boundary input passed straight
            # through is out of scope as a whole, as a whole pass-through is: modelling
            # the other elements alone would leave the port half-planned, and its
            # binding (or its unbound warning) speaking of only some of its elements.
            if any(isinstance(leaf, _EntryInput) for _, leaf in leaves):
                continue
            # The planner's exit: one arc per Object, the element index on the
            # boundary end.
            for index, leaf in leaves:
                if isinstance(leaf, _Producer) and not leaf.index:
                    exp.exit_arcs.append(
                        Arc(Endpoint(leaf.path, leaf.port), Endpoint((), out_name, index))
                    )
        if (resolved := _source_of(value)) is not None:
            exp.output_sources[out_name] = resolved

    # Pure Data fan-in can occasionally add the same precedence edge twice; keep
    # each edge once, in first-seen order for a deterministic activity ordering.
    seen: set[tuple[NodePath, NodePath]] = set()
    precedence: list[tuple[NodePath, NodePath]] = []
    for edge in exp.precedence:
        if edge not in seen:
            seen.add(edge)
            precedence.append(edge)
    return exp, precedence


@dataclass(frozen=True)
class _Producer:
    """The concrete atomic output that ultimately feeds a reference, after
    resolving through any composite boundaries (a `returns` map, or a composite's
    own input). `index` selects one element of that output's Array, where a `map` /
    `fold` traverses it; empty, the whole value."""

    path: NodePath
    port: str
    index: tuple[int, ...] = ()


@dataclass(frozen=True)
class _EntryInput:
    """A reference that resolves to one of the *workflow's* own entry inputs, i.e.
    a `main`-level input port with no in-body producer. Carried (instead of a
    `_Producer`) so the atomic that ultimately consumes it can be recorded as a
    boundary connection (`Workflow.entry_inputs`); the name survives nesting because
    the marker propagates through composite input environments. `index` selects one
    element of an Array entry input, as `_Producer.index` does."""

    name: str
    index: tuple[int, ...] = ()


@dataclass(frozen=True)
class _Literal:
    """A binding to a static literal value (`bind: {port: {value: ...}}`, §11), which
    has no in-body producer. Carried like `_EntryInput` so the atomic that ultimately
    consumes it can be recorded (`Workflow.data_literals`); the value survives nesting
    because the marker propagates through composite input environments. Literals are
    Pure Data and, like `data_arcs`, are recorded for the sibling `ofplang-run` runner
    alone -- the scheduler never reads them."""

    value: object


@dataclass(frozen=True)
class _Gathered:
    """An Array assembled element by element: the output of a `map`, or a `fold`'s
    collected output, element `i` being invocation `i`'s value. Its elements are any
    of the markers above (or None where an invocation produced nothing to read)."""

    items: tuple


# Structured node kinds the expansion does not handle yet (D57: `branch` is the
# next step; `do_while`'s count is a run-time value).
_UNSUPPORTED_KINDS = {"do_while", "branch"}


class _Expander:
    """Flattens the entry composite into atomic activities, splicing dataflow
    across composite boundaries and expanding `map` / `fold` nodes.

    Node bindings and returns use body dataflow references (v0 §2.6.1): `inputs.X`
    names an input port of the current composite, `Node.Y` an output of a direct
    child. Flattening a nested composite therefore means:

    - inward — a child's `inputs.X` reference resolves to whatever the enclosing
      invocation bound to X;
    - outward — an enclosing `Child.Y` reference resolves through the child's
      `returns[Y]` to the atomic that actually produces the value.

    Both directions are handled by `_resolve`, which walks these boundaries down to
    the producing atomic. Only atomic invocations become activities; a binding to an
    Object-bearing atomic input yields a transport arc, one to a Pure Data input only
    a precedence edge (v0 §11).

    A `map` / `fold` node is expanded into its invocations, invocation `i` under the
    path `node_id, i` (design.md D57): each `each` source gives element `i`, each
    `bind` the whole value, and a `fold`'s carry threads invocation `i`'s output into
    invocation `i + 1`. How many invocations there are -- L -- is the length of the
    `each` sources, which must be known before the run: from an `interface` list for
    an Array of Objects at the boundary, a literal, or another `map` / `fold`'s
    output. A structured node is expanded once, the first time it is reached --
    walking the body or resolving a reference to its output, whichever comes first --
    and its outputs are kept, so a second reference reads the same invocations
    rather than making new ones."""

    def __init__(
        self,
        procs: dict,
        atomic: dict[str, AtomicProcess],
        diags: Diagnostics,
        domains: dict | None = None,
        interface: dict | None = None,
        entry_ports: dict[str, bool] | None = None,
    ) -> None:
        self.procs = procs
        self.atomic = atomic
        self.diags = diags
        self.domains = domains or {}
        # The entry composite's input ports -> Object-bearing, and the `interface`
        # bindings of its inputs: an Array of Objects at the boundary has as many
        # elements as its binding has spots (§6.8), which is where L comes from.
        self.entry_ports = entry_ports or {}
        # Read leniently: the document has not been validated yet when a workflow is
        # read (`api` parses first), so a section that is not shaped as a mapping is no
        # bindings here and is the document validation's to report.
        inputs = interface.get("inputs") if isinstance(interface, dict) else None
        self.entry_bindings = dict(inputs) if isinstance(inputs, dict) else {}
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
        # endpoint, and main output port -> producing atomic endpoint (from the
        # entry's `returns`), for a whole port. The planner reads `entry_arcs` /
        # `exit_arcs` instead, which also hold one arc per element of an Array.
        self.entry_inputs: dict[str, Endpoint] = {}
        self.exit_outputs: dict[str, Endpoint] = {}
        self.entry_arcs: list[Arc] = []
        self.exit_arcs: list[Arc] = []
        # Main output port -> a static literal it returns (see `Workflow.exit_literals`).
        self.exit_literals: dict[str, object] = {}
        # The same dataflow as `Source` trees, for the runner (see `Workflow`):
        # consuming atomic input -> its source, and main output -> its source.
        self.input_sources: dict[Endpoint, Source] = {}
        self.output_sources: dict[str, Source] = {}
        # Nested composite invocation boundaries (D34), keyed by the composite's node
        # path -> CompositeIO. Recorded for the runner's composite contract checks
        # only; the scheduler never reads them (like data_arcs). The entry composite
        # `()` is omitted (the runner checks it via its whole-workflow handles, D33).
        self.composites: dict[NodePath, CompositeIO] = {}
        # `map` / `fold` node path -> its invocation count, and the lengths the plan
        # assumed but only the runner can check (see `Workflow`).
        self.iterations: dict[NodePath, int] = {}
        self.length_checks: list[LengthCheck] = []
        # `map` / `fold` node path -> its outputs, once expanded (see the class doc).
        self.structured: dict[NodePath, dict] = {}
        # Atomic process -> whether it can be planned (see `_plannable`), decided once.
        self.plannable: dict[str, bool] = {}

    def expand(
        self, comp: dict, prefix: NodePath, inputs_env: dict, stack: tuple[str, ...]
    ) -> None:
        """Expand one composite `comp` whose body node paths are prefixed by
        `prefix`. `inputs_env` maps this composite's input ports to their producer
        (resolved in the enclosing scope); `stack` is the chain of composite process
        names currently open, used to catch recursive definitions."""
        siblings = _body_nodes(comp)
        for node in siblings.values():
            self._expand_node(node, prefix, inputs_env, siblings, stack)

    def _expand_node(
        self, node: dict, prefix: NodePath, inputs_env: dict, siblings: dict, stack: tuple[str, ...]
    ) -> None:
        node_id: str = node["id"]  # every body node has an id (see _body_nodes)
        path = prefix + (node_id,)
        kind = node.get("kind")
        if kind in ("map", "fold"):
            # Expanded here, unless a reference to one of its outputs already did.
            self._structured_outputs(node, prefix, inputs_env, siblings, stack)
            return
        if kind in _UNSUPPORTED_KINDS:
            self.diags.error(
                errors.UNSUPPORTED_FEATURE,
                f"structured node {node_id!r} (kind {kind!r}) is out of scope",
            )
            return
        pname = node.get("process")
        if pname not in self.procs:
            self.diags.error(
                errors.PROCESS_NOT_DEFINED,
                f"node {node_id!r} invokes undefined process {pname!r}",
            )
            return
        assert isinstance(pname, str)  # a key of the str-keyed self.procs

        child_kind = self.procs[pname].get("kind")
        if child_kind == "atomic":
            items = [
                (port, section,
                 self._resolve(_parse_ref(binding), prefix, inputs_env, siblings, stack))
                for section in ("state", "bind")
                for port, binding in (node.get(section) or {}).items()
            ]
            self._invoke_atomic(path, pname, items)
        elif child_kind == "composite":
            # A composite invocation is structural: resolve its input bindings here,
            # then expand its body one level deeper with those producers in scope.
            if pname in stack:
                self.diags.error(
                    errors.RECURSIVE_COMPOSITE,
                    f"composite {pname!r} is recursively defined (via node {node_id!r})",
                )
                return
            child_env = self._resolve_inputs(node, prefix, inputs_env, siblings, stack)
            self.expand(self.procs[pname], path, child_env, stack + (pname,))
            # Record this composite invocation's value-layer boundary for the runner's
            # contract checks (D34): its inputs (from `child_env`) and its outputs (its
            # `returns`, resolved to producing atomics in its own scope). Value-
            # independent metadata; the scheduler never reads it.
            self._record_composite(pname, path, child_env, stack)
        else:
            self.diags.error(
                errors.UNSUPPORTED_FEATURE,
                f"node {node_id!r} invokes process {pname!r} of unsupported kind {child_kind!r}",
            )

    def _invoke_atomic(self, path: NodePath, pname: str, items: list) -> None:
        """One atomic invocation at `path`: a processing activity, with each input
        wired to its source. `items` is `(port, section, resolved value)` per bound
        input; the section is consulted only for a port the process does not declare.

        An Object-bearing port gets an Object arc (a transport) and a precedence edge,
        a Pure Data port a precedence edge and a data arc."""
        if not self._plannable(pname):
            return
        self.activities.append(NodeInvocation(path, pname))
        sig = self.atomic[pname]
        self.used[pname] = sig
        # 🔴 Whether a binding moves an Object is the target **port's type**, never
        # the section it is written under. The spec pairs the two (`state` for
        # Object-bearing ports, `bind` for Pure Data, v0 §11), but this reader does
        # not run the validator, and a section that disagrees with the port is a
        # document to diagnose upstream, not one to mis-plan: read by section, a Pure
        # Data entry input written under `state` became an Object boundary input the
        # interface was then required to place on a spot, and a literal under
        # `state` was dropped. A port the process does not declare (invalid
        # upstream) falls back to its section, which is all there is to go on.
        object_ports = {p.name: p.object_bearing for p in sig.inputs}
        for port, section, value in items:
            object_bearing = object_ports.get(port, section == "state")
            if value is None:
                continue  # an unconnected workflow input
            dst = Endpoint(path, port)
            # The runner's one record of where this input's value comes from, whatever
            # kind of source it is (see `model.Source`). The per-kind records below
            # are kept alongside it.
            source = _source_of(value)
            if source is not None:
                self.input_sources[dst] = source
            if isinstance(value, _Literal):
                # A static literal: no producer, no precedence, no arc. A Pure Data one
                # is recorded port-level for the runner's value layer only (like
                # data_arcs); the scheduler never reads it. A literal on an
                # Object-bearing port names no Object to move, so there is nothing to
                # record for it.
                if not object_bearing:
                    self.data_literals[dst] = value.value
                continue
            if isinstance(value, _EntryInput):
                # A workflow entry input -- or one element of it: no in-body producer,
                # so no precedence. An Object-bearing port records the boundary
                # connection the planner places (an element of an Array on an arc of
                # its own); a Pure Data one carries no spot but its port-level boundary
                # is recorded for the runner (D26-0) so it can seed the value.
                boundary = Endpoint((), value.name, value.index)
                if object_bearing:
                    self.entry_arcs.append(Arc(boundary, dst))
                    if not value.index:
                        self.entry_inputs[value.name] = dst
                else:
                    # Unlike an Object, one Pure Data entry input may feed any number
                    # of atomics, so each consumer gets a data arc of its own whose
                    # source is the boundary node `()` -- the same convention as the
                    # plan's boundary arcs. `data_entry_inputs` maps a port to ONE
                    # consumer and so can hold only the last of them; it is kept for
                    # callers that read it, but `data_arcs` is complete.
                    self.data_arcs.append(Arc(boundary, dst))
                    if not value.index:
                        self.data_entry_inputs[value.name] = dst
                continue
            if isinstance(value, _Gathered):
                # An Array assembled from several invocations, read whole (a Pure Data
                # port: an Object-bearing Array port is refused above). It waits for
                # every one of them; where its value comes from is `input_sources`,
                # which no per-port arc could say.
                for _index, leaf in _leaves(value):
                    if isinstance(leaf, _Producer):
                        self.precedence.append((leaf.path, path))
                continue
            self.precedence.append((value.path, path))
            arc = Arc(Endpoint(value.path, value.port, value.index), dst)
            if object_bearing:
                self.arcs.append(arc)
            else:
                # Pure Data: a precedence edge for the solver (added above), plus the
                # port-level arc for the runner's value routing (D26-0). The scheduler
                # does not read `data_arcs`.
                self.data_arcs.append(arc)

    def _plannable(self, pname: str) -> bool:
        """Whether an atomic process can be planned in this stage (design.md D57): not
        if any of its ports is an Array of Objects. Each element of such an Array is an
        Object on a spot of its own, and a mode that maps a port to one spot cannot
        place them; the modes that could are not defined yet. Decided -- and reported --
        once per process, however many invocations a traversal makes of it."""
        if pname in self.plannable:
            return self.plannable[pname]
        proc = self.procs.get(pname) or {}
        arrays = [
            f"{side}.{port}"
            for side in ("inputs", "outputs")
            for port, spec in (proc.get(side) or {}).items()
            if isinstance(spec, dict)
            and _array_rank(spec.get("type", "")) > 0
            and _object_bearing(str(spec.get("type", "")), self.domains)
        ]
        self.plannable[pname] = not arrays
        if arrays:
            self.diags.error(
                errors.UNSUPPORTED_FEATURE,
                f"atomic process {pname!r} has Object-bearing Array port(s) {arrays}: each "
                "element is an Object on a spot of its own, which a mode cannot place yet; "
                "traverse the Array with a map or fold instead",
            )
        return not arrays

    def _structured_outputs(
        self, node: dict, prefix: NodePath, inputs_env: dict, siblings: dict, stack: tuple[str, ...]
    ) -> dict:
        """Expand a `map` / `fold` node once and return its outputs (port -> value)."""
        node_id = node["id"]
        path = prefix + (node_id,)
        if path in self.structured:
            return self.structured[path]
        self.structured[path] = {}  # a reference back into itself finds nothing
        kind = node.get("kind")
        pname = node.get("process")
        if pname not in self.procs:
            self.diags.error(
                errors.PROCESS_NOT_DEFINED,
                f"node {node_id!r} invokes undefined process {pname!r}",
            )
            return {}
        if pname in stack:
            self.diags.error(
                errors.RECURSIVE_COMPOSITE,
                f"composite {pname!r} is recursively defined (via node {node_id!r})",
            )
            return {}

        def resolved(section: str) -> dict:
            return {
                port: self._resolve(_parse_ref(binding), prefix, inputs_env, siblings, stack)
                for port, binding in (node.get(section) or {}).items()
            }

        each, bind = resolved("each"), resolved("bind")
        length = self._traversal_length(path, kind, each)
        if length is None:
            return {}
        self.iterations[path] = length
        if kind == "map":
            outputs = self._expand_map(path, pname, each, bind, length, stack)
        else:
            outputs = self._expand_fold(path, node, pname, resolved("carry"), each, bind,
                                        length, stack)
        self.structured[path] = outputs
        return outputs

    def _expand_map(self, path, pname, each, bind, length, stack) -> dict:
        """`map` (§17): invocation `i` gets element `i` of every `each` source and the
        whole of every `bind`; no invocation depends on another, so none is ordered
        after another. Every target output `p` is exposed as the Array of the
        invocations' `p`, in invocation order."""
        out_ports = list((self.procs[pname].get("outputs") or {}).keys())
        collected: dict[str, list] = {p: [] for p in out_ports}
        for i in range(length):
            env = {**bind, **{port: _element(value, i) for port, value in each.items()}}
            outs = self._invoke(pname, path + (i,), env, stack)
            for p in out_ports:
                collected[p].append(outs.get(p))
        return {p: _Gathered(tuple(values)) for p, values in collected.items()}

    def _expand_fold(self, path, node, pname, carry, each, bind, length, stack) -> dict:
        """`fold` (§18): as `map`, plus the carry -- invocation `i + 1` gets invocation
        `i`'s carry outputs, the first gets the node's `carry` bindings. That data
        dependency is the whole of the ordering between invocations (design.md D57):
        the parts of one invocation that do not read the carry are not held back by
        the previous one. Outputs by mode (§18.1): `carry` is the last invocation's
        value (the initial one if there are none), `collect` the Array of every
        invocation's, `drop` nothing."""
        target_outputs = self.procs[pname].get("outputs") or {}
        modes = self._fold_modes(path, node, set(carry), target_outputs)
        if modes is None:
            return {}
        state = dict(carry)
        collected: dict[str, list] = {p: [] for p, mode in modes.items() if mode == "collect"}
        for i in range(length):
            env = {**bind, **{port: _element(value, i) for port, value in each.items()}, **state}
            outs = self._invoke(pname, path + (i,), env, stack)
            for port in carry:
                state[port] = outs.get(port)
            for port, values in collected.items():
                values.append(outs.get(port))
        result: dict = {}
        for port, mode in modes.items():
            if mode == "carry":
                result[port] = state.get(port)
            elif mode == "collect":
                result[port] = _Gathered(tuple(collected[port]))
        return result

    def _fold_modes(self, path, node, carry: set, target_outputs: dict) -> dict | None:
        """Each target output's mode (§18.1). An `outputs` section is explicit and
        complete; without one (§18.2) a carry output is `carry`, a Pure Data one is
        dropped, and an Object-bearing one makes the section required -- a document
        without it is not valid v0, and guessing a mode for an Object would plan it to
        go somewhere the document never said.

        The same holds of a section that is there but does not say what an Object does:
        an Object-bearing output left out of it, written without a mode, or dropped
        (§18.1 rules 6, 7, 9). Reading any of those as `drop` would plan the Objects to
        vanish without a word, so each is refused as the invalid document it is."""
        where = f"fold node {format_node_path(path)!r}"
        section = node.get("outputs")
        if isinstance(section, dict):
            modes = {}
            for port, spec in section.items():
                mode = spec.get("mode") if isinstance(spec, dict) else None
                if mode not in ("carry", "collect", "drop"):
                    self.diags.error(
                        errors.WRONG_TYPE,
                        f"{where}: output {port!r} has no mode carry, collect or drop (§18.1)",
                    )
                    return None
                modes[port] = mode
            for port, spec in target_outputs.items():
                object_bearing = _object_bearing(str((spec or {}).get("type", "")), self.domains)
                if object_bearing and modes.get(port, "drop") == "drop":
                    self.diags.error(
                        errors.WRONG_TYPE,
                        f"{where}: its target's output {port!r} is Object-bearing, so the "
                        "outputs section must expose it as carry or collect (§18.1)",
                    )
                    return None
            return modes
        modes = {}
        for port, spec in target_outputs.items():
            if port in carry:
                modes[port] = "carry"
            elif _object_bearing(str((spec or {}).get("type", "")), self.domains):
                self.diags.error(
                    errors.WRONG_TYPE,
                    f"{where} has no outputs section, but its target's output {port!r} is "
                    "Object-bearing and not carried; v0 requires the section then (§18.2)",
                )
                return None
            else:
                modes[port] = "drop"
        return modes

    def _invoke(self, pname: str, path: NodePath, env: dict, stack: tuple[str, ...]) -> dict:
        """One invocation of a structured node's target at `path`, its inputs already
        resolved (`env`: port -> value). Returns its outputs (port -> value)."""
        kind = self.procs[pname].get("kind")
        if kind == "atomic":
            self._invoke_atomic(path, pname, [(port, "each", v) for port, v in env.items()])
            return {p.name: _Producer(path, p.name) for p in self.atomic[pname].outputs}
        if kind == "composite":
            cproc = self.procs[pname]
            self.expand(cproc, path, env, stack + (pname,))
            self._record_composite(pname, path, env, stack)
            body = _body_nodes(cproc)
            return {
                out: self._resolve(_parse_ref(source), path, env, body, stack + (pname,))
                for out, source in _returns(cproc).items()
            }
        self.diags.error(
            errors.UNSUPPORTED_FEATURE,
            f"{format_node_path(path)!r} invokes process {pname!r} of unsupported kind {kind!r}",
        )
        return {}

    def _traversal_length(self, path: NodePath, kind, each: dict) -> int | None:
        """L, the common length of the `each` sources (§17, §18 -- zip-equal), or None
        where it cannot be known before the run (reported).

        A source whose length the scheduler can see -- an Array of Objects bound in
        `interface`, a literal, another `map` / `fold`'s output -- decides it, and two
        such sources disagreeing is the error spec §6.2 makes it. A source whose
        length is a value only the run has -- a Pure Data entry input, an atomic's
        output -- is planned at that L and recorded for the runner to check
        (`LengthCheck`); one of those alone decides nothing (design.md D57)."""
        where = f"{kind} node {format_node_path(path)!r}"
        if not each:
            self.diags.error(
                errors.ARRAY_LENGTH_UNKNOWN,
                f"{where} has no each source, so nothing says how many invocations it makes",
            )
            return None
        known: dict[str, int] = {}
        for port, value in each.items():
            length = self._length(value)
            if length is not None:
                known[port] = length
                continue
            if isinstance(value, _EntryInput) and self.entry_ports.get(value.name):
                # An Array of Objects at the boundary whose binding gave no length: say
                # what is wrong with the binding rather than that no length is known.
                if value.name not in self.entry_bindings:
                    self.diags.error(
                        errors.INTERFACE_INPUT_MISSING,
                        f"entry input {value.name!r} is an Array of Objects traversed by "
                        f"{where}; its length is the length of its interface.inputs binding, "
                        "a list of spots, and it has none",
                    )
                else:
                    self.diags.error(
                        errors.INTERFACE_SHAPE_MISMATCH,
                        f"entry input {value.name!r} is an Array of Objects traversed by "
                        f"{where}; its interface.inputs binding must be a list of spots, one "
                        "per element, and is not one at this depth",
                    )
                return None
        if not known:
            self.diags.error(
                errors.ARRAY_LENGTH_UNKNOWN,
                f"{where}: none of its each sources {sorted(each)} has a length known before "
                "the run (an Array of Objects bound in interface, a literal, or a map or "
                "fold output)",
            )
            return None
        if len(set(known.values())) > 1:
            self.diags.error(
                errors.EACH_LENGTH_MISMATCH,
                f"{where} traverses each sources of different lengths {known}; they are "
                "zipped and must be equal",
            )
            return None
        length = next(iter(known.values()))
        for port, value in each.items():
            if port in known or value is None:
                continue
            source = _source_of(value)
            if source is not None:
                self.length_checks.append(LengthCheck(path, port, source, length))
        return length

    def _length(self, value) -> int | None:
        """The length of an Array value, where it is known before the run."""
        if isinstance(value, _Gathered):
            return len(value.items)
        if isinstance(value, _Literal):
            return len(value.value) if isinstance(value.value, list) else None
        if isinstance(value, _EntryInput):
            if not self.entry_ports.get(value.name):
                return None  # Pure Data: a value the scheduler is not given (D57)
            binding = self.entry_bindings.get(value.name)
            for i in value.index:
                binding = binding[i] if isinstance(binding, list) and i < len(binding) else None
            return len(binding) if isinstance(binding, list) else None
        return None  # an atomic's output: its length is a run-time value

    def _resolve_inputs(
        self, node: dict, prefix: NodePath, inputs_env: dict, siblings: dict, stack: tuple[str, ...]
    ) -> dict:
        """Resolve every input binding of a composite invocation to its producer, so
        the child body's `inputs.*` references can be resolved against it."""
        env: dict = {}
        for section in ("state", "bind"):
            for port, binding in (node.get(section) or {}).items():
                env[port] = self._resolve(_parse_ref(binding), prefix, inputs_env, siblings, stack)
        return env

    def _record_composite(
        self, pname: str, path: NodePath, child_env: dict, stack: tuple[str, ...]
    ) -> None:
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
        output_sources: dict[str, Source] = {}
        for out_port, source in _returns(cproc).items():
            producer = self._resolve(
                _parse_ref(source), path, child_env, _body_nodes(cproc), stack + (pname,)
            )
            self._place_source(producer, out_port, outputs, output_literals)
            if (resolved := _source_of(producer)) is not None:
                output_sources[out_port] = resolved
        input_sources = {
            port: resolved
            for port, producer in child_env.items()
            if (resolved := _source_of(producer)) is not None
        }
        self.composites[path] = CompositeIO(
            process=pname,
            inputs=inputs,
            input_literals=input_literals,
            outputs=outputs,
            output_literals=output_literals,
            input_sources=input_sources,
            output_sources=output_sources,
        )

    @staticmethod
    def _place_source(producer, port: str, endpoints: dict, literals: dict) -> None:
        """Classify a resolved producer into a value source: a producing atomic
        `Endpoint`, the workflow boundary `Endpoint((), name)` for an entry input, or a
        static literal value. An unconnected source (None) records nothing -- no value
        flows into that port, so a contract referencing it never becomes ready. An
        element of an Array, or an Array gathered from several invocations, is not a
        value-store key at all; only `CompositeIO.*_sources` can say it."""
        if isinstance(producer, _Producer) and not producer.index:
            endpoints[port] = Endpoint(producer.path, producer.port)
        elif isinstance(producer, _EntryInput) and not producer.index:
            endpoints[port] = Endpoint((), producer.name)  # boundary value-store key
        elif isinstance(producer, _Literal):
            literals[port] = producer.value

    def _resolve(
        self, ref, prefix: NodePath, inputs_env: dict, siblings: dict, stack: tuple[str, ...]
    ):
        """Resolve a body dataflow reference to the atomic that produces it (a
        `_Producer`), a boundary marker (`_EntryInput` / `_Literal`), the Array a
        `map` / `fold` assembles (`_Gathered`), or None for an unconnected source."""
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
        child_kind = child.get("kind")
        if child_kind in ("map", "fold"):
            # Expanded on first reach, in this scope, and read thereafter.
            return self._structured_outputs(child, prefix, inputs_env, siblings, stack).get(right)
        if child_kind is not None:
            return None  # a structured node this stage does not expand (reported)
        pname = child.get("process")
        cproc = self.procs.get(pname)
        if cproc is None:
            return None
        if cproc.get("kind") == "atomic":
            return _Producer(prefix + (left,), right)
        if cproc.get("kind") == "composite":
            if pname in stack:
                self.diags.error(
                    errors.RECURSIVE_COMPOSITE,
                    f"composite {pname!r} is recursively defined (via node {left!r})",
                )
                return None
            # Follow the child composite's `returns[Y]` to the real producer, resolved
            # in the child's own scope (its inputs resolved here, body prefixed by Node).
            child_env = self._resolve_inputs(child, prefix, inputs_env, siblings, stack)
            returns = _returns(cproc)
            return self._resolve(
                _parse_ref(returns.get(right)),
                prefix + (left,),
                child_env,
                _body_nodes(cproc),
                stack + (pname,),
            )
        return None  # an unknown child kind cannot be a scheduler source


def _element(value, i: int):
    """Element `i` of an Array value: the `i`-th item of a gathered Array or of a
    literal list, or a reference to element `i` of a producer's / entry input's
    Array. None where there is no such element."""
    if isinstance(value, _Gathered):
        return value.items[i] if i < len(value.items) else None
    if isinstance(value, _Producer):
        return _Producer(value.path, value.port, (*value.index, i))
    if isinstance(value, _EntryInput):
        return _EntryInput(value.name, (*value.index, i))
    if isinstance(value, _Literal):
        items = value.value
        return _Literal(items[i]) if isinstance(items, list) and i < len(items) else None
    return None


def _leaves(value, index: tuple[int, ...] = ()):
    """Every (element index, value) a value is made of: itself at `()`, or -- for a
    gathered Array, recursively -- each element under its index."""
    if value is None:
        return
    if isinstance(value, _Gathered):
        for i, item in enumerate(value.items):
            yield from _leaves(item, (*index, i))
    else:
        yield index, value


def _source_of(producer) -> Source | None:
    """A resolved reference as a `Source`: a producing atomic output or the workflow
    boundary `()` (where entry inputs are seeded) as a reference -- to one element
    where it names one --, a static literal as itself, a gathered Array as the
    sequence of its elements' sources, and an unconnected source (None) as nothing."""
    if isinstance(producer, _Producer):
        return SourceRef(producer.path, producer.port, producer.index)
    if isinstance(producer, _EntryInput):
        return SourceRef((), producer.name, producer.index)
    if isinstance(producer, _Literal):
        return SourceLiteral(producer.value)
    if isinstance(producer, _Gathered):
        items = [_source_of(item) for item in producer.items]
        if any(item is None for item in items):
            return None  # an element with no source: nothing whole to say
        return SourceSeq(tuple(item for item in items if item is not None))
    return None


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
