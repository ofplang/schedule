"""Combine a workflow and an environment into a solver-ready instance.

This is where the execution-layer checks live (SPECIFICATIONS.md §9.3 subset):
every invoked atomic process must have a capability with at least one mode, each
mode's `input_spots` / `output_spots` must name only real Object-bearing ports of
the process in the correct direction and map all of them, and every arc must be
transportable (some mode pair + transporter can move the source spot to the
destination spot). The instance precomputes, per activity, its candidate modes,
and per arc, the concrete transport options (source/destination spot, duration,
transporter) keyed by the endpoint mode indices — everything the CP-SAT builder
needs without touching the raw documents again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.core.identifiers import format_endpoint, parse_qualified_spot
from ofplang.schedule.scheduler.model import (
    Arc,
    Endpoint,
    Environment,
    Mode,
    NodePath,
    Workflow,
)
from ofplang.schedule.validation import errors


@dataclass(frozen=True)
class RelayInfo:
    """Output provenance of a relay activity (a transport junction, SPEC §6.4.1):
    the logical arc it belongs to, its chain position `seq`, and the spot it
    occupies. A relay is not a workflow node, so this — not `node` — is its
    identity. Present only on relay activities (added by `normalize`)."""

    arc: Arc
    seq: int
    spot: str


@dataclass(frozen=True)
class BoundaryInfo:
    """Marks a synthetic **boundary node** (SPEC §6.8, FORMULATION §Activities):
    the `input` node (produces every Object-bearing entry input at its interface
    spot, pinned to time 0) or the `output` node (consumes every Object-bearing
    final output at its interface spot, its end pinned to the makespan). Like a
    relay it is an ordinary ActivityInstance with a single spot-fixing, device-less
    mode; `kind` drives the solver's time pinning and rendering skips it.

    `job` is which job of a joint plan the node belongs to (§6.11), set by
    `prefix_instance` -- the node path stays empty, because an empty path is what
    *marks* an endpoint as the interface side, so the owner has to be recorded here
    instead. The solver needs it to pin an input node at that job's release.

    **None means the node belongs to no job**, which is the single-workflow case
    today. It is deliberately expressible: material left behind by a job that has
    left the plan belongs to no job either, and that is the shape a withdrawal will
    need (design.md "ジョブの退出").
    """

    kind: str  # "input" | "output"
    job: str | None = None


@dataclass(frozen=True)
class ActivityInstance:
    """One processing activity and the modes it may run in. A relay (§6.4.1) is
    also an ActivityInstance — with a single 0-duration, device-less, single-spot
    mode and `relay` set — so the solver treats it exactly like any processing
    activity; only construction (`normalize`) and rendering (`plan`) are aware of
    it. A boundary node (§6.8) is likewise an ActivityInstance with `boundary` set.
    `node` / `process` are unused on a relay or boundary node."""

    node: NodePath
    process: str
    modes: tuple[Mode, ...]
    relay: RelayInfo | None = None
    boundary: BoundaryInfo | None = None


@dataclass(frozen=True)
class TransportOption:
    """A viable way to serve an arc: a source/destination mode pair, a
    transporter, the resolved source/destination spots, and the duration."""

    src_mode_index: int
    dst_mode_index: int
    transporter: str | None  # None for a same-spot no-op route (§5.4/§6.4)
    from_spot: str
    to_spot: str
    duration: int


@dataclass(frozen=True)
class ArcInstance:
    """One transport leg. `arc` is the logical connection served (all legs of a
    multi-leg move share it); `seq` is the leg's chain position (§6.6), None for a
    single-leg transport. `src_activity` / `dst_activity` are the physical
    endpoints (either may be a relay), which can differ from `arc`'s endpoints."""

    arc: Arc
    src_activity: int
    dst_activity: int
    options: tuple[TransportOption, ...]
    seq: int | None = None


