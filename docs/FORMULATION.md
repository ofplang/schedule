# FORMULATION

## Purpose

This document defines the scheduling problem as a mathematical optimization
model. It is the theory `ofplang.schedule` implements, ported from the
`ofp-scheduler` prototype.

`ofp-scheduler` grew the model in incremental steps (fixed devices → device
selection → spot hierarchy → transport → device-local resources →
replenishment → multiple jobs). This document keeps **only the final,
consolidated form** and drops the step-by-step history. Parts that are present
in the final model but outside the immediate implementation focus (e.g.
replenishment) are retained as-is.

Terminology follows `SPECIFICATIONS.md`: **activity**, **processing activity**,
**transport activity**, **device**, **spot**, **mode**, **transporter**, and the
`pending` / `running` / `completed` statuses. One term is added here: a **job**
is one scheduled run of a workflow (see below). This document covers the
optimization model only; the scheduler input, environment schema,
execution-document schema, identifiers, and validator scope are in
`SPECIFICATIONS.md`.

### Note on jobs and workflows

A **workflow** is the ofplang v0 dataflow graph — the IR defined by the spec. A
**job** is one submission of that workflow to the scheduler: a single run to be
planned. The two are distinct — several jobs may run the same workflow — and
`job` is used throughout for the scheduled unit to avoid overloading `workflow`.

The model is written for the general **multi-job** case: a set of independent
jobs sharing physical resources. The current `ofplang.schedule` scope is a single
job ($|J| = 1$; SPEC §1 — a single workflow at a time; see the reduction at the
end), and the multi-job constraints below then reduce to the single-job case
without change.

## Activities

The scheduled units are **activities**, each with a start and an end. Three kinds
are scheduled together:

- **Processing activity** — one per atomic process invocation.
- **Transport activity** — one per Object-bearing arc; moves an Object from a
  source spot to a destination spot.
- **Replenishment activity** — refills a device-local consumable resource.

Every activity has, at minimum:

- a start time and an end time,
- a set of occupied resources, and
- an execution status (`pending` / `running` / `completed`).

The occupied-resource set is not a constant: it depends on the selected mode.
Two resource kinds are occupied — **spots** and **devices** — and both are
exclusive (mutual-exclusion applies to each; SPEC §4.4).

## Sets and indices

