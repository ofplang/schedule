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
  source spot to a destination spot. An endpoint is normally an activity port
  (its spot chosen by that activity's mode), but one endpoint may instead be a
  **fixed spot** — the position of the workflow's boundary material declared by
  the `interface` input (SPEC §3, §6): a boundary-input transport has a fixed
  *source* spot (where an entry-input Object sits at the start), a boundary-output
  transport a fixed *destination* spot (where a final-output Object is delivered).
  This is the only generalization interface needs; there is no separate "boundary"
  construct in the model. (Likewise a **relay** — the junction of a multi-leg move
  on replan — is not a model primitive: it is an ordinary spot-occupancy between
  two transports, introduced only by replan construction; see §9.)

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
- $R$: Object-bearing arc set (Object-bearing connections, each realised as a
  transport). Pure Data arcs contribute a dependency to $A$ only and are not in
  $R$ (SPEC §4.3, §4.5). $R$ partitions by endpoint kind:
  - $R^{\mathrm{int}}$: **interior** arcs — output-port → input-port between two
    activities (the classic case);
  - $R^{\mathrm{in}}$: **boundary-input** arcs — a fixed source spot → an input
    port (the entry-input Object at its declared position feeds a consumer);
  - $R^{\mathrm{out}}$: **boundary-output** arcs — an output port → a fixed
    destination spot (a producer's Object is delivered to a declared position).

  A boundary arc is a normal transport with one endpoint pinned to a fixed spot;
  everything below treats it uniformly with the fixed side simply offering a
  single spot instead of a mode-indexed one.
- $L$: device set. A device is an exclusive resource that owns spots and carries
  out work (SPEC §4.4).
- $L^{\mathrm{tr}} \subseteq L$: transporters — individual devices used for moves
  (SPEC §4.6). Each transport activity is assigned to exactly one transporter.
- $P$: spot set. A spot is a holding/processing position on a device and holds at
  most one item at a time (SPEC §4.4).
- $M_i$: candidate mode set of processing activity $i$. Each mode fixes the
  device(s) used, the processing duration, and the spot assigned to each
  Object-bearing port (SPEC §5.5).
- $H = \{\tau_r \mid r \in R\}$: transport-activity set.
- $\mathcal{A} = T \cup H$: the full activity set (processing and transport).
- $I_i$, $O_i$: Object-bearing input-port and output-port sets of processing
  activity $i$. (Pure Data ports occupy no spot and are not listed.)

An interior arc $r = (i,j) \in R^{\mathrm{int}}$ corresponds to a dependency pair
in $A$, so the relation induced by $R^{\mathrm{int}}$ is a subset of $A$. A
boundary arc has only one activity endpoint (the other is a fixed spot), so it
induces a one-sided ordering (a consumer after a boundary-input move; a
boundary-output move after its producer) rather than an $A$-pair. Every arc
denotes a transport. Where a constraint below is written for $r=(i,j)$ it applies
to whichever of $i$, $j$ is an activity endpoint.

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
- $d_{t,p,q} \in \mathbb{Z}_{\ge 0}$: duration for transporter $t \in
  L^{\mathrm{tr}}$ to move from spot $p$ to spot $q$ (SPEC §5.4). Durations are
  per-transporter (transporters may differ in speed). May be treated as symmetric;
  $d_{t,p,p} = 0$. A missing entry means transporter $t$ **cannot** perform that
  move — the pair $(t,p,q)$ is then simply excluded from the route choice below
  (reachability is expressed by presence in the table).
- $L_{r,m,n,t} \subseteq L$: devices occupied by the transport activity for arc
  $r = (i,j)$ under source mode $m \in M_i$, destination mode $n \in M_j$, and
  transporter $t \in L^{\mathrm{tr}}$. It contains the source device, the
  destination device, and the transporter $t$ (so typically $|L_{r,m,n,t}| = 3$;
  SPEC §4.5).
- $k_r^{\mathrm{out}}$, $k_r^{\mathrm{in}}$: the source output port and
  destination input port of arc $r$ (each defined only on the side that is an
  activity port).

Interface (boundary spots, SPEC §3, §6):

- $\beta_r \in P$: the fixed spot of the boundary endpoint of arc
  $r \in R^{\mathrm{in}} \cup R^{\mathrm{out}}$ — the source spot for
  $r \in R^{\mathrm{in}}$, the destination spot for $r \in R^{\mathrm{out}}$.
  Supplied by the `interface` input; one spot per Object-bearing boundary port.
- $\rho_r \in \mathbb{Z}_{\ge 0}$: availability time of a boundary-**input** arc's
  source Object — $0$ on an initial plan, $now$ on a replan (the Object has been
  sitting at $\beta_r$ and is available from then). Plays the role $e_i$ plays for
  an interior source (the time from which the source spot is occupied and the move
  may start).

Replanning:

- $now \in \mathbb{Z}_{\ge 0}$: replan time.
- $m \in \mathbb{Z}_{\ge 0}$: running-task safety margin (`running_task_margin`,
  default $0$); see §9.
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

- $q_{r,m,n,t} \in \{0,1\}$: arc $r=(i,j)$'s transport uses source mode
  $m \in M_i$, destination mode $n \in M_j$, and transporter $t \in
  L^{\mathrm{tr}}$. A variable exists only for a **feasible** combination — one
  whose duration $d_{t,\sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}},\,
  \sigma^{\mathrm{in}}_{j,n,k_r^{\mathrm{in}}}}$ is defined; infeasible
  combinations are omitted, which is how reachability enters the model.
  For a **boundary** arc the fixed side offers a single spot $\beta_r$ and has no
  mode index: a boundary-input arc uses $q_{r,n,t}$ (source spot $\beta_r$, dest
  mode $n$, transporter $t$; duration $d_{t,\beta_r,\sigma^{\mathrm{in}}_{j,n,
  k_r^{\mathrm{in}}}}$), a boundary-output arc uses $q_{r,m,t}$ (source mode $m$,
  dest spot $\beta_r$; duration $d_{t,\sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}},
  \beta_r}$). Reachability enters identically (a boundary arc with no feasible
  $(\cdot,t)$ has no route — the instance is infeasible).
