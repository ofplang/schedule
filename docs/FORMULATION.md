# FORMULATION

## Purpose

This document defines the scheduling problem as a mathematical optimization
model. It is the theory `ofplang.schedule` implements, ported from the
`ofp-scheduler` prototype.

The model covers the current `ofplang.schedule` scope: workflows scheduled onto
devices and spots, with mode selection, transport, and device-local consumable
resources with replenishment — one workflow at a time, or several planned together
against the same laboratory (SPEC §6.11).

**The document is in two parts, and the second is written as a delta.**

- **Part I — a single workflow** (§Activities through §CP-SAT implementation notes)
  is the whole model for one workflow, and reads straight through without Part II.
  Where a joint plan changes something it says so in a parenthesis, which a reader
  who only ever plans one workflow can ignore.
- **Part II — several jobs** (§J0 through §J8) says what changes when more than one
  is planned at once. Every section there names the Part I sections it touches. Most
  only add — a set, a constraint, an objective stage; two generalise something Part I
  states as a constant, and say which constant.

That shape is not an editorial convenience — it is the property the implementation
holds itself to. Part II ends by showing that with one job every one of its
constraints is vacuous or identical to Part I's, which is why a single-workflow plan
is unchanged, to the byte, by everything in Part II existing.

> The resource and replenishment part of this model (§10, §11, and the terms they
> add to §7, §8 and §9) is implemented. One deliberate departure: where **no refill
> can reach a stock**, its level is monotone and §11's reservoir is replaced by the
> single inequality it collapses to — everything still to be drawn must fit in what
> is left. That is an exact equivalence, not an approximation, and it keeps an
> environment with stocks and no replenisher (SPEC §5.6) from paying for event machinery
> it cannot use.

Terminology follows `SPECIFICATIONS.md`: **activity**, **processing activity**,
**transport activity**, **replenishment activity**, **device**, **spot**,
**resource**, **mode**, **transporter**, **replenisher**,
**workflow**, **job**, and the `pending` / `running` / `completed` statuses together
with the terminal `failed` / `cancelled` (SPEC §6.2, used in §J4). This document
covers the optimization model only; the scheduler input, environment schema,
execution-document schema, identifiers, and validator scope are in
`SPECIFICATIONS.md`.

# Part I — a single workflow

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
  times (§3-bis) and empty device set. Write $T^{\mathrm{bnd}} \subseteq T$ for the
  boundary nodes. (A joint plan adds one more kind of member, the **held node** of
  §J5, on the same footing.)
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
- $\mu \in \mathbb{Z}_{\ge 0}$: running-task safety margin (`running_task_margin`,
  default $0$); see §9. (Written $\mu$ and not $m$: $m$ is the mode index, and §9
  would otherwise use one letter for both in a single equation.)
- $T^{\mathrm{done}}, T^{\mathrm{run}}, T^{\mathrm{pend}}$: completed, running,
  and pending processing activities;
  $T^{\mathrm{pend}} = T \setminus (T^{\mathrm{done}} \cup T^{\mathrm{run}})$.
  (A joint plan carves a fourth set out of $T^{\mathrm{pend}}$ — the work of a job
  that has stopped, §J4. With one job it is empty and the partition is the one
  above.)
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
outgoing arc reserves each entry spot until the Object is picked up); with several
jobs the same pin is at that job's release instead (§J1). The output
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
has an **empty device set**, so it holds no device — only its spot(s) (§6). So does a
**non-accessing** mode (SPEC §4.4.2): $L_{i,m} = \emptyset$ where the mode declares
`device_access: false`, though it binds its spots in §6 like any other. The device the
mode names still owns those spots; it is simply not held while the material rests in
them. The
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
C_{\max} \ge e_i, \quad \forall i \in T \setminus T^{\mathrm{held}}
$$

Held nodes (§J5) are excluded: their end is the horizon, and a spot being taken is
not work. Without several jobs $T^{\mathrm{held}}$ is empty and this is the plain
$\forall i \in T$ it has always been.

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
s_i = \hat{s}_i,\ e_i = \max(\hat{e}_i,\ now + \mu),\ x_{i,m} = \hat{x}_{i,m},
\quad \forall i \in T^{\mathrm{run}}
$$

