"""Collapsing a class of interchangeable spots (SPEC §10.4, FORMULATION CP-SAT
notes).

Bays a schedule never tells apart are encoded as one resource with room for as
many Objects as there are bays, the modes and routes that only chose between them
become one, and which bay each stay of an Object gets is decided after the solve.

What has to be pinned is that the problem did not change and the plan is still
executable:

- the optimum is the one the separate bays allowed;
- every stay names a real bay, and the mode's **id agrees with its spots** -- the
  plan writes both, so a document where they disagree would contradict itself;
- **no two Objects are in one bay at once**, which is the thing a capacity
  resource does not check;
- the model really is smaller, or nothing was gained;
- and it does not fire where a bay is distinguishable, nor where the guards say a
  capacity resource and the non-overlaps would differ.
"""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule import schedule
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.symmetry import aggregatable_spots, interchangeable_classes
from ofplang.schedule.scheduler.workflow import parse_workflow
from tests.schedutil import SIMPLE_WF, st_env, write

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# One source, two targets, and a shelf with `bays` interchangeable places. The
# shelf is the only thing the two chains share, so how many bays there are is what
# decides whether they overlap.
WORKFLOW = """
spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  make: { kind: atomic, outputs: { o: { type: Sample, phase: data } },
          objects: { create: [outputs.o] } }
  rest:
    kind: atomic
    behavior: [object_identity_map]
    inputs:  { i: { type: Sample, phase: data } }
    outputs: { o: { type: Sample, phase: data } }
  drop: { kind: atomic, inputs: { i: { type: Sample, phase: data } },
          objects: { consume: [inputs.i] } }
  main:
    kind: composite
    inputs: {}
    outputs: {}
    body:
      nodes:
        - { id: M1, process: make }
        - { id: M2, process: make }
        - { id: R1, process: rest, state: { i: { from: M1.o } } }
        - { id: R2, process: rest, state: { i: { from: M2.o } } }
        - { id: D1, process: drop, state: { i: { from: R1.o } } }
        - { id: D2, process: drop, state: { i: { from: R2.o } } }
      returns: {}
entry: main
"""


def env_text(
    bays: int, *, rest: int = 20, slow_bay: int | None = None, instant_in: bool = False
) -> str:
    """A shelf with `bays` places. `slow_bay` makes one of them slower, which is how
    the negative case tells them apart; `instant_in` makes the moves onto the shelf
    take no time, which is how the zero-length guard is reached.

    🔴 The shelf modes are **non-accessing** (§4.4.2). Without that the shelf
    *device* serialises the stays however many bays it has, and the test would be
    about the machine rather than about the bays -- the same point the `storage`
    example makes with its chiller.
    """
    names = [f"bay_{k}" for k in range(1, bays + 1)]
    durations = dict.fromkeys(names, rest)
    if slow_bay is not None:
        durations[names[slow_bay]] = rest + 5
    modes = "\n".join(
        f"      - {{ id: {n}, devices: [shelf], device_access: false,\n"
        f"          duration: {durations[n]},\n"
        f"          input_spots: {{ i: shelf.{n} }}, output_spots: {{ o: shelf.{n} }} }}"
        for n in names
    )
    inbound = 0 if instant_in else 1
    transports = "\n".join(
        f"  - {{ transporter: arm, from: bench.only, to: shelf.{n}, duration: {inbound} }}\n"
        f"  - {{ transporter: arm, from: shelf.{n}, to: bin.only, duration: 1 }}"
        for n in names
    )
    return (
        "time: { unit: second }\n"
        "devices:\n"
        "  - { id: bench, spots: [only] }\n"
        f"  - {{ id: shelf, spots: [{', '.join(names)}] }}\n"
        "  - { id: bin,   spots: [only] }\n"
        "transporters: [ { id: arm } ]\n"
        f"transports:\n{transports}\n"
        "processes:\n"
        "  make: { modes: [ { devices: [bench], duration: 1,\n"
        "          output_spots: { o: bench.only } } ] }\n"
        f"  rest:\n    modes:\n{modes}\n"
        "  drop: { modes: [ { devices: [bin], duration: 1,\n"
        "          input_spots: { i: bin.only } } ] }\n"
    )


