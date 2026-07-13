# ofplang.schedule conformance test suite

This suite pins the behavioral contract of `ofplang.schedule`'s **schema
validators** (SPECIFICATIONS.md §9): the execution *environment definition*
(`cases/env/`) and the execution *document* — plan or status (`cases/doc/`).

Each case is a spec-derived example document paired with its expected validation
outcome. Cases are asserted on **stable error codes** (SPECIFICATIONS.md §10),
never on message strings, so they survive refactors and force invalid cases to
fail *for the intended reason*.

> **Status: fixtures only.** These are the case documents and their expectations.
> The validators and the test runner that executes these cases are written later;
> until then the suite is a spec-first specification of the intended behavior
> (analogous to ofplang/validate's `pending`/xfail model).

## Case layout

Every case is a *sidecar* pair — a document plus its expectation:

```
cases/env/spots/unknown_spot.yaml
cases/env/spots/unknown_spot.expected.yaml
```

The case id is the path under `cases/` with the expectation suffix stripped
(`env/spots/unknown_spot`). A shared minimal valid document lives at
`cases/env/_baseline.yaml` and `cases/doc/_baseline.yaml` for reference.

## Expected-outcome schema

```yaml
outcome: invalid          # required: valid | invalid
match: exact              # optional: exact | superset (default: exact)
errors:                   # required iff outcome == invalid
  - code: unknown_spot    # required; must exist in SPECIFICATIONS.md §10
    path: "..."           # optional location hint (not matched by default)
warnings:                 # optional; may accompany outcome: valid
  - code: cross_kind_id_coincidence
notes: "why, citing the spec clause"                     # optional
pending: "reason"         # optional: reserved for the future runner (spec-first)
```

- **`outcome: valid`** — no errors. May still carry `warnings` (e.g. cross-kind id
  coincidence, §8.2); warnings never make a document invalid.
- **`match: exact`** (default): the produced error codes must equal the expected
  set. Used for the one-violation-per-case cases below.
- **`match: superset`**: every expected code must appear; extras tolerated. Use
  only when one violation legitimately fans out.

Codes are compared as **sets** (order- and duplicate-insensitive).

## Authoring conventions

1. **One violation per invalid case.** Break exactly one rule; keep the rest
   valid so the intended code is the only one produced.
2. **Minimal documents.** Include only what the rule needs; derive from the
   `_baseline` where possible.
3. **Cite the spec.** Put the governing section in `notes`.
4. **Shape only.** These validators check a single document's shape
   (SPECIFICATIONS.md §9.1, §9.2). Cross-document checks — against the workflow or
   environment, and solvability — are execution-layer (§9.3) and out of scope
   here.
5. **New error code?** Add it to SPECIFICATIONS.md §10 first.

## Category map

`cases/env/` (environment definition — §9.1, §10.2):

| Directory        | Covers |
|------------------|--------|
| `valid/`         | minimal / full / multi-device mode / Pure-Data process |
| `shape/`         | unknown/missing/extra keys, wrong value kinds, empty devices/modes |
| `identifiers/`   | id grammar, duplicates, cross-kind coincidence (warning) |
| `values/`        | durations, `time.unit`, `objective.kind` |
| `spots/`         | qualified-spot form, unknown device/spot, intra-mode spot rules |
| `transports/`    | unknown transporter, duplicate transport entry |

`cases/doc/` (execution document — §9.2, §10.3):

| Directory        | Covers |
|------------------|--------|
| `valid/`         | plan / status / minimal / Pure-Data + transport |
| `shape/`         | unknown/missing keys, wrong value kinds |
| `activity/`      | kind, status, missing start, ordering, negative times |
| `processing/`    | required fields, node-path shape |
| `transport/`     | required fields, arc shape, qualified spots |
| `toplevel/`      | outcome, objective, now |
| `placements/`    | placement object shape, qualified spot |