A running activity's end is fixed to its expected finish $\hat{e}_i$ (SPEC §6.2),
clamped up to $now + \mu$ by the safety margin $\mu$ so that an overrunning task
(one whose expected finish is already in the past, $\hat{e}_i < now$) is never
fixed to a finish before $now$; it holds its resources until $now + \mu$. With the
default $\mu = 0$ the clamp is simply $\max(\hat{e}_i, now)$.

$$
s_i \ge now, \quad \forall i \in T^{\mathrm{pend}}
$$

A third case exists once a document carries several jobs: work belonging to a job
that has **stopped** is neither fixed history nor pending, and is fixed to a
zero-length interval instead (§J4).

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
\gamma_\omega = \hat{s}_\omega,\ \delta_\omega = \max(\hat{e}_\omega,\ now + \mu),
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
totally ordered, and replaying them in that order sets each selected
$\Delta_{\omega,g}$ to $c_{\ell,g}$ minus the level immediately before.

**Completions go before starts, on a doubled time axis.** "Totally ordered" holds
only up to coincidence: a refill's end may meet a draw's start, which is the
ordinary way a schedule packs work. SPEC §4.7 orders such an instant by applying the
completion first and checking the level after *each* change. A reservoir cannot
express that on its own — it checks its bounds between time points, so two changes
at one time point are read as a single net change and the level between them is
never checked. That level is real: it is what the device holds when the refill
finishes, and SPEC §4.7 requires it to fit. So the events are handed to the reservoir on
a doubled axis — a completion at $2t$, a start at $2t+1$ — and the bound is checked
between them. Without the separation the solver admits a refill that takes a full
stock past $c_{\ell,g}$ whenever a draw shares the instant, and the normalisation
above (which fills from the level *before* the draw) then finds no room for it,
drops it, and hands out a plan whose stock later goes negative.

The doubling costs nothing. The time expressions are affine over the same
start/end variables, so no variable is added; the two images are disjoint by parity,
so every order relation is preserved and no case is newly distinguished
($e \le s$ maps to "completion first" whether or not $e = s$).

This is sound and costs nothing. Raising an amount raises every later level, so the
lower bound only slackens; the upper bound is met with equality by construction. The
normalised solution keeps the same times, the same selections and therefore the same
$C_{\max}$ and $N_{\mathrm{repl}}$ — it is the same optimum, reported determinately
instead of arbitrarily among the many amount assignments the constraints admit. A
selected candidate whose normalised amounts are all zero adds nothing and is dropped
from the plan, which can only lower $N_{\mathrm{repl}}$.

**What the soundness rests on, and what checks it.** "Totally ordered" is not free:
it holds because every activity that touches a stock occupies the device holding it,
and a device is exclusive (§4), so no two of them overlap. A refill therefore has to
reach the non-overlap constraints like any other activity — while it did not, the
events were unordered, the replay disagreed with the solved model, and it dropped a
refill the solver was counting on, emitting a plan that took more than it added.

Simultaneous events are the delicate remainder: a refill ending at exactly the
instant a draw starts is how a schedule packs work, and the reservoir reads such
changes *together*. The replay does the same. Because the argument above is an
argument rather than a constraint, the rendered plan is replayed once more before it
is handed out (`plan_inventory_inconsistent`, SPEC §10.4): a finding there is a
defect in the implementation, reported instead of shipped.

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

$C_{\max}$ is the maximum over the ends of **all** activities that are work,
replenishments included (SPEC §4.8), so a refill cannot be parked after the
productive work. The two things that are not work are excluded: the output boundary
node, whose end *is* $C_{\max}$ (§3-bis), and a held spot (§J5).

