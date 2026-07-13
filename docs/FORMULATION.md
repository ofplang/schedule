# FORMULATION

## Purpose

This document defines the scheduling problem as a mathematical optimization
model. It is the theory `ofplang.schedule` implements, ported from the
`ofp-scheduler` prototype.

`ofp-scheduler` grew the model in incremental steps (fixed stations → station
selection → spot hierarchy → transport → station-local resources →
replenishment → multiple jobs). This document keeps **only the final,
consolidated form** and drops the step-by-step history. Parts that are present
in the final model but outside the immediate implementation focus (e.g.
replenishment) are retained as-is.

The scheduler input, environment schema, execution-document schema, identifiers,
and validator scope are described in `SPECIFICATIONS.md`; this document covers
the optimization model only.

### Note on jobs and workflows

The model is written for the general **multi-job** case: a set of independent
jobs (workflows) sharing physical resources. In the current `ofplang.schedule`
scope a single workflow corresponds to $|J| = 1$; the multi-job constraints
below reduce to the single-job case without change.

## Activities

The scheduling target is not a bare set of tasks but a set of **Activities**.
Three kinds of Activity are scheduled together:

- **Processing Activity** — one per processing Task.
- **Transport Activity** — one per arc; moves an Object from a source spot to a
  destination spot.
- **Replenishment Activity** — refills a station-local consumable resource.

Every Activity has, at minimum:

- a start time and an end time,
- a set of occupied resources, and
- an execution state (`PENDING` / `RUNNING` / `COMPLETED`).

The occupied-resource set is not a constant: it depends on the selected Mode.
Two resource kinds are occupied — **Spots** and **Stations**.

## Sets and indices

