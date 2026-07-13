# ofplang.schedule — Specification (draft)

> **Status: draft.** This document captures the current design. The scheduling
> model, the execution environment schema (§5), the execution document schema that
> serves as both plan and status (§6, §7), the identifiers (§8), the schema-
> validator scope (§9), and the error-code catalog (§10) are settled.

## 1. Overview

`ofplang.schedule` is a scheduler for **Object-flow Programming Language v0**
(defined in [ofplang/spec](https://github.com/ofplang/spec)). It plans *when* the
work in a workflow runs.

It consumes:

- an **ofplang v0 workflow** (the logical dataflow graph), and
- an **execution environment definition** (devices, timing, transport), which
  supplies the physical layer the language deliberately omits,

and produces an **execution plan** (what runs where and when).

It also supports **replanning**: given a workflow, an environment, and an
**execution status** describing what has happened so far, it produces an updated
plan for the remaining work.

Scope is intentionally narrow:

- A single workflow at a time (not multiple concurrent workflows).
- Replanning is in scope.

## 2. Supported v0 subset

The scheduler targets a subset of v0. The following are **out of scope** for the
initial versions:

- **Structured nodes** — `node_map`, `node_fold`, `node_do_while`, `node_branch`.
  Excluding these keeps the schedulable graph a static, non-branching DAG.
- **Scheduling policies** — `scheduling_policies` (both scheduling and Object
  policy targets). Documents that declare the feature or carry a `scheduling`
  section are accepted, but the policies are **ignored** (not applied).
- **Contracts / constraints** — a graph-time and runtime verification concern,
  ignored here.

In scope:

- Core dataflow: atomic and composite processes, node invocation bindings, ports,
  linear Object tracking, and the atomic `objects` section
  (`map` / `consume` / `create` / `transform`).
- **`python_script_processes`** — per v0 §22.1 these are Pure Data only (no
  Object-bearing ports, no `objects` section). They are treated as opaque
  Pure Data atomic steps: they take time but occupy no spot and are not
  transported. Their code is not executed by the scheduler.

## 3. Inputs and outputs

| Artifact | Role | Nature |
| --- | --- | --- |
| ofplang workflow YAML | The logical DAG (what to do; data / Object flow) | Logical, invariant |
| Execution environment YAML (§5) | Where / how long (capabilities, durations, transport) | Physical, static, reusable |
| Execution status YAML (§6, §7) | What has happened (actuals, fixed state); replanning only | Dynamic |
| **Execution plan YAML (§6)** | Output: what runs where and when | Result |

The initial state at the start of a run — where Object-bearing material sits —
belongs to the execution status input, not the environment definition.

## 4. Scheduling model

### 4.1 Time

Time is measured in non-negative integers. The granularity and meaning of one
unit come from the environment definition (`time.unit`).

### 4.2 What is scheduled

The scheduled units are **activities**, each with a start and an end. There are
two kinds: a **processing** activity for each atomic process invocation (composite
processes are structural and expand into their atomic invocations), and a
**transport** activity for each Object-bearing arc (§4.5). Future versions may add
further activity kinds (e.g. replenishment).

### 4.3 Object-bearing values vs Pure Data

The v0 distinction between Object-bearing and Pure Data ports drives the physical
model:

| Port kind | Spot | Transport | Meaning |
| --- | --- | --- | --- |
| Object-bearing | Occupies a spot | Transported between spots | Physical material |
| Pure Data | None | None | Information; produces a dependency (ordering) only |

### 4.4 Devices and spots (the exclusive resources)

Both **devices** and **spots** are exclusive resources; the model applies
mutual-exclusion to each.

A **spot** is a holding/processing position on a device. A spot holds at most one
item at a time: the intervals that occupy a spot may not overlap. Material simply
resting in a spot occupies that spot and nothing else.

A **device** is a machine that owns spots and carries out work. A device is
occupied only while one of its spots is being **accessed** by an activity — a
processing activity over its `[start, end]`, or a transport picking up from or
dropping off at a spot over the transport interval (§4.5). Idle material resting in
a spot does **not** occupy the device. A device permits at most one access at a
time: activities that occupy a device may not overlap.

Consequently one device may own several spots and hold several items at once (e.g.
a storage hotel, or an incubator with several plate positions) — the items merely
occupy their spots. What a device cannot do is *access* two spots at once: two
activities that touch the same device are serialised.

- A processing activity occupies its device(s) (see §4.4.1) and the spots bound to
  its Object-bearing input and output ports over `[start, end]`.
- Spot occupation can extend **beyond** an activity's own interval — material waits
  in a spot before and after transport (§4.5) — while the device is free during
  that wait.

#### 4.4.1 Multi-device activities

An activity may occupy **more than one device** at once (e.g. a transport activity
occupies its source device, its destination device, and a transporter; §4.5). A
processing mode may therefore declare more than one device (§5.5). Each occupied
device is subject to the same non-overlap rule.

### 4.5 Transport

Each **Object-bearing arc** (an Object flowing from one activity's output to
another's input) generates one **transport activity**. Pure Data arcs generate a
dependency only, no transport.

A transport activity from source spot `p` to destination spot `q`, with
transport start `a` and end `b`, source activity end `e_i`, and destination
activity start `s_j`:

- selects one **transporter** capable of moving `p → q`;
- has duration `d(transporter, p, q)` (see §5), so `b = a + d`;
- occupies:
  - the **source spot** over `[e_i, b]` — material stays in the source spot from
    the moment the source activity finishes until it has been transported away;
  - the **destination spot** over `[a, s_j]` — reserved from the start of
    transport until the destination activity begins;
  - the **source device**, the **destination device**, and the **transporter**
    (all devices, §4.4 / §4.6) over `[a, b]` — the transporter is busy for the
    whole move, and accessing the source and destination spots occupies their
    devices for that interval.
- If `p == q` the two spot intervals collapse to `[e_i, s_j]` and the duration is
  zero.

(This three-device occupation is the conservative formulation of ofp-scheduler's
final model; a looser alternative would occupy only the transporter.)

Ordering: `a >= e_i` and `s_j >= b`.

### 4.6 Transporters

A transporter is an **individual** device with its own id, exclusive like any
device: the transport activities assigned to a given transporter may not overlap
in time (one move at a time per transporter). Multiple transporters are multiple
distinct ids, and each transport activity is assigned to one of them.

- The **initial version uses a single transporter**, so all transports are
  serialized on it.
- The transporter's position and any empty-travel (repositioning) time are **not**
  modelled. Transport duration depends only on the chosen transporter and the
  source/destination spot pair.

### 4.7 Objective

The only objective in the initial version is **makespan** minimisation. The
objective may be supplied by the environment definition or overridden on the
command line; only makespan is accepted for now.

### 4.8 Replanning

Replanning takes the workflow, the environment, and an execution status, and
produces an updated plan for the remaining work. Completed and running activities
are fixed to their actual times and assignments; unstarted activities are
re-optimised. Durations are otherwise deterministic, though a replan may supply
revised estimates.

## 5. Execution environment definition schema

The execution environment definition is a YAML document with the following
top-level sections. `time`, `devices`, and `processes` are required; `transporters`,
`transports`, and `objective` are optional (`transporters` / `transports` may be
omitted when the workflow has no Object-bearing arcs). Durations are YAML integers.

### 5.1 `time`

- `unit` — the meaning of one time unit (e.g. `second`, `minute`). Times are
  non-negative integers at this granularity.

### 5.2 `devices`

A non-empty list of devices. Each device is an exclusive resource (§4.4) and groups
spots.

- `id` — unique device id.
- `spots` — a list of spot names belonging to this device (may be empty). A spot name is unique
  **within its device**; the globally unique spot id is the qualified form
  `<device>.<spot>` (see §8). Because neither part contains a `.`, the qualified
  form parses unambiguously.

### 5.3 `transporters`

A list of transporters (§4.6). Each entry is an individual transporter:

- `id` — unique transporter id.

The initial version uses a single transporter; the schema permits several.

### 5.4 `transports`

The transport-duration table, keyed by `(transporter, from_spot, to_spot)`:

- `transporter` — a defined transporter id.
- `from` — source spot, a defined spot in qualified form `<device>.<spot>` (§8).
- `to` — destination spot, a defined spot in qualified form `<device>.<spot>` (§8).
- `duration` — a non-negative integer, in `time.unit`.

Semantics:

- A missing `(transporter, from, to)` entry means that transporter **cannot**
  perform that move (reachability is expressed by presence in the table). At
  least one transporter must be able to perform each required move.
- Same-spot moves (`from == to`) are treated as duration `0` and may be omitted.

### 5.5 `processes`

Execution capability, keyed by the **atomic process definition name** used in the
workflow. Capability is attached per process definition; there is no per-node-
instance override.

Each process has a non-empty list of **modes**. A mode is one way to run the
process:

- `id` — an optional mode id, unique within the process. If omitted it is assigned
  automatically (e.g. by position). The execution plan records the selected mode by
  this id, and replanning fixes a completed/running activity to its mode by id.
- `devices` — a **list** of device ids the mode occupies simultaneously (§4.4.1).
  Usually one device, but a mode may occupy several. **Optional**: a
  Pure-Data-only process (e.g. a `python_script` step) may omit `devices` and
  declare only a `duration`, occupying no device and no spot.
- `duration` — the estimated processing time, a **positive** integer in
  `time.unit` (transport durations, §5.4, may be zero, but a processing mode may
  not).
- `input_spots` — a mapping from **Object-bearing** input port name to a spot,
  given in **qualified form** `<device>.<spot>` (§8). The qualified form is
  required because a mode may name more than one device, so a bare local spot name
  would be ambiguous. The device must be one of the mode's `devices`.
- `output_spots` — a mapping from **Object-bearing** output port name to a
  qualified spot. Within one mode, input ports must not share a spot with each
  other and output ports must not share a spot with each other, but an input and
  an output port may share a spot.

Pure Data ports are not listed in `input_spots` / `output_spots` (they occupy no
spot).

### 5.6 `objective` (optional)

- `kind` — the objective. Only `makespan` is accepted in the initial version. May
  be overridden on the command line. When `objective` is omitted, the default is
  `makespan`.

### 5.7 Example

```yaml
time:
  unit: second

devices:                              # ids use [A-Za-z_][A-Za-z0-9_]* (no hyphens)
  - id: incubator_0
    spots: [slot_0, slot_1]           # two plate positions on one device
  - id: reader_0
    spots: [stage]
  - id: hotel_0                       # storage / buffer
    spots: [h0, h1]

transporters:
  - id: arm_0
  - id: arm_1

transports:  # from/to are qualified <device>.<spot>
  - { transporter: arm_0, from: incubator_0.slot_0, to: reader_0.stage, duration: 20 }
  - { transporter: arm_1, from: incubator_0.slot_0, to: reader_0.stage, duration: 15 }  # arm_1 is faster
  - { transporter: arm_0, from: reader_0.stage,     to: hotel_0.h0,     duration: 25 }
  # no arm_1 entry for reader_0.stage -> hotel_0.h0, so arm_1 cannot make that move

processes:
  heat_sample:                        # keyed by the v0 process definition name
    modes:
      - id: fast
        devices: [incubator_0]        # a list; may name several devices
        duration: 60
        input_spots:  { plate: incubator_0.slot_0 }   # qualified <device>.<spot>
        output_spots: { plate: incubator_0.slot_0 }
  measure_od:
    modes:
      - devices: [reader_0]           # no id -> auto-assigned (e.g. "0")
        duration: 45
        input_spots:  { plate: reader_0.stage }
        output_spots: { plate: reader_0.stage }
  compute_mean:                       # Pure Data only -> duration only
    modes:
      - id: mean_v1
        duration: 5

objective:
  kind: makespan
```

## 6. Execution document (plan and status)

The scheduler's output (an **execution plan**) and the replanning input (an
**execution status**) share one schema — an *execution document*. A plan and a
status are the same shape filled differently:

- a **plan** carries a solved schedule (every activity `pending`, with its planned
  times and assignments);
- a **status** carries what has happened so far (some activities `completed` or
  `running`, with actual times) plus the current material placement.

Fields that appear in only one use are optional. Times are non-negative integers
in `time.unit`.

An activity is **action-first**: its main fields say what is actually done (the
concrete operation and when), and the workflow provenance is carried alongside.
`node` (processing) and `arc` (transport) are **required**: they map an activity to
its workflow position, which is the identity used to line a status up against a
plan (§6.6) and cannot be reconstructed from the other files. The derivable echo
(`devices`, spots) may be omitted — it follows from `process` + `mode` + the
environment.

### 6.1 Top level

- `time` (optional) — `unit`; echoed from the environment.
- `now` (optional) — the reference time. A plan usually omits it; a replanning
  status sets it, and the remaining work is scheduled at or after it.
- `outcome` (optional) — the solver result: `optimal`, `feasible`, `infeasible`,
  or `unknown` (`unknown` = feasible but optimality unproven, e.g. on timeout).
  Present on a plan; absent on a status input.
- `objective` (optional) — `kind` (`makespan`) and `value`.
- `activities` (required).
- `placements` (optional) — where Object-bearing material sits at `now` (§6.5).
- `meta` (optional) — provenance, e.g. `workflow` and `environment` source
  references.

### 6.2 Activity — common fields

- `kind` (required) — `processing` or `transport`.
- `status` (optional) — `pending`, `running`, or `completed`; default `pending`.
  A plan leaves it out (all activities are pending). A status sets `completed` /
  `running` on the activities that have started; pending activities are omitted
  from a status (the scheduler re-derives them from the workflow).
- `start`, `end` (required) — integers in `time.unit`. Planned times on a plan;
  actual times on a `completed` activity; on a `running` activity `start` is
  actual and `end` is the expected finish.

### 6.3 Processing activity

- `kind: processing`; plus `status` / `start` / `end` (§6.2).
- `process` (required) — the atomic process definition invoked.
- `mode` (required) — the selected mode id (process-local, §5.5; the auto-assigned
  id if the mode had none), resolved against `process`.
- `node` (required) — provenance / identity: the node path, i.e. node ids from the
  entry composite's body down to the atomic node invoked, as a list (e.g.
  `[brew, heat]`); a single-level workflow yields a one-element list.
- `devices`, `input_spots`, `output_spots` (optional) — derivable echo of the
  mode's devices and qualified spot mappings. A Pure-Data-only activity has none.

### 6.4 Transport activity

- `kind: transport`; plus `status` / `start` / `end` (§6.2).
- `from_spot` (required) — the qualified source spot `<device>.<spot>`.
- `to_spot` (required) — the qualified destination spot.
- `transporter` (required) — the selected transporter id. The activity also
  occupies the source and destination devices (§4.5); all three are derivable
  (from `transporter`, `from_spot`, `to_spot`), so there is no `devices` field.
- `arc` (required) — provenance / identity: the Object-bearing arc served, as
  `from` / `to`, each `{ node: <path>, port: <name> }`.

Durations are not stored (`end - start`).

### 6.5 Placements

`placements` records where Object-bearing material sits at `now` when that is not
already implied by the activities — chiefly the workflow's entry inputs at the
start of a run. Each entry is:

- `object` — the material: `{ input: <name> }` for an entry input of the workflow,
  or `{ node: <path>, port: <name> }` for the output of a produced Object.
- `spot` — the qualified spot `<device>.<spot>` it occupies.

For example, at the start of a run the entry input sits where the first step
expects it:

```yaml
placements:
  - object: { input: sample }
    spot: incubator_0.slot_0
```

### 6.6 Identity and replanning

A status is matched against the workflow (and any prior plan) by each activity's
provenance: a processing activity by its `node`, a transport activity by its
`arc`. On a replan the scheduler fixes `completed` and `running` activities to
their reported times and assignments, takes material positions from `placements`,
and re-optimises the rest at or after `now`. The main fields are not identities —
a process/mode or a spot/transporter combination does not distinguish repeated
occurrences, and the times change from one plan to the next.

### 6.7 Examples

The same schema, filled two ways.

**As a plan** (solver output):

```yaml
outcome: optimal
objective:
  kind: makespan
  value: 130
time:
  unit: second

activities:
  - kind: processing
    start: 0
    end: 60
    process: heat_sample
    mode: fast
    node: [heat]                              # required provenance / identity
    devices: [incubator_0]                    # optional echo
    input_spots:  { plate: incubator_0.slot_0 }
    output_spots: { plate: incubator_0.slot_0 }

  - kind: transport
    start: 60
    end: 80
    from_spot: incubator_0.slot_0
    to_spot:   reader_0.stage
    transporter: arm_0
    arc:                                      # required provenance / identity
      from: { node: [heat],  port: plate }
      to:   { node: [assay], port: plate }

  - kind: processing
    start: 80
    end: 125
    process: measure_od
    mode: "0"                                 # auto-assigned (the mode had no id)
    node: [assay]                             # echo omitted here

  - kind: processing                          # Pure Data compute: no device / spot
    start: 125
    end: 130
    process: compute_mean
    mode: mean_v1
    node: [compute]

meta:
  workflow: workflow.yaml
  environment: env.yaml
```

**As a status** (replanning input at `now = 70`):

```yaml
time:
  unit: second
now: 70

activities:
  - kind: processing
    status: completed
    start: 0
    end: 60
    process: heat_sample
    mode: fast
    node: [heat]

  - kind: transport
    status: running
    start: 60
    end: 80                                   # expected finish
    from_spot: incubator_0.slot_0
    to_spot:   reader_0.stage
    transporter: arm_0
    arc:
      from: { node: [heat],  port: plate }
      to:   { node: [assay], port: plate }

# No `placements` here: the plate is handled by the running transport, and the
# entry input was already consumed by `heat`. `placements` lists only material not
# implied by the activities (see §6.5).
```

## 7. Execution status

The execution status is the replanning input. It is the **same document as the
execution plan (§6)**, used with `now` set, a `status` on each started activity,
and `placements` giving the current material positions; see §6, and the status
example in §6.7.

## 8. Identifiers

### 8.1 Syntax

All ids defined by the environment (device, spot, transporter) use the v0
identifier grammar:

```text
[A-Za-z_][A-Za-z0-9_]*
```

ASCII, case-sensitive. Note the grammar allows `_` but **not `-`**, and `.` is
excluded (reserved as the separator for qualified spot ids, §8.2). The v0
reserved-word list applies to the workflow, not to environment-local ids.

Process names and port names are **referenced from the workflow** and therefore
follow the v0 rules for those positions.

### 8.2 Uniqueness and namespaces

- **device id** — unique among devices.
- **transporter id** — unique among transporters.
- **spot** — a spot name is unique within its device. The globally unique spot id
  is the qualified form `<device>.<spot>`. Both `modes` and `transports` reference
  spots by the qualified form (a mode may name several devices, so a bare local
  name would be ambiguous).
- **Cross-kind coincidence is allowed**: a device, a spot name, and a transporter
  may share the same string, and a port name may coincide with any of them (port
  names live in a per-process, per-direction namespace — v0 §2.4). Coincidence
  across the device / spot / transporter kinds is permitted but **should raise a
  warning** for readability.

## 9. Validation

Validation splits into **schema validators** (shape only, standalone) and
**execution-layer validation** (needs the workflow or solvability).

`ofplang.schedule` provides a schema validator for each document it defines: the
execution environment definition (§9.1) and the execution document — plan or
status (§9.2). Each checks only the shape of its own document and does not read the
workflow; `ofplang.validate` is not involved.

Diagnostics follow the same convention as `ofplang.validate`: stable **error
codes** with a `file:line:col` source position (§10). Each diagnostic has a
`severity` of `error` or `warning`; warnings (e.g. cross-kind id coincidence, §8.2)
do not make a document invalid. The codes are shared across `ofplang.schedule`'s
own validators, but are a separate catalog from `ofplang.validate`'s.

**Execution-layer validation** (§9.3) covers everything that needs the workflow or
depends on solvability. The execution layer reads the workflow itself with
`ofplang.schedule`'s own minimal parser — extracting only what it needs (process
kinds, type domains, per-port Object-bearing-ness) — without depending on
`ofplang.validate`. Full v0 validation of the workflow is out of scope here and is
expected to be done separately (by `ofplang.validate`); the scheduler assumes it is
given a valid v0 workflow.

