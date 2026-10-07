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

from ofplang.schedule.core.identifiers import format_element

# A node path (SPECIFICATIONS.md §6.3): node ids from the entry composite's body
# down to the atomic invocation. Single-level workflows yield a one-tuple. It is
# the stable identity of a processing activity. Inside a `map` / `fold` node the
# path carries an iteration index (an int) straight after that node's id --
# `("Wash", 2, "aspirate")` is node `aspirate` in invocation 2 of `Wash`. The index
# stays an int everywhere, in the plan and in a status read back, never the string
# "2": these paths are compared as tuples, so the two would silently not match.
NodePath = tuple[str | int, ...]


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
    # (transporter, from_spot, to_spot) -> duration, both spots qualified. The
    # transporter is None for a route that needs none (SPEC section 5.4).
    transports: dict[tuple[str | None, str, str], int]
    processes: dict[str, ProcessCapability]
    # No `objective` here. How a run is to be optimised is a property of that run,
    # not of the lab, so it is declared in the execution document (§6.1); this
    # environment was the second declaration site until 0.2.1.
    # Replenishers (§5.6) and the (replenisher, device) -> duration table (§5.7).
    # Like transporters and transports, and absent for the same kind of reason:
    # an environment where nothing is refilled needs neither.
    replenishers: tuple[str, ...] = ()
    replenishments: dict[tuple[str, str], int] = field(default_factory=dict)

    def transport_duration(self, transporter: str | None, frm: str, to: str) -> int | None:
        """Duration for one transporter to move `frm` -> `to`, or None if it
        cannot (no table entry). `transporter` is None to ask for the route that
        needs no transporter (§5.4). A same-spot move is always 0 (§5.4) -- for
        every transporter and for None alike, a hand-off within one spot being a
        physical no-op the table need not mention."""
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
    """One side of an arc: the node that owns the port and the port name.

    `index` selects one element of an Array-valued port -- one per nesting level,
    outermost first -- where the arc carries a single element rather than the whole
    value (SPECIFICATIONS.md §6.4). Empty, the default, is the whole port, which is
    every endpoint of a workflow without Arrays of Objects."""

    node: NodePath
    port: str
    index: tuple[int, ...] = ()


def slot_key(endpoint: Endpoint) -> str:
    """The key a mode's `input_spots` / `output_spots` hold this endpoint's spot
    under: the port name, or -- for one element of an Array-valued port, which sits
    on a spot of its own -- the port with its index, `plates[2]`.

    Only a boundary node (§6.8) has element keys in this stage: an atomic process's
    modes map whole ports. A port name is an identifier and cannot contain `[`, so an
    element key never collides with a port. Every lookup of an arc endpoint's spot
    goes through here, so a whole-port endpoint finds exactly what it always did."""
    return format_element(endpoint.port, endpoint.index)


@dataclass(frozen=True)
class Arc:
    """An Object-bearing connection (source output port -> destination input
    port); each generates one transport activity."""

    src: Endpoint
    dst: Endpoint


# --------------------------------------------------------------------------
# Where a value comes from (for the `ofplang-run` runner, D57)
# --------------------------------------------------------------------------
#
# A `Source` says where the value of one port comes from, as a small tree. One
# shape covers an arc from one producer, a literal, a boundary input, an Array
# gathered from several producers (the collected output of a `map`) and one
# element taken out of a producer's Array (a `map` traversing it), in three cases:
#
#   SourceRef(node, port, index)  the value recorded at (node, port) -- or, with an
#                                 `index`, one element of it. `node == ()` is the
#                                 workflow boundary, where entry inputs are seeded.
#   SourceLiteral(value)          a static literal, with no producer at all.
#   SourceSeq(items)              an Array assembled element by element, each from
#                                 its own Source; `SourceSeq(())` is the empty Array.
#
# A whole value that one producer recorded is one SourceRef, not a SourceSeq of its
# elements: the tree is only as deep as the value is actually assembled. These are
# additive metadata the scheduler MUST NOT read for planning, and their node paths
# are the plan's (see `Workflow.input_sources`).


@dataclass(frozen=True)
class SourceRef:
    node: NodePath
    port: str
    index: tuple[int, ...] = ()


@dataclass(frozen=True)
class SourceLiteral:
    value: object


@dataclass(frozen=True)
class SourceSeq:
    items: tuple[Source, ...]


Source = SourceRef | SourceLiteral | SourceSeq


@dataclass(frozen=True)
class LengthCheck:
    """A length the plan was built on but the scheduler could not see (D57).

    A `map` / `fold` takes its invocation count from the `each` sources whose length
    is known before the run -- an Array of Objects bound in `interface`, a literal.
    A Pure Data `each` source zipped with one of those has a length only its value
    shows, so the plan assumes it equals `length` and the runner, the one layer that
    sees values, must check it (spec §17 / §18, zip-equal; a mismatch is a preflight
    error for a run-phase value and a runtime data error for a data-phase one).

    `node` is the structured node's path, `port` the `each` port, `source` where that
    port's Array comes from."""

    node: NodePath
    port: str
    source: Source
    length: int


