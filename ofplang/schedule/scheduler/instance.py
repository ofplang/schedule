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

from dataclasses import dataclass

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
    mode; `kind` drives the solver's time pinning and rendering skips it."""

    kind: str  # "input" | "output"


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
    transporter: str
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
class Instance:
    env: Environment
    time_unit: str
    activities: tuple[ActivityInstance, ...]
    arcs: tuple[ArcInstance, ...]
    # Precedence edges as (source activity index, destination activity index).
    precedence: tuple[tuple[int, int], ...]


def build_instance(
    workflow: Workflow, env: Environment, *, interface: dict | None = None, check_reachability: bool = True
) -> tuple[Instance | None, Diagnostics]:
    """Build the solver instance from the workflow and environment.

    `interface` (SPEC §6.8), when given, pins the workflow's Object-bearing boundary
    material to spots. Each binding adds a synthetic boundary node and an ordinary
    arc (`input node → consumer` for an entry input), so the rest of the model is
    unchanged. It is optional in the current phase: an unbound entry input leaves
    its consumer's mode unconstrained (the pre-`interface` behaviour).

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
            diags.error(errors.NO_CAPABILITY, f"process {act.process!r} has no capability/modes in the environment")
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
                f"arc references an unknown node: {format_endpoint(arc.src.node, arc.src.port)} -> {format_endpoint(arc.dst.node, arc.dst.port)}",
            )
            continue
        options = _transport_options(activities[si], arc.src.port, activities[di], arc.dst.port, env)
        if not options and check_reachability:
            diags.error(
                errors.ARC_UNREACHABLE,
                f"no transporter can serve the arc {format_endpoint(arc.src.node, arc.src.port)} -> {format_endpoint(arc.dst.node, arc.dst.port)}",
            )
        arcs.append(ArcInstance(arc, si, di, tuple(options)))

    # Boundary connections (SPEC §6.8): synthesize the input node and its arcs.
    if interface:
        _add_boundary_inputs(workflow, env, interface, activities, arcs, index_by_node, check_reachability, diags)

    precedence = tuple(
        (index_by_node[s], index_by_node[d])
        for s, d in workflow.precedence
        if s in index_by_node and d in index_by_node
    )

    if any(d.severity == "error" for d in diags.items):
        return None, diags
    return Instance(env, env.time_unit, tuple(activities), tuple(arcs), precedence), diags


def report_unreachable(instance: "Instance", fixed_arc_indices: set[int], diags: Diagnostics) -> None:
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
            f"no transporter can serve the arc {format_endpoint(arc.arc.src.node, arc.arc.src.port)} -> {format_endpoint(arc.arc.dst.node, arc.arc.dst.port)}{leg}",
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
            diags.error(errors.INTERFACE_DUPLICATE_SPOT, f"interface inputs {name!r} and {spot_owner[spot]!r} both bind spot {spot!r}")
            continue
        consumer = workflow.entry_inputs.get(name)
        if consumer is None:
            object_bearing = workflow.entry_input_ports.get(name)
            if object_bearing is None:
                diags.error(errors.INTERFACE_UNKNOWN_PORT, f"interface input {name!r} is not an entry input of the workflow")
            elif not object_bearing:
                diags.error(errors.INTERFACE_PURE_DATA_PORT, f"interface input {name!r} is a Pure Data port and occupies no spot")
            else:
                diags.error(errors.INTERFACE_UNKNOWN_PORT, f"interface input {name!r} is a pass-through entry input with no consuming activity (out of scope)")
            continue
        spot_owner[spot] = name
        valid.append((name, spot, consumer))

    if not valid:
        return

    # A single input node: one mode placing every bound entry input at its spot,
    # no device (it holds spots only), zero duration (pinned to time 0 by cpsat).
    mode = Mode(id="interface_in", devices=(), duration=0, input_spots={}, output_spots={n: s for n, s, _ in valid})
    node_index = len(activities)
    activities.append(ActivityInstance((), "", (mode,), boundary=BoundaryInfo("input")))

    for name, _spot, consumer in valid:
        di = index_by_node.get(consumer.node)
        if di is None:
            continue  # a consumer that is not a scheduled activity; cannot happen for a valid workflow
        options = _transport_options(activities[node_index], name, activities[di], consumer.port, env)
        if not options and check_reachability:
            diags.error(
                errors.ARC_UNREACHABLE,
                f"no transporter can serve the boundary input {name!r} -> {format_endpoint(consumer.node, consumer.port)}",
            )
        arc = Arc(Endpoint((), name), Endpoint(consumer.node, consumer.port))
        arcs.append(ArcInstance(arc, node_index, di, tuple(options)))


def _spot_exists(spot: str, env: Environment, name: str, diags: Diagnostics) -> bool:
    """True iff `spot` is a `<device>.<spot>` naming a device/spot defined in the
    environment; diagnose `unknown_device` / `unknown_spot` otherwise (SPEC §9.3)."""
    parsed = parse_qualified_spot(spot)
    if parsed is None:
        diags.error(errors.MALFORMED_QUALIFIED_SPOT, f"interface spot {spot!r} for {name!r} is not a qualified spot")
        return False
    device, spot_name = parsed
    dev = env.devices.get(device)
    if dev is None:
        diags.error(errors.UNKNOWN_DEVICE, f"interface spot {spot!r} names an unknown device {device!r}")
        return False
    if spot_name not in dev.spots:
        diags.error(errors.UNKNOWN_SPOT, f"interface spot {spot!r} names an unknown spot on device {device!r}")
        return False
    return True


def _check_mode_ports(process: str, workflow: Workflow, modes: tuple[Mode, ...], diags: Diagnostics) -> None:
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
        _check_side(process, mode, "input_spots", mode.input_spots, input_names, output_names, obj_input, diags)
        _check_side(process, mode, "output_spots", mode.output_spots, output_names, input_names, obj_output, diags)
        # Coverage: every Object-bearing port must receive a spot in this mode.
        missing = (obj_input - set(mode.input_spots)) | (obj_output - set(mode.output_spots))
        if missing:
            diags.error(
                errors.MODE_PORTS_INCOMPLETE,
                f"process {process!r} mode {mode.id!r} does not map Object-bearing port(s) {sorted(missing)}",
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
                    f"process {process!r} mode {mode.id!r} maps Pure Data port {port!r} in {section}",
                )
        elif port in other_names:
            # The name exists on the process, but on the opposite side.
            diags.error(
                errors.WRONG_PORT_DIRECTION,
                f"process {process!r} mode {mode.id!r} maps port {port!r} in {section}, but it is on the other side",
            )
        else:
            diags.error(
                errors.UNKNOWN_PROCESS_PORT,
                f"process {process!r} mode {mode.id!r} maps unknown port {port!r} in {section}",
            )


def _transport_options(
    src: ActivityInstance,
    src_port: str,
    dst: ActivityInstance,
    dst_port: str,
    env: Environment,
) -> list[TransportOption]:
    """Enumerate viable transport options over the endpoint mode pairs and the
    transporters. A same-spot move is free (duration 0)."""
    options: list[TransportOption] = []
    for m, src_mode in enumerate(src.modes):
        from_spot = src_mode.output_spots.get(src_port)
        if from_spot is None:
            continue
        for n, dst_mode in enumerate(dst.modes):
            to_spot = dst_mode.input_spots.get(dst_port)
            if to_spot is None:
                continue
            for transporter in env.transporters:
                duration = env.transport_duration(transporter, from_spot, to_spot)
                if duration is not None:
                    options.append(TransportOption(m, n, transporter, from_spot, to_spot, duration))
    return options
