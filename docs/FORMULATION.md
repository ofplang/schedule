# FORMULATION

## Purpose

This document defines the scheduling problem as a mathematical optimization
model. It is the theory `ofplang.schedule` implements, ported from the
`ofp-scheduler` prototype.

The model covers the current `ofplang.schedule` scope: a **single workflow**
scheduled onto devices and spots, with mode selection and transport.
`ofp-scheduler`'s final model additionally covers multiple concurrent runs and
device-local consumable resources with replenishment; both are outside the
current scope (SPEC §1: a single workflow at a time; SPEC §4.2: replenishment is
a future activity kind) and are omitted here.

Terminology follows `SPECIFICATIONS.md`: **activity**, **processing activity**,
**transport activity**, **device**, **spot**, **mode**, **transporter**,
**workflow**, and the `pending` / `running` / `completed` statuses. This document
covers the optimization model only; the scheduler input, environment schema,
execution-document schema, identifiers, and validator scope are in
`SPECIFICATIONS.md`.

## Activities

The scheduled units are **activities**, each with a start and an end. Two kinds
are scheduled together:

- **Processing activity** — one per atomic process invocation.
- **Transport activity** — one per Object-bearing arc; moves an Object from a
  source spot to a destination spot.

Every activity has, at minimum:

- a start time and an end time,
- a set of occupied resources, and
- an execution status (`pending` / `running` / `completed`).

The occupied-resource set is not a constant: it depends on the selected mode.
Two resource kinds are occupied — **spots** and **devices** — and both are
exclusive (mutual-exclusion applies to each; SPEC §4.4).

## Sets and indices

- $T$: the workflow's processing-activity set.
- $A \subseteq T \times T$: dependency (precedence) relation; $(i,j) \in A$ means
  "$j$ may start after $i$ completes".
- $R$: Object-bearing arc set (output-port → input-port connections). Pure Data
  arcs contribute a dependency to $A$ only and are not in $R$ (SPEC §4.3, §4.5).
- $L$: device set. A device is an exclusive resource that owns spots and carries
  out work (SPEC §4.4).
- $L^{\mathrm{tr}} \subseteq L$: transporters — individual devices used for moves
  (SPEC §4.6). The initial version uses a single transporter, $|L^{\mathrm{tr}}| =
  1$, with unique element $\ell^{\mathrm{tr}}$.
- $P$: spot set. A spot is a holding/processing position on a device and holds at
  most one item at a time (SPEC §4.4).
- $M_i$: candidate mode set of processing activity $i$. Each mode fixes the
  device(s) used, the processing duration, and the spot assigned to each
  Object-bearing port (SPEC §5.5).
- $H = \{\tau_r \mid r \in R\}$: transport-activity set.
- $\mathcal{A} = T \cup H$: the full activity set (processing and transport).
- $I_i$, $O_i$: Object-bearing input-port and output-port sets of processing
  activity $i$. (Pure Data ports occupy no spot and are not listed.)

Every arc $r = (i,j) \in R$ corresponds to some dependency pair in $A$, so the
relation induced by $R$ is a subset of $A$. An arc always denotes a transport.

## Parameters

Processing and transport:

- $p_{i,m} \in \mathbb{Z}_{>0}$: processing duration of activity $i$ under mode
  $m$.
- $L_{i,m} \subseteq L$: devices occupied by processing activity $i$ under mode
  $m$ (usually $|L_{i,m}| = 1$; multi-device modes are allowed, SPEC §4.4.1).
- $\sigma^{\mathrm{in}}_{i,m,k} \in P$: spot for input port $k \in I_i$ of
  activity $i$ under mode $m$.
- $\sigma^{\mathrm{out}}_{i,m,k} \in P$: spot for output port $k \in O_i$ of
  activity $i$ under mode $m$.
- $S_{i,m} = \{\sigma^{\mathrm{in}}_{i,m,k} \mid k \in I_i\}
  \cup \{\sigma^{\mathrm{out}}_{i,m,k} \mid k \in O_i\}$: spots occupied by
  activity $i$ under mode $m$.
- $d_{p,q} \in \mathbb{Z}_{\ge 0}$: transport duration from spot $p$ to spot $q$.
  May be treated as symmetric; $d_{p,p} = 0$. With a single transporter the
  duration depends on the spot pair only; multiple transporters with
  per-transporter durations $d_{\mathrm{transporter},p,q}$ (SPEC §5.4) are a
  future extension.