- $z_{r,t} = \sum_{m \in M_i}\sum_{n \in M_j} q_{r,m,n,t} \in \{0,1\}$: whether
  arc $r$'s transport uses transporter $t$ (derived; the per-transporter resource
  in §7).
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
- a transport activity $\tau_r$ occupies the devices $L_{r,m,n,t}$ of its selected
  source mode, destination mode, and transporter, and its source and destination
  spots (§6).

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

For each arc $r = (i,j) \in R$, its transport starts after its source is ready
and finishes before its destination is needed. On an **interior** arc both
endpoints are activities:

$$
a_r \ge e_i, \qquad s_j \ge b_r, \quad \forall r=(i,j) \in R^{\mathrm{int}}
$$

On a **boundary-input** arc there is no source activity; the source Object is
ready at $\rho_r$:

$$
a_r \ge \rho_r, \qquad s_j \ge b_r, \quad \forall r \in R^{\mathrm{in}}
$$

On a **boundary-output** arc there is no destination activity; only the source
bound applies (the delivery time enters the makespan, §8):

$$
a_r \ge e_i, \quad \forall r \in R^{\mathrm{out}}
$$

### 4. Transport route selection

Exactly one feasible route (source mode, destination mode, transporter) is chosen
per arc, and it must agree with the endpoint activities' mode selection:

$$
\sum_{n \in M_j}\sum_{t \in L^{\mathrm{tr}}} q_{r,m,n,t} = x_{i,m},
\quad \forall r=(i,j)\in R^{\mathrm{int}},\ \forall m \in M_i
$$
$$
\sum_{m \in M_i}\sum_{t \in L^{\mathrm{tr}}} q_{r,m,n,t} = x_{j,n},
\quad \forall r=(i,j)\in R^{\mathrm{int}},\ \forall n \in M_j
$$

Summed over all $m,n,t$, these force exactly one $q_{r,m,n,t} = 1$ per arc, so
each transport selects one transporter. An arc with no feasible combination has
no route to select and the instance is infeasible (SPEC §9.3 `arc_unreachable`).

On a **boundary** arc the mode-coupling applies only to the endpoint that is an
activity port; the fixed side has no mode variable. A boundary-input arc couples
its destination only,
$\sum_{t} q_{r,n,t} = x_{j,n}\ (\forall n \in M_j)$; a boundary-output arc couples
its source only, $\sum_{t} q_{r,m,t} = x_{i,m}\ (\forall m \in M_i)$. Either way
exactly one route is chosen per arc.

### 5. Transport duration

$$
b_r = a_r + \sum_{m \in M_i}\sum_{n \in M_j}\sum_{t \in L^{\mathrm{tr}}}
d_{t,\sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}},\ \sigma^{\mathrm{in}}_{j,n,k_r^{\mathrm{in}}}}\,
q_{r,m,n,t}, \quad \forall r=(i,j) \in R^{\mathrm{int}}
$$