The objective is declared in the execution document (SPEC §6.1), which also records
the achieved values as `objective.value`. A joint plan adds one stage ahead of these
two (§J3); the weighted encoding above generalises to it unchanged.

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
- **Horizon $\mathcal{H}$.** The trivial upper bound on any end time is a fully serial schedule,
  so it must now include replenishment: adding $\max_t \rho_{t,\ell_\omega}$ for
  every $\omega \in W$ keeps it a valid bound. Doing exactly that is also the
  hazard — $|W|$ grows with the consuming activities, and the horizon is already
  known to be several times the optimum and to dominate search cost (see
  `dev-notes/report-solver-scalability.md`). A tighter bound that stays valid is to
  add, per device, only as many refill durations as that device can actually fit
  between its own consuming activities. Getting this wrong in either direction is a
  real defect: too small silently turns feasible instances infeasible, too large
  slows every solve.

# Part II — several jobs, planned together

Several workflows may be planned in one solve, as **jobs** competing for the same
machines and drawing on the same stocks (SPEC §6.11). Everything in Part I still
holds: the same activities, the same modes, the same spot and device exclusion, the
same replenishment and inventory model. What follows is only what is *added*.

Each section names the Part I sections it touches: §J0 adds sets, §J2, §J4, §J5 and
§J6 add constraints, §J3 adds an objective stage, §J7 widens a bound. Two generalise
a constant rather than adding: §J1 replaces the $0$ that §3-bis pins the input node
at, and §J4 carves the abandoned work of a stopped job out of §9's pending set. §J8
collects what is deliberately outside the model, and the closing section shows that
with one job the whole of Part II is vacuous.

## J0. Jobs (adds to §Sets and indices, §Parameters)

- $J$: the **job** set — the workflows this solve covers, in the order they were
  given (SPEC §6.11). A single workflow is $|J| = 1$.
- $j(\cdot)$: which job an activity's **work** belongs to. It is defined for every
  activity that came from a workflow — every $i \in T \setminus T^{\mathrm{held}}$ and
  every arc $r \in R$ — and $\bot$ for the two kinds that did not: a replenishment
  candidate $\omega \in W$ and a held node $h \in T^{\mathrm{held}}$ (§J5). A held
  node's document entry *may* name the job that left the material, but that is
  traceability and not ownership (SPEC §6.12): it is nobody's work, and nothing
  job-scoped below reads it.
- $J^{\mathrm{stop}} \subseteq J$: the jobs that have **stopped** (§J4).

Ownership is by construction, not inference: the activities of one workflow are
built from that workflow, so $j$ is fixed when the instance is. An arc belongs to
the job of its endpoints; a **boundary arc** has one endpoint in a boundary node,
and both belong to the same job in any case, since a boundary node is that job's
interface.

**A boundary node belongs to a job but is not its work.** It is in $T$, it holds its
spot, and it belongs to $j$ — but it is excluded from that job's completion time
(§J2), because the output node's end is $C_{\max}$ by §3-bis and counting it would
make every job finish exactly when the last one does.

**A replenishment belongs to no job.** One refill commonly serves several, and which
jobs draw from it afterwards is the solver's decision, not a property of the
candidate. That is also why $C_{\max}$ stays in the objective beside the sum of
completion times (§J3): the sum cannot see a refill, so nothing else would stop one
being parked after all the work.

Per-job parameters (SPEC §6.11):

- $rel_j \in \mathbb{Z}_{\ge 0}$: the job's **release** — the earliest time any of
  its activities may start. Default $0$.
- $B_j \in \mathbb{Z}_{\ge 0} \cup \{\infty\}$: the completion time this job was
  **promised** by an earlier solve, read back from the roster. $\infty$ (absent)
  for a job that has not been promised anything yet.

## J1. Release times (adds to §3-bis and §9)

A job's release holds back every activity of it that has not already run:

$$
s_i \ge rel_{j(i)}, \quad \forall i \in T^{\mathrm{pend}}, \; j(i) \ne \bot
$$

and the job's **input boundary node** is pinned at its release rather than at $0$,
replacing the first pin of §3-bis:

$$
s_{\mathrm{in}(j)} = e_{\mathrm{in}(j)} = rel_j
$$