- $J$: set of jobs (workflows) to plan.
- $T_j$: processing Task set of job $j$.
- $A_j \subseteq T_j \times T_j$: precedence relation of job $j$;
  $(i,j') \in A_j$ means "$j'$ may start after $i$ completes".
- $R_j$: arc set of job $j$ (output-port → input-port connections).
- $T = \bigcup_{j \in J} T_j$, $A = \bigcup_{j \in J} A_j$,
  $R = \bigcup_{j \in J} R_j$: cross-job processing-Task, precedence, and arc
  sets.
- $L$: Station set (competing spatial resource is the Spot below; a Station
  groups spots and is itself a shared device resource).
- $L^{\mathrm{tr}} \subseteq L$: transport-device Stations. Current scope
  assumes $|L^{\mathrm{tr}}| = 1$, with unique element $\ell^{\mathrm{tr}} \in L$.
- $\ell^{\mathrm{rep}} \in L$: the replenishment-execution Station shared by all
  replenishment Activities.
- $P$: Spot set (the competing spatial resource).
- $M_i$: candidate Mode set of Task $i$. Each Mode fixes the execution station,
  the processing time, and the spot assigned to each port.
- $S_i \subseteq L$: Stations Task $i$ may use across its candidate Modes.
- $G$: consumable resource-type set.
- $H = \{\tau_r \mid r \in R\}$: transport-Activity set.
- $K$: replenishment-candidate set (cross-job). Each $k \in K$ corresponds to a
  processing Task $i(k)$ and a target Station $\ell(k)$.
- $\mathcal{A} = T \cup H \cup \{\text{replenishment Activities}\}$: the full
  Activity set. Processing Tasks are identified with processing Activities.
- $\Theta$: time points at which inventory constraints are checked.
- $I_i$, $O_i$: input-port and output-port sets of Task $i$.

Each Task belongs to exactly one job. There is **no cross-job precedence and no
cross-job arc**: $A_j \subseteq T_j \times T_j$ and $R_j$ is internal to $T_j$.

Every arc $r = (i,j) \in R$ corresponds to some precedence pair in $A$, so the
relation induced by $R$ is a subset of $A$. An arc always denotes a transport.

## Parameters

Processing and transport:

- $p_{i,m} \in \mathbb{Z}_{>0}$: processing time of Task $i$ under Mode $m$.
- $\ell(i,m) \in L$: Station of Task $i$ under Mode $m$.
- $L_{i,m} \subseteq L$: Stations occupied by processing Task $i$ under Mode $m$
  (usually $|L_{i,m}| = 1$; multi-station processing Modes are allowed).
- $\sigma^{\mathrm{in}}_{i,m,k} \in P$: spot for input port $k \in I_i$ of Task
  $i$ under Mode $m$.
- $\sigma^{\mathrm{out}}_{i,m,k} \in P$: spot for output port $k \in O_i$ of Task
  $i$ under Mode $m$.
- $S_{i,m} = \{\sigma^{\mathrm{in}}_{i,m,k} \mid k \in I_i\}
  \cup \{\sigma^{\mathrm{out}}_{i,m,k} \mid k \in O_i\}$: spots occupied by
  Task $i$ under Mode $m$.
- $d_{p,q} \in \mathbb{Z}_{\ge 0}$: transport time from spot $p$ to spot $q$.
  May be treated as symmetric; $d_{p,p} = 0$.
- $L_{r,m,n} \subseteq L$: Stations occupied by the transport Activity for arc
  $r = (i,j)$ under source Mode $m \in M_i$ and destination Mode $n \in M_j$. It
  contains at least the source Station, the destination Station, and the
  transport Station (so typically $|L_{r,m,n}| = 3$).
- $k_r^{\mathrm{out}}$, $k_r^{\mathrm{in}}$: the source output port and
  destination input port of arc $r$.

Station-local consumable resources and replenishment:

- $u_{\ell,g}^{0} \in \mathbb{Z}_{\ge 0}$: initial inventory of resource $g$ at
  Station $\ell$.
- $\bar{u}_{\ell,g} \in \mathbb{Z}_{\ge 0}$: inventory capacity (upper bound) of
  resource $g$ at Station $\ell$.
- $c_{i,m,g} \in \mathbb{Z}_{\ge 0}$: amount of resource $g$ consumed when Task
  $i$ starts under Mode $m$.
- $\rho_{\ell} \in \mathbb{Z}_{\ge 0}$: duration of a replenishment Activity
  targeting Station $\ell$.

Replenishment supply is external and treated as unlimited (no supply-side
inventory constraint). Only processing Tasks consume resources; transport and
replenishment Activities do not.

Rescheduling:

- $now \in \mathbb{Z}_{\ge 0}$: replan time.
- $T^{\mathrm{done}}, T^{\mathrm{run}}, T^{\mathrm{pend}}$: completed, running,
  and pending processing Tasks;
  $T^{\mathrm{pend}} = T \setminus (T^{\mathrm{done}} \cup T^{\mathrm{run}})$.
- $\hat{s}_i, \hat{e}_i$: actual / fixed start and end times.
- $\hat{x}_{i,m}$: actual Mode assignment of a fixed Task.
- $m^{\mathrm{safe}} \in \mathbb{Z}_{\ge 0}$: safety margin for running-Task
  completion.
- $\tilde{e}_i = \max(\hat{e}_i,\ now + m^{\mathrm{safe}})$ for
  $i \in T^{\mathrm{run}}$: fixed end time of a running Task.

Transport and replenishment Activities carry the same `PENDING` / `RUNNING` /
`COMPLETED` states.

## Decision variables

Processing Activities:

- $x_{i,m} \in \{0,1\}$: Task $i$ selects Mode $m$.
- $z_{i,\ell} \in \{0,1\}$: Task $i$ selects a Mode that uses Station
  $\ell \in S_i$.
- $s_i, e_i \in \mathbb{Z}_{\ge 0}$: start and end of Task $i$.

Transport Activities:

- $q_{r,m,n} \in \{0,1\}$: arc $r=(i,j)$'s transport uses source Mode
  $m \in M_i$ and destination Mode $n \in M_j$.
- $a_r, b_r \in \mathbb{Z}_{\ge 0}$: start and end of transport Activity
  $\tau_r$.

Replenishment Activities:

- $y_k \in \{0,1\}$: replenishment candidate $k$ is executed.
- $a_k, b_k \in \mathbb{Z}_{\ge 0}$: start and end of candidate $k$.
- $r_{k,g} \in \mathbb{Z}_{\ge 0}$: amount of resource $g$ replenished by
  candidate $k$.

Objective auxiliaries:

- $C_j \in \mathbb{Z}_{\ge 0}$: completion time of job $j$.
- $C_{\max} \in \mathbb{Z}_{\ge 0}$: global makespan.

## Common Activity-time notation

For an Activity $\alpha \in \mathcal{A}$, write $start_\alpha$ and $end_\alpha$
for its start and end. For processing Activity $\alpha = i \in T$,
$start_\alpha = s_i$ and $end_\alpha = e_i$; for transport Activity
$\alpha = \tau_r$, $start_\alpha = a_r$ and $end_\alpha = b_r$; for
replenishment candidate $k$, $start_\alpha = a_k$ and $end_\alpha = b_k$.

For each spot $p \in P$, let $\mathcal{A}_p$ be the Activities occupying $p$;
for each Station $\ell \in L$, let $\mathcal{A}_\ell$ be the Activities occupying
$\ell$. Occupancy follows the selected Modes:

- processing Activity $i$ occupies $L_{i,m}$ and the spots $S_{i,m}$ of its
  selected Mode,
- transport Activity $\tau_r$ occupies $L_{r,m,n}$ of its selected source/
  destination Mode pair,
- replenishment candidate $k$ occupies target Station $\ell(k)$ and
  $\ell^{\mathrm{rep}}$, and occupies no spot/port.

## Constraints

### 1. Mode selection

$$
\sum_{m \in M_i} x_{i,m} = 1, \quad \forall i \in T
$$

Task–station selector:

$$
z_{i,\ell} = \sum_{m \in M_i:\ \ell(i,m)=\ell} x_{i,m},
\quad \forall i \in T,\ \forall \ell \in S_i
$$

### 2. Processing time

$$
e_i = s_i + \sum_{m \in M_i} p_{i,m}\, x_{i,m}, \quad \forall i \in T
$$

### 3. Precedence and arc ordering

Every precedence pair is respected:

$$
s_j \ge e_i, \quad \forall (i,j) \in A
$$

For each arc $r = (i,j) \in R$, its transport starts after the source Task ends
and finishes before the destination Task starts:

$$
a_r \ge e_i, \qquad s_j \ge b_r, \quad \forall r=(i,j) \in R
$$

### 4. Transport route selection

The transport source/destination Modes must agree with the endpoint Tasks'
Mode selection:

$$
\sum_{n \in M_j} q_{r,m,n} = x_{i,m}, \quad \forall r=(i,j)\in R,\ \forall m \in M_i
$$
$$
\sum_{m \in M_i} q_{r,m,n} = x_{j,n}, \quad \forall r=(i,j)\in R,\ \forall n \in M_j
$$

### 5. Transport time

$$
b_r = a_r + \sum_{m \in M_i}\sum_{n \in M_j}
d_{\sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}},\ \sigma^{\mathrm{in}}_{j,n,k_r^{\mathrm{in}}}}\,
q_{r,m,n}, \quad \forall r=(i,j) \in R
$$