A boundary arc uses the same relation with its single-sided route variable and
its fixed spot in place of one $\sigma$: a boundary-input arc sums
$d_{t,\beta_r,\sigma^{\mathrm{in}}_{j,n,k_r^{\mathrm{in}}}}\,q_{r,n,t}$ over
$(n,t)$; a boundary-output arc sums
$d_{t,\sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}},\beta_r}\,q_{r,m,t}$ over
$(m,t)$.

The duration depends on the chosen transporter as well as the spot pair. For a
zero-distance transport ($d_{t,p,p}=0$) one may fix $a_r = b_r$ to the source
readiness ($e_i$ for a port source, $\rho_r$ for a boundary-input source) by
convention to avoid time indeterminacy.

### 6. Spot resource constraint

A processing activity occupies each spot of its selected mode over $[s_i, e_i]$.
A transport activity occupies its **source** and **destination** spots over
*different* intervals. For arc $r=(i,j)$ the source/destination spot and the
occupancy interval each depend on whether that endpoint is an activity port or a
fixed boundary spot:

- **source spot** $p_r^{\mathrm{src}} = \sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}}$
  (port; interior / boundary-output) or $\beta_r$ (fixed; boundary-input), held
  over $I_r^{\mathrm{src}} = [\rho_r^{\mathrm{src}},\ b_r]$ where
  $\rho_r^{\mathrm{src}} = e_i$ (port) or $\rho_r$ (fixed source);
- **destination spot** $p_r^{\mathrm{dst}} = \sigma^{\mathrm{in}}_{j,n,k_r^{\mathrm{in}}}$
  (port; interior / boundary-input) or $\beta_r$ (fixed; boundary-output), held
  over $I_r^{\mathrm{dst}} = [a_r,\ \rho_r^{\mathrm{dst}}]$ where
  $\rho_r^{\mathrm{dst}} = s_j$ (port) or $C_{\max}$ (fixed destination).

The two fixed-endpoint rules are exactly the boundary occupancy: a boundary-input
Object holds $\beta_r$ from its availability $\rho_r$ (0 / $now$) until it is
picked up ($b_r$); a boundary-output Object holds $\beta_r$ from delivery ($a_r$)
until the end of the schedule ($C_{\max}$), since nothing consumes it.

For each spot $p \in P$, the following intervals must be mutually
non-overlapping:

- $[s_i, e_i]$ for each processing activity that occupies $p$;
- $I_r^{\mathrm{src}}$ for each transport with $p = p_r^{\mathrm{src}}$;
- $I_r^{\mathrm{dst}}$ for each transport with $p = p_r^{\mathrm{dst}}$.

Input ports never share a spot with each other, and output ports never share a
spot with each other; an input port and an output port may share a spot.

### 7. Device resource constraint

For each device $\ell \in L$, the activities occupying it are mutually
non-overlapping. A processing activity occupies its mode's devices $L_{i,m}$ over
$[s_i,e_i]$; a transport activity occupies $L_{r,m,n,t}$ over its transport
interval $[a_r,b_r]$ (the conservative formulation: source device, destination
device, and the chosen transporter are all held during transport). For a boundary
endpoint the corresponding device is the one that owns the fixed spot $\beta_r$
(spots are qualified `device.spot`, SPEC §8.2), so a boundary transport still
holds a source device, a destination device, and its transporter.

$$
(end_\alpha \le start_\beta) \lor (end_\beta \le start_\alpha),
\quad \forall \ell \in L,\ \forall \alpha \ne \beta \in \mathcal{A}_\ell
$$

A transporter is one of these devices, so the same rule governs it: for each
transporter $t \in L^{\mathrm{tr}}$, the transports with $z_{r,t} = 1$ are
mutually non-overlapping (one move at a time per transporter), while transports
assigned to different transporters may run concurrently.

### 8. Makespan

$$
C_{\max} \ge e_i, \quad \forall i \in T
$$

A **boundary-output** transport has no successor activity, so its delivery must
be counted explicitly (an interior or boundary-input transport is already bounded
by the activity that follows it):

$$
C_{\max} \ge b_r, \quad \forall r \in R^{\mathrm{out}}
$$

This also closes the boundary-output occupancy $[a_r, C_{\max}]$ of §6: the
delivered Object holds $\beta_r$ to the end of the schedule.

### 9. Replanning fixation