def _plan(tmp_path, text: str):
    env_path = write(tmp_path, "env.yaml", text)
    wf_path = write(tmp_path, "wf.yaml", WORKFLOW)
    return schedule(wf_path, env_path)


def _pools(tmp_path, text: str):
    wf, _ = parse_workflow(write(tmp_path, "wf.yaml", WORKFLOW))
    env, _ = load_environment(write(tmp_path, "env.yaml", text))
    instance, _ = build_instance(wf, env)
    assert instance is not None
    return aggregatable_spots(instance, interchangeable_classes(instance))


def _stays(report):
    """Each Object's stay on the shelf, as (bay, start, end)."""
    out = []
    for a in report.plan["activities"]:
        if a["kind"] == "processing" and a["process"] == "rest":
            (bay,) = set(a["input_spots"].values()) | set(a["output_spots"].values())
            out.append((bay, a["start"], a["end"]))
    return out


# --- the decision ---------------------------------------------------------


def test_interchangeable_bays_are_collapsed(tmp_path):
    assert _pools(tmp_path, env_text(3)) == {
        "shelf.bay_1": ("shelf.bay_1", 3),
        "shelf.bay_2": ("shelf.bay_1", 3),
        "shelf.bay_3": ("shelf.bay_1", 3),
    }


def test_bays_of_different_speed_are_not_collapsed(tmp_path):
    assert _pools(tmp_path, env_text(2, slow_bay=1)) == {}


def test_a_zero_length_occupancy_stops_the_class_being_collapsed(tmp_path):
    # G1. A move that takes no time holds its bay for no time, and a zero-length
    # interval is the one place a capacity resource is weaker than a non-overlap:
    # CP-SAT refuses a point strictly inside another interval, counting concurrent
    # demand does not.
    #
    # A mode of no duration would do it too, but §10.2 refuses a processing
    # duration that is not positive -- so a route is how it arises, and it is how
    # it arises in practice: a hand-off that stays where it is takes no time
    # (dev-notes/report-model-size-and-presolve.md §22).
    assert _pools(tmp_path, env_text(2, instant_in=True)) == {}


def test_a_spot_only_one_route_reaches_is_not_collapsed(tmp_path):
    text = env_text(2).replace(
        "  - { transporter: arm, from: bench.only, to: shelf.bay_2, duration: 1 }\n", ""
    )
    assert _pools(tmp_path, text) == {}


# --- the problem is unchanged --------------------------------------------


def test_the_optimum_is_what_the_separate_bays_allowed(tmp_path):
    # Two Objects, each resting 20. With one bay they queue; with two they do not.
    # The collapse must reproduce both answers, or it changed the question.
    one = _plan(tmp_path, env_text(1))
    two = _plan(tmp_path, env_text(2))
    assert one.plan is not None and two.plan is not None
    assert _pools(tmp_path, env_text(1)) == {}, "one bay is no class"
    assert _pools(tmp_path, env_text(2)), "two bays are"
    assert two.makespan < one.makespan
    # And a third bay buys nothing, there being only two Objects.
    assert _plan(tmp_path, env_text(3)).makespan == two.makespan


def test_the_model_is_smaller(tmp_path):
    two, three = _plan(tmp_path, env_text(2)), _plan(tmp_path, env_text(3))
    one = _plan(tmp_path, env_text(1))
    # One mode and one route per group, so the model is the size the one-bay
    # laboratory has however many bays there are.
    assert two.stats.model.variables == one.stats.model.variables
    assert three.stats.model.variables == one.stats.model.variables
    # The laboratory still offers them all.
    assert three.stats.model.transport_options == 3 * one.stats.model.transport_options


# --- the plan is executable ----------------------------------------------


def test_every_stay_names_a_real_bay(tmp_path):
    report = _plan(tmp_path, env_text(3))
    bays = {f"shelf.bay_{k}" for k in (1, 2, 3)}
    named = {bay for bay, _s, _e in _stays(report)}
    assert named and named <= bays