- $L_{r,m,n} \subseteq L$: devices occupied by the transport activity for arc
  $r = (i,j)$ under source mode $m \in M_i$ and destination mode $n \in M_j$. It
  contains at least the source device, the destination device, and the
  transporter (so typically $|L_{r,m,n}| = 3$; SPEC §4.5).
- $k_r^{\mathrm{out}}$, $k_r^{\mathrm{in}}$: the source output port and
  destination input port of arc $r$.

Replanning:

- $now \in \mathbb{Z}_{\ge 0}$: replan time.
- $T^{\mathrm{done}}, T^{\mathrm{run}}, T^{\mathrm{pend}}$: completed, running,
  and pending processing activities;
  $T^{\mathrm{pend}} = T \setminus (T^{\mathrm{done}} \cup T^{\mathrm{run}})$.
- $\hat{s}_i, \hat{e}_i$: actual / fixed start and end times.
- $\hat{x}_{i,m}$: actual mode assignment of a fixed activity. For a running
  activity, $\hat{e}_i$ is the expected finish (SPEC §6.2).

Transport activities carry the same `pending` / `running` / `completed` statuses.

## Decision variables

Processing activities:

- $x_{i,m} \in \{0,1\}$: activity $i$ selects mode $m$.
- $s_i, e_i \in \mathbb{Z}_{\ge 0}$: start and end of activity $i$.

Transport activities:

- $q_{r,m,n} \in \{0,1\}$: arc $r=(i,j)$'s transport uses source mode
  $m \in M_i$ and destination mode $n \in M_j$.
- $a_r, b_r \in \mathbb{Z}_{\ge 0}$: start and end of transport activity
  $\tau_r$.

Objective:

- $C_{\max} \in \mathbb{Z}_{\ge 0}$: makespan.

## Common activity-time notation

For an activity $\alpha \in \mathcal{A}$, write $start_\alpha$ and $end_\alpha$
for its start and end. For processing activity $\alpha = i \in T$,
$start_\alpha = s_i$ and $end_\alpha = e_i$; for transport activity
$\alpha = \tau_r$, $start_\alpha = a_r$ and $end_\alpha = b_r$.

For each device $\ell \in L$, let $\mathcal{A}_\ell$ be the activities occupying
$\ell$ (used by §7). Occupancy follows the selected modes:

- a processing activity $i$ occupies the devices $L_{i,m}$ and the spots
  $S_{i,m}$ of its selected mode;
- a transport activity $\tau_r$ occupies the devices $L_{r,m,n}$ of its selected
  source/destination mode pair, and its source and destination spots (§6).

Device occupancy spans the whole activity interval (§7); spot occupancy can
differ per spot and is given interval-by-interval in §6.

## Constraints

### 1. Mode selection

$$
\sum_{m \in M_i} x_{i,m} = 1, \quad \forall i \in T
$$

### 2. Processing duration

$$
e_i = s_i + \sum_{m \in M_i} p_{i,m}\, x_{i,m}, \quad \forall i \in T
$$

### 3. Dependency and arc ordering

Every dependency pair is respected:

$$
s_j \ge e_i, \quad \forall (i,j) \in A
$$

For each arc $r = (i,j) \in R$, its transport starts after the source activity
ends and finishes before the destination activity starts:

$$
a_r \ge e_i, \qquad s_j \ge b_r, \quad \forall r=(i,j) \in R
$$

### 4. Transport route selection

The transport source/destination modes must agree with the endpoint activities'
mode selection:

$$
\sum_{n \in M_j} q_{r,m,n} = x_{i,m}, \quad \forall r=(i,j)\in R,\ \forall m \in M_i
$$
$$
\sum_{m \in M_i} q_{r,m,n} = x_{j,n}, \quad \forall r=(i,j)\in R,\ \forall n \in M_j
$$

### 5. Transport duration

$$
b_r = a_r + \sum_{m \in M_i}\sum_{n \in M_j}
d_{\sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}},\ \sigma^{\mathrm{in}}_{j,n,k_r^{\mathrm{in}}}}\,
q_{r,m,n}, \quad \forall r=(i,j) \in R
$$