### 9.1 Schema validator — the environment definition (shape only)

- Identifier syntax (§8.1) and per-kind uniqueness (§8.2); cross-kind coincidence
  raises a warning.
- Required sections present (`time`, `devices`, `processes`); `devices` non-empty;
  each process has at least one mode.
- Value constraints: a processing `duration` is a positive YAML integer, a
  transport `duration` is a non-negative YAML integer; `time.unit` is a non-empty
  string; if `objective` is present, its `kind` is `makespan`.
- Env-internal references: each `transports.transporter` is a defined transporter;
  every `from` / `to` and every `input_spots` / `output_spots` value is a
  well-formed qualified spot `<device>.<spot>` (exactly one `.`) naming a defined
  device and a spot defined on it.
- Duplicate detection: duplicate ids within a kind (two devices, two transporters,
  or two spots on one device sharing an id); duplicate `transports` entries for the
  same `(transporter, from, to)`.
- Intra-mode spot rules: within a mode, input ports do not share a spot, output
  ports do not share a spot, and every referenced spot's device is one of the
  mode's `devices`.
- **Unknown or extra keys are errors** (strict only; there is no
  extension-tolerant mode).

### 9.2 Schema validator — the execution document (shape only)

The same shape-only approach applies to an execution document (§6), used as a plan
or a status. Cross-document checks (that a `node` / `arc` / `process` exists in the
workflow, or that a spot exists in the environment) are execution-layer (§9.3).

