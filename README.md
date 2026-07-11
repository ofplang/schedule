# ofplang schedule

A scheduler for **Object-flow Programming Language v0** — a YAML-based dataflow
workflow IR with linear Object tracking. The language is defined in the
[ofplang/spec](https://github.com/ofplang/spec) repository.

The scheduler takes a portable v0 document and plans when its work runs,
honouring the best-effort **scheduling policies** the language defines
(temporal references, intervals, gaps, and Object policy targets — spec §23–24).

> **Status:** early construction. Only the packaging scaffold and CLI entry
> point exist so far; the scheduler itself is not implemented yet.

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
ofp-schedule <file>...                 # or: python -m ofplang.schedule <file>...
```

Exit codes: `0` scheduled, `2` usage/input error, `3` not implemented yet
(placeholder while the scheduler is under construction).

This tool is also intended to be exposed as the `schedule` subcommand of the
umbrella `ofp` CLI (a separate repository in the `ofplang` organization).

The package lives under the `ofplang` PEP 420 namespace (`ofplang.schedule`),
shared across the organization's tools.

## Tests

```sh
pytest
```