For a zero-distance transport ($d_{p,p}=0$) one may fix $a_r = b_r = e_i$ by
convention to avoid time indeterminacy.

### 6. Spot resource constraint

A processing activity occupies each spot of its selected mode over $[s_i, e_i]$.
A transport activity occupies its **source** and **destination** spots over
*different* intervals. For arc $r=(i,j)$ under mode pair $(m,n)$, let
$p_r^{\mathrm{src}}(m,n) = \sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}}$ and
$p_r^{\mathrm{dst}}(m,n) = \sigma^{\mathrm{in}}_{j,n,k_r^{\mathrm{in}}}$. Then

- the source spot is held over $I_r^{\mathrm{src}} = [e_i,\ b_r]$, and
- the destination spot is held over $I_r^{\mathrm{dst}} = [a_r,\ s_j]$.

For each spot $p \in P$, the following intervals must be mutually
non-overlapping:

- $[s_i, e_i]$ for each processing activity that occupies $p$;
- $I_r^{\mathrm{src}}$ for each transport with $p = p_r^{\mathrm{src}}(m,n)$;
- $I_r^{\mathrm{dst}}$ for each transport with $p = p_r^{\mathrm{dst}}(m,n)$.

Input ports never share a spot with each other, and output ports never share a
spot with each other; an input port and an output port may share a spot.

### 7. Device resource constraint

For each device $\ell \in L$, the activities occupying it are mutually
non-overlapping. A processing activity occupies its mode's devices $L_{i,m}$ over
$[s_i,e_i]$; a transport activity occupies $L_{r,m,n}$ over its transport
interval $[a_r,b_r]$ (the conservative formulation: source device, destination
device, and transporter are all held during transport).

$$
(end_\alpha \le start_\beta) \lor (end_\beta \le start_\alpha),
\quad \forall \ell \in L,\ \forall \alpha \ne \beta \in \mathcal{A}_\ell
$$

Since $|L^{\mathrm{tr}}| = 1$, all transport activities are mutually exclusive
through the transporter $\ell^{\mathrm{tr}}$.

### 8. Makespan

$$
C_{\max} \ge e_i, \quad \forall i \in T
$$

### 9. Replanning fixation

Completed and running activities are fixed; pending ones are re-optimised. This
applies to processing and transport activities alike; the processing case is
shown below and transport is analogous (its times and route $q_{r,m,n}$ are
fixed).

$$
s_i = \hat{s}_i,\ e_i = \hat{e}_i,\ x_{i,m} = \hat{x}_{i,m},
\quad \forall i \in T^{\mathrm{done}}
$$
$$
s_i = \hat{s}_i,\ e_i = \hat{e}_i,\ x_{i,m} = \hat{x}_{i,m},
\quad \forall i \in T^{\mathrm{run}}
$$

(a running activity's end is fixed to its expected finish $\hat{e}_i$, SPEC §6.2).

$$
s_i \ge now, \quad \forall i \in T^{\mathrm{pend}}
$$

Pending activities' mode assignment is not fixed and may change on replan; the
spot occupancy of a pending activity follows automatically from its selected
mode.

Replan input is assumed **normalized**: a `running` / `completed` transport
activity never feeds directly into a `pending` processing activity (such cases
are removed before solving).

## Objective

The objective is **makespan minimization**:

$$
\min C_{\max}
$$

Only makespan is accepted in the initial version; the objective is supplied by
the environment definition's `objective` or overridden on the command line (SPEC
§4.7, §5.6). The execution plan records the achieved objective as `objective.kind`
and `objective.value` (SPEC §6.1).

## CP-SAT implementation notes

The reference implementation uses OR-Tools CP-SAT. The MILP-style formulations
above (e.g. big-M ordering) are reference models; CP-SAT expresses the same
structure more directly with optional intervals.

- Each processing/transport activity is one or more optional intervals whose
  presence is its mode/route selector.
- Spot non-overlap: feed each processing interval and each transport's
  source-spot interval $[e_i,b_r]$ and destination-spot interval $[a_r,s_j]$ into
  the spot's `NoOverlap`.
- Device non-overlap: feed processing intervals and the transport body interval
  $[a_r,b_r]$ into each device's `NoOverlap`.
- Makespan: bind $C_{\max}$ as the max over all $e_i$ (e.g. `AddMaxEquality`).
