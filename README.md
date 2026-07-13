# ofplang schedule

A scheduler for **Object-flow Programming Language v0** — a YAML-based dataflow
workflow IR with linear Object tracking. The language is defined in the
[ofplang/spec](https://github.com/ofplang/spec) repository.

The scheduler takes a portable v0 workflow plus an execution environment
definition and plans when its work runs; it also replans from an execution
status. The design is documented in [docs/SPECIFICATIONS.md](docs/SPECIFICATIONS.md).

> **Status:** under construction. The **schema validators** for the environment
> definition and the execution document (plan/status) are implemented (spec §9);
> the scheduler that actually produces a plan is not written yet.

This is a fresh implementation that targets the spec directly. The prototype
[`ofp-scheduler`](https://github.com/ofplang) (OR-Tools CP-SAT) is a reference
for ideas but not a dependency.

## Install

```sh
pip install -e ".[test]"
```

Requires Python 3.10+. The only runtime dependency is PyYAML.

## Command line

```sh
ofp-schedule validate <file>...        # validate an environment or a plan/status
ofp-schedule validate --kind environment env.yaml
ofp-schedule validate --format json doc.yaml
ofp-schedule schedule <file>...        # produce a plan (not implemented yet)
```

`validate` auto-detects whether the file is an environment definition or an
execution document; pass `--kind` to force it. Diagnostics are reported as
`file:line:col: <severity> <code>`. Exit codes: `0` valid (warnings do not fail),
`1` validation errors, `2` usage/input error, `3` not implemented (`schedule`).

This tool is also intended to be exposed as the `schedule` subcommand of the
umbrella `ofp` CLI (a separate repository in the `ofplang` organization).

The package lives under the `ofplang` PEP 420 namespace (`ofplang.schedule`),
shared across the organization's tools.

## Tests

```sh
pytest
```
