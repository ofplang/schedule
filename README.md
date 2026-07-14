# ofplang schedule

A scheduler for **Object-flow Programming Language v0** — a YAML-based dataflow
workflow IR with linear Object tracking. The language is defined in the
[ofplang/spec](https://github.com/ofplang/spec) repository.

The scheduler takes a portable v0 workflow plus an execution environment
definition and plans when its work runs; it also replans from an execution
status. The design is documented in [docs/SPECIFICATIONS.md](docs/SPECIFICATIONS.md).

> **Status:** early. The **schema validators** (environment definition and
> execution document, spec §9) and the **scheduler** are implemented: it produces
> a makespan-optimal plan for a single workflow with mode selection, spot/device
> occupancy, and transport, and **replans** from an execution status (`--status`)
> by fixing completed/running activities and re-optimising the rest at or after
> `now` (device-local resources not yet). A `visualize` command renders a plan as
> a self-contained SVG/HTML Gantt chart. The model is documented in
> [docs/FORMULATION.md](docs/FORMULATION.md).

This is a fresh implementation that targets the spec directly. The prototype
[`ofp-scheduler`](https://github.com/ofplang) (OR-Tools CP-SAT) is a reference
for ideas but not a dependency.

## Install

```sh
pip install -e ".[test]"
```

Requires Python 3.10+. Runtime dependencies are PyYAML and OR-Tools (the CP-SAT
solver used by the scheduler).

## Command line

```sh
ofp-schedule validate <file>...                 # validate an environment or a plan/status
ofp-schedule schedule <workflow> --env <env> [--status status.yaml] [--running-margin N] [--seed N] [-o plan.yaml] [--format yaml|json]
ofp-schedule visualize <plan> [--view device|workflow|lane] [--theme light|dark|auto] [-o out.svg]
```

`validate` auto-detects whether the file is an environment definition or an
execution document (pass `--kind` to force it); diagnostics are reported as
`file:line:col: <severity> <code>`. `schedule` produces an execution plan (§6)
that minimises makespan; with `--status` it replans from an execution status
(§7), emitting the full timeline (fixed history + re-optimised future) that
round-trips as the next status input. By default the solve is non-deterministic
(a multi-worker search that may return a different equally-optimal schedule each
run); `--seed N` makes it reproducible by fixing the CP-SAT seed and using a
single worker. `visualize` renders a plan as a self-contained Gantt
chart — SVG by default (fixed colours, transparent background, PowerPoint-safe)
or HTML. Exit codes: `0` success, `1` validation errors or no feasible schedule,
`2` usage/input error.

This tool is also intended to be exposed as the `schedule` subcommand of the
umbrella `ofp` CLI (a separate repository in the `ofplang` organization).

The package lives under the `ofplang` PEP 420 namespace (`ofplang.schedule`),
shared across the organization's tools.

## Tests

```sh
pytest
```
