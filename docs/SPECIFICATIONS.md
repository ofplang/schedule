# ofplang.schedule — Specification (draft)

> **Status: draft.** This document captures the current design. The scheduling
> model and the execution environment schema are settled; the execution plan and
> execution status schemas are still to be defined (see §6, §7).

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
| Execution status YAML (§7) | What has happened (actuals, fixed state); replanning only | Dynamic |
| **Execution plan YAML (§6)** | Output: what runs where and when | Result |

The initial state at the start of a run (device positions, where material sits)
belongs to the execution status input, not the environment definition.

## 4. Scheduling model

### 4.1 Time

Time is measured in non-negative integers. The granularity and meaning of one
unit come from the environment definition (`time.unit`).

### 4.2 What is scheduled

Only **atomic** process invocations consume time and resources. Composite
processes are structural and expand into their constituent atomic invocations.
Each schedulable atomic invocation is an **activity** with a start and an end.

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

A **device** is a machine that carries out work. A device runs at most one
activity at a time: the activities that occupy a device may not overlap in time.

A **spot** is a holding/processing position on a device. A spot holds at most one
item at a time: the intervals that occupy a spot may not overlap.

- A processing activity occupies its device(s) (see §4.4.1) and the spots bound to
  its Object-bearing input and output ports over the interval `[start, end]`.
- Spots additionally capture material held **beyond** an activity's own interval —
  in particular, material waiting in a spot before and after transport (§4.5) —
  which device occupation does not.

Because a device runs one activity at a time, a resource that must hold several
items concurrently (a storage hotel, a multi-slot incubator) is modelled as
several devices — typically one device per position — rather than one device with
many spots.

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
  - the chosen **transporter** (which is itself a device, §4.6) over `[a, b]`.
- If `p == q` the two spot intervals collapse to `[e_i, s_j]` and the duration is
  zero.

For now a transport activity occupies only the **transporter** among devices; it
does not lock the source or destination devices (their spots are still occupied,
above). This is the looser of the two formulations ofp-scheduler describes and may
be revisited.

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
    spots: [slot_0, slot_1, slot_2]   # holding positions on one device
  - id: reader_0
    spots: [stage]
  - id: hotel_0                       # storage / buffer
    spots: [h0, h1, h2, h3]

transporters:
  - id: arm_0
  - id: arm_1

transports:  # from/to are qualified <device>.<spot>
  - { transporter: arm_0, from: incubator_0.slot_0, to: reader_0.stage, duration: 20 }
  - { transporter: arm_1, from: incubator_0.slot_0, to: reader_0.stage, duration: 15 }  # arm_1 is faster
  - { transporter: arm_0, from: reader_0.stage,     to: hotel_0.h0,     duration: 25 }
  # no arm_1 entry for reader_0.stage -> hotel_0.h0, so arm_1 cannot make that move

processes:
  measure_od:                         # keyed by the v0 process definition name
    modes:
      - devices: [reader_0]           # a list; may name several devices
        duration: 60
        input_spots:  { plate: reader_0.stage }   # qualified <device>.<spot>
        output_spots: { plate: reader_0.stage }
      - devices: [incubator_0]
        duration: 45
        input_spots:  { plate: incubator_0.slot_0 }
        output_spots: { plate: incubator_0.slot_0 }
  compute_mean:                       # Pure Data only -> duration only
    modes:
      - duration: 5

objective:
  kind: makespan
```

## 6. Execution plan schema

The execution plan is the scheduler's output: a list of timed activities plus
top-level metadata. Times are non-negative integers in `time.unit`.

An activity is **physical-first**: its main fields say what is actually done
(when, where, with what), and workflow provenance is attached as supplementary
information. The two kinds are deliberately asymmetric:

- A **processing** activity realizes a node the workflow author wrote, so it is
  anchored in the workflow: `node` and `mode` are primary. The physical detail
  (`process`, `devices`, spots) follows from `node`, `mode`, the workflow, and the
  environment, and is supplementary.
- A **transport** activity is induced by the scheduler to move an Object, so it is
  anchored in the physical move: `from_spot`, `to_spot`, and `transporter` are
  primary. The `arc` it serves is supplementary provenance.

Supplementary fields are derivable and may be omitted; they are included for
self-containment or for tooling that should not re-derive them.

### 6.1 Top level

- `time` (optional) — `unit`; derivable from the environment, echoed for
  self-containment.
- `status` (required) — one of `optimal`, `feasible`, `infeasible`, `unknown`
  (`unknown` = a feasible plan whose optimality was not proven, e.g. on timeout).
- `objective` (required) — `kind` (`makespan`) and `value` (for makespan, the
  makespan).
- `activities` (required) — the scheduled activities.
- `meta` (optional) — provenance, e.g. `workflow` and `environment` source
  references.

### 6.2 Processing activity

Main (required):

- `kind: processing`.
- `start`, `end` — integers in `time.unit`.
- `node` — the node path: node ids from the entry composite's body down to the
  atomic node invoked, as a list (e.g. `[brew, heat]`); a single-level workflow
  yields a one-element list. This is also the activity's stable identity (§6.4).
- `mode` — the selected mode id (process-local, §5.5; the auto-assigned id if the
  mode had none). Resolved against the activity's process.

Supplementary (optional; copied from the workflow and the mode):

- `process` — the atomic process definition invoked (from `node` + workflow).
- `devices` — the mode's devices.
- `input_spots` / `output_spots` — the mode's qualified spot mappings.

A Pure-Data-only processing activity has no device or spot, so among the
supplementary fields it carries at most `process`.

### 6.3 Transport activity

Main (required):

- `kind: transport`.
- `start`, `end` — the transport interval `[start, end]`.
- `from_spot` — the qualified source spot `<device>.<spot>`.
- `to_spot` — the qualified destination spot.
- `transporter` — the selected transporter id. The transporter is the only device
  the activity occupies (§4.5), so there is no separate `devices` field.

Supplementary (optional; provenance):

- `arc` — the Object-bearing arc served: `from` / `to`, each `{ node: <path>,
  port: <name> }`. When present it is the activity's identity for replanning
  (§6.4).

### 6.4 Identity for replanning

The execution status (§7) matches plan activities by their workflow-anchored
identity: a processing activity by its `node` (always present), a transport
activity by its `arc` (include it when the plan will be replanned). Physical
fields are not identities — they change from one plan to the next.

Durations are not stored (`end - start`).

### 6.5 Example

```yaml
time:
  unit: second