- Top level: `activities` is required; `time` / `now` / `outcome` / `objective` /
  `placements` / `meta` are optional. Unknown or extra keys are errors.
- `now` (if present) is a non-negative integer; `outcome` (if present) is one of
  `optimal` / `feasible` / `infeasible` / `unknown`; `objective` (if present) has
  `kind: makespan` and a non-negative integer `value`.
- Each activity: `kind` is required and is `processing` or `transport`; `status`
  (if present) is `pending` / `running` / `completed`; `start` and `end` are
  required non-negative integers with `end >= start`. Unknown keys are errors.
  - processing: `process`, `mode`, and `node` (a non-empty list of identifiers) are
    required; `devices`, `input_spots`, `output_spots` are optional.
  - transport: `from_spot`, `to_spot` (qualified spots), `transporter`, and `arc`
    (`from` / `to`, each `{ node: <list>, port: <id> }`) are required.
- `placements` (if present): each entry is `{ object, spot }`, where `object` is
  exactly one of `{ input: <name> }` or `{ node: <list>, port: <id> }`, and `spot`
  is a qualified spot.
- Form rules: identifiers match `[A-Za-z_][A-Za-z0-9_]*`; a qualified spot has
  exactly one `.` with identifier parts; a node path is a non-empty list of
  identifiers.