@dataclass(frozen=True)
class RefillOption:
    """One way to perform a refill: which replenisher, and how long it takes."""

    replenisher: str
    duration: int


@dataclass(frozen=True)
class RefillCandidate:
    """A refill the solver *may* run, on the device `device` (FORMULATION §10).

    Candidates are constructed, not read: one per (pending consuming activity,
    device) pair, which is enough to cover any schedule that is possible at all
    because a planned refill fills to capacity. `origin` is the activity index the
    candidate was generated from -- it gates the candidate (a refill for a device
    the activity does not run on is never useful) but does **not** tie it in time:
    a refill may be placed arbitrarily early (§4.7.1).

    `resources` is every resource the device declares, not only the ones the origin
    draws on: one visit tops the device up.
    """

    id: str
    device: str
    origin: int
    options: tuple[RefillOption, ...]
    resources: tuple[str, ...]


@dataclass(frozen=True)
class Instance:
    env: Environment
    time_unit: str
    activities: tuple[ActivityInstance, ...]
    arcs: tuple[ArcInstance, ...]
    # Precedence edges as (source activity index, destination activity index).
    precedence: tuple[tuple[int, int], ...]
    # Refills the solver may run (§10). Kept apart from `activities` deliberately:
    # those are indexed, and the fixation keys off those indices, while a refill is
    # matched by `id` and has no workflow element to be indexed against. Defaulted
    # so an instance built without resources stays a five-argument construction.
    replenishments: tuple[RefillCandidate, ...] = ()


def build_instance(
    workflow: Workflow,
    env: Environment,
    *,
    interface: dict | None = None,
    check_reachability: bool = True,
) -> tuple[Instance | None, Diagnostics]:
    """Build the solver instance from the workflow and environment.

    `interface` (SPEC §6.8) pins the workflow's Object-bearing boundary material to
    spots. Each binding adds a synthetic boundary node and an ordinary arc
    (`input node → consumer` for an entry input), so the rest of the model is
    unchanged. It is required for Object-bearing entry inputs: every such input must
    be bound in `interface.inputs`, otherwise its consumer's mode would be left
    unconstrained, so an unbound one is rejected with `INTERFACE_INPUT_MISSING` (the
    check below) even when no `interface` is given. Object-bearing outputs are
    optional.

    `check_reachability` reports `arc_unreachable` for any workflow arc no
    transporter can serve — correct for an initial plan, where every arc is a
    single pending transport. On a **replan** it is passed False: an arc whose
    transport is already committed may have no *direct* current-env route (the
    move is completed, and a re-route goes through a relay), so reachability is
    re-checked per pending leg after normalization (`report_unreachable`)."""
    diags = Diagnostics()

    index_by_node = {a.path: i for i, a in enumerate(workflow.activities)}
    activities: list[ActivityInstance] = []

    for act in workflow.activities:
        capability = env.processes.get(act.process)
        if capability is None or not capability.modes:
            diags.error(
                errors.NO_CAPABILITY,
                f"process {act.process!r} has no capability/modes in the environment",
            )
            activities.append(ActivityInstance(act.path, act.process, ()))
            continue
        _check_mode_ports(act.process, workflow, capability.modes, diags)
        activities.append(ActivityInstance(act.path, act.process, capability.modes))

    arcs: list[ArcInstance] = []
    for arc in workflow.arcs:
        si = index_by_node.get(arc.src.node)
        di = index_by_node.get(arc.dst.node)
        if si is None or di is None:
            diags.error(
                errors.PROCESS_NOT_DEFINED,
                f"arc references an unknown node: {format_endpoint(arc.src.node, arc.src.port)}"
                f" -> {format_endpoint(arc.dst.node, arc.dst.port)}",
            )
            continue
        options = transport_options(
            activities[si], arc.src.port, activities[di], arc.dst.port, env
        )
        if not options and check_reachability:
            diags.error(
                errors.ARC_UNREACHABLE,
                f"no transporter can serve the arc {format_endpoint(arc.src.node, arc.src.port)}"
                f" -> {format_endpoint(arc.dst.node, arc.dst.port)}",
            )
        arcs.append(ArcInstance(arc, si, di, tuple(options)))

    # Interface is required (SPEC §6.8): every Object-bearing entry input must be
    # bound, otherwise the upstream activity's mode would be unconstrained. (Outputs
    # are optional.) Runs even without an interface, so a workflow with entry inputs
    # and no interface is rejected rather than silently unconstrained.
    bound_inputs = set((interface or {}).get("inputs") or {})
    for name in workflow.entry_inputs:
        if name not in bound_inputs:
            diags.error(
                errors.INTERFACE_INPUT_MISSING,
                f"entry input {name!r} is Object-bearing and must be bound in interface.inputs",
            )

    # Boundary connections (SPEC §6.8): synthesize the input / output nodes and arcs.
    if interface:
        _add_boundary_inputs(
            workflow, env, interface, activities, arcs, index_by_node, check_reachability, diags
        )
        _add_boundary_outputs(
            workflow, env, interface, activities, arcs, index_by_node, check_reachability, diags
        )

    precedence = tuple(
        (index_by_node[s], index_by_node[d])
        for s, d in workflow.precedence
        if s in index_by_node and d in index_by_node
    )

    if any(d.severity == "error" for d in diags.items):
        return None, diags
    return Instance(env, env.time_unit, tuple(activities), tuple(arcs), precedence), diags


