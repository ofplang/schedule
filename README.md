# ofplang schedule

[![CI](https://github.com/ofplang/schedule/actions/workflows/ci.yml/badge.svg)](https://github.com/ofplang/schedule/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ofplang-schedule.svg)](https://pypi.org/project/ofplang-schedule/)

A scheduler for **Object-flow Programming Language v0** — a YAML-based dataflow
workflow IR with linear Object tracking. The language is defined in the
[ofplang/spec](https://github.com/ofplang/spec) repository.

The scheduler takes a portable v0 workflow plus an execution environment
definition and plans when its work runs; it also replans from an execution
status. The design is documented in [docs/SPECIFICATIONS.md](docs/SPECIFICATIONS.md).

> **Status:** the **schema validators** (environment definition and execution
> document, spec §9) and the **scheduler** are implemented: it produces an optimal
> plan for a single workflow with mode selection, spot/device occupancy, and
> transport, lets a mode **hold a spot without holding its device** for storage and
> incubation (`device_access: false`, spec §4.4.2), pins a workflow's boundary
> material to spots via an `interface`
> (spec §6.8), respects **device-local consumable resources** — what a mode draws
> and what a **replenishment** puts back (spec §4.7) — and **replans** from an
> execution document (`--document`) by fixing completed/running activities and
> re-optimising the rest at or after `now`. A `visualize` command renders a plan as
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
ofp-schedule schedule <workflow> --env <env> [--document doc.yaml] [--running-margin N] [--seed N] [--no-validate] [-o plan.yaml] [--format yaml|json]
ofp-schedule visualize <plan|status> [--view device|workflow|lane] [--theme light|dark|auto] [--format svg|html] [-o FILE]
```

`validate` auto-detects whether the file is an environment definition or an
execution document (pass `--kind` to force it); diagnostics are reported as
`file:line:col: <severity> <code>`. `schedule` produces an execution plan (§6),
minimising the objective the document declares (§4.8; makespan, then the number of
refills). A `--document` (execution document, §6) supplies the `interface` boundary
constraint (§6.8, where a workflow's entry inputs / final outputs sit), the
`inventories` a run starts with (§6.10) where devices hold consumables, the
`objective` (§6.1, now its only declaration site) and, when it sets `now`, the prior status to replan from (§7) —
emitting the full timeline (fixed history + re-optimised future) that round-trips
as the next status input. By default the solve is non-deterministic
(a multi-worker search that may return a different equally-optimal schedule each
run); `--seed N` makes it reproducible by fixing the CP-SAT seed and using a
single worker. `--ignore-resources` switches consumables off (§4.7.3): the
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