status: optimal
objective:
  kind: makespan
  value: 80

activities:
  - kind: processing
    start: 0
    end: 60
    node: [brew, heat]                        # main (also the identity)
    mode: fast
    process: heat_sample                      # supplementary
    devices: [reader_0]
    input_spots:  { plate: reader_0.stage }
    output_spots: { plate: reader_0.stage }

  - kind: processing                          # Pure Data compute: no device/spot
    start: 60
    end: 65
    node: [compute]
    mode: mean_v1
    process: compute_mean

  - kind: transport
    start: 60
    end: 80
    from_spot: reader_0.stage                 # main
    to_spot:   incubator_0.slot_0
    transporter: arm_0
    arc:                                      # supplementary provenance
      from: { node: [brew, heat], port: plate }
      to:   { node: [assay],      port: sample }

meta:                                         # optional provenance
  workflow: workflow.yaml
  environment: env.yaml
```

## 7. Execution status schema

*Forthcoming.* This is the replanning input: completed and running activities with
their actual times and assignments, plus the initial state (device positions,
where material currently sits). It will be aligned with the execution plan schema.

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

Validation splits into a standalone **schema validator** and **execution-layer
validation**.

The **schema validator** is owned by `ofplang.schedule` and checks only the shape
of the environment definition on its own (§9.1). It does not read the workflow.
`ofplang.validate` is not involved. Its diagnostics follow the same convention as
`ofplang.validate`: stable **error codes** with a `file:line:col` source position.

**Execution-layer validation** (§9.2) covers everything that needs the workflow or
depends on solvability. The execution layer reads the workflow itself with
`ofplang.schedule`'s own minimal parser — extracting only what it needs (process
kinds, type domains, per-port Object-bearing-ness) — without depending on
`ofplang.validate`. Full v0 validation of the workflow is out of scope here and is
expected to be done separately (by `ofplang.validate`); the scheduler assumes it is
given a valid v0 workflow.

### 9.1 Schema validator — shape only (standalone; no workflow needed)

- Identifier syntax (§8.1) and per-kind uniqueness (§8.2); cross-kind coincidence
  raises a warning.
- Required sections present (`time`, `devices`, `processes`); `devices` non-empty;
  each process has at least one mode.
- Value constraints: a processing `duration` is a positive YAML integer, a
  transport `duration` is a non-negative YAML integer; `time.unit` is a non-empty
  string; `objective.kind` is `makespan`.
- Env-internal references: each `transports.transporter` is a defined transporter;
  every `from` / `to` and every `input_spots` / `output_spots` value is a
  well-formed qualified spot `<device>.<spot>` (exactly one `.`) naming a defined
  device and a spot defined on it.
- Duplicate detection: duplicate ids; duplicate `transports` entries for the same
  `(transporter, from, to)`.
- Intra-mode spot rules: within a mode, input ports do not share a spot, output
  ports do not share a spot, and every referenced spot's device is one of the
  mode's `devices`.
- **Unknown or extra keys are errors** (strict only; there is no
  extension-tolerant mode).

### 9.2 Execution-layer validation (needs the workflow / solvability)

Not part of the schema validator; checked by the execution layer.

- **Against the workflow**: each `processes` key names a process that exists in the
  workflow, is **atomic**, and is in scope (§2); each port in `input_spots` /
  `output_spots` exists on that process, in the correct direction, and is
  **Object-bearing** (Pure Data ports must not appear).
- **Coverage / completeness**: every atomic process actually invoked by the
  workflow has at least one mode, and every mode maps exactly the Object-bearing
  ports of its process.
- **Reachability / solvability**: for each Object-bearing arc, a feasible
  combination of endpoint modes and a transporter that can move between the
  chosen spots exists. This depends on mode selection and is a solvability
  concern, not a schema check.