def prefix_instance(instance: Instance, prefix: NodePath) -> Instance:
    """`instance` with every workflow node path prefixed by `prefix`.

    Planning several workflows together (SPEC §6.11) merges their instances into
    one, and two workflows may well name the same node -- both have a `[Heat]`. The
    prefix is the job id, so node paths stay distinct across the merged instance
    **without anything downstream having to know that jobs exist**: `normalize`, the
    solver and the fixation all treat a node path as an opaque key. `plan` splits the
    prefix back off into each activity's `job` field, so the rendered `node` is the
    workflow-relative path it has always been -- the node-path convention the sibling
    runner keys its value store by (see `model.Workflow`, INVARIANT 2).

    A boundary node (§6.8) keeps its **empty** path: the empty path is what marks an
    endpoint as the interface side, so prefixing it would make it an ordinary node
    path. A boundary arc stays unambiguous in the merged instance anyway, because it
    is keyed by both of its endpoints (§6.6) and the other end is prefixed. The owner
    is recorded on the node instead (`BoundaryInfo.job`), because the solver has to
    know whose release to pin an input node at.

    An empty prefix returns the instance unchanged -- what the single-workflow path
    passes -- so a plan for one workflow is byte-for-byte what it always was.
    """
    if not prefix:
        return instance

    def endpoint(activity_index: int, path: NodePath) -> NodePath:
        # The interface side of a boundary arc keeps its empty path (see above);
        # every other endpoint names a real node and is prefixed.
        if instance.activities[activity_index].boundary is not None:
            return path
        return prefix + path

    activities = tuple(
        replace(a, boundary=replace(a.boundary, job=prefix[0]))
        if a.boundary is not None
        else replace(a, node=prefix + a.node)
        for a in instance.activities
    )
    arcs = tuple(
        replace(
            r,
            arc=Arc(
                Endpoint(endpoint(r.src_activity, r.arc.src.node), r.arc.src.port),
                Endpoint(endpoint(r.dst_activity, r.arc.dst.node), r.arc.dst.port),
            ),
        )
        for r in instance.arcs
    )
    # `precedence` is index-based and indices do not move, so it needs no rewriting.
    return replace(instance, activities=activities, arcs=arcs)


