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

## `interface_load` — a workflow with boundary (entry/exit) material

- `interface_load.workflow.yaml` — a v0 workflow whose `main` takes an
  Object-bearing **entry input** `sample` and returns a final **output** `result`.
- `interface_load.env.yaml` — the sample is loaded on `loader`, heated on
  `heater`, and the result delivered to `output`.
- `interface_load.document.yaml` — the **interface** (SPEC §6.8): where the sample
  starts (`loader.stage`) and where the result is delivered (`output.slot`).

Unlike the other examples (which create their material internally), this one has
boundary ports. The `interface` constraint pins them, and the plan gains a
**boundary transport** at each end (empty-path arc endpoint = the workflow
interface): `loader.stage → heater.stage` for the entry input and
`heater.stage → output.slot` for the final output. Run it with

```sh
ofp-schedule schedule interface_load.workflow.yaml --env interface_load.env.yaml \
    --document interface_load.document.yaml
```

An Object-bearing entry input with no `interface` binding is an error
(`interface_input_missing`), so `--document` is required here.

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

- `reroute.env.yaml` + `reroute.status.yaml` — a **re-routing** replan. Mid-run,
  `station_1` (where `target` ran) becomes unavailable, so the environment now
  offers `target` only on `station_2` (the device and its spot are kept, plus a
  `station_1.core -> station_2.core` route). The status reports the transport as
  already `completed`, delivering the sample to `station_1.core`.

```sh
ofp-schedule schedule examples/simple.workflow.yaml --env examples/reroute.env.yaml \
    --status examples/reroute.status.yaml
```

Because the sample has already landed on the now-unusable `station_1`, the
scheduler inserts a **relay** at `station_1.core` and a **re-transport** to
`station_2.core`, and runs the target there:
`SampleSource → transport → relay@station_1.core → re-transport → target@station_2.core`
(makespan 11). Committed history (`completed` transport) is pinned as a fact and
not re-validated against the changed environment; only the pending future is
re-optimised. Output `outputs/reroute.replan.yaml` (+ `.lane.svg`). This is the
smallest end-to-end case of transport-arrival normalization (SPEC §6.4.1).

- `reroute_stay.env.yaml` + `reroute_stay.status.yaml` — a **stay-put** replan,
  the folding counterpart to the reroute above. The transport has delivered the
  sample to `station_1.core` and `target` *still* runs there. Normalization still
  derives a relay at the arrival spot and a re-transport to the target's input
  spot, but that spot is the same `station_1.core`, so the re-transport is a
  **zero-distance no-op**: it and its relay are **folded out** of the output
  (SPEC §6.4.1). The committed leg then delivers straight to the target
  (`SampleSource → transport → target@station_1.core`, makespan 5). Output
  `outputs/reroute_stay.replan.yaml` (+ `.lane.svg`) has no relay and no
  zero-distance transport.

- `reroute_chain.env.yaml` + `reroute_chain.status.yaml` — a **chained** reroute.
  Two committed real legs have carried the sample `station_0 → station_1 →
  station_2`; `target` now runs only on `station_3`, reached by a third, real
  leg. Each arrival becomes a relay, so the relays chain, and because **every leg
  is a real move** (no no-op), all of them are **kept**:
  `source → leg → relay@station_1 → leg → relay@station_2 → leg → target@station_3`
  (makespan 14). Output `outputs/reroute_chain.replan.yaml` (+ `.lane.svg`).
  Together with `reroute_stay`, this shows the fold rule both ways: a stay-put
  relay is folded, a relay between real moves is not.

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
`rf_link` spot at zero transport time. Those two are same-spot no-ops
(`from_spot == to_spot`), so they carry no `transporter` (SPEC §6.4); being
single-leg (no relay), they are kept in the plan rather than folded (§6.4.1).

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
