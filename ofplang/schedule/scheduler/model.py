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

from dataclasses import dataclass, field

# A node path (SPECIFICATIONS.md §6.3): node ids from the entry composite's body
# down to the atomic invocation. Single-level workflows yield a one-tuple. It is
# the stable identity of a processing activity.
NodePath = tuple[str, ...]


# --------------------------------------------------------------------------
# Execution environment (§5)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Mode:
    """One way to run a process: the device(s) it occupies, its duration, the
    spot bound to each Object-bearing port (qualified `device.spot`), and what it
    consumes."""

    id: str
    devices: tuple[str, ...]
    duration: int
    input_spots: dict[str, str]
    output_spots: dict[str, str]
    # Consumable drawn on when this mode runs (§4.7): qualified `device.resource`
    # -> a positive amount, taken in full at the activity's start. Qualified
    # because a mode may name several devices, exactly as its spots are. Defaulted:
    # most modes consume nothing, and a Mode is built positionally in places that
    # predate resources.
    consumption: dict[str, int] = field(default_factory=dict)
    # Whether running this mode **accesses** its devices (SPEC §4.4.2). False says
    # the material merely rests on them -- a plate chilling in a refrigerator, a rack
    # waiting in a hotel: the spots are bound as usual, but no device is held, so
    # anything else may access them meanwhile. The devices stay listed because they
    # own the spots and because a mode whose device is down is unavailable either
    # way. Defaulted like `consumption`, so the positional constructions that
    # predate it keep working.
    device_access: bool = True

    @property
    def occupied_devices(self) -> tuple[str, ...]:
        """The devices this mode holds -- empty for a non-accessing mode, which is
        the empty device set a boundary node has (FORMULATION §7)."""
        return self.devices if self.device_access else ()


@dataclass(frozen=True)
class ProcessCapability:
    """The modes available for one atomic process definition (keyed by its name)."""

    name: str
    modes: tuple[Mode, ...]


@dataclass(frozen=True)
class Device:
    id: str
    spots: frozenset[str]
    # Consumable resources this device holds (§5.2): resource name -> capacity, the
    # largest level it can hold. Names are device-local; the globally unique id is
    # the qualified `<device>.<resource>`. Empty for a device holding no consumable.
    resources: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Environment:
    time_unit: str
    devices: dict[str, Device]
    transporters: tuple[str, ...]
    # (transporter, from_spot, to_spot) -> duration, both spots qualified.
    transports: dict[tuple[str, str, str], int]
    processes: dict[str, ProcessCapability]
    # No `objective` here. How a run is to be optimised is a property of that run,
    # not of the lab, so it is declared in the execution document (§6.1); this
    # environment was the second declaration site until 0.2.1.
    # Replenishers (§5.6) and the (replenisher, device) -> duration table (§5.7).
    # Like transporters and transports, and absent for the same kind of reason:
    # an environment where nothing is refilled needs neither.
    replenishers: tuple[str, ...] = ()
    replenishments: dict[tuple[str, str], int] = field(default_factory=dict)

    def transport_duration(self, transporter: str, frm: str, to: str) -> int | None:
        """Duration for one transporter to move `frm` -> `to`, or None if it
        cannot (no table entry). A same-spot move is always 0 (§5.4)."""
        if frm == to:
            return 0
        return self.transports.get((transporter, frm, to))

    def refills(self, device: str) -> tuple[tuple[str, int], ...]:
        """The (replenisher, duration) pairs that can refill `device`, or empty if
        none can. Absence from the table is how §5.7 says "cannot", exactly as it
        does for transport -- and unlike a transport, a refill is never required, so
        empty is a legitimate answer rather than a dead end."""
        return tuple(
            (replenisher, duration)
            for (replenisher, target), duration in sorted(self.replenishments.items())
            if target == device
        )


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
class CompositeIO:
    """The value-layer boundary of one composite invocation, for external consumers
    only (the sibling `ofplang-run` runner's composite contract checks, D34).

    A composite is flattened away in the schedulable graph -- only atomic activities
    remain -- so the port-level mapping of a composite invocation's own inputs and
    outputs to the concrete values that flow across its boundary is otherwise lost.
    This records it: each input / output port maps either to the value-store key
    (an atomic output `Endpoint`, or the workflow boundary `Endpoint((), name)`) that
    supplies it, or -- when a port is bound to / returns a static literal -- to that
    literal value. Same INVARIANTS as `data_arcs` (see `Workflow`): additive metadata
    the scheduler MUST NOT read for planning, with plan-matching node paths."""

    process: str
    inputs: dict[str, Endpoint] = field(default_factory=dict)
    input_literals: dict[str, object] = field(default_factory=dict)
    outputs: dict[str, Endpoint] = field(default_factory=dict)
    output_literals: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class JobSpec:
    """One job of a joint plan, as the planner sees it (SPEC §6.11) -- the roster
    entry with its planning parameters resolved.

    Distinct from `api.JobInput`, which is what a *caller* hands over (an id and a
    workflow). This is what the roster carries and what the solver is given.

    - `release` (§6.11) is the earliest time any of the job's activities may start.
      It is **not** the priority: priority is the roster's order, so a job submitted
      today and released tomorrow still outranks one submitted tomorrow.
    - `bound` is B_j, the completion time this job was promised when it arrived --
      the whole of "an earlier job is not disturbed by a later one" (design.md D38).
      None until the job's first solve assigns it. Scheduler-owned: it is not a
      deadline, since a relaxation may loosen it, and a deadline that quietly
      loosened would be worse than none.
    - `fingerprint` identifies *which workflow* the job runs (`workflow.fingerprint`),
      so a replan cannot be handed the same ids against different workflows.
    """

    id: str
    release: int = 0
    bound: int | None = None
    fingerprint: str | None = None