def job_membership(instance: Instance, jobs: Sequence[str]) -> tuple[str | None, ...]:
    """Which job each activity of a merged instance belongs to (SPEC §6.11), by index.

    The job id is the first element of a workflow activity's node path
    (`prefix_instance`), so most of this is reading it off. Two kinds of activity have
    no node path of their own and are read from what they serve:

    - a **relay** (§6.4.1) belongs to the job whose arc it carries -- its identity is
      that arc, and one end of it always names a real node;
    - a **boundary node** (§6.8) carries its owner on itself (`BoundaryInfo.job`), the
      node path being empty by design; `None` there means it belongs to no job.

    🔴 This is **ownership**, not "whose completion counts it". A boundary output node
    ends at the makespan (it holds its spots until the run is over), so counting it
    towards its job's completion would make every job finish when the last one does.
    The completion sum excludes boundary nodes explicitly for that reason.

    With no jobs -- the single-workflow case, where node paths carry no prefix and
    nothing may be read off them -- every workflow activity belongs to the same
    **implicit** job, named by the empty string. That is what lets a single-workflow
    document ask for `completion_time_sum` and mean something by it: the run has one
    job, and the sum is that job's completion time.
    """
    known = set(jobs)

    def of_path(path: NodePath) -> str | None:
        if not known:
            return ""
        return path[0] if path and path[0] in known else None

    out: list[str | None] = []
    for activity in instance.activities:
        if activity.boundary is not None:
            out.append(activity.boundary.job if known else "")
        elif activity.relay is not None:
            arc = activity.relay.arc
            out.append(of_path(arc.src.node) or of_path(arc.dst.node))
        else:
            out.append(of_path(activity.node))
    return tuple(out)


def merge_instances(instances: Sequence[Instance]) -> Instance:
    """One instance holding every activity and arc of `instances` (SPEC §6.11).

    This is all that planning several workflows jointly takes at this layer, because
    everything that makes them interact is already environment-wide rather than
    workflow-wide: the solver puts one non-overlap constraint on each device, spot and
    transporter, and a consumable stock is keyed by `(device, resource)`. Merged jobs
    therefore compete for machines and draw on the same stocks with no further work --
    including a refill that only the *combination* needs, since `normalize` derives
    refill candidates from the merged activity list rather than per workflow.

    Every index in the model is positional (`ArcInstance.src_activity` /
    `dst_activity`, `precedence`), so merging is concatenation plus an offset.
    `replenishments` is empty here and is not merged: candidates are constructed
    later, by `normalize`, from the merged instance.

    The instances must have been built against the same environment; the merged
    instance carries the first one's.
    """
    if len(instances) == 1:
        return instances[0]

    activities: list[ActivityInstance] = []
    arcs: list[ArcInstance] = []
    precedence: list[tuple[int, int]] = []
    for inst in instances:
        offset = len(activities)
        activities += list(inst.activities)
        arcs += [
            replace(
                a,
                src_activity=a.src_activity + offset,
                dst_activity=a.dst_activity + offset,
            )
            for a in inst.arcs
        ]
        precedence += [(s + offset, d + offset) for s, d in inst.precedence]

    first = instances[0]
    return Instance(
        first.env, first.time_unit, tuple(activities), tuple(arcs), tuple(precedence)
    )


def report_unreachable(instance: Instance, fixed_arc_indices: set[int], diags: Diagnostics) -> None:
    """Emit `arc_unreachable` for every **pending** leg (an arc not in
    `fixed_arc_indices`) that no transporter can serve. Committed (fixed) legs are
    facts and are not re-checked (SPEC §9.3). Used on the augmented instance after
    normalization, so a re-routed move is judged per pending leg, not by whether
    the original arc had a direct route."""
    for r, arc in enumerate(instance.arcs):
        if r in fixed_arc_indices or arc.options:
            continue
        leg = f" (leg seq {arc.seq})" if arc.seq is not None else ""
        diags.error(
            errors.ARC_UNREACHABLE,
            f"no transporter can serve the arc "
            f"{format_endpoint(arc.arc.src.node, arc.arc.src.port)} -> "
            f"{format_endpoint(arc.arc.dst.node, arc.arc.dst.port)}{leg}",
        )