- $J$: set of jobs to plan.
- $T_j$: processing-activity set of job $j$.
- $A_j \subseteq T_j \times T_j$: dependency (precedence) relation of job
  $j$; $(i,j') \in A_j$ means "$j'$ may start after $i$ completes".
- $R_j$: Object-bearing arc set of job $j$ (output-port → input-port
  connections). Pure Data arcs contribute a dependency to $A_j$ only and are not
  in $R_j$ (SPEC §4.3, §4.5).
- $T = \bigcup_{j \in J} T_j$, $A = \bigcup_{j \in J} A_j$,
  $R = \bigcup_{j \in J} R_j$: cross-job processing-activity, dependency, and
  arc sets.
- $L$: device set. A device is an exclusive resource that owns spots and carries
  out work (SPEC §4.4).
- $L^{\mathrm{tr}} \subseteq L$: transporters — individual devices used for moves
  (SPEC §4.6). The initial version uses a single transporter, $|L^{\mathrm{tr}}| =
  1$, with unique element $\ell^{\mathrm{tr}}$.
- $\ell^{\mathrm{rep}} \in L$: the replenishment-execution device shared by all
  replenishment activities.
- $P$: spot set. A spot is a holding/processing position on a device and holds at
  most one item at a time (SPEC §4.4).
- $M_i$: candidate mode set of processing activity $i$. Each mode fixes the
  device(s) used, the processing duration, and the spot assigned to each
  Object-bearing port (SPEC §5.5).
- $S_i \subseteq L$: devices processing activity $i$ may use across its candidate
  modes.
- $G$: consumable resource-type set.
- $H = \{\tau_r \mid r \in R\}$: transport-activity set.
- $K$: replenishment-candidate set (cross-job). Each $k \in K$ corresponds to
  a processing activity $i(k)$ and a target device $\ell(k)$.
- $\mathcal{A} = T \cup H \cup \{\text{replenishment activities}\}$: the full
  activity set. Processing activities are identified with their processing
  activity.
- $\Theta$: time points at which inventory constraints are checked.
- $I_i$, $O_i$: Object-bearing input-port and output-port sets of processing
  activity $i$. (Pure Data ports occupy no spot and are not listed.)

Each processing activity belongs to exactly one job. There is **no
cross-job dependency and no cross-job arc**: $A_j \subseteq T_j \times
T_j$ and $R_j$ is internal to $T_j$.

Every arc $r = (i,j) \in R$ corresponds to some dependency pair in $A$, so the
relation induced by $R$ is a subset of $A$. An arc always denotes a transport.

## Parameters

Processing and transport:

- $p_{i,m} \in \mathbb{Z}_{>0}$: processing duration of activity $i$ under mode
  $m$.
- $\ell(i,m) \in L$: device of processing activity $i$ under mode $m$.
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

Device-local consumable resources and replenishment:

- $u_{\ell,g}^{0} \in \mathbb{Z}_{\ge 0}$: initial inventory of resource $g$ at
  device $\ell$.
- $\bar{u}_{\ell,g} \in \mathbb{Z}_{\ge 0}$: inventory capacity (upper bound) of
  resource $g$ at device $\ell$.
- $c_{i,m,g} \in \mathbb{Z}_{\ge 0}$: amount of resource $g$ consumed when
  activity $i$ starts under mode $m$.
- $\rho_{\ell} \in \mathbb{Z}_{\ge 0}$: duration of a replenishment activity
  targeting device $\ell$.

Replenishment supply is external and treated as unlimited (no supply-side
inventory constraint). Only processing activities consume resources; transport
and replenishment activities do not.

Replanning:

- $now \in \mathbb{Z}_{\ge 0}$: replan time.
- $T^{\mathrm{done}}, T^{\mathrm{run}}, T^{\mathrm{pend}}$: completed, running,
  and pending processing activities;
  $T^{\mathrm{pend}} = T \setminus (T^{\mathrm{done}} \cup T^{\mathrm{run}})$.
- $\hat{s}_i, \hat{e}_i$: actual / fixed start and end times.
- $\hat{x}_{i,m}$: actual mode assignment of a fixed activity. For a running
  activity, $\hat{e}_i$ is the expected finish (SPEC §6.2).

Transport and replenishment activities carry the same `pending` / `running` /
`completed` statuses.

## Decision variables

Processing activities:

- $x_{i,m} \in \{0,1\}$: activity $i$ selects mode $m$.
- $z_{i,\ell} \in \{0,1\}$: activity $i$ selects a mode that uses device
  $\ell \in S_i$.
- $s_i, e_i \in \mathbb{Z}_{\ge 0}$: start and end of activity $i$.

Transport activities:

- $q_{r,m,n} \in \{0,1\}$: arc $r=(i,j)$'s transport uses source mode
  $m \in M_i$ and destination mode $n \in M_j$.
- $a_r, b_r \in \mathbb{Z}_{\ge 0}$: start and end of transport activity
  $\tau_r$.

Replenishment activities:

- $y_k \in \{0,1\}$: replenishment candidate $k$ is executed.
- $a_k, b_k \in \mathbb{Z}_{\ge 0}$: start and end of candidate $k$.
- $r_{k,g} \in \mathbb{Z}_{\ge 0}$: amount of resource $g$ replenished by
  candidate $k$.

Objective auxiliaries:

- $C_j \in \mathbb{Z}_{\ge 0}$: completion time of job $j$.
- $C_{\max} \in \mathbb{Z}_{\ge 0}$: overall makespan.

## Common activity-time notation

For an activity $\alpha \in \mathcal{A}$, write $start_\alpha$ and $end_\alpha$
for its start and end. For processing activity $\alpha = i \in T$,
$start_\alpha = s_i$ and $end_\alpha = e_i$; for transport activity
$\alpha = \tau_r$, $start_\alpha = a_r$ and $end_\alpha = b_r$; for
replenishment candidate $k$, $start_\alpha = a_k$ and $end_\alpha = b_k$.

For each spot $p \in P$, let $\mathcal{A}_p$ be the activities occupying $p$;
for each device $\ell \in L$, let $\mathcal{A}_\ell$ be the activities occupying
$\ell$. Occupancy follows the selected modes:

- processing activity $i$ occupies the devices $L_{i,m}$ and the spots $S_{i,m}$
  of its selected mode,
- transport activity $\tau_r$ occupies the devices $L_{r,m,n}$ of its selected
  source/destination mode pair,
- replenishment candidate $k$ occupies target device $\ell(k)$ and
  $\ell^{\mathrm{rep}}$, and occupies no spot/port.

## Constraints

### 1. Mode selection

$$
\sum_{m \in M_i} x_{i,m} = 1, \quad \forall i \in T
$$

Activity–device selector:

$$
z_{i,\ell} = \sum_{m \in M_i:\ \ell(i,m)=\ell} x_{i,m},
\quad \forall i \in T,\ \forall \ell \in S_i
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
non-overlapping. A processing activity occupies its mode's devices over
$[s_i,e_i]$; a transport activity occupies $L_{r,m,n}$ over its transport
interval $[a_r,b_r]$ (the conservative formulation: source device, destination
device, and transporter are all held during transport); a replenishment
candidate occupies $\ell(k)$ and $\ell^{\mathrm{rep}}$ over $[a_k,b_k]$.

$$
(end_\alpha \le start_\beta) \lor (end_\beta \le start_\alpha),
\quad \forall \ell \in L,\ \forall \alpha \ne \beta \in \mathcal{A}_\ell
$$

Since $|L^{\mathrm{tr}}| = 1$, all transport activities are mutually exclusive
through the transporter $\ell^{\mathrm{tr}}$.

### 8. Device-local inventory

Resources are consumed at activity start and replenished at
replenishment-activity end. Bound each replenishment amount so a non-selected
candidate replenishes nothing:

$$
0 \le r_{k,g} \le y_k\,\bar{u}_{\ell(k),g}, \quad \forall k \in K,\ \forall g \in G
$$

A candidate can be selected only if its target device is actually used:

$$
y_k \le z_{i(k),\ell(k)}, \quad \forall k \in K
$$

Define the inventory of resource $g$ at device $\ell$ just after time $t$:

$$
I_{\ell,g}(t) = u_{\ell,g}^{0}
+ \sum_{k \in K_\ell} r_{k,g}\,\mathbf{1}[b_k \le t]
- \sum_{i \in T_\ell}\sum_{m \in M_i} c_{i,m,g}\,x_{i,m}\,\mathbf{1}[s_i \le t]
$$

where $K_\ell = \{k \in K \mid \ell(k)=\ell\}$ and $T_\ell$ is the set of
processing activities whose selected mode uses device $\ell$. Inventory must stay
within $[0, \bar{u}_{\ell,g}]$ at every checked time point:

$$
0 \le I_{\ell,g}(t) \le \bar{u}_{\ell,g},
\quad \forall \ell \in L,\ \forall g \in G,\ \forall t \in \Theta
$$

$\Theta$ may be restricted to processing-activity start times and
replenishment-activity end times.

Each selected replenishment activity must replenish a positive amount of at
least one resource. Advance replenishment is allowed: a candidate need not sit
immediately before its triggering activity. Candidates are generated per
(processing activity × candidate device) to bound model size; the model assumes
this candidate set does not lose the optimum.

### 9. Job completion and makespan

$$
C_j \ge e_i, \quad \forall j \in J,\ \forall i \in T_j
$$
$$
C_{\max} \ge C_j, \quad \forall j \in J
$$

### 10. Replanning fixation

Completed and running activities are fixed; pending ones are re-optimised. This
applies uniformly to processing, transport, and replenishment activities.

Processing activities:

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

Resources on replan:

- $u_{\ell,g}^{0}$ is the actual remaining inventory at `now`.
- A `completed` replenishment activity's effect is already folded into the
  remaining inventory; its amount is retained as history only.
- A `running` replenishment activity contributes a fixed positive event at its
  `end`; it is non-interruptible and its amount is fixed.
- `pending` replenishment activities are not carried over; candidates are
  regenerated. A `pending` replenishment activity supplied as state input is
  rejected as invalid.
- An input under which a `running` replenishment completion alone forces
  $I_{\ell,g}(t) > \bar{u}_{\ell,g}$ is rejected as invalid.

Jobs added at replan are treated as new jobs whose activities are all
`pending`; no cross-job dependency or arc is created. There is no
`not_released` state — all jobs given to the scheduler are considered
already released at `now`.

## Objective

The objective is **makespan minimization**:

$$
\min C_{\max}
$$

Only makespan is accepted in the initial version; the objective is supplied by
the environment definition's `objective` or overridden on the command line (SPEC
§4.7, §5.6). The execution plan records the achieved objective as `objective.kind`
and `objective.value` (SPEC §6.1); the per-job completion times $C_j$ are
available alongside.

