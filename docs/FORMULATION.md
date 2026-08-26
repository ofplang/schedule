# FORMULATION

## Purpose

This document defines the scheduling problem as a mathematical optimization
model. It is the theory `ofplang.schedule` implements, ported from the
`ofp-scheduler` prototype.

The model covers the current `ofplang.schedule` scope: a **single workflow**
scheduled onto devices and spots, with mode selection, transport, and
device-local consumable resources with replenishment. `ofp-scheduler`'s final
model additionally covers multiple concurrent runs, which is outside the current
scope (SPEC §1: a single workflow at a time) and is omitted here.

> The resource and replenishment part of this model (§10, §11, and the terms they
> add to §7, §8 and §9) is implemented. One deliberate departure: where **no refill
> can reach a stock**, its level is monotone and §11's reservoir is replaced by the
> single inequality it collapses to — everything still to be drawn must fit in what
> is left. That is an exact equivalence, not an approximation, and it keeps an
> environment with stocks and no replenisher (§5.6) from paying for event machinery
> it cannot use.

Terminology follows `SPECIFICATIONS.md`: **activity**, **processing activity**,
**transport activity**, **replenishment activity**, **device**, **spot**,
**resource**, **mode**, **transporter**, **replenisher**,
**workflow**, and the `pending` / `running` / `completed` statuses. This document
covers the optimization model only; the scheduler input, environment schema,
execution-document schema, identifiers, and validator scope are in
`SPECIFICATIONS.md`.

## Activities

The scheduled units are **activities**, each with a start and an end. Three kinds
are scheduled together:

- **Processing activity** — one per atomic process invocation.
- **Transport activity** — one per Object-bearing arc; moves an Object from a
  source spot to a destination spot.
- **Replenishment activity** — refills the consumable resources of one device
  (SPEC §4.7.1). Unlike the other two it is **not** given by the workflow: the set
  of candidates is constructed by the model (§10) and how many run is decided by
  the solver. It occupies two devices (the refilled one and a replenisher) and no
  spot.

**Boundary nodes.** The workflow's boundary Object-bearing material (entry inputs,
final outputs) is handled by two synthetic **boundary nodes**, added when the
`interface` constraint (SPEC §3, §6.8) is present:

- the **input node** — a single processing activity whose mode's `output_spots`
  place every Object-bearing entry-input port at its declared interface spot; it
  occupies **no device**, has duration 0, and is pinned to start and end at time 0
  (the initial material is a given, present from the start of the run);
- the **output node** — a single processing activity whose mode's `input_spots`
  are every Object-bearing final-output port at its declared interface spot; it
  occupies **no device**, its start follows its incoming transport(s) like any
  activity, and its end is pinned to the **makespan** (the delivered result holds
  its spot until the schedule ends).

A boundary connection is then an **ordinary arc**: `input node → consumer` for an
entry input, `producer → output node` for a final output. No special arc form,
transport variable, or occupancy rule is needed — the boundary node is just an
activity with a single spot-fixing mode, and the arc is scheduled by the ordinary
rules below. (Likewise a **relay** — the junction of a multi-leg move on replan —
is not a model primitive: it is an ordinary spot-occupancy between two transports,
introduced only by replan construction; see §9.)

Every activity has, at minimum:

- a start time and an end time,
- a set of occupied resources, and
- an execution status (`pending` / `running` / `completed`).

The occupied-resource set is not a constant: it depends on the selected mode.
Two resource kinds are occupied — **spots** and **devices** — and both are
exclusive (mutual-exclusion applies to each; SPEC §4.4).

**Consumables are a third, independent axis.** A device's consumable resources are
not occupied and released; they are a *level* that falls and rises (§11). Nothing
in §6 or §7 refers to them, and §11 refers to no spot — the two axes constrain the
same schedule without interacting.

## Sets and indices

- $T$: the processing-activity set. It includes the two **boundary nodes** (the
  input node and the output node, above) when `interface` is present; they are
  ordinary members of $T$ with a single mode, distinguished only by their pinned
  times (§3-bis) and empty device set.