### 9.3 Execution-layer validation (needs the workflow / solvability)

Not part of a schema validator; checked by the execution layer while building the
solver instance. The catalog of codes these checks emit is §10.4.

The scheduler is capability-driven by the processes it actually schedules: it
expands the entry composite into the atomic, in-scope invocations it will run and
validates the capability of each. Existence, atomic-ness, and scope (§2) hold by
construction for those invocations (a structured node or a nested composite is
diagnosed separately as `unsupported_feature`, not scheduled). Capabilities
declared in the environment for processes the workflow never invokes are not
checked.

- **Against the workflow** — for each invoked process, its capability's modes are
  checked port by port against the process's signature. A mapped port the process
  does not have is `unknown_process_port`; a port mapped on the wrong side (an
  output under `input_spots`, or an input under `output_spots`) is
  `wrong_port_direction`; a Pure Data port given a spot is `pure_data_port_mapped`
  (only Object-bearing ports occupy spots).
- **Coverage / completeness**: every atomic process actually invoked by the
  workflow has at least one mode (`no_capability` otherwise), and every mode maps
  every Object-bearing port of its process (`mode_ports_incomplete` otherwise).
- **Reachability / solvability**: for each Object-bearing arc, a feasible
  combination of endpoint modes and a transporter that can move between the
  chosen spots exists (`arc_unreachable` otherwise). This depends on mode
  selection and is a solvability concern, not a schema check.

