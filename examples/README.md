# Examples

Development / verification fixtures for the scheduler. These are not conformance
cases (those live under `tests/conformance/`); they are complete, realistic
inputs used to drive and eyeball the scheduler.

Generated artifacts (a produced workflow, rendered charts) live under
`outputs/`.

## `basic_workflow` — a parametric generator + shared environment

- `gen_basic_workflow.py` — generates a v0 workflow: `--branches` independent
  chains, each `source → (peal → dispense → seal → thermal_cycle → rotate) ×
  --repeats → sink`. Ported (structure only) from ofp-scheduler's
  `basic_workflow_demo.py`.
- `basic_workflow.env.yaml` — the shared environment. It does **not** depend on
  the branch/repeat count: every generated node invokes one of a fixed set of
  process definitions. Single-device stages (peal/dispense/seal/rotate, and the
  loader used by source/sink) are contended across branches; `thermal_cycle` has
  a two-device pool, so parallel branches cycle at once via mode selection. The
  stages are `elidable_iso` (the plate passes through unchanged); the source
  creates the plate and the sink consumes it.
- `outputs/basic_workflow.workflow.yaml` — a sample generated with
  `--branches 2 --repeats 2`.

```sh
python examples/gen_basic_workflow.py --branches 3 --repeats 2 -o wf.yaml
python -m ofplang.schedule schedule wf.yaml --env examples/basic_workflow.env.yaml
```

## `job_sample` — minimal source → target

- `job_sample.workflow.yaml` — the v0 workflow.
- `job_sample.env.yaml` — the matching environment definition.

Ported from `ofp-scheduler`'s `examples/app/job_sample.json`: a `source` step
produces one `Sample`, a `target` step consumes it, connected by a single
Object-bearing arc (one transport between two devices, one spot each). The
reagent consumption + replenishment in the original context is dropped
(device-local resources are outside the initial scope). This is the smallest
end-to-end case, for bringing the scheduler up before the larger `reformatter`.

## `reformatter` — a plate-reformatting DAG

- `reformatter.workflow.yaml` — the ofplang v0 workflow (the logical dataflow
  graph).
- `reformatter.env.yaml` — the matching execution environment definition
  (docs/SPECIFICATIONS.md §5).

Ported from `ofp-scheduler`'s `examples/reformatter_workflow_demo.py`: eight
atomic plate operations fan out from a `Preparation` step across
reformatter / motoman / biomek devices and merge back into a final
`Reformatter3`. Twelve Object-bearing arcs connect them; ten cross-device arcs
become transports, and two intra-reformatter handoffs pass through the shared
`rf_link` spot at zero transport time.

### Translation choices (ofp-scheduler → v0 + environment)

- **One atomic process per operation.** Each ofp-scheduler task has its own port
  signature and duration, and capability is keyed per process definition
  (§5.5), so the eight tasks become eight atomic process definitions rather than
  reusing a shared "reformatter" type.
- **`Biomek2000.A3` / `.A4` → two devices** (`biomek2000_a3`, `biomek2000_a4`):
  a v0 identifier cannot contain `.` (§8.1). They were already distinct,
  independently-serialized stations in ofp-scheduler.
- **Ids normalized**: hyphens → underscores; global spot ids re-expressed as
  `<device>.<local_spot>`.
- **Object accounting**: every operation `consume`s its input plates and
  `create`s its output plates. This is the simplest linearly-complete choice and
  is all the scheduler needs (it only reads process kind, port Object-bearing
  ness, and the arcs). A more physical model could `map` identity through the
  1:1 steps instead.
- **One transporter**, per the initial ofplang.schedule scope.

### Validated

```sh
# environment definition (this repo's validator)
python -m ofplang.schedule validate --kind environment examples/reformatter.env.yaml

# v0 workflow (ofplang.validate, a sibling tool)
python -m ofplang.validate examples/reformatter.workflow.yaml
```

Both report valid.