## CP-SAT implementation notes

The reference implementation uses OR-Tools CP-SAT. The MILP-style formulations
above (e.g. big-M ordering) are reference models; CP-SAT expresses the same
structure more directly with optional intervals.

- Each processing/transport/replenishment activity is one or more optional
  intervals whose presence is its mode/route selector.
- Spot non-overlap: feed each processing interval and each transport's
  source-spot interval $[e_i,b_r]$ and destination-spot interval $[a_r,s_j]$ into
  the spot's `NoOverlap`.
- Device non-overlap: feed processing intervals, the transport body interval
  $[a_r,b_r]$, and replenishment intervals into each device's `NoOverlap`.
- Inventory: model inventory as a level with negative events at activity starts
  and positive events at replenishment ends, bounded by $[0,\bar{u}_{\ell,g}]$;
  the replenishment-free case may use a `reservoir` constraint. On replan, add
  fixed intervals/positive events for `running` replenishment activities and fold
  `completed` ones into the initial inventory.
- Job completion: derive each $C_j$ with `AddMaxEquality` and bind $C_{\max}$
  as the max over $C_j$.
- Validate the absence of cross-job dependency/arc before solving.

## Implementation scope (initial version)

The initial `ofplang.schedule` implementation targets a **restriction** of the
general model above. Two restrictions apply:

