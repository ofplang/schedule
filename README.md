# ofplang schedule

[![CI](https://github.com/ofplang/schedule/actions/workflows/ci.yml/badge.svg)](https://github.com/ofplang/schedule/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ofplang-schedule.svg)](https://pypi.org/project/ofplang-schedule/)

A scheduler for **Object-flow Programming Language v0** — a YAML-based dataflow
workflow IR with linear Object tracking. The language is defined in the
[ofplang/spec](https://github.com/ofplang/spec) repository.

The scheduler takes one or more portable v0 workflows plus an execution environment
definition and plans when their work runs; it also replans from an execution
status. The design is documented in [docs/SPECIFICATIONS.md](docs/SPECIFICATIONS.md).

> **Status:** the **schema validators** (environment definition and execution
> document, spec §9) and the **scheduler** are implemented: it produces an optimal
> plan with mode selection, spot/device occupancy, and
> transport, lets a mode **hold a spot without holding its device** for storage and
> incubation (`device_access: false`, spec §4.4.2), pins a workflow's boundary
> material to spots via an `interface`
> (spec §6.8), respects **device-local consumable resources** — what a mode draws
> and what a **replenishment** puts back (spec §4.7) — and **replans** from an
> execution document (`--document`) by fixing completed/running activities and
> re-optimising the rest at or after `now`. Several workflows can be planned
> **together** against one environment as separate **jobs** (spec §6.11), so they
> compete for the same machines and share the same stocks — a refill neither needs
> alone is then planned once for both. A `visualize` command renders a plan as
> a self-contained SVG/HTML Gantt chart. The model is documented in
> [docs/FORMULATION.md](docs/FORMULATION.md).

This is a fresh implementation that targets the spec directly. The prototype
[`ofp-scheduler`](https://github.com/ofplang) (OR-Tools CP-SAT) is a reference
for ideas but not a dependency.

## Install

```sh
pip install ofplang-schedule
```

Requires Python 3.10+. Runtime dependencies are PyYAML, OR-Tools (the CP-SAT
solver used by the scheduler), and the sibling
[`ofplang-validate`](https://pypi.org/project/ofplang-validate/) (pulled in
automatically), which the CLI's front-door check uses. The scheduler *library*
never imports validate, so embedders that only call `ofplang.schedule` take no
validation overhead.

For development, install editable with the test extra from a clone:

```sh
pip install -e ".[test]"
```

## Command line

```sh
ofp-schedule validate <file>...                 # validate an environment or a plan/status
ofp-schedule schedule <workflow>... --env <env> [--document doc.yaml] [--withdraw ID] [--carry-levels-to-now] [--running-margin N] [--max-time SECONDS] [--seed N] [--max-transport-legs N] [--no-validate] [-o plan.yaml] [--format yaml|json]
ofp-schedule visualize <plan|status> [--view device|workflow|lane] [--theme light|dark|auto] [--format svg|html] [-o FILE]
```

`validate` auto-detects whether the file is an environment definition or an
execution document (pass `--kind` to force it); diagnostics are reported as
`file:line:col: <severity> <code>`. `schedule` produces an execution plan (§6),
minimising the objective the document declares (§4.8; makespan, then the number of
refills). **Give several workflows to plan them together** as separate **jobs** (§6.11):
they compete for the same machines and draw on the same stocks, so a refill neither
needs alone is planned once for both. They are numbered `job1`, `job2`, ... in the
order written, or write `ID=FILE` to name one yourself; every activity in the plan
then carries the job it belongs to. A job may be given its own `interface` and a
`release` time in the document's `jobs` roster, and each is promised the completion
its first plan achieves (`bound`) — which later plans keep, so a job already being
planned is not disturbed by one that arrives later, and which a job that **stops**
has withdrawn rather than restated. Two jobs may share a loading bay where their
releases leave the first job's material time to be collected (a warning, since whether
they do is the solve's to decide); released together onto one spot, or delivering to
one spot, they cannot, and the document is refused — a plan that comes true only
because one of the jobs fails is not one to accept. A job that is not going to deliver
leaves the plan instead, in the same call that plans the next one onto its spot. Every roster entry a plan writes states its
`release`, 0 included: absent, a release means 0 for a job the roster names and `now`
for one it does not, so a plan says which. A job completes when its output
arrives somewhere it may rest; sitting there, or being moved aside later because
another job needs that spot, is not the job's work and does not move its completion.

A `--document` (execution document, §6) supplies the `interface` boundary
constraint (§6.8, where a workflow's entry inputs / final outputs sit — an entry
input has to be bound, while a final output left unbound comes to rest wherever the
schedule finds room, so bind the ones whose destination matters), the
`inventories` levels as of a moment it names (§6.10) where devices hold
consumables, the
`objective` (§6.1, now its only declaration site), the `jobs` roster (§6.11) and the
`occupied` spots something is physically holding (§6.12), and, when it sets `now`,
the prior status to replan from (§7) — emitting the full timeline (fixed history +
re-optimised future) that round-trips as the next status input. By default the solve is non-deterministic
(a multi-worker search that may return a different equally-optimal schedule each
run); `--seed N` makes it reproducible by fixing the CP-SAT seed and using a
single worker. `--max-time SECONDS` caps the search: the best schedule found so
far is returned instead of the proven optimum, which the plan says by reporting
`outcome: feasible` rather than `optimal` — and a search that found nothing in
the budget reports no schedule at all (exit `1`), since an instance is not
unschedulable merely because time ran out.
`--max-transport-legs N` is how many transport activities one Object-bearing arc may
be moved in (§6.4.1). It is **1 by default** — the single hop per arc this has
always planned. Raise it to describe a device the transporter reaches at one position
only, or a plate that has to cross a hand-off station: the arc is then carried in as
many legs as the shortest chain of moves between its endpoint spots takes, joined by
**relay** activities. Only the fewest possible moves are offered, so an arc one move
apart is never sent round by way of somewhere else.
`--withdraw ID` (repeatable) takes a job **out** of a joint plan (§6.11). The roster
is the set of jobs something of which is still in the laboratory, so an entry is
removed when nothing is — which the scheduler cannot see, hence an instruction rather
than an inference, and one refused while the document says the job still has work to
do or running. Pass no workflow for a job being withdrawn. What the job was holding is
written down before it goes (§6.12), dated when the material actually got there:
everything except a spot you **bound as an output and whose product is on it**, which
leaving says you collected. Only that — a delivery that failed on the way left no
product, and entry material a job never collected is material you cannot know the fate
of from outside, so both are written down rather than assumed gone. And a job that
drew on a stock since the moment `inventories` states its levels for cannot leave
quietly — its draws would be given back — so ask for the levels to be carried forward
in the same call.
`--carry-levels-to-now` restates `inventories` as of `now` (§6.10) instead of echoing
the moment it was given. **The scheduler never moves that moment by itself**: working
out the levels is the half it can do and you cannot, and deciding whether the history
before the moment may be let go of is the half only you can.
`--ignore-resources` switches consumables off (§4.7.3): the
declarations are still checked for shape but nothing is applied, and the plan is
shaped as it would be from an environment that never declared one — a relaxation,
so it never turns a solvable instance unsolvable. `--no-validate` skips the one-shot `ofplang-validate` front-door
check of the workflow — use it when the workflow was already validated upstream
(e.g. by the `ofp` umbrella CLI); `$import` is still resolved, since that is
structural rather than a validation check. `visualize` renders any §6 execution
document — a plan, or the status a finished run produced — as a self-contained
Gantt chart, either SVG (fixed colours, transparent background, PowerPoint-safe)
or HTML. `--format` chooses; without it the output is SVG, except that an `-o`
path ending in `.html` or `.htm` is taken as asking for HTML, and an explicit
`--format` always wins — `--format svg -o chart.html` writes SVG. Exit codes:
`0` success, `1` validation errors or no feasible schedule, `2` usage/input
error.

This tool is also the `schedule` subcommand of the umbrella `ofp` CLI
([`ofplang`](https://pypi.org/project/ofplang/)), which forwards to it in-process
with this CLI's own subcommands intact: `ofp schedule schedule …`,
`ofp schedule visualize …`, each with the same options and exit codes as above.

## Feature support

v0 defines seven optional features (spec §4.2), and a document requiring one an
implementation does not have "is valid v0 but unsupported by that implementation"
(§4.1). So `ofp-validate` accepting a workflow does not mean this scheduler can
plan it:

| v0 feature | `ofplang-schedule` |
|---|---|
| `python_script_processes` | Supported. A script process is scheduled like any atomic one; its mode `duration` is the estimate of the compute cost. Running the script is the runner's job. |
| `scheduling_policies` | Accepted, then **ignored**: §23 makes these best-effort preferences, and a composite's `scheduling` section is dropped when the composite is flattened. The report's diagnostics carry a `scheduling_policies_ignored` warning. |
| `node_map`, `node_fold`, `node_do_while`, `node_branch` | **Not supported.** A structured node reshapes dataflow in ways the flat scheduler graph cannot represent, so it is refused with `unsupported_feature`. |
| `generic_processes` | **Not supported.** Refused with `unsupported_feature`. |

## Library

```python
from ofplang.schedule import schedule

report = schedule(workflow, environment, document_path=status)  # -> ScheduleReport
```

Alongside the plan, the report carries `stats`: what the *solve* cost, as opposed
to what it decided — timings (including CP-SAT's machine-independent
`deterministic_time`), the bound the answer was measured against, and the size of
the model. It is there on every path that reached the solver, an infeasible
instance included, and `None` where the inputs were refused before solving.
Passing `collect_solutions=True` additionally records each improving solution as
the search finds it (`stats.phases[-1].history`), which is what an anytime
measurement — how good was the schedule at time *t*? — reads; it is off by default
because a solution callback runs inside the search. None of this enters the plan:
a plan is a portable v0 document and says nothing about how it was found.

## Resources the instance never tells apart

Where an instance offers **several interchangeable ways of using one resource** —
bays of a device, devices of a pool, arms that can make the same moves in the same
times — its model holds a mode and a route for each of them, and every one of those
choices leads to the same schedule. That grows the model quadratically in the size
of such a class, and model size is what bounds solve time. It is not a small effect
on a real laboratory: the standard RNA-seq case study spends three quarters of its
route options choosing between four identical arms, and a growth-curve protocol
run against a laboratory whose devices are written with several places to put
things pays for that annotation with half its model.

So two of the three kinds are **reduced away** before the solver sees them. A class
of interchangeable arms becomes one machine with room for as many moves at once as
there are arms; a class of interchangeable bays becomes one shelf with room for as
many Objects. Their modes and routes collapse to one between them, and which arm
makes each move — and which bay holds each Object — is decided after the solve.

The schedule cannot change. At most that many things ever overlap, and a set of
intervals that thin can always be handed out, so every schedule the separate
resources allowed is still there and no other is added. Bays are the harder of the
two because material stays put: what gets a bay is not one interval but one
Object's whole **stay** — the activity that holds it, the move that brought it,
and the move that takes it away. And neither is applied where the two encodings
could differ, chiefly where an occupancy can have no length at all (a transport
may take no time, §5.4). `docs/FORMULATION.md` Part III sets out both, and what
makes them exact.

Measured on the benchmark, the sixty-four-job instance goes from 34,498 variables
to 2,242 and enters the search for the first time; sixteen jobs solve in under two
seconds where it used to take longer to prove the same answer.

Every class that is **not** reduced is reported instead
(`interchangeable_resources`, a warning), naming its members and what treating
them as one would take off. A class that is reduced says nothing, there being no
cost left to report. It is a claim about *that instance* rather than about the
laboratory — only the processes the workflow instantiated and the routes its arcs
kept are compared — so a resource the document has pinned something to is never
named, and a class shrinks as a run accumulates history.

Nothing about the plan changes: it names one concrete bay, machine and arm per
activity, as always. `stats.model` carries both sides of the count — the modes and
routes the laboratory offers, and the `encoded_` ones the model was given — so the
difference is visible.

## The first schedule is built, not searched for

Before the model reaches CP-SAT the scheduler **constructs a complete schedule by
hand** — list scheduling, forward in time, each activity placed at the first moment
its material, its machine and its bay are all free — and hands it to the solver as a
starting point. The solver may ignore it and may improve on it. Nothing about a plan
says whether it was used.

This is here because of the reduction above. Collapsing a class of interchangeable
resources sharpens what the solver can *prove* about an instance without making any
one schedule easier to *find*: on the two widest benchmark environments the reduced
model proved the optimum in a tenth of a second and then spent a full minute without
producing a single schedule worth that much. A model can carry a good bound and no
witness, and a bound with no witness is not an answer. A constructed schedule is the
witness.

What it is worth, on the RNA-seq case study's standard laboratory:

| | before | now |
|---|---|---|
| one job | optimal in 2.1 s | optimal in 0.6 s |
| two jobs | optimal in 71.4 s | optimal in 2.3 s |
| five jobs | nothing, after twelve minutes | a schedule at once, which two further minutes of search did not better |

The construction is **not general**, and does not pretend to be: it declines an
instance that refills a stock, draws on one, relays an Object between spots, starts
with material already held, replans from a reported history, or promises a job a
completion time — and the solve then proceeds exactly as it did before. Where it
runs, `stats.hint_makespan` is the makespan it built, and `None` where it declined,
so what the solver started from is visible.

It is a *valid* schedule, not a *good* one, and neither is promised. On the widest
benchmark instances it is the optimum. On the five-job laboratory above it is what
comes back — 1.05× the shortest makespan anything has found, and within 1.84× of
what counting the work through the bottleneck says is possible.

Each input is either a path or an already-loaded document (a mapping), so an
embedder that holds them in memory — a rolling-horizon runner rendering a fresh
status every replan — passes them straight in, with no temporary files and nothing
re-parsed. An in-memory document is read, never written to, and the plan it
produces shares no structure with it. Because such a document has no file to point
at, its diagnostics carry no `file:line:col` and locate by their `path` instead,
and the plan's `meta` provenance reads `<in-memory>` unless the caller names the
original file (`workflow_source` / `environment_source` / `document_source`).

The package lives under the `ofplang` PEP 420 namespace (`ofplang.schedule`),
shared across the organization's tools.

`derived_holds(document)` answers the other question a driver of a rolling run has to
ask: **which spots does this document imply are held, beyond the ones it states?**
(§6.12). What a stopped job is still holding follows from its own history rather than
being declared, so the scheduler works it out on every solve — and a caller that worked
it out for itself would be a second implementation of the same rule, differing from it
in ways that show up only as an unplannable document. Deriving it is this package's;
asking is anybody's.

## Examples

[`examples/`](examples/README.md) holds complete workflow + environment pairs used
to drive and eyeball the scheduler: a minimal source → target, a workflow with
boundary material pinned by an `interface`, two jobs on a two-transporter fleet, a
plate-reformatting DAG, and a parametric generator that scales the instance up.
Each comes with its solved plan and a rendered chart under `examples/outputs/`.

## Tests

```sh
pytest
```