- $A \subseteq T \times T$: dependency (precedence) relation; $(i,j) \in A$ means
  "$j$ may start after $i$ completes".
- $R$: Object-bearing arc set (Object-bearing connections, each realised as a
  transport). Pure Data arcs contribute a dependency to $A$ only and are not in
  $R$ (SPEC §4.3, §4.5). A **boundary arc** (one endpoint is a boundary node) is
  an ordinary member of $R$; nothing below special-cases it.
- $L$: device set. A device is an exclusive resource that owns spots and carries
  out work (SPEC §4.4).
- $L^{\mathrm{tr}} \subseteq L$: transporters — individual devices used for moves
  (SPEC §4.6). Each transport activity is assigned to exactly one transporter.
- $L^{\mathrm{rp}} \subseteq L$: replenishers — individual devices that perform
  refills (SPEC §5.6). Like a transporter, a replenisher is an ordinary member of
  $L$, so §7 serialises it without a rule of its own. $L^{\mathrm{tr}}$,
  $L^{\mathrm{rp}}$ and the work devices are pairwise disjoint (SPEC §8.2: no two
  machines share an id).
- $P$: spot set. A spot is a holding/processing position on a device and holds at
  most one item at a time (SPEC §4.4).
- $G_\ell$: the consumable resources declared on device $\ell$ (SPEC §5.2),
  possibly empty. Resources are device-local, so the pair $(\ell, g)$ with
  $g \in G_\ell$ is the unit everything below is indexed by.
- $W$: the **replenishment candidate** set, constructed in §10. Each $\omega \in W$
  targets one device $\ell_\omega \in L$ with $G_{\ell_\omega} \ne \emptyset$.
  $W$ is empty when resources are disabled (SPEC §4.7.3), and everything below then
  degenerates to the resource-free model.
- $M_i$: candidate mode set of processing activity $i$. Each mode fixes the
  device(s) used, the processing duration, and the spot assigned to each
  Object-bearing port (SPEC §5.5).
- $H = \{\tau_r \mid r \in R\}$: transport-activity set.
- $\mathcal{A} = T \cup H \cup W$: the full activity set (processing, transport and
  replenishment).
- $I_i$, $O_i$: Object-bearing input-port and output-port sets of processing
  activity $i$. (Pure Data ports occupy no spot and are not listed.)

Every arc $r = (i,j) \in R$ corresponds to a dependency pair in $A$, so the
relation induced by $R$ is a subset of $A$. An arc always denotes a transport. A
boundary arc has a boundary node as one of $i$, $j$ (the input node as the source,
or the output node as the destination), so it is an ordinary $(i,j)$ pair like any
other.

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
  destination input port of arc $r$.

The boundary nodes need no special parameters: each has a single mode whose
$\sigma^{\mathrm{out}}$ (input node) or $\sigma^{\mathrm{in}}$ (output node) are the
interface spots (SPEC §6.8), and whose device set is empty. Their spots enter the
model as ordinary $\sigma$ values.

Resources and replenishment:

- $c_{\ell,g} \in \mathbb{Z}_{>0}$: capacity of resource $g \in G_\ell$ on device
  $\ell$ (SPEC §5.2).
- $u_{i,m,\ell,g} \in \mathbb{Z}_{\ge 0}$: amount of $(\ell,g)$ consumed by
  processing activity $i$ under mode $m$ (SPEC §5.5), taken in full at the
  activity's **start**. Nonzero only for $\ell \in L_{i,m}$, and never exceeding
  $c_{\ell,g}$ (SPEC §4.7.1 — the environment is rejected otherwise). Transport and
  replenishment activities consume nothing.
- $v^{0}_{\ell,g} \in \mathbb{Z}_{\ge 0}$: the level of $(\ell,g)$ at the **start of
  the run** (`inventories.levels`, SPEC §6.10), with $v^{0}_{\ell,g} \le c_{\ell,g}$.
  An unstated resource is $0$.
- $\rho_{t,\ell} \in \mathbb{Z}_{>0}$: duration for replenisher $t \in
  L^{\mathrm{rp}}$ to refill device $\ell$ (SPEC §5.7). A missing entry means $t$
  **cannot** refill $\ell$ — the pair is excluded from the choice in §10, exactly as
  a missing $d_{t,p,q}$ is excluded from the route choice. The duration does not
  depend on which resources are refilled or by how much.