For a zero-distance transport ($d_{p,p}=0$) one may fix $a_r = b_r = e_i$ by
convention to avoid time indeterminacy.

### 6. Spot resource constraint

A processing Activity occupies each spot of its selected Mode over $[s_i, e_i]$.
A transport Activity occupies its **source** and **destination** spots over
*different* intervals. For arc $r=(i,j)$ under Mode pair $(m,n)$, let
$p_r^{\mathrm{src}}(m,n) = \sigma^{\mathrm{out}}_{i,m,k_r^{\mathrm{out}}}$ and
$p_r^{\mathrm{dst}}(m,n) = \sigma^{\mathrm{in}}_{j,n,k_r^{\mathrm{in}}}$. Then

- the source spot is held over $I_r^{\mathrm{src}} = [e_i,\ b_r]$, and
- the destination spot is held over $I_r^{\mathrm{dst}} = [a_r,\ s_j]$.

For each spot $p \in P$, the following intervals must be mutually
non-overlapping:

- $[s_i, e_i]$ for each processing Activity that occupies $p$;
- $I_r^{\mathrm{src}}$ for each transport with $p = p_r^{\mathrm{src}}(m,n)$;
- $I_r^{\mathrm{dst}}$ for each transport with $p = p_r^{\mathrm{dst}}(m,n)$.

