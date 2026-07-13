# Examples

Development / verification fixtures for the scheduler. These are not conformance
cases (those live under `tests/conformance/`); they are complete, realistic
inputs used to drive and eyeball the scheduler.

Generated artifacts (a produced workflow, rendered charts) live under
`outputs/`.

## `basic_workflow` — a parametric generator (workflow + environment)

- `gen_basic_workflow.py` — generates **both** a v0 workflow and a matching
  environment. One `source` fans out to `--branches` branches, each running
  `source → (peal → dispense → seal → thermal_cycle → rotate) × --repeats`, and
  one `sink` gathers them. Ported (structure only) from ofp-scheduler's
  `basic_workflow_demo.py`.
- The source has one output port per branch and the sink one input port per
  branch, so their signatures — and the loader's spots — scale with the branch
  count; that is why the environment is generated alongside the workflow rather
  than shared. Single-device stages (peal/dispense/seal/rotate) are contended
  across branches; `thermal_cycle` has a fixed two-device pool the scheduler
  spreads parallel branches over via mode selection. Stages are `elidable_iso`
  (a single `plate` port passes through); the source creates each plate and the
  sink consumes it.
- `outputs/basic_workflow.{workflow,env}.yaml` — a sample pair generated with
  `--branches 2 --repeats 2` (plus its rendered charts).

```sh
python examples/gen_basic_workflow.py --branches 3 --repeats 2 --out-dir /tmp/bw
python -m ofplang.schedule schedule /tmp/bw/basic_workflow.workflow.yaml \
    --env /tmp/bw/basic_workflow.env.yaml
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
