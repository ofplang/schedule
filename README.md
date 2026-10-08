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
> alone is then planned once for both. A **`map` / `fold`** over an Array — of plates
> at the boundary, of values whose count the run states, of a literal's values, of
> another traversal's results — is
> expanded into one invocation per element before planning, so a protocol repeated
> over every plate is written once (spec §2). A `visualize` command renders a plan as
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
ofp-schedule schedule <workflow>... --env <env> [--document doc.yaml] [--withdraw ID] [--carry-levels-to-now] [--ignore-resources] [--running-margin N] [--planner cpsat|greedy] [--max-time SECONDS] [--seed N] [--max-transport-legs N] [--no-validate] [-o plan.yaml] [--format yaml|json]
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
schedule finds room, so bind the ones whose destination matters; an `Array<Plate>`
port is bound to a list of spots, one per plate, and that list's length is how many
plates a `map` / `fold` over it traverses), the `expansion` lengths (§6.13, how many
elements each list of values the run was given has — the run writes them, so a
`map` / `fold` over a list of labels can be expanded without the scheduler reading
a label) and arms (which arm each `branch` on a given flag takes), the
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
`--planner greedy` asks for the schedule to be **built instead of searched for** —
milliseconds rather than seconds, and a valid schedule rather than a short one, with
`plan_constructed` among the diagnostics to say so. The default is `cpsat` and it is
the search; `--max-time` and `--seed` are the search's and do nothing to a built plan.
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
| `node_map`, `node_fold` | Supported. Each node is expanded into its invocations before planning, invocation `i` of `N` under the node path `[N, i, …]`. The number of invocations has to be known before the run — from an Array of Objects bound in `interface` as a list of spots, a Pure Data entry input whose length `expansion` states, a literal, or another map / fold's output. An atomic process with an Object-bearing Array port is not supported (`unsupported_feature`); traverse the Array with a map or fold. |
| `node_branch` | Supported where the arm is known before the run. The branch is expanded with that arm, its activity at the branch's own path; a literal condition decides it here, and an entry-input one -- a flag, or one flag per element inside a `map` -- is decided by the run and stated in the document's `expansion.arms`. A condition produced during the run is refused (`branch_arm_unknown`). |
| `node_do_while` | **Not supported.** How many times a `do_while` runs is decided by values the run produces, so there is no single graph to plan; refused with `unsupported_feature`. |
| `generic_processes` | **Not supported.** Refused with `unsupported_feature`. |

## Library

```python
from ofplang.schedule import schedule

report = schedule(workflow, environment, document_path=status)  # -> ScheduleReport
report = schedule(workflow, environment, planner="greedy")      # built, not searched
```

Alongside the plan, the report carries `stats`: what the *solve* cost, as opposed
to what it decided — timings (including CP-SAT's machine-independent
`deterministic_time`), the bound the answer was measured against, and the size of
the model. It is there on every path that reached the solver, an infeasible
instance included, and `None` on every path that did not — inputs refused before
solving, and a plan that was built rather than searched for, which costs no solve
to report on.

Importing the package does not import the solver. `ortools` is loaded when
something actually solves, so a process that only builds schedules
(`planner="greedy"`), reads a plan or renders a chart never pays for it.
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

## Some plans are impossible, and that is said before the search

Material is always somewhere. It rests in the bay it was made in until a move
takes it away, a move needs its destination empty before it sets off, and two
things never share a bay. A laboratory with few enough bays can therefore reach a
state where every remaining move is into a bay something else is standing in, and
then **no schedule exists** — not a slow one, not a bad one, none.

The solver cannot tell you that. It searches a bounded model and runs out of
budget, so what comes back is `unknown`, which is a fact about the budget rather
than about your laboratory. So the question is settled first, by walking the work
with the clock taken away: if there is no order that gets every Object to the end
even when nothing takes any time, there is none when things do. The plan is
refused as `objects_deadlocked`, naming the bays that were full, and the solve is
not run.

On the RNA-seq case study's minimal laboratory at two jobs — one Tecan bay, one
PCR bay, and a protocol that alternates between them thirteen times — the solver
spent two minutes proving nothing. The walk settles it in about ten milliseconds.

**Silence is not a promise.** It says the Objects can be got to the end, not that
a schedule exists: nothing in the walk is weighed against a duration, a machine, a
transporter or a promised completion. Silence is also the answer when the walk is
too large to finish, so a plan is never refused on a partial search.

## You can ask for the built one instead

`schedule(..., planner="greedy")`, or `--planner greedy` on the command line,
returns the constructed schedule and does not search at all. On the worked
examples that is milliseconds against seconds — and it is a *valid* schedule,
not a short one: the plan carries `plan_constructed` to say so, and nothing
about it claims a better one does not exist.

Ask for it when an answer is wanted sooner than an optimum: a rolling run that
replans every few minutes, a dry run, an interactive tool that wants a first
picture. The default is unchanged, and it is the solver.

Three things can come back instead of a plan, and they mean different things:

| | |
|---|---|
| `planner_unsupported` | the builder does not handle some shape of this plan. **Nothing about the plan** — the solver will schedule it |
| `plan_not_constructed` | it ran and found nothing. **Not a proof of anything**: with nothing yet settled, a plan with no schedule is refused earlier and told why, but after reported history or a spot held since a stated time it may not be -- ask the solver |
| an error before either | the plan has no schedule at all, and the message names which counting argument settled it |

A constructed schedule is read back against the constraints — every spot,
machine and arm exclusion, every route agreement, every precedence — before it
is handed out, and dropped if it does not survive. A wrong hint costs nothing;
a wrong plan is a wrong plan.

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

A forward pass is not always enough. Where the laboratory is narrow the pass can
fill every contended bay and stop — each Object's next bay holding another Object's
material — and no order of the work undoes a state already arrived at. For that
case, and only when every pass has come out empty, the same walk that decides whether
a plan is possible at all is asked for the order it found, and times are laid over
it. On the benchmark's hardest grid that is the difference between a minute of search
returning nothing and a schedule every time.

The construction is **not general**, and does not pretend to be. Two shapes are
declined outright: a refill already under way, which is history it does not carry,
and the work a stopped job abandoned, which it cannot place at an instant it does
not derive. It declines where its passes simply come out empty, too. In every case
the solve proceeds exactly as it did before, and where the construction ran,
`stats.hint_makespan` is the makespan it built — `None` where it declined, so what
the solver started from is visible.

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
boundary material pinned by an `interface`, a `fold` and a `map` over an Array of
plates, a `map` over a list of labels whose length `expansion` states, a branch per
cup whose arm `expansion` states, two jobs on a two-transporter fleet, a plate-reformatting DAG, and a
parametric generator that scales the instance up.
Each comes with its solved plan and a rendered chart under `examples/outputs/`.

## Tests

```sh
pytest
```