def _add_boundary_inputs(
    workflow: Workflow,
    env: Environment,
    interface: dict,
    activities: list[ActivityInstance],
    arcs: list[ArcInstance],
    index_by_node: dict[NodePath, int],
    check_reachability: bool,
    diags: Diagnostics,
) -> None:
    """Append the input boundary node and one boundary arc per bound entry input.

    Each valid binding contributes an output port on the single input node (its
    mode places that port at the interface spot) and an arc from the input node to
    the consuming activity. Invalid bindings are diagnosed and skipped (SPEC §9.3):
    an unknown / wrong-side / pass-through port, a Pure Data port, a duplicate spot,
    or a spot the environment does not define.
    """
    inputs = interface.get("inputs") or {}
    valid: list[tuple[str, str, Endpoint]] = []  # (port name, spot, consumer endpoint)
    spot_owner: dict[str, str] = {}
    for name, spot in inputs.items():
        if not _spot_exists(spot, env, name, diags):
            continue
        if spot in spot_owner:
            diags.error(
                errors.INTERFACE_DUPLICATE_SPOT,
                f"interface inputs {name!r} and {spot_owner[spot]!r} both bind spot {spot!r}",
            )
            continue
        consumer = workflow.entry_inputs.get(name)
        if consumer is None:
            object_bearing = workflow.entry_input_ports.get(name)
            if object_bearing is None:
                diags.error(
                    errors.INTERFACE_UNKNOWN_PORT,
                    f"interface input {name!r} is not an entry input of the workflow",
                )
            elif not object_bearing:
                diags.error(
                    errors.INTERFACE_PURE_DATA_PORT,
                    f"interface input {name!r} is a Pure Data port and occupies no spot",
                )
            else:
                diags.error(
                    errors.INTERFACE_UNKNOWN_PORT,
                    f"interface input {name!r} is a pass-through entry input with no consuming"
                    f" activity (out of scope)",
                )
            continue
        spot_owner[spot] = name
        valid.append((name, spot, consumer))

    if not valid:
        return

    # A single input node: one mode placing every bound entry input at its spot,
    # no device (it holds spots only), zero duration (pinned to time 0 by cpsat).
    mode = Mode(
        id="interface_in",
        devices=(),
        duration=0,
        input_spots={},
        output_spots={n: s for n, s, _ in valid},
    )
    node_index = len(activities)
    activities.append(ActivityInstance((), "", (mode,), boundary=BoundaryInfo("input")))

    for name, _spot, consumer in valid:
        di = index_by_node.get(consumer.node)
        if di is None:
            # a consumer that is not a scheduled activity; cannot happen for a valid workflow
            continue
        options = transport_options(
            activities[node_index], name, activities[di], consumer.port, env
        )
        if not options and check_reachability:
            diags.error(
                errors.ARC_UNREACHABLE,
                f"no transporter can serve the boundary input {name!r} -> "
                f"{format_endpoint(consumer.node, consumer.port)}",
            )
        arc = Arc(Endpoint((), name), Endpoint(consumer.node, consumer.port))
        arcs.append(ArcInstance(arc, node_index, di, tuple(options)))