**Pinned, not merely bounded.** The entry material is a fact about the world: it is
*there*, given, from the moment the job is released (SPEC §6.8, §6.11). The solver
does not get to decide when it appears. That is what lets one loading bay serve two
jobs whose releases leave room for it — job 1 holds it over $[rel_1, b_{r_1}]$ and
job 2 over $[rel_2, b_{r_2}]$, which do not overlap once $b_{r_1} \le rel_2$ — and
equally what makes two jobs released *together* onto one bay infeasible rather than
queued. A model in which entry material appears when convenient would be the other
reading, and cannot express the first.

**Transports need no rule of their own.** §3 already starts a transport after its
source ends, and that source is either an activity of the same job (held above) or
that job's input node (pinned above), so $a_r \ge rel_{j(r)}$ follows rather than
being imposed.

**Fixed activities are not re-held.** A release constrains the future, not the past:
history that already ran is pinned by §9, and applying $rel_j$ to it would make the
past infeasible rather than say anything about what remains.

## J2. Completion times and the promise (adds to §Decision variables, §Constraints)

Each job has a completion time — the last end among its own work:

$$
C_j = \max\Bigl(
  \{\, e_i \;\mid\; j(i) = j,\ i \notin T^{\mathrm{bnd}} \,\} \cup
  \{\, b_r \;\mid\; j(r) = j \,\}
\Bigr), \quad \forall j \in J \setminus J^{\mathrm{stop}}
$$

where $T^{\mathrm{bnd}}$ is the boundary nodes (§Sets and indices). Replenishments
are absent by construction, having no job.

A job that carries a promise must keep it:

$$
C_j \le B_j, \quad \forall j \in J \setminus J^{\mathrm{stop}}
$$

🔴 **This single inequality is the whole of "a job already being planned is not
disturbed by one that arrives later."** It is a *constraint*, deliberately, and not
a term in the objective: an objective can trade one job's lateness against another's
and would make the guarantee a preference. Being a constraint, it can also be
*refused* — if no schedule keeps every promise, they are relaxed in **roster order
and by as little as possible** (SPEC §6.11), rather than quietly broken.

A stopped job is excluded from both. It is not going to complete, so it has no
completion time to bound, and holding it to a promise it can never reach would make
every plan that continues past a failure infeasible.

## J3. The sum of completion times (adds to §Objective)

Joint planning adds one objective stage:

$$
\Sigma C = \sum_{j \in J \setminus J^{\mathrm{stop}}} C_j
$$

and the default stage sequence depends on the job count (SPEC §4.8):

| jobs | default stages, most significant first |
|---|---|
| one | $(C_{\max},\ N_{\mathrm{repl}})$ |
| several | $(\Sigma C,\ C_{\max},\ N_{\mathrm{repl}})$ |

With one workflow there is nothing for $\Sigma C$ to trade off against, and leading
with it would change what an existing plan means: $C_{\max}$ counts refills (§8)
while a completion time does not, so a refill could be parked after all the work.
With several, minimising the makespan alone says nothing about *which* job finishes
when — every schedule with the same last end is equally good, including the one that
finishes nothing until the end.

The weighted encoding of Part I generalises unchanged. For stages $g_1, \dots, g_k$
most significant first, each with a known bound $0 \le g_\sigma \le U_\sigma$:

$$
\min \sum_{\sigma=1}^{k} w_\sigma\, g_\sigma,
\qquad w_\sigma = \prod_{\sigma' > \sigma} (U_{\sigma'} + 1)
$$

which is exact for the same reason: one unit of a stage outweighs every attainable
value of everything below it. Part I's $(|W| + 1)\,C_{\max} + N_{\mathrm{repl}}$ is
this with $k = 2$ and $U_2 = |W|$.

## J4. Stopped jobs (adds to §9, §6, §7, §11)

A terminal status — `failed` or `cancelled` — stops the **job** it belongs to
(SPEC §6.2). Its history stays; its remaining work is not planned.

$$
J^{\mathrm{stop}} = \{\, j(\alpha) \;\mid\; \alpha \text{ has a terminal status} \,\}
$$

