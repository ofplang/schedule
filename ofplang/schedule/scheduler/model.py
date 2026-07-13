"""Typed data model for the scheduler.

Two domains, both plain frozen dataclasses populated by the loaders:

- the **execution environment** (devices, transporters, transport durations, and
  per-process capabilities/modes), read from the environment definition (§5); and
- the **workflow** (atomic process port signatures plus the expanded node graph:
  processing activities, Object-bearing arcs, and precedence), read from the v0
  workflow by our own minimal parser (D17).

The pipeline-internal solver instance and the rendered plan are defined by their
own modules (`instance`, `plan`); this module holds only the parsed inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

# A node path (SPECIFICATIONS.md §6.3): node ids from the entry composite's body
# down to the atomic invocation. Single-level workflows yield a one-tuple. It is
# the stable identity of a processing activity.
NodePath = tuple[str, ...]


# --------------------------------------------------------------------------
# Execution environment (§5)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Mode:
    """One way to run a process: the device(s) it occupies, its duration, and the
    spot bound to each Object-bearing port (qualified `device.spot`)."""

    id: str
    devices: tuple[str, ...]
    duration: int
    input_spots: dict[str, str]
    output_spots: dict[str, str]


@dataclass(frozen=True)
class ProcessCapability:
    """The modes available for one atomic process definition (keyed by its name)."""

    name: str
    modes: tuple[Mode, ...]


@dataclass(frozen=True)
class Device:
    id: str
    spots: frozenset[str]


@dataclass(frozen=True)
class Environment:
    time_unit: str
    devices: dict[str, Device]
    transporters: tuple[str, ...]
    # (transporter, from_spot, to_spot) -> duration, both spots qualified.
    transports: dict[tuple[str, str, str], int]
    processes: dict[str, ProcessCapability]
    objective_kind: str = "makespan"

    def transport_duration(self, transporter: str, frm: str, to: str) -> int | None:
        """Duration for one transporter to move `frm` -> `to`, or None if it
        cannot (no table entry). A same-spot move is always 0 (§5.4)."""
        if frm == to:
            return 0
        return self.transports.get((transporter, frm, to))


# --------------------------------------------------------------------------
# Workflow (the v0 dataflow graph, minimally parsed)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Port:
    """A process port; `object_bearing` is true iff its type carries an Object
    slot (§5), which is what makes it occupy a spot and generate transport."""

    name: str
    object_bearing: bool


@dataclass(frozen=True)
class AtomicProcess:
    """An atomic process definition's port signature (used for Object-bearing
    detection and mode/port coverage checks)."""

    name: str
    inputs: tuple[Port, ...]
    outputs: tuple[Port, ...]

    def object_input_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.inputs if p.object_bearing)

    def object_output_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.outputs if p.object_bearing)


@dataclass(frozen=True)
class NodeInvocation:
    """One atomic processing activity: its node path and the process it invokes."""

    path: NodePath
    process: str


@dataclass(frozen=True)
class Endpoint:
    """One side of an arc: the node that owns the port and the port name."""

    node: NodePath
    port: str


@dataclass(frozen=True)
class Arc:
    """An Object-bearing connection (source output port -> destination input
    port); each generates one transport activity."""

    src: Endpoint
    dst: Endpoint


@dataclass(frozen=True)
class Workflow:
    """The expanded, schedulable graph: atomic activities, Object-bearing arcs,
    precedence edges (a superset of arcs, covering Pure Data dependencies too),
    and the atomic process signatures referenced by the activities."""

    activities: tuple[NodeInvocation, ...]
    arcs: tuple[Arc, ...]
    precedence: tuple[tuple[NodePath, NodePath], ...]
    processes: dict[str, AtomicProcess]
