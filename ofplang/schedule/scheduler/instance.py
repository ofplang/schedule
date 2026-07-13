"""Combine a workflow and an environment into a solver-ready instance.

This is where the execution-layer checks live (SPECIFICATIONS.md §9.3 subset):
every invoked atomic process must have a capability with at least one mode, each
mode must map exactly the process's Object-bearing ports, and every arc must be
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
    """Each mode must map exactly the process's Object-bearing ports — no missing
    port, no spot for a Pure Data port (§9.3 coverage)."""
    sig = workflow.processes.get(process)
    if sig is None:
        return
    want_in = set(sig.object_input_names())
    want_out = set(sig.object_output_names())
    for mode in modes:
        if set(mode.input_spots) != want_in or set(mode.output_spots) != want_out:
            diags.error(
                errors.MODE_PORTS_MISMATCH,
                f"process {process!r} mode {mode.id!r} does not map exactly its Object-bearing ports",
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