- $L_{\omega,t} = \{\ell_\omega, t\}$: devices occupied by replenishment candidate
  $\omega$ performed by replenisher $t$ — the refilled device and the replenisher,
  so $|L_{\omega,t}| = 2$ (SPEC §4.7.1). No spot is occupied.

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
- $W^{\mathrm{done}}, W^{\mathrm{run}}$: completed and running replenishment
  activities read from the status. They are **not** members of $W$: $W$ holds the
  candidates the solver may still choose, while these are fixed history matched by
  `id` (SPEC §6.6). A `pending` replenishment in the input is discarded and
  re-derived like any other pending work.
- $\hat{\Delta}_{\omega,g} \in \mathbb{Z}_{\ge 0}$: the amount of $(\ell_\omega,g)$
  a fixed replenishment **reported** adding (SPEC §6.9). This is history, not a
  fill-to-capacity figure: a refill that only partly filled the stock is stated as
  such and used as reported.
- $\hat{u}_{i,\ell,g} \in \mathbb{Z}_{\ge 0}$: the consumption a fixed processing
  activity reported, read from its `consumption` echo and falling back to the
  current environment's mode of the reported id (SPEC §6.3). The echo exists
  because a replan may withdraw the very mode the activity used, and fixed parts
  are never re-read against the current environment (SPEC §7).

Transport and replenishment activities carry the same `pending` / `running` /
`completed` statuses.

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
  combinations are omitted, which is how reachability enters the model. A boundary
  arc is no different: one endpoint is a boundary node, whose single mode fixes its
  spot, so its $q_{r,m,n,t}$ ranges over that node's one mode, the other endpoint's
  modes, and the transporters.
- $z_{r,t} = \sum_{m \in M_i}\sum_{n \in M_j} q_{r,m,n,t} \in \{0,1\}$: whether
  arc $r$'s transport uses transporter $t$ (derived; the per-transporter resource
  in §7).
- $a_r, b_r \in \mathbb{Z}_{\ge 0}$: start and end of transport activity
  $\tau_r$.

Replenishment activities:

- $y_{\omega,t} \in \{0,1\}$: candidate $\omega \in W$ runs, performed by
  replenisher $t \in L^{\mathrm{rp}}$. A variable exists only where $\rho_{t,
  \ell_\omega}$ is defined, which is how replenishment reachability enters the model
  — the same device as the omitted $q_{r,m,n,t}$ combinations in transport.
- $\bar{y}_\omega = \sum_{t \in L^{\mathrm{rp}}} y_{\omega,t} \in \{0,1\}$: whether
  candidate $\omega$ is selected at all (derived). A candidate may run at most once.
- $\gamma_\omega, \delta_\omega \in \mathbb{Z}_{\ge 0}$: start and end of
  replenishment activity $\omega$.
- $\Delta_{\omega,g} \in \mathbb{Z}_{\ge 0}$: amount of $(\ell_\omega,g)$ added by
  $\omega$, for $g \in G_{\ell_\omega}$. This is a solver variable only so that §11
  has an arithmetic term to work with; the value that reaches the plan is fixed
  afterwards by the fill-to-capacity rule (§11), not chosen.

Objective:

- $C_{\max} \in \mathbb{Z}_{\ge 0}$: makespan.
- $N_{\mathrm{repl}} = \sum_{\omega \in W} \bar{y}_\omega$: number of
  replenishments in the plan (derived).

## Common activity-time notation

For an activity $\alpha \in \mathcal{A}$, write $start_\alpha$ and $end_\alpha$
for its start and end. For processing activity $\alpha = i \in T$,
$start_\alpha = s_i$ and $end_\alpha = e_i$; for transport activity
$\alpha = \tau_r$, $start_\alpha = a_r$ and $end_\alpha = b_r$; for replenishment
activity $\alpha = \omega \in W$, $start_\alpha = \gamma_\omega$ and
$end_\alpha = \delta_\omega$.