@dataclass(frozen=True)
class Workflow:
    """The expanded, schedulable graph: atomic activities, Object-bearing arcs,
    precedence edges (a superset of arcs, covering Pure Data dependencies too),
    and the atomic process signatures referenced by the activities.

    `entry_inputs` / `exit_outputs` are the workflow's Object-bearing boundary
    connections (SPEC §6.8): each maps a main (entry composite) port name to the
    atomic endpoint that consumes that entry input, or produces that final output.
    These have no in-body producer/consumer, so they carry no arc here; the
    scheduler attaches them to synthetic boundary nodes when an `interface`
    constraint pins their spots (see `instance`)."""

    activities: tuple[NodeInvocation, ...]
    arcs: tuple[Arc, ...]
    precedence: tuple[tuple[NodePath, NodePath], ...]
    processes: dict[str, AtomicProcess]
    # main input port name -> the atomic input Endpoint that consumes it.
    entry_inputs: dict[str, Endpoint] = field(default_factory=dict)
    # main output port name -> the atomic output Endpoint that produces it.
    exit_outputs: dict[str, Endpoint] = field(default_factory=dict)
    # every main input / output port name -> whether it is Object-bearing (used to
    # classify an `interface` binding: unknown port vs Pure Data vs pass-through).
    entry_input_ports: dict[str, bool] = field(default_factory=dict)
    exit_output_ports: dict[str, bool] = field(default_factory=dict)

    # -- Pure Data port-level dataflow, for external consumers only (D26-0) --------
    #
    # WHAT: the port-level producer->consumer mapping of Pure Data (`bind`)
    # bindings, plus the Pure Data input boundary. The scheduler already carries the
    # Object-bearing (`state`) equivalent in `arcs` / `entry_inputs`; these two
    # fields are the Pure Data counterparts.
    #
    # WHY (this exists solely for the sibling `ofplang-run` runner): the runner
    # propagates Pure Data *values* from each producer output port to the consumer
    # input port it feeds. The scheduler compiles a `bind` down to a node-level
    # `precedence` edge only (a Pure Data value affects ordering but not timing or
    # resources), which *discards* which output port feeds which input port. That
    # mapping cannot be recovered from `precedence` (it is ambiguous when a producer
    # has several Pure Data outputs), so the flattener records it here for the runner.
    #
    # INVARIANT -- do not break these, or the runner mis-routes values silently:
    #  1. These are additive metadata for an external consumer. The scheduler MUST
    #     NOT use them for planning: the solver model, objective, and rendered plan
    #     must be byte-for-byte identical whether or not these are populated. Do not
    #     fold them into `arcs` / `precedence` / `entry_inputs` (which the solver
    #     does read) -- keep them separate.
    #  2. Endpoints here use the SAME node-path convention as `arcs` and the rendered
    #     plan's `node` paths (`prefix + (node_id,)`, entry body prefixed by `()`).
    #     The runner keys its value store by these paths, so changing the node-path
    #     naming silently breaks the runner -- see `scheduler/workflow.py` and
    #     coordinate any change with ofplang-run (its dev-notes design.md D26).
    #
    # (Pure Data final outputs need no new field: the runner reads `exit_outputs`
    # together with `exit_output_ports` to recover every return, Object or Pure Data.)
    #
    # Pure Data connection: producer output Endpoint -> consumer input Endpoint.
    data_arcs: tuple[Arc, ...] = ()
    # main Pure Data input port name -> the atomic input Endpoint that consumes it.
    data_entry_inputs: dict[str, Endpoint] = field(default_factory=dict)
    # Static literal bindings (`bind: {port: {value: ...}}`, §11): the consuming
    # atomic input Endpoint -> the literal value. Like `data_arcs`, this is additive
    # metadata for the runner only (same INVARIANTS above -- the scheduler MUST NOT
    # read it for planning, and its node paths match the plan's). The runner seeds
    # these as the input values of the ports they bind, in place of a typed default.
    data_literals: dict[Endpoint, object] = field(default_factory=dict)
    # Nested composite invocation boundaries, keyed by the composite's node path ->
    # its `CompositeIO` (port -> value-store key / literal). For the runner's composite
    # contract checks only (D34); same INVARIANTS as `data_arcs` (the scheduler MUST
    # NOT read this, and its node paths match the plan's). The top-level entry
    # composite `()` is omitted -- the runner checks it via its whole-workflow
    # boundary handles (D33); only nested composites need this. The runner uses only
    # those with contracts.
    composites: dict[NodePath, CompositeIO] = field(default_factory=dict)