Input ports never share a spot with each other, and output ports never share a
spot with each other; an input port and an output port may share a spot.

### 7. Station resource constraint

For each Station $\ell \in L$, the Activities occupying it are mutually
non-overlapping. A processing Activity occupies its Mode's Stations over
$[s_i,e_i]$; a transport Activity occupies $L_{r,m,n}$ over its transport
interval $[a_r,b_r]$ (the conservative formulation: source, destination, and
transport Stations are all held during transport); a replenishment candidate
occupies $\ell(k)$ and $\ell^{\mathrm{rep}}$ over $[a_k,b_k]$.

$$
(end_\alpha \le start_\beta) \lor (end_\beta \le start_\alpha),
\quad \forall \ell \in L,\ \forall \alpha \ne \beta \in \mathcal{A}_\ell
$$

Since $|L^{\mathrm{tr}}| = 1$, all transport Activities are mutually exclusive
through $\ell^{\mathrm{tr}}$.

### 8. Station-local inventory

Resources are consumed at Task start and replenished at replenishment-Activity
end. Bound each replenishment amount so a non-selected candidate replenishes
nothing:

$$
0 \le r_{k,g} \le y_k\,\bar{u}_{\ell(k),g}, \quad \forall k \in K,\ \forall g \in G
$$

A candidate can be selected only if its target Station is actually used:

$$
y_k \le z_{i(k),\ell(k)}, \quad \forall k \in K
$$

Define the inventory of resource $g$ at Station $\ell$ just after time $t$:

$$
I_{\ell,g}(t) = u_{\ell,g}^{0}
+ \sum_{k \in K_\ell} r_{k,g}\,\mathbf{1}[b_k \le t]
- \sum_{i \in T_\ell}\sum_{m \in M_i} c_{i,m,g}\,x_{i,m}\,\mathbf{1}[s_i \le t]
$$

where $K_\ell = \{k \in K \mid \ell(k)=\ell\}$ and $T_\ell$ is the set of
processing Tasks whose selected Mode uses Station $\ell$. Inventory must stay
within $[0, \bar{u}_{\ell,g}]$ at every checked time point:

$$
0 \le I_{\ell,g}(t) \le \bar{u}_{\ell,g},
\quad \forall \ell \in L,\ \forall g \in G,\ \forall t \in \Theta
$$

$\Theta$ may be restricted to processing-Task start times and
replenishment-Activity end times.

Each selected replenishment Activity must replenish a positive amount of at
least one resource. Advance replenishment is allowed: a candidate need not sit
immediately before its triggering Task. Candidates are generated per
(processing Task × candidate Station) to bound model size; the model assumes
this candidate set does not lose the optimum.

### 9. Job completion and makespan

$$
C_j \ge e_i, \quad \forall j \in J,\ \forall i \in T_j
$$
$$
C_{\max} \ge C_j, \quad \forall j \in J
$$

### 10. Rescheduling fixation

Completed and running Activities are fixed; pending ones are re-optimized. This
applies uniformly to processing, transport, and replenishment Activities.

Processing Tasks:

$$
s_i = \hat{s}_i,\ e_i = \hat{e}_i,\ x_{i,m} = \hat{x}_{i,m},
\quad \forall i \in T^{\mathrm{done}}
$$
$$
s_i = \hat{s}_i,\ e_i = \tilde{e}_i,\ x_{i,m} = \hat{x}_{i,m},
\quad \forall i \in T^{\mathrm{run}}
$$
$$
s_i \ge now, \quad \forall i \in T^{\mathrm{pend}}
$$

Pending Tasks' Mode assignment is not fixed and may change on replan; the spot
occupancy of a pending Task follows automatically from its selected Mode.