The work of a stopped job that has not already run is neither fixed history nor
pending: it is taken out of $T^{\mathrm{pend}}$ (so §9's $s_i \ge now$ does not
apply to it) and fixed to a **zero-length interval** at that job's stopping instant:

$$
s_i = e_i = stop_j, \quad
a_r = b_r = stop_j, \qquad \forall \alpha \text{ of } j \in J^{\mathrm{stop}},\
\alpha \notin \text{fixed}
$$
$$
stop_j = \max\Bigl(now,\ \max\{\, \max(\hat{e}_i,\ now + \mu) \;\mid\;
  i \in T^{\mathrm{run}},\ j(i) = j \,\}\Bigr)
$$

🔴 **Not at $now$: at the moment the job's last running operation comes off.** A job
does not necessarily stop with nothing in flight — one branch of it can fail while
another is still on the machine, and a running operation is never aborted. Its
abandoned work has to be placed *after* that operation, because §3 orders it after
it. At $now$ it would sit before the thing it waits on, which no schedule satisfies,
so the whole document would be infeasible — taking every other job with it, the
exact opposite of what stopping one job is for. One instant per job, rather than a
per-activity walk, also keeps chains of abandoned work consistent among themselves:
zero-length intervals at one instant satisfy §3 against each other.

🔴 **Cancelled work occupies nothing.** It never ran, so it is removed from every
spot's non-overlap set in §6, from $\mathcal{A}_\ell$ in §7, and from §11's event
table — it draws no consumption and frees none. Being zero-length is *not* enough to
arrange that: a point strictly inside another interval is still a point inside it,
and §6's disjunction refuses the pair. That is not a modelling nicety, it is what
"cancelled" means.

A stopped job is also excluded from $C_j$ and $C_j \le B_j$ (§J2), from $\Sigma C$
(§J3), and from the interchangeability of §J6 — cancelled work is not started work,
so a job whose work was abandoned is not interchangeable with one that still has all
of it to do.

**The document is unplannable only when every job has stopped.** A single workflow is
one job, so its stopping is the whole document stopping, which is what a terminal
status has always meant for one workflow (SPEC §6.2).

## J5. Held spots (adds to §Sets and indices, §6, §8)

A spot may be physically occupied by something the plan does not otherwise account
for — material a stopped job left behind (SPEC §6.12). §6 knows a spot is taken only
while some activity's interval covers it, and the interval of the activity that put
the material there has ended, so without saying otherwise the model believes the spot
free and will send other work to a place that is full.

- $T^{\mathrm{held}} \subseteq T$: one **held node** per `occupied` entry. Like a
  boundary node it is a single-mode processing activity in $T$, with no device and no
  consumption; unlike one it has no arcs, is in no dependency pair, and is excluded
  from the makespan (§8). It is nobody's work: its entry may name the job that left
  the material, for a reader and for a later withdrawal, but nothing job-scoped reads
  that (§J0).
- $p_h \in P$: the spot it holds. $since_h \in \mathbb{Z}_{\ge 0}$: when the document
  says it became occupied.

$$
s_h = \max(since_h,\ now), \qquad e_h = \mathcal{H}, \qquad \forall h \in T^{\mathrm{held}}
$$

It occupies $p_h$ over $[s_h, e_h]$ in §6 like any other activity, and no device in
§7.

**It ends at the horizon $\mathcal{H}$ (§J7), not at $C_{\max}$.** Its start is
pinned, so tying its end to the makespan would force $C_{\max} \ge since_h$ and
report a makespan for a run that finished long before. A spot being taken is not
work and must not be timed as though it were; held nodes are therefore also excluded
from §8. The horizon is past every activity by construction, so holding to it says
"for the rest of this plan", which is what the document is claiming.

🔴 **It starts at $since_h$ or $now$, whichever is later.** A $since$ in the past can
constrain nothing — pending work starts at or after $now$ (§9), and fixed work is
pinned by its own history — so the only thing pinning the hold there could do is
collide with that history and refuse the document. And the document refused is the
ordinary one: a stopped job's material is described *twice*, once by the activity
that put it there and once by this section, and nothing in the model can tell that
from a genuine contradiction, having no identity for material. The two descriptions
are composed instead — the history accounts for the spot up to $now$, the held node
from $now$ on. The stated $since_h$ is echoed unchanged in the rendered plan (SPEC §6.1), so
what it records is not lost.