For each device $\ell \in L$, let $\mathcal{A}_\ell$ be the activities occupying
$\ell$ (used by §7). Occupancy follows the selected modes:

- a processing activity $i$ occupies the devices $L_{i,m}$ and the spots
  $S_{i,m}$ of its selected mode;
- a transport activity $\tau_r$ occupies the devices $L_{r,m,n,t}$ of its selected
  source mode, destination mode, and transporter, and its source and destination
  spots (§6);
- a replenishment activity $\omega$ occupies the devices $L_{\omega,t}$ of its
  selected replenisher — the refilled device and $t$ — and **no spot**, so material
  may rest in the refilled device's spots throughout (SPEC §4.7.1).

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

This applies to boundary arcs unchanged: for `input node → consumer` the source
is the input node (with $e = 0$, §3-bis), for `producer → output node` the
destination is the output node.

### 3-bis. Boundary node timing

The two boundary nodes (§Activities) are pinned:

$$
s_{\mathrm{in}} = e_{\mathrm{in}} = 0, \qquad e_{\mathrm{out}} = C_{\max}
$$

The input node sits at time 0 (its output-spot occupancy over $[0, b_r]$ via the
outgoing arc reserves each entry spot until the Object is picked up). The output
node's end is the makespan, so a delivered Object holds its spot from arrival
until the schedule ends (its input-spot occupancy over $[s_{\mathrm{out}},
C_{\max}]$ joins the incoming arc's $[a_r, s_{\mathrm{out}}]$ into $[a_r,
C_{\max}]$). On a replan the input node stays pinned at 0 (a given origin, exempt
from the $s \ge now$ rule of §9); the output node's end tracks the current
$C_{\max}$.

### 4. Transport route selection

Exactly one feasible route (source mode, destination mode, transporter) is chosen
per arc, and it must agree with the endpoint activities' mode selection:

$$
\sum_{n \in M_j}\sum_{t \in L^{\mathrm{tr}}} q_{r,m,n,t} = x_{i,m},
\quad \forall r=(i,j)\in R,\ \forall m \in M_i
$$
$$
\sum_{m \in M_i}\sum_{t \in L^{\mathrm{tr}}} q_{r,m,n,t} = x_{j,n},
\quad \forall r=(i,j)\in R,\ \forall n \in M_j
$$

Summed over all $m,n,t$, these force exactly one $q_{r,m,n,t} = 1$ per arc, so
each transport selects one transporter. An arc with no feasible combination has
no route to select and the instance is infeasible (SPEC §9.3 `arc_unreachable`).
A boundary arc is included: its boundary node has a single mode $M = \{0\}$, so
the coupling on that side degenerates to $x_{\cdot,0} = 1$ and the sum ranges over
the other endpoint's modes and the transporters.

### 5. Transport duration

$$
b_r = a_r + \sum_{m \in M_i}\sum_{n \in M_j}\sum_{t \in L^{\mathrm{tr}}}
d_{t,\sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}},\ \sigma^{\mathrm{in}}_{j,n,k_r^{\mathrm{in}}}}\,
q_{r,m,n,t}, \quad \forall r=(i,j) \in R
$$

