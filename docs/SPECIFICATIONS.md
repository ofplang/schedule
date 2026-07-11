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

### 4.4 Spots (the processing resource)

A **spot** is a holding/processing position on a device. Spots are the only
exclusive resource for processing:

- A processing activity occupies the spots bound to its Object-bearing input and
  output ports for the interval `[start, end]`.
- Two activities may not occupy the same spot at overlapping times.

**Devices are not locked.** A device's concurrency is governed entirely by its
spots: a device with one spot effectively runs one activity at a time, while a
device with many spots (e.g. an incubator or a storage hotel) can hold or process
several items concurrently. Devices exist only to group spots and to label the
plan; they are not themselves an exclusive resource.

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
  - the chosen **transporter** over `[a, b]`.
- If `p == q` the two spot intervals collapse to `[e_i, s_j]` and the duration is
  zero.

Ordering: `a >= e_i` and `s_j >= b`.

### 4.6 Transporters

A transporter is an **individual** entity with its own id, modelled as an
exclusive resource: the transport activities assigned to a given transporter may
not overlap in time (one move at a time per transporter). Multiple transporters
are multiple distinct ids, and each transport activity is assigned to one of
them.

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
top-level sections.

### 5.1 `time`

- `unit` — the meaning of one time unit (e.g. `second`, `minute`). Times are
  non-negative integers at this granularity.

### 5.2 `devices`

A list of devices. Each device groups spots; the device itself is not an
exclusive resource (§4.4).

- `id` — unique device id.
- `spots` — a list of spot names belonging to this device. A spot name is unique
  **within its device**; the globally unique spot id is the qualified form
  `<device>.<spot>` (see §8). Because neither part contains a `.`, the qualified
  form parses unambiguously.

### 5.3 `transporters`

A list of transporters (§4.6). Each entry is an individual transporter:

- `id` — unique transporter id.

The initial version expects exactly one entry.

### 5.4 `transports`

The transport-duration table, keyed by `(transporter, from_spot, to_spot)`:

- `transporter` — a transporter id.
- `from` — source spot, in qualified form `<device>.<spot>` (§8).
- `to` — destination spot, in qualified form `<device>.<spot>` (§8).
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

Each process has a list of **modes**. A mode is one way to run the process:

- `device` — the device the mode runs on. **Optional**: a Pure-Data-only process
  (e.g. a `python_script` step) may omit `device` and declare only a `duration`,
  occupying no spot and no transporter.
- `duration` — the estimated processing time, a non-negative integer in
  `time.unit`.
- `input_spots` — a mapping from **Object-bearing** input port name to a spot on
  `device`, given as a **local spot name** (resolved against this mode's
  `device`; not the qualified form).
- `output_spots` — a mapping from **Object-bearing** output port name to a local
  spot name on `device`. Within one mode, input ports must not share a spot with
  each other and output ports must not share a spot with each other, but an input
  and an output port may share a spot.

Pure Data ports are not listed in `input_spots` / `output_spots` (they occupy no
spot).

### 5.6 `objective` (optional)

- `kind` — the objective. Only `makespan` is accepted in the initial version. May
  be overridden on the command line.

### 5.7 Example

```yaml
time:
  unit: second

devices:
  - id: incubator-0
    spots: [slot-0, slot-1, slot-2]   # many spots -> concurrent holding/processing
  - id: reader-0
    spots: [stage]                    # one spot -> one at a time
  - id: hotel-0                       # storage / buffer
    spots: [h0, h1, h2, h3]

transporters:
  - id: arm-0
  - id: arm-1

transports:  # from/to are qualified <device>.<spot>
  - { transporter: arm-0, from: incubator-0.slot-0, to: reader-0.stage, duration: 20 }
  - { transporter: arm-1, from: incubator-0.slot-0, to: reader-0.stage, duration: 15 }  # arm-1 is faster
  - { transporter: arm-0, from: reader-0.stage,     to: hotel-0.h0,     duration: 25 }
  # no arm-1 entry for reader-0.stage -> hotel-0.h0, so arm-1 cannot make that move

processes:
  measure_od:                         # keyed by the v0 process definition name
    modes:
      - device: reader-0
        duration: 60
        input_spots:  { plate: stage }
        output_spots: { plate: stage }
      - device: incubator-0
        duration: 45
        input_spots:  { plate: slot-0 }
        output_spots: { plate: slot-0 }
  compute_mean:                       # Pure Data only -> duration only
    modes:
      - duration: 5

objective:
  kind: makespan
```

## 6. Execution plan schema

*Forthcoming.* This will describe the scheduler output: for each activity
(processing and transport), its start and end, the selected mode / device / spots,
and the selected transporter for transport activities.

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

ASCII, case-sensitive, no `.`. The v0 reserved-word list applies to the workflow,
not to environment-local ids.

Process names and port names are **referenced from the workflow** and therefore
follow the v0 rules for those positions.

### 8.2 Uniqueness and namespaces

- **device id** — unique among devices.
- **transporter id** — unique among transporters.
- **spot** — a spot name is unique within its device. The globally unique spot id
  is the qualified form `<device>.<spot>`. `modes` reference spots by local name
  (the device is the mode's `device`); `transports` reference spots by qualified
  form because they span devices.
- **Cross-kind coincidence is allowed**: a device, a spot name, and a transporter
  may share the same string, and a port name may coincide with any of them (port
  names live in a per-process, per-direction namespace — v0 §2.4). Coincidence
  across the device / spot / transporter kinds is permitted but **should raise a
  warning** for readability.

## 9. Validation

Validation of the environment definition is owned by `ofplang.schedule`.
`ofplang.validate` is not involved; it validates only the v0 workflow.

Checks are organised in layers:

### 9.1 Shape (standalone; no workflow needed)

- Identifier syntax (§8.1) and per-kind uniqueness (§8.2); cross-kind coincidence
  raises a warning.
- Value constraints: `duration` is a non-negative integer; `time.unit` is a
  non-empty string; `objective.kind` is `makespan`.
- Duplicate detection: duplicate ids; duplicate `transports` entries for the same
  `(transporter, from, to)`.
- Intra-mode spot rules: within a mode, input ports do not share a spot, output
  ports do not share a spot, and every referenced spot belongs to the mode's
  `device`.
- **Unknown or extra keys are errors** (strict only; there is no
  extension-tolerant mode).

### 9.2 Against the workflow

- Each `processes` key names a process that exists in the workflow, is **atomic**,
  and is in scope (§2).
- Each port in `input_spots` / `output_spots` exists on that process, in the
  correct direction, and is **Object-bearing**; Pure Data ports must not appear.

### 9.3 Out of scope for the validator (checked by the execution layer)

- **Coverage / completeness**: every atomic process actually invoked by the
  workflow has at least one mode, and every mode maps exactly the Object-bearing
  ports of its process.
- **Reachability / solvability**: for each Object-bearing arc, a feasible
  combination of endpoint modes and a transporter that can move between the
  chosen spots exists. This depends on mode selection and is a solvability
  concern, not a schema check.