def _add_boundary_outputs(
    workflow: Workflow,
    env: Environment,
    interface: dict,
    activities: list[ActivityInstance],
    arcs: list[ArcInstance],
    index_by_node: dict[NodePath, int],
    check_reachability: bool,
    diags: Diagnostics,
) -> None:
    """Append the output boundary node and one boundary arc per bound final output
    (the mirror of `_add_boundary_inputs`). The output node consumes every bound
    output at its interface spot; its end is pinned to the makespan by the solver
    so a delivered Object holds its spot to the end (SPEC §6.8). Invalid bindings
    are diagnosed and skipped: an unknown / Pure Data / pass-through port, a
    duplicate spot (within outputs), or a spot the environment does not define.
    """
    outputs = interface.get("outputs") or {}
    valid: list[tuple[str, str, Endpoint]] = []  # (port name, spot, producer endpoint)
    spot_owner: dict[str, str] = {}
    for name, spot in outputs.items():
        if not _spot_exists(spot, env, name, diags):
            continue
        if spot in spot_owner:
            diags.error(
                errors.INTERFACE_DUPLICATE_SPOT,
                f"interface outputs {name!r} and {spot_owner[spot]!r} both bind spot {spot!r}",
            )
            continue
        producer = workflow.exit_outputs.get(name)
        if producer is None:
            object_bearing = workflow.exit_output_ports.get(name)
            if object_bearing is None:
                diags.error(
                    errors.INTERFACE_UNKNOWN_PORT,
                    f"interface output {name!r} is not a final output of the workflow",
                )
            elif not object_bearing:
                diags.error(
                    errors.INTERFACE_PURE_DATA_PORT,
                    f"interface output {name!r} is a Pure Data port and occupies no spot",
                )
            else:
                diags.error(
                    errors.INTERFACE_UNKNOWN_PORT,
                    f"interface output {name!r} is a pass-through entry input returned"
                    f" directly (out of scope)",
                )
            continue
        spot_owner[spot] = name
        valid.append((name, spot, producer))

    if not valid:
        return

    # A single output node: one mode placing every bound final output at its spot,
    # no device, its end pinned to the makespan by cpsat (holds the spots to the end).
    mode = Mode(
        id="interface_out",
        devices=(),
        duration=0,
        input_spots={n: s for n, s, _ in valid},
        output_spots={},
    )
    node_index = len(activities)
    activities.append(ActivityInstance((), "", (mode,), boundary=BoundaryInfo("output")))

    for name, _spot, producer in valid:
        si = index_by_node.get(producer.node)
        if si is None:
            # a producer that is not a scheduled activity; cannot happen for a valid workflow
            continue
        options = transport_options(
            activities[si], producer.port, activities[node_index], name, env
        )
        if not options and check_reachability:
            diags.error(
                errors.ARC_UNREACHABLE,
                f"no transporter can serve the boundary output "
                f"{format_endpoint(producer.node, producer.port)} -> {name!r}",
            )
        arc = Arc(Endpoint(producer.node, producer.port), Endpoint((), name))
        arcs.append(ArcInstance(arc, si, node_index, tuple(options)))


def _spot_exists(spot: str, env: Environment, name: str, diags: Diagnostics) -> bool:
    """True iff `spot` is a `<device>.<spot>` naming a device/spot defined in the
    environment; diagnose `unknown_device` / `unknown_spot` otherwise (SPEC §9.3)."""
    parsed = parse_qualified_spot(spot)
    if parsed is None:
        diags.error(
            errors.MALFORMED_QUALIFIED_SPOT,
            f"interface spot {spot!r} for {name!r} is not a qualified spot",
        )
        return False
    device, spot_name = parsed
    dev = env.devices.get(device)
    if dev is None:
        diags.error(
            errors.UNKNOWN_DEVICE,
            f"interface spot {spot!r} names an unknown device {device!r}",
        )
        return False
    if spot_name not in dev.spots:
        diags.error(
            errors.UNKNOWN_SPOT,
            f"interface spot {spot!r} names an unknown spot on device {device!r}",
        )
        return False
    return True