This covers boundary arcs unchanged (the boundary node's single mode supplies its
$\sigma$ — the interface spot). The duration depends on the chosen transporter as
well as the spot pair. For a zero-distance transport ($d_{t,p,p}=0$) one may fix
$a_r = b_r = e_i$ by convention to avoid time indeterminacy (for a boundary-input
arc $e_i = 0$, the input node's end).

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

The boundary nodes are ordinary occupiers here, and their pinned times (§3-bis)
produce exactly the intended boundary reservations: the input node's outgoing arc
holds each entry spot over $[e_{\mathrm{in}}{=}0,\ b_r]$; the output node holds each
delivery spot over $[s_{\mathrm{out}},\ e_{\mathrm{out}}{=}C_{\max}]$, which with
the incoming arc's $[a_r,\ s_{\mathrm{out}}]$ covers $[a_r,\ C_{\max}]$.

Input ports never share a spot with each other, and output ports never share a
spot with each other; an input port and an output port may share a spot.

### 7. Device resource constraint

For each device $\ell \in L$, the activities occupying it are mutually
non-overlapping. A processing activity occupies its mode's devices $L_{i,m}$ over
$[s_i,e_i]$; a transport activity occupies $L_{r,m,n,t}$ over its transport
interval $[a_r,b_r]$ (the conservative formulation: source device, destination
device, and the chosen transporter are all held during transport). A boundary node
has an **empty device set**, so it holds no device — only its spot(s) (§6). The
boundary *transport* still holds a source device, a destination device (the ones
owning the interface spot and the endpoint spot), and its transporter during
$[a_r,b_r]$, exactly like any transport.

$$
(end_\alpha \le start_\beta) \lor (end_\beta \le start_\alpha),
\quad \forall \ell \in L,\ \forall \alpha \ne \beta \in \mathcal{A}_\ell
$$

A replenishment activity $\omega$ occupies $L_{\omega,t} = \{\ell_\omega, t\}$ over
$[\gamma_\omega, \delta_\omega]$ when $y_{\omega,t} = 1$, so it enters
$\mathcal{A}_{\ell_\omega}$ and $\mathcal{A}_t$ and is serialised by the same
inequality. Nothing above is special-cased for it: a two-device activity is already
ordinary (SPEC §4.4.1). One consequence worth naming is that a refill and the work
on the device it refills can never overlap — which is exactly why a device's
consumption and replenishment events are totally ordered in time (§11).

A transporter is one of these devices, so the same rule governs it: for each
transporter $t \in L^{\mathrm{tr}}$, the transports with $z_{r,t} = 1$ are
mutually non-overlapping (one move at a time per transporter), while transports
assigned to different transporters may run concurrently. A replenisher $t \in
L^{\mathrm{rp}}$ works the same way with $y_{\omega,t}$.

### 8. Makespan

$$
C_{\max} \ge e_i, \quad \forall i \in T
$$

Each boundary-output delivery is also counted: the `producer → output node`
transport ends at $b_r$, and the output node's end is pinned to $C_{\max}$
(§3-bis), so

$$
C_{\max} \ge b_r, \quad \forall \text{boundary-output arc } r
$$

follows from $e_{\mathrm{out}} = C_{\max} \ge s_{\mathrm{out}} \ge b_r$. This is
what holds a delivered Object's spot to the end of the schedule (§6). ($C_{\max}$
is the max over real processing ends and boundary deliveries; the output node's own
end equals it and is not itself a driver.)

Replenishments count too (SPEC §4.8):

$$
C_{\max} \ge \delta_\omega, \quad \forall \omega \in W
$$

This never binds for a refill that is actually needed — such a refill precedes the
work that consumes from it, so its end is already below that work's start. What it
rules out is a selected replenishment parked after all productive work, where it
would otherwise be free.

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

**Replenishments replan by history, not by re-selection.** A candidate in $W$ is
always pending — the set is rebuilt every solve (§10), so no $\bar{y}_\omega$ is
ever fixed. What is fixed is $W^{\mathrm{done}}$ and $W^{\mathrm{run}}$, matched by
`id` (SPEC §6.6) and pinned from their reported times, device, replenisher and
$\hat{\Delta}_{\omega,g}$:

$$
\gamma_\omega = \hat{s}_\omega,\ \delta_\omega = \hat{e}_\omega,
\quad \forall \omega \in W^{\mathrm{done}}
$$
$$
\gamma_\omega = \hat{s}_\omega,\ \delta_\omega = \max(\hat{e}_\omega,\ now + m),
\quad \forall \omega \in W^{\mathrm{run}}
$$

They hold their two devices over those intervals in §7 like any fixed activity. The
difference from processing and transport is where their *effect* lands: a completed
replenishment has already raised the level and is folded into $v^{now}$ (§11), while
a running one has not and enters §11 as a fixed positive event at $\delta_\omega$.
That split is the whole of their role — nothing re-derives them, and nothing
re-validates them against the current environment (SPEC §7).