1. **Single job** — exactly one job, $|J| = 1$ (SPEC §1: a single workflow at a
   time).
2. **No device-local resources** — the consumable-resource concept is dropped
   entirely (no consumption, and hence no replenishment).

Everything not mentioned below is used unchanged. The retained model is:
mode selection, spot hierarchy, and transport activities — a single-job
model with spatial (spot/device) occupancy only.

### Single job ($|J| = 1$)

Let $j_0$ be the only job, so $T = T_{j_0}$, $A = A_{j_0}$, $R = R_{j_0}$.
The per-job completion variables $C_j$ are unnecessary; §9 (job
completion and makespan) collapses to

$$
C_{\max} \ge e_i, \quad \forall i \in T
$$

The cross-job clauses of §10 (jobs added at replan; no cross-job
dependency/arc) are vacuous and drop out, as does the pre-solve cross-job
validation. The "replan adds new jobs" path is not modeled.

### No device-local resources

The consumable-resource concept — and with it replenishment — is removed
entirely:

- **Sets** — drop the resource-type set $G$, the replenishment-candidate set
  $K$, the replenishment device $\ell^{\mathrm{rep}}$, and the inventory-check
  time points $\Theta$. The activity set is $\mathcal{A} = T \cup H$ (processing
  and transport only).
- **Parameters** — drop the consumption $c_{i,m,g}$, the initial inventory
  $u_{\ell,g}^{0}$, the capacity $\bar{u}_{\ell,g}$, and the replenishment
  duration $\rho_{\ell}$.
- **Decision variables** — drop $y_k$, $a_k$, $b_k$, $r_{k,g}$. The
  activity–device selector $z_{i,\ell}$ (and the set $S_i$ behind it) is no longer
  needed; its only uses were the inventory and replenishment clauses.
- **Constraints** — §8 (device-local inventory) is removed in full, and the
  activity–device selector equation of §1 (which defines $z_{i,\ell}$) is dropped
  with it.

The objective is unaffected (makespan; SPEC §4.7). Only the spatial resources —
spots (§6) and devices (§7) — remain as competing resources.

### Resulting model

Collecting the two restrictions, the scoped model is fully specified by:

- **Sets** — the single job's $T$, $A$, $R$; devices $L$ (with the
  transporter $\ell^{\mathrm{tr}}$); spots $P$; candidate modes $M_i$; transport
  activities $H$; ports $I_i$, $O_i$. (No $G$, $K$, $\Theta$,
  $\ell^{\mathrm{rep}}$.)
- **Parameters** — $p_{i,m}$, $L_{i,m}$, the spot maps $\sigma^{\mathrm{in/out}}$
  (hence $S_{i,m}$), $d_{p,q}$, $L_{r,m,n}$, $k_r^{\mathrm{out/in}}$, and the
  replanning parameters ($now$, actuals).
- **Variables** — $x_{i,m}$, $s_i$, $e_i$ (processing); $q_{r,m,n}$, $a_r$, $b_r$
  (transport); $C_{\max}$.
- **Constraints** — §1 (mode selection, selector dropped), §2 (processing
  duration), §3 (dependency and arc ordering), §4 (transport route selection),
  §5 (transport duration), §6 (spot resource), §7 (device resource), the
  replanning fixation of §10 for processing and transport activities, and the
  collapsed makespan $C_{\max} \ge e_i,\ \forall i \in T$.
- **Objective** — makespan minimization (SPEC §4.7).

This is the single-job transport-and-spot model, with spot and device
occupancy as the only competing resources.

### CP-SAT notes for the scoped model

The general CP-SAT notes apply, minus the resource and multi-job parts:

- No inventory / `reservoir` constraint at all; no replenishment intervals and no
  $\ell^{\mathrm{rep}}$. Only the spot and device `NoOverlap` constraints remain.
- Skip the per-job $C_j$ derivation; bind $C_{\max}$ directly as the max over
  all $e_i$.