@dataclass(frozen=True)
class CompositeIO:
    """The value-layer boundary of one composite invocation, for external consumers
    only (the sibling `ofplang-run` runner's composite contract checks, D34).

    A composite is flattened away in the schedulable graph -- only atomic activities
    remain -- so where the values crossing a composite invocation's own ports come from
    is otherwise lost. This records it, as `Source` trees: each input / output port ->
    the value it reads (an atomic's output, the workflow boundary, a literal, an Array
    assembled element by element). Runner-only, under the INVARIANTS stated with
    `Workflow.input_sources`."""

    process: str
    input_sources: dict[str, Source] = field(default_factory=dict)
    output_sources: dict[str, Source] = field(default_factory=dict)


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
    - `interface` is where this job's boundary material sits (§6.8) -- the same
      section a single-workflow document carries at the top level. It is per job
      because it binds one workflow's ports, and two jobs of the same workflow bind
      the same port names to different spots.
    - `expansion` is what this job's values say about how its workflow expands
      (§6.13), per job for the same reason: two jobs of one workflow are given
      different values.
    """

    id: str
    release: int = 0
    bound: int | None = None
    fingerprint: str | None = None
    interface: dict | None = None
    expansion: dict | None = None


@dataclass(frozen=True)
class Workflow:
    """The expanded, schedulable graph: atomic activities, Object-bearing arcs,
    precedence edges (a superset of arcs, covering Pure Data dependencies too),
    and the atomic process signatures referenced by the activities.

    The workflow's Object-bearing boundary (SPEC §6.8) is `entry_arcs` / `exit_arcs` /
    `through_arcs`: one arc per Object crossing it, with an empty-path endpoint on the
    boundary side. They have no in-body producer or consumer, so the scheduler attaches
    them to synthetic boundary nodes (see `instance`)."""

    activities: tuple[NodeInvocation, ...]
    arcs: tuple[Arc, ...]
    precedence: tuple[tuple[NodePath, NodePath], ...]
    processes: dict[str, AtomicProcess]
    # every main input / output port name -> whether it is Object-bearing (used to
    # classify an `interface` binding: unknown port vs Pure Data vs Object-bearing).
    entry_input_ports: dict[str, bool] = field(default_factory=dict)
    exit_output_ports: dict[str, bool] = field(default_factory=dict)
    # The Object-bearing boundary as the planner reads it (§6.8): one arc per Object
    # that crosses it -- `Endpoint((), port)` -> consumer for an entry input, producer
    # -> `Endpoint((), port)` for a final output, with the element `index` where the
    # port is an Array and each element is an Object on a spot of its own.
    entry_arcs: tuple[Arc, ...] = ()
    exit_arcs: tuple[Arc, ...] = ()
    # An Object that crosses the boundary in and straight back out, untouched by any
    # activity (an entry input returned as it came, or an element of one): one arc per
    # Object, `Endpoint((), input, index)` -> `Endpoint((), output, index)` (D60). The
    # planner moves it to the output's spot, or leaves it where it came in.
    through_arcs: tuple[Arc, ...] = ()
    # Every main input / output port -> how deeply its type nests Arrays (0 for a
    # scalar, 2 for `Array<Array<Plate>>`), which is the shape its `interface`
    # binding has to have: a spot, or lists of spots that deep.
    entry_input_ranks: dict[str, int] = field(default_factory=dict)
    exit_output_ranks: dict[str, int] = field(default_factory=dict)

    # -- The dataflow, for the runner only (D26-0, D57) ----------------------------
    #
    # WHAT: for every input port of every atomic activity, and for every final output,
    # where its value comes from, as a `Source` tree -- Object-bearing and Pure Data
    # alike, an Array gathered from a `map`'s invocations and one element of another's
    # Array included.
    #
    # WHY (this exists solely for the sibling `ofplang-run` runner): the runner routes
    # *values* from each producer to each consumer. The scheduler compiles a Pure Data
    # binding down to a node-level `precedence` edge (a value affects ordering, not
    # timing or resources), which discards which output port feeds which input port,
    # so the flattener records it here.
    #
    # INVARIANT -- do not break these, or the runner mis-routes values silently:
    #  1. Additive metadata for an external consumer. The scheduler MUST NOT use it for
    #     planning: the solver model, objective and rendered plan are byte-for-byte the
    #     same whether or not it is populated. Do not fold it into `arcs` /
    #     `precedence` / the boundary arcs, which the solver does read.
    #  2. Node paths here use the SAME convention as `arcs` and the rendered plan's
    #     `node` paths (`prefix + (node_id,)`, an iteration index after an expanded
    #     node). The runner keys its value store by them, so changing the naming
    #     silently breaks the runner -- coordinate any change with ofplang-run.
    #
    # (Through 0.13.1 the same dataflow was also given per kind -- `data_arcs`,
    # `data_entry_inputs`, `data_literals`, `entry_inputs`, `exit_outputs`,
    # `exit_literals` -- for readers from before the trees. None was left.)
    #
    # consuming atomic input Endpoint -> its Source.
    input_sources: dict[Endpoint, Source] = field(default_factory=dict)
    # main output port -> its Source; every output has one (an Object-bearing
    # pass-through is `SourceRef((), input)`, or one element of it).
    output_sources: dict[str, Source] = field(default_factory=dict)
    # `map` / `fold` node path -> its invocation count L, the length the plan was
    # built on. Recorded because L = 0 leaves no iteration path behind to count.
    iterations: dict[NodePath, int] = field(default_factory=dict)
    # Lengths the plan assumed but the runner has to check (see `LengthCheck`).
    length_checks: tuple[LengthCheck, ...] = ()
    # Nested composite invocation boundaries, keyed by the composite's node path ->
    # its `CompositeIO`. For the runner's composite contract checks only (D34), under
    # the same INVARIANTS. The top-level entry composite `()` is omitted -- the runner
    # checks it via its whole-workflow boundary handles (D33); only nested composites
    # need this. The runner uses only those with contracts.
    composites: dict[NodePath, CompositeIO] = field(default_factory=dict)