## 10. Error codes

Stable codes for the schema validators (§9.1, §9.2). Codes are shared across
`ofplang.schedule`'s validators, and are a separate catalog from
`ofplang.validate`'s. Severity is `error` unless marked *warning*.

### 10.1 Shared

| code | meaning |
| --- | --- |
| `unknown_key` | an unknown or extra key (strict) |
| `missing_required_field` | a required field is absent (the path locates it) |
| `wrong_type` | a value has the wrong YAML type (mapping / list / integer / string) |
| `invalid_identifier` | an id does not match `[A-Za-z_][A-Za-z0-9_]*` |
| `malformed_qualified_spot` | a spot is not in `<device>.<spot>` form (exactly one `.`) |
| `unknown_objective_kind` | `objective.kind` is not `makespan` |
| `negative_value` | an integer that must be non-negative is negative |

### 10.2 Environment definition (§9.1)

| code | meaning |
| --- | --- |
| `missing_required_section` | `time`, `devices`, or `processes` is absent |
| `empty_devices` | `devices` is empty |
| `empty_modes` | a process has no modes |
| `duplicate_device_id` | a device id repeats |
| `duplicate_transporter_id` | a transporter id repeats |
| `duplicate_spot_id` | a spot name repeats within a device |
| `cross_kind_id_coincidence` | a device / spot / transporter share an id (*warning*) |
| `nonpositive_duration` | a processing mode `duration` is not positive |
| `empty_time_unit` | `time.unit` is empty or not a string |
| `unknown_transporter` | `transports.transporter` is not a defined transporter |
| `unknown_device` | the device part of a qualified spot is not defined |
| `unknown_spot` | the spot part of a qualified spot is not defined on that device |
| `duplicate_transport_entry` | a `(transporter, from, to)` triple repeats |
| `input_spots_share_spot` | two input ports of a mode use the same spot |
| `output_spots_share_spot` | two output ports of a mode use the same spot |
| `spot_device_not_in_mode` | a mode's spot is on a device not in that mode's `devices` |