def test_no_two_objects_are_in_one_bay_at_once(tmp_path):
    # The one thing a capacity resource does not check.
    report = _plan(tmp_path, env_text(2))
    by_bay: dict[str, list[tuple[int, int]]] = {}
    for bay, start, end in _stays(report):
        by_bay.setdefault(bay, []).append((start, end))
    for bay, spans in by_bay.items():
        spans.sort()
        for (_s, end), (start, _e) in zip(spans, spans[1:], strict=False):
            assert end <= start, f"{bay} holds two Objects at once: {spans}"
    # Two Objects resting at once is the whole point of the second bay.
    assert len(by_bay) == 2


def test_the_mode_id_agrees_with_the_spots_it_names(tmp_path):
    # 🔴 The plan writes both. Editing the canonical mode's spots and keeping its id
    # would produce a document that contradicts itself, so the assignment finds the
    # activity's own mode that binds the bay it was given.
    report = _plan(tmp_path, env_text(3))
    for a in report.plan["activities"]:
        if a["kind"] != "processing" or a["process"] != "rest":
            continue
        (bay,) = set(a["input_spots"].values()) | set(a["output_spots"].values())
        assert a["mode"] == bay.split(".", 1)[1], (a["mode"], bay)


def test_the_plan_is_a_valid_document_and_the_report_is_ok(tmp_path):
    report = _plan(tmp_path, env_text(3))
    assert report.ok, [d.code for d in report.diagnostics]
    assert report.plan is not None


# --- the examples ---------------------------------------------------------


def test_the_storage_examples_two_classes_are_collapsed():
    wf, _ = parse_workflow(EXAMPLES / "storage.workflow.yaml")
    env, _ = load_environment(EXAMPLES / "storage.env.yaml")
    instance, _ = build_instance(wf, env)
    assert instance is not None
    pools = aggregatable_spots(instance, interchangeable_classes(instance))
    assert {rep for rep, _cap in pools.values()} == {"fridge.slot_1", "prep.bench_1"}
    report = schedule(EXAMPLES / "storage.workflow.yaml", EXAMPLES / "storage.env.yaml")
    assert report.makespan == 450
    # Both fridge slots are used, and each chill's mode names the slot it is in.
    chills = [
        a
        for a in report.plan["activities"]
        if a["kind"] == "processing" and a["process"] == "chill"
    ]
    assert len({a["input_spots"]["plate"] for a in chills}) == 2
    for a in chills:
        assert a["mode"] == a["input_spots"]["plate"].split(".", 1)[1]


def test_the_simple_example_collapses_nothing():
    wf, _ = parse_workflow(SIMPLE_WF)
    env, _ = load_environment(EXAMPLES / "simple.env.yaml")
    instance, _ = build_instance(wf, env)
    assert instance is not None
    assert aggregatable_spots(instance, interchangeable_classes(instance)) == {}


def test_an_environment_with_one_spot_per_device_is_untouched(tmp_path):
    env_path = write(
        tmp_path,
        "env.yaml",
        st_env(
            devices=[("station_0", ["core"]), ("station_1", ["core"])],
            transports=[("station_0.core", "station_1.core", 2)],
        ),
    )
    wf, _ = parse_workflow(SIMPLE_WF)
    env, _ = load_environment(env_path)
    instance, _ = build_instance(wf, env)
    assert instance is not None
    assert aggregatable_spots(instance, interchangeable_classes(instance)) == {}


# --- what the record says the model was -----------------------------------


def test_the_record_says_both_what_the_laboratory_offers_and_what_the_model_got(tmp_path):
    # 🔴 The spot collapse takes mostly *modes* off, so a record carrying only the
    # route counts would have made it look like nothing happened. Both pairs are
    # kept, and each pair is what shows the collapse.
    three = _plan(tmp_path, env_text(3))
    one = _plan(tmp_path, env_text(1))
    model = three.stats.model
    assert model.modes == one.stats.model.modes + 2 * 2, "three bays, two resting nodes"
    assert model.encoded_modes == one.stats.model.encoded_modes
    assert model.transport_options == 3 * one.stats.model.transport_options
    assert model.encoded_transport_options == one.stats.model.encoded_transport_options
    # The encoded counts are what the variables follow.
    assert model.variables == one.stats.model.variables


def test_nothing_collapsed_means_the_two_counts_agree(tmp_path):
    only = _plan(tmp_path, env_text(1))
    model = only.stats.model
    assert model.encoded_modes == model.modes
    assert model.encoded_transport_options == model.transport_options