**Boundary nodes replan uniformly.** The boundary nodes and their arcs are
re-created every solve from the workflow and `interface` (they never appear in the
status input, so they are not read back — like relays). The input node stays pinned
at $s = e = 0$ and the output node at $e = C_{\max}$; a committed boundary leg is
matched by its logical arc (the empty-path endpoint, SPEC §6.4/§6.8) and pinned
like any committed leg, and a still-pending boundary leg is re-optimised (and
re-routes through relays if its committed arrival can no longer feed the
destination). No boundary case is special-cased in the constraints — only the model
**construction** (reading `interface` into the boundary nodes) differs.

### 10. Replenishment candidates, selection and duration

Replenishment candidates are **constructed**, not read. For each pending processing
activity $i \in T^{\mathrm{pend}}$ and each device $\ell$, a single candidate
$\omega = (i,\ell)$ is created iff some mode of $i$ both runs on $\ell$ and consumes
something there:

$$
\exists\, m \in M_i:\ \ell \in L_{i,m}\ \wedge\ \exists\, g \in G_\ell:\ u_{i,m,\ell,g} > 0
$$

The activity $i$ is the candidate's *origin*, not its schedule position: nothing
below ties $\omega$ to $i$'s interval, so a refill may be placed arbitrarily early
(SPEC §4.7.1, refilling ahead).

**Selection.** A candidate runs at most once, on one replenisher:

$$
\bar{y}_\omega = \sum_{t \in L^{\mathrm{rp}}} y_{\omega,t} \le 1,
\quad \forall \omega \in W
$$

and only if its origin actually runs on the device it refills:

$$
\bar{y}_\omega \le \sum_{m \in M_i:\ \ell \in L_{i,m}} x_{i,m},
\quad \forall \omega = (i,\ell) \in W
$$

This prunes without losing anything: if $i$ runs elsewhere it consumes nothing on
$\ell$, and every other activity that does consume on $\ell$ carries its own
candidate.

**Duration.**

$$
\delta_\omega = \gamma_\omega + \sum_{t \in L^{\mathrm{rp}}} \rho_{t,\ell_\omega}\, y_{\omega,t},
\quad \forall \omega \in W
$$

and on a replan a candidate is future work like any other pending activity:

$$
\gamma_\omega \ge now, \quad \forall \omega \in W
$$

**Why one candidate per (consuming activity, device) is enough.** Take any feasible
schedule using arbitrarily many refills, each filling to capacity (§11). Fix a pair
$(\ell,g)$. Its consumption events are totally ordered (§7), and between two
consecutive consumptions at most one refill is worth keeping: if two occur with no
consumption between them, deleting the earlier one leaves every later level
unchanged, because the later refill restores the stock to $c_{\ell,g}$ regardless,
and no lower-bound check falls between them. A refill after the last consumption on
$\ell$ affects nothing and is deleted too. What remains can be indexed by the
consumption that immediately follows it — one per (consuming activity, $\ell$) pair
at most. The construction above therefore loses no feasible schedule, **provided**
no single mode consumes more than a full stock, $u_{i,m,\ell,g} \le c_{\ell,g}$;
that is a static property of the environment and is rejected there (SPEC §10.2,
`consumption_exceeds_capacity`) rather than left to surface as infeasibility.

### 11. Inventory level constraint

Consumable levels are **replayed, not supplied** (SPEC §4.7.2). The level of
$(\ell,g)$ at $now$ is derived from the run's initial level and the history:

$$
v^{now}_{\ell,g} = v^{0}_{\ell,g}
- \sum_{i \in T^{\mathrm{done}} \cup T^{\mathrm{run}}} \hat{u}_{i,\ell,g}
+ \sum_{\omega \in W^{\mathrm{done}}} \hat{\Delta}_{\omega,g}
$$

A running processing activity has already taken its consumption (it is taken at the
start, and the activity has started), while a running *replenishment* has not yet
delivered — it appears below as a fixed future event instead. Replaying the history
in time order must keep the level within $[0, c_{\ell,g}]$ at every historical
event; otherwise the reported history and the environment disagree and the document
cannot be replanned (SPEC §9.3, `status_inventory_inconsistent`).