## J6. Symmetry among interchangeable jobs (adds to §CP-SAT implementation notes)

Two jobs running the same workflow with nothing to tell them apart make the search
explore every relabelling of one schedule. Jobs $g$ and $g'$ are **interchangeable**
when all of:

- their workflows have the same structure (equal fingerprints, SPEC §6.11);
- $rel_g = rel_{g'}$ — a job held back until later is not the same job as one that
  may start now;
- neither carries a promise ($B = \infty$) — a bound is exactly a thing that tells
  two jobs apart, and once bounds exist they break the symmetry anyway;
- neither has started, and neither has stopped — reported history is what tells two
  otherwise identical jobs apart.

For an interchangeable group $(g_1, \dots, g_k)$ in roster order:

$$
\min\{\, s_i \mid j(i) = g_\kappa \,\} \;\le\;
\min\{\, s_i \mid j(i) = g_{\kappa+1} \,\},
\quad \kappa = 1, \dots, k-1
$$

This keeps one representative of each equivalence class and loses no optimum:
relabelling interchangeable jobs is an exact automorphism of the instance, so every
schedule it forbids has an equally good permitted twin. It is only sound where the
jobs really are interchangeable — an order imposed on jobs that differ would prune
schedules that are perfectly legitimate — which is why the conditions above are
strict rather than convenient.

In practice it applies to the initial plan, which is also where it is needed: on a
replan the jobs carry bounds and history and are no longer interchangeable at all.

## J7. Horizon (adds to §CP-SAT implementation notes)

Part I's horizon $\mathcal{H}$ is a fully serial schedule. Two more terms are needed
before it is still an upper bound on every end time:

- $\max_j rel_j$ — a job released beyond the bound would have nowhere to be
  scheduled, and the instance would read as infeasible;
- $\max_h since_h$ — a held node stated as taken from a moment beyond the bound
  likewise has nowhere to start.

$now$ and the fixed ends are already in it from Part I's replan case, which is what
keeps $s_h = \max(since_h, now)$ inside the horizon as well.

## J8. Not part of the model

**Which job made a plan impossible.** When the promises cannot all be kept and
relaxing them does not help, each job is taken out in turn and the rest re-solved; a
job whose removal makes the remainder feasible is named (SPEC §6.11, §10.4). This is
an outer procedure over the model, not a constraint in it — and it **reports and
does nothing else**, because discarding work somebody asked for is the caller's
decision, not the scheduler's.

**Withdrawing a job.** Nothing removes a job from a plan. A job's roster entry exists
as long as anything of it is still in the laboratory — unfinished work, or material
nobody has collected — so removing one is only sound when neither is true. Until
then a finished or stopped job stays in the roster, which is also what says its
material still occupies its spot (§J5).

## Reduction to Part I

With one job, everything above is vacuous or identical to Part I:

| | with $|J| = 1$ |
|---|---|
| §J0 | one job, named by the empty string; $j$ distinguishes nothing |
| §J1 | $rel = 0$, so $s_i \ge 0$ and the input node is pinned at $0$ — §3-bis exactly |
| §J2 | no promise, so $C_j \le B_j$ is absent; $C_j$ itself is unused |
| §J3 | the default stages are $(C_{\max}, N_{\mathrm{repl}})$ — Part I's objective |
| §J4 | a terminal status stops the only job, so the document is unplannable, as it always was |
| §J5 | no `occupied` section, so $T^{\mathrm{held}} = \emptyset$ |
| §J6 | one job forms no group |
| §J7 | both added terms are $0$ |

So a single workflow is not *a case of* the joint problem that happens to coincide
with Part I — it **is** Part I's problem, with every addition above switched off by
its own definition. The implementation holds itself to the same statement by
measurement: the plans, charts and diagnostics of every single-workflow example are
byte-for-byte what they were before any of Part II existed.