Completed and running activities are fixed; pending ones are re-optimised. This
applies to processing and transport activities alike; the processing case is
shown below and transport is analogous (its times and route $q_{r,m,n,t}$ — which
fixes the transporter too — are fixed).

$$
s_i = \hat{s}_i,\ e_i = \hat{e}_i,\ x_{i,m} = \hat{x}_{i,m},
\quad \forall i \in T^{\mathrm{done}}
$$
$$
s_i = \hat{s}_i,\ e_i = \max(\hat{e}_i,\ now + m),\ x_{i,m} = \hat{x}_{i,m},
\quad \forall i \in T^{\mathrm{run}}
$$

A running activity's end is fixed to its expected finish $\hat{e}_i$ (SPEC §6.2),
clamped up to $now + m$ by the safety margin $m$ so that an overrunning task
(one whose expected finish is already in the past, $\hat{e}_i < now$) is never
fixed to a finish before $now$; it holds its resources until $now + m$. With the
default $m = 0$ the clamp is simply $\max(\hat{e}_i, now)$.

$$
s_i \ge now, \quad \forall i \in T^{\mathrm{pend}}
$$

Pending activities' mode assignment is not fixed and may change on replan; the
spot occupancy of a pending activity follows automatically from its selected
mode. The same $s_i \ge now$ lower bound applies to a pending **transport**
activity's start $a_r$, so a transport whose source finished before $now$ is
still not scheduled in the past.

Replan input is **normalized** before solving (SPEC §4.5 / §6.4.1). A `running`
/ `completed` transport that has committed an Object to a spot while its
destination processing is still `pending` does not feed that processing directly:
a **relay** (an instantaneous, device-less activity holding the arrival spot) is
inserted, and a pending re-transport leg carries the Object from the relay to the
destination's chosen input spot. The destination's mode is therefore free — a
zero-distance re-transport if it stays at the arrival spot, a real move if it is
re-routed. Relays and re-transports are ordinary activities and transports in the
model above (a relay is a $p_{i,m}=0$ activity occupying one spot and no device),
so no term here is special-cased; only the model **construction** introduces them.
Repeated re-routes chain (relay after relay); the fixed part — committed legs and
completed relays — is pinned exactly as $T^{\mathrm{done}}$ / $T^{\mathrm{run}}$
above, and only pending legs are optimised. The model keeps every relay and leg;
rendering the plan is where a stay-put relay together with its zero-distance
re-transport is folded away as a no-op (SPEC §6.4.1), since the committed leg
already delivers where the destination reads.

**Boundary arcs replan uniformly.** A boundary transport is an ordinary transport,
so it is fixed / re-optimised and re-routed by the same rules: a boundary-input
arc whose move has started is pinned like any committed leg, and a still-pending
boundary-input arc takes $\rho_r = now$ (the entry-input Object has been waiting
at $\beta_r$). A committed boundary-input leg that delivered while its destination
is still pending re-routes through a relay just like an interior arc; a
boundary-output arc likewise re-routes to its fixed $\beta_r$. No boundary case is
special-cased here — only the model **construction** (reading `interface` into the
fixed endpoints) differs.

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
  presence is its mode/route selector. A transport's route options enumerate the
  feasible $(m,n,t)$ combinations; the presence literal of each is $q_{r,m,n,t}$,
  and `AddExactlyOne` over them realises the §4 route selection.
- Spot non-overlap: feed each processing interval and each transport's
  source-spot interval $[\rho_r^{\mathrm{src}},b_r]$ and destination-spot interval
  $[a_r,\rho_r^{\mathrm{dst}}]$ into the spot's `NoOverlap`. (Spot assignment
  depends on the mode pair, not on $t$.) A boundary endpoint contributes the same
  intervals with its fixed spot $\beta_r$ and the substitutions
  $\rho_r^{\mathrm{src}}=\rho_r$ / $\rho_r^{\mathrm{dst}}=C_{\max}$ — so the
  waiting entry-input and the resting final-output are just ordinary interval
  members of their spot's `NoOverlap`, needing no boundary-specific machinery.
- Device non-overlap: feed processing intervals and the transport body interval
  $[a_r,b_r]$ into each device's `NoOverlap`. The transporter is a device like any
  other: route each transport option's body interval into its chosen transporter's
  `NoOverlap` (present iff $q_{r,m,n,t}$), so each transporter serialises only its
  own moves while different transporters run in parallel.
- Makespan: bind $C_{\max}$ as the max over all $e_i$ and every boundary-output
  delivery $b_r$ ($r \in R^{\mathrm{out}}$) (e.g. `AddMaxEquality`).