Replan input is assumed **normalized**: a `RUNNING` / `COMPLETED` transport
Activity never feeds directly into a `PENDING` Task (such cases are removed
before solving).

Resources on replan:

- $u_{\ell,g}^{0}$ is the actual remaining inventory at `now`.
- A `COMPLETED` replenishment Activity's effect is already folded into the
  remaining inventory; its amount is retained as history only.
- A `RUNNING` replenishment Activity contributes a fixed positive event at its
  `end`; it is non-interruptible and its amount is fixed.
- `PENDING` replenishment Activities are not carried over; candidates are
  regenerated. A `PENDING` replenishment Activity supplied as state input is
  rejected as invalid.
- An input under which a `RUNNING` replenishment completion alone forces
  $I_{\ell,g}(t) > \bar{u}_{\ell,g}$ is rejected as invalid.

Jobs added at replan are treated as new jobs whose Activities are all `PENDING`;
no cross-job precedence or arc is created. There is no `NOT_RELEASED` state — all
jobs in the `SchedulingContext` are considered already released at `now`.

## Objective

The objective is a **lexicographic** sequence of stages given by
`SchedulingContext.objective`, a non-empty tuple of distinct, known stage names.
Unknown stage names, an empty tuple, and duplicate stages are rejected. The
initial stages are:

- `"makespan"`: minimize $C_{\max}$ (all completion times);
- `"arc_gap_sum"`: minimize the total arc gap $G_{\mathrm{arc}}$ (below);
- `"replenishment_count"`: minimize the number of new replenishment Activities.

For each arc $r = (i,j) \in R$ define the **arc gap** $g_r = s_j - e_i$ — the
time between the source Task's completion and the destination Task's start
(covering both transport and waiting; zero-distance arcs included). Then

$$
G_{\mathrm{arc}} = \sum_{r \in R} g_r
$$

For simplicity all arcs may be included in the sum, treating gaps fixed by
`RUNNING` / `COMPLETED` state as constants.

The new-replenishment stage minimizes

$$
\sum_{k \in K^{\mathrm{new}}} y_k
$$

where $K^{\mathrm{new}}$ is the set of replenishment candidates generated in the
current solve; `RUNNING` / `COMPLETED` replenishment Activities are fixed history
and are not counted.

### Lexicographic solve

The stages are solved sequentially rather than as a single weighted objective:
minimize stage 1 to its optimum $Z_1^{*}$, fix $Z_1 = Z_1^{*}$ and minimize stage
2, and so on. A common sequence is `("arc_gap_sum", "makespan",
"replenishment_count")`: minimize the arc-gap sum, then minimize makespan at that
optimum, then minimize the count of new replenishment Activities.

`ScheduleResult` returns, in addition to `makespan` and `job_makespans`, the
final value of each executed stage as `objective_values`; `objective_value` is a
compatibility property returning the first element.

## CP-SAT implementation notes

The reference implementation uses OR-Tools CP-SAT. The MILP-style formulations
above (e.g. big-M ordering) are reference models; CP-SAT expresses the same
structure more directly with optional intervals.

- Each processing/transport/replenishment Activity is one or more optional
  intervals whose presence is its Mode/route selector.
- Spot non-overlap: feed each Station's processing interval and each transport's
  source-spot interval $[e_i,b_r]$ and destination-spot interval $[a_r,s_j]$ into
  the spot's `NoOverlap`.
- Station non-overlap: feed processing intervals, the transport body interval
  $[a_r,b_r]$, and replenishment intervals into each Station's `NoOverlap`.
- Inventory: model inventory as a level with negative events at Task starts and
  positive events at replenishment ends, bounded by $[0,\bar{u}_{\ell,g}]$; the
  replenishment-free case may use a `reservoir` constraint. On replan, add fixed
  intervals/positive events for `RUNNING` replenishment Activities and fold
  `COMPLETED` ones into the initial inventory.
- Job completion: derive each $C_j$ with `AddMaxEquality` and bind $C_{\max}$ as
  the max over $C_j$.
- Validate the absence of cross-job precedence/arc before solving.
