# Examples

Development / verification fixtures for the scheduler. These are not conformance
cases (those live under `tests/conformance/`); they are complete, realistic
inputs used to drive and eyeball the scheduler.

Generated artifacts live under `outputs/`: for each example a solved execution
plan (`<name>.plan.yaml`, §6) and a rendered `device`-view chart
(`<name>.device.svg`), plus the `plate_batch` generator's produced workflow and
environment. The committed plans are a saved snapshot of one optimal solve (the
concrete schedule among equal-makespan optima is not unique); each is a valid
execution document (checked by `test_plan.py`).

## `plate_batch` — a parametric generator (workflow + environment)

- `gen_plate_batch.py` — generates **both** a v0 workflow and a matching
  environment. One `source` fans out to `--branches` branches, each running
  `source → (peal → dispense → seal → thermal_cycle → rotate) × --repeats`, and
  one `sink` gathers them. Ported (structure only) from ofp-scheduler's
  `basic_workflow_demo.py`.
- The repeated structure uses **nested composites**: `repeat_unit` (the five-stage
  chain), `branch` (`--repeats` of those chained), and `main` (a `branch` per
  source port, gathered into the sink). The scheduler flattens this to the same
  atomic graph, so the schedule is identical; plan node paths are hierarchical
  (e.g. `b1/rep1/peal`). This example also exercises nested-composite expansion at
  scale.
- The source has one output port per branch and the sink one input port per
  branch, so their signatures — and the loader's spots — scale with the branch
  count; that is why the environment is generated alongside the workflow rather
  than shared. Single-device stages (peal/dispense/seal/rotate) are contended
  across branches; `thermal_cycle` has a `--thermal-cycler-pool`-device pool (default 2,
  an environment-only knob) the scheduler spreads parallel branches over via mode
  selection. Stages are `elidable_iso`
  (a single `plate` port passes through); the source creates each plate and the
  sink consumes it.
- `outputs/plate_batch.{workflow,env}.yaml` — a sample pair generated with
  `--branches 2 --repeats 2` (plus its rendered charts).

```sh
python examples/gen_plate_batch.py --branches 3 --repeats 2 --out-dir /tmp/bw
python -m ofplang.schedule schedule /tmp/bw/plate_batch.workflow.yaml \
    --env /tmp/bw/plate_batch.env.yaml
```

## `simple` — minimal source → target

- `simple.workflow.yaml` — the v0 workflow.
- `simple.env.yaml` — the matching environment definition.

Ported from `ofp-scheduler`'s `examples/app/job_sample.json`: a `source` step
produces one `Sample`, a `target` step consumes it, connected by a single
Object-bearing arc (one transport between two devices, one spot each). The
reagent consumption + replenishment in the original context is dropped
(device-local resources are outside the initial scope). This is the smallest
end-to-end case, for bringing the scheduler up before the larger `reformatter`.

- `simple.status.yaml` — a **replanning input** (execution status, §7) for this
  example: the `source` step has finished (`completed`, `[0, 2]`) but nothing
  else has, replanned at `now = 3`.

```sh
ofp-schedule schedule examples/simple.workflow.yaml --env examples/simple.env.yaml \
    --status examples/simple.status.yaml
```

The scheduler fixes `SampleSource` to its reported times and mode, and
re-optimises the transport and `SampleTarget` at or after `now`. They slip from
`[2..5]` to `[3..6]`, so the makespan grows from 5 to 6. The output
(`outputs/simple.replan.yaml`) carries the full timeline — the fixed
`completed` history plus the re-optimised future, with `now` echoed — and is
itself a valid execution document that round-trips as the next status input.
Its `lane` chart (`outputs/simple.replan.lane.svg`,
`visualize --view lane`) draws the `now = 3` marker as a dashed line, with the
fixed `source` to its left and the re-optimised work to its right.

## `two_arms` — two jobs on a two-transporter fleet

- `two_arms.workflow.yaml` — the v0 workflow.
- `two_arms.env.yaml` — the matching environment definition.

The smallest case that benefits from more than one transporter (docs
FORMULATION.md §7 / SPEC §4.6). Two independent `source → target` jobs (A and B)
share nothing, so their transports can run at the same time. The environment has
two arms: `arm0` serves both routes, while `arm1` is faster on route A (7 vs 10)
but has no table entry for route B, so it *cannot* make that move (reachability
is presence in the transport table, §5.4).

The optimal schedule is **makespan 20**: job B's move can only use `arm0`
(10 units), and job A takes the faster `arm1` in parallel — the two moves overlap
because they touch disjoint devices. A single transporter would serialise them
for makespan 30. The `device` view (`outputs/two_arms.device.svg`) shows one lane
per arm, with a transport bar on each.

A move occupies its *source* device for its whole duration (the conservative
3-device model, §4.5), so two moves picking up from the same device would
serialise on that device regardless of the arm count — which is why this example
uses two fully independent chains rather than one step fanning out. That makes
the transporter the only shared resource, and the second arm the thing that
relieves it.

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
- **One transporter.** This example models a single arm; for the
  multiple-transporter case see `two_arms`.

### Validated

```sh
# environment definition (this repo's validator)
python -m ofplang.schedule validate --kind environment examples/reformatter.env.yaml

# v0 workflow (ofplang.validate, a sibling tool)
python -m ofplang.validate examples/reformatter.workflow.yaml
```

Both report valid.