**Events.** For each $(\ell,g)$ let $\mathcal{E}_{\ell,g}$ hold, each with a time, a
signed change, and an activation literal:

| event | time | change | active iff |
| --- | --- | --- | --- |
| consumption, $i \in T^{\mathrm{pend}}$, $m \in M_i$, $\ell \in L_{i,m}$ | $s_i$ | $-u_{i,m,\ell,g}$ | $x_{i,m} = 1$ |
| candidate refill, $\omega \in W$, $\ell_\omega = \ell$ | $\delta_\omega$ | $+\Delta_{\omega,g}$ | $\bar{y}_\omega = 1$ |
| running refill, $\omega \in W^{\mathrm{run}}$, $\ell_\omega = \ell$ | $\delta_\omega$ | $+\hat{\Delta}_{\omega,g}$ | always |

**Constraint.** At every point in time the level stays within its bounds:

$$
0 \;\le\; v^{now}_{\ell,g} \;+\!\!\sum_{\substack{\varepsilon \in \mathcal{E}_{\ell,g} \\ time_\varepsilon \,\le\, \theta}}\!\! \chi_\varepsilon\, change_\varepsilon
\;\le\; c_{\ell,g},
\quad \forall \ell \in L,\ \forall g \in G_\ell,\ \forall \theta
$$

Only the event times need checking, so the quantifier over $\theta$ is finite.
Events sharing a time are summed, which is what realises SPEC §4.7's rule that a
refill ending exactly when the work it feeds begins does feed it: the two changes
net, and no intermediate level is examined between them.

Every activity touching $(\ell,g)$ occupies $\ell$, so §7 already serialises them
and the events of one $(\ell,g)$ are totally ordered up to that tie. Transport and
relay activities contribute no events; the constraint is entirely independent of
§6's spot occupancy.

**Amounts.** In the solver, an amount is bounded and forced to zero for an
unselected candidate, and a selected candidate must add something:

$$
0 \le \Delta_{\omega,g} \le c_{\ell_\omega,g}\, \bar{y}_\omega,
\qquad
\sum_{g \in G_{\ell_\omega}} \Delta_{\omega,g} \ge \bar{y}_\omega
$$

**Fill to capacity is imposed after solving, not as a constraint.** SPEC §4.7.1 says
a planned refill fills each resource to `capacity`, but the constraint above offers
no level *variable* to write $level = c_{\ell,g}$ against — the running sum is an
expression, and a reservoir formulation (the intended implementation) does not
expose the level at all. So the amounts are left free above and normalised once the
solution is known: with times and selections fixed, each $(\ell,g)$'s events are
totally ordered, and replaying them in time order sets each selected
$\Delta_{\omega,g}$ to $c_{\ell,g}$ minus the level immediately before.

This is sound and costs nothing. Raising an amount raises every later level, so the
lower bound only slackens; the upper bound is met with equality by construction. The
normalised solution keeps the same times, the same selections and therefore the same
$C_{\max}$ and $N_{\mathrm{repl}}$ — it is the same optimum, reported determinately
instead of arbitrarily among the many amount assignments the constraints admit. A
selected candidate whose normalised amounts are all zero adds nothing and is dropped
from the plan, which can only lower $N_{\mathrm{repl}}$.

## Objective

The objective is a lexicographic sequence of stages (SPEC §4.8). v0 defines
$C_{\max}$ (makespan) and $N_{\mathrm{repl}} = \sum_{\omega \in W} \bar{y}_\omega$,
the number of selected replenishment activities (§10).

Lexicographic minimization of $(C_{\max}, N_{\mathrm{repl}})$ is encoded as a
**single weighted objective** rather than a staged re-solve:

$$
\min\; (|W| + 1)\, C_{\max} + N_{\mathrm{repl}}
$$

This is exact, because $0 \le N_{\mathrm{repl}} \le |W|$: one unit of $C_{\max}$
outweighs every attainable value of $N_{\mathrm{repl}}$. It is preferred over
solving one stage at a time and fixing its optimum, which would double the solve
passes on a model that is already the scalability bottleneck. Where replenishment
is disabled or no candidate exists, $W = \emptyset$ and the objective degenerates
to $\min C_{\max}$.

