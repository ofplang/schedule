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
from ofplang.schedule.scheduler.model import (
    Arc,
    Environment,
    Mode,
    NodePath,
    Workflow,
)
from ofplang.schedule.validation import errors


@dataclass(frozen=True)
class ActivityInstance:
    """One processing activity and the modes it may run in."""

    node: NodePath
    process: str
    modes: tuple[Mode, ...]


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
    arc: Arc
    src_activity: int
    dst_activity: int
    options: tuple[TransportOption, ...]


@dataclass(frozen=True)
class Instance:
    env: Environment
    time_unit: str
    activities: tuple[ActivityInstance, ...]
    arcs: tuple[ArcInstance, ...]
    # Precedence edges as (source activity index, destination activity index).
    precedence: tuple[tuple[int, int], ...]


def build_instance(workflow: Workflow, env: Environment) -> tuple[Instance | None, Diagnostics]:
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
            diags.error(errors.PROCESS_NOT_DEFINED, f"arc references an unknown node: {arc}")
            continue
        options = _transport_options(activities[si], arc.src.port, activities[di], arc.dst.port, env)
        if not options:
            diags.error(
                errors.ARC_UNREACHABLE,
                f"no transporter can serve the arc {arc.src.node}.{arc.src.port} -> {arc.dst.node}.{arc.dst.port}",
            )
        arcs.append(ArcInstance(arc, si, di, tuple(options)))

    precedence = tuple(
        (index_by_node[s], index_by_node[d])
        for s, d in workflow.precedence
        if s in index_by_node and d in index_by_node
    )

    if any(d.severity == "error" for d in diags.items):
        return None, diags
    return Instance(env, env.time_unit, tuple(activities), tuple(arcs), precedence), diags


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