def _check_mode_ports(
    process: str, workflow: Workflow, modes: tuple[Mode, ...], diags: Diagnostics
) -> None:
    """Validate each mode's spot mapping against the process's port signature
    (§9.3 "against the workflow" + coverage), reporting each kind of violation
    with its own code rather than one catch-all:

    - a mapped port the process does not have at all -> `unknown_process_port`;
    - a port mapped on the wrong side (an output under `input_spots`, or vice
      versa) -> `wrong_port_direction`;
    - a Pure Data port given a spot -> `pure_data_port_mapped`;
    - an Object-bearing port left unmapped -> `mode_ports_incomplete`.
    """
    sig = workflow.processes.get(process)
    if sig is None:
        return
    # Port names live in a per-direction namespace (§8.2), so the same name may be
    # both an input and an output; classification checks the correct side first.
    input_names = {p.name for p in sig.inputs}
    output_names = {p.name for p in sig.outputs}
    obj_input = set(sig.object_input_names())
    obj_output = set(sig.object_output_names())

    for mode in modes:
        _check_side(
            process,
            mode,
            "input_spots",
            mode.input_spots,
            input_names,
            output_names,
            obj_input,
            diags,
        )
        _check_side(
            process,
            mode,
            "output_spots",
            mode.output_spots,
            output_names,
            input_names,
            obj_output,
            diags,
        )
        # Coverage: every Object-bearing port must receive a spot in this mode.
        missing = (obj_input - set(mode.input_spots)) | (obj_output - set(mode.output_spots))
        if missing:
            diags.error(
                errors.MODE_PORTS_INCOMPLETE,
                f"process {process!r} mode {mode.id!r} does not map Object-bearing"
                f" port(s) {sorted(missing)}",
            )


def _check_side(
    process: str,
    mode: Mode,
    section: str,
    mapping: dict[str, str],
    own_names: set[str],
    other_names: set[str],
    object_names: set[str],
    diags: Diagnostics,
) -> None:
    """Check one side (`input_spots` or `output_spots`) of a mode. `own_names` are
    the process's ports on this side, `other_names` those on the opposite side,
    and `object_names` the Object-bearing subset of `own_names`."""
    for port in mapping:
        if port in own_names:
            # A real port on this side; it must be Object-bearing to occupy a spot.
            if port not in object_names:
                diags.error(
                    errors.PURE_DATA_PORT_MAPPED,
                    f"process {process!r} mode {mode.id!r} maps Pure Data port {port!r}"
                    f" in {section}",
                )
        elif port in other_names:
            # The name exists on the process, but on the opposite side.
            diags.error(
                errors.WRONG_PORT_DIRECTION,
                f"process {process!r} mode {mode.id!r} maps port {port!r} in {section},"
                f" but it is on the other side",
            )
        else:
            diags.error(
                errors.UNKNOWN_PROCESS_PORT,
                f"process {process!r} mode {mode.id!r} maps unknown port {port!r} in {section}",
            )


def transport_options(
    src: ActivityInstance,
    src_port: str,
    dst: ActivityInstance,
    dst_port: str,
    env: Environment,
) -> list[TransportOption]:
    """Enumerate viable transport options over the endpoint mode pairs and the
    transporters. A same-spot move is free (duration 0).

    Public because `normalize` enumerates the same options when it re-creates the
    boundary and relay arcs of a replan: one definition of what routes are viable,
    used by whoever needs it, rather than a private one reached across modules."""
    options: list[TransportOption] = []
    for m, src_mode in enumerate(src.modes):
        from_spot = src_mode.output_spots.get(src_port)
        if from_spot is None:
            continue
        for n, dst_mode in enumerate(dst.modes):
            to_spot = dst_mode.input_spots.get(dst_port)
            if to_spot is None:
                continue
            served = False
            for transporter in env.transporters:
                duration = env.transport_duration(transporter, from_spot, to_spot)
                if duration is not None:
                    options.append(TransportOption(m, n, transporter, from_spot, to_spot, duration))
                    served = True
            # A same-spot hand-off (§5.4) is a physical no-op that no transporter
            # carries (§6.4). Ensure it is always schedulable -- even in an
            # environment that defines no transporters (a purely in-place workflow)
            # -- by synthesizing a transporter-less zero-duration route, matching the
            # plan output which omits the transporter for a same-spot move.
            if from_spot == to_spot and not served:
                options.append(TransportOption(m, n, None, from_spot, to_spot, 0))
    return options