$C_{\max}$ is the maximum over the ends of **all** activities, replenishments
included (SPEC §4.8), so a refill cannot be parked after the productive work.

The objective is declared in the execution document (SPEC §6.1), which also records
the achieved values as `objective.value`.

## CP-SAT implementation notes

The reference implementation uses OR-Tools CP-SAT. The MILP-style formulations
above (e.g. big-M ordering) are reference models; CP-SAT expresses the same
structure more directly with optional intervals.

- Each processing/transport activity is one or more optional intervals whose
  presence is its mode/route selector. A transport's route options enumerate the
  feasible $(m,n,t)$ combinations; the presence literal of each is $q_{r,m,n,t}$,
  and `AddExactlyOne` over them realises the §4 route selection.
- Spot non-overlap: feed each processing interval and each transport's
  source-spot interval $[e_i,b_r]$ and destination-spot interval $[a_r,s_j]$ into
  the spot's `NoOverlap`. (Spot assignment depends on the mode pair, not on $t$.)
  The boundary nodes are ordinary occupiers: the input node's interval $[0,0]$ and
  the output node's interval $[s_{\mathrm{out}},C_{\max}]$ carry their spots into
  the same `NoOverlap`, so the waiting entry-input and the resting final-output
  need no boundary-specific machinery.
- Device non-overlap: feed processing intervals and the transport body interval
  $[a_r,b_r]$ into each device's `NoOverlap`. The transporter is a device like any
  other: route each transport option's body interval into its chosen transporter's
  `NoOverlap` (present iff $q_{r,m,n,t}$), so each transporter serialises only its
  own moves while different transporters run in parallel. (Boundary nodes have no
  device, so they add nothing here.)
- Boundary nodes: create $C_{\max}$ before the activity intervals so the output
  node's interval can end at it; pin the input node to $[0,0]$ (exempt from the
  replan $s\ge now$ lower bound) and the output node's end to $C_{\max}$.
- Makespan: bind $C_{\max}$ as the max over all real processing ends $e_i$, every
  boundary-output delivery $b_r$, and every replenishment end $\delta_\omega$ (e.g.
  `AddMaxEquality`); the output node's own end is then set equal to $C_{\max}$.
- Replenishment: one optional interval per $(\omega, t)$ pair with presence
  $y_{\omega,t}$, fed into both $\ell_\omega$'s and $t$'s `NoOverlap`;
  `AddAtMostOne` over $t$ realises §10's selection. A candidate is *optional* rather
  than required, which is the only structural difference from a transport's route
  choice (`AddExactlyOne` there).
- Inventory: one `AddReservoirConstraintWithActive` per $(\ell,g)$, with the event
  times, signed changes and activation literals of §11's table, `min_level` $0$,
  `max_level` $c_{\ell,g}$, and $v^{now}_{\ell,g}$ folded in as a constant event at
  time $0$. The constraint sums simultaneous events, which is the tie rule §11
  wants. Note it exposes **no level variable**, which is precisely why fill-to-
  capacity is a post-solve normalisation (§11) and not a constraint.
- Amount normalisation: after solving, walk each $(\ell,g)$'s events in time order
  and set each selected $\Delta_{\omega,g}$ to $c_{\ell,g}$ minus the level before
  it; drop any replenishment left with all-zero amounts, and recompute the reported
  $N_{\mathrm{repl}}$ if it did.
- **Horizon.** The trivial upper bound on any end time is a fully serial schedule,
  so it must now include replenishment: adding $\max_t \rho_{t,\ell_\omega}$ for
  every $\omega \in W$ keeps it a valid bound. Doing exactly that is also the
  hazard — $|W|$ grows with the consuming activities, and the horizon is already
  known to be several times the optimum and to dominate search cost (see
  `dev-notes/report-solver-scalability.md`). A tighter bound that stays valid is to
  add, per device, only as many refill durations as that device can actually fit
  between its own consuming activities. Getting this wrong in either direction is a
  real defect: too small silently turns feasible instances infeasible, too large
  slows every solve.