### 10.3 Execution document (§9.2)

| code | meaning |
| --- | --- |
| `missing_activities` | `activities` is absent |
| `unknown_activity_kind` | `kind` is not `processing` or `transport` |
| `unknown_status` | `status` is not `pending` / `running` / `completed` |
| `unknown_outcome` | `outcome` is not one of the defined values |
| `end_before_start` | `end` is earlier than `start` |
| `empty_node_path` | `node` is an empty list |
| `malformed_arc` | an `arc`'s `from` / `to` / `node` / `port` structure is wrong |
| `malformed_placement` | a `placements` `object` is not exactly one of `input` or `node` + `port` |

Absent `process` / `mode` / `from_spot` and similar use the shared
`missing_required_field`; type violations use `wrong_type`.

### 10.4 Execution layer (§9.3)

Emitted by the scheduler (not a schema validator) while reading the workflow and
building the solver instance. All are error severity.

| code | meaning |
| --- | --- |
| `unsupported_feature` | a workflow feature outside the scheduler's v0 subset (a structured node, a nested composite) |
| `no_entry_process` | the workflow has no resolvable entry process |
| `process_not_defined` | a node invokes, or an arc references, a process/node not defined in the workflow |
| `no_capability` | an invoked atomic process has no capability (or no modes) in the environment |
| `unknown_process_port` | a mode's `input_spots` / `output_spots` names a port the process does not have |
| `wrong_port_direction` | a port is mapped on the wrong side (an output under `input_spots`, or an input under `output_spots`) |
| `pure_data_port_mapped` | a mode maps a Pure Data (non-Object-bearing) port to a spot |
| `mode_ports_incomplete` | a mode does not map every Object-bearing port of its process |
| `arc_unreachable` | no endpoint-mode pair and transporter can serve an Object-bearing arc |
| `infeasible` | the solver proved the instance has no feasible schedule |
