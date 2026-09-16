"""Interchangeable resource classes (scheduler/symmetry.py, SPEC §10.4).

The detector claims that relabelling a class's members maps the instance onto
itself, so nothing is lost by choosing differently among them. That claim is what
these tests pin, from both sides: it must fire where the choice really is free,
and it must stay silent wherever anything at all tells the members apart -- a
different duration, a route that reaches only one of them, a document that has
put something on one of them. A false claim here would, in a later slice, be a
schedule quietly thrown away.

The positives are also checked against the repository's own examples, because a
diagnostic is worth what it finds and `storage` / `consumable` are what a real
environment looks like.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ofplang.schedule import schedule
from ofplang.schedule.core.diagnostics import WARNING
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.symmetry import DEVICE, SPOT, TRANSPORTER, interchangeable_classes
from ofplang.schedule.scheduler.workflow import parse_workflow
from tests.schedutil import SIMPLE_WF, committed_source_and_leg, st_env, write

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CODE = "interchangeable_resources"


def _classes(tmp_path, env_text: str):
    """The classes of the `simple` workflow against a hand-built environment."""
    env_path = write(tmp_path, "env.yaml", env_text)
    wf, _ = parse_workflow(SIMPLE_WF)
    env, _ = load_environment(env_path)
    instance, _ = build_instance(wf, env)
    assert instance is not None
    return interchangeable_classes(instance)


def _summary(classes):
    return sorted((c.scope, c.members) for c in classes)


# --- spots of one device --------------------------------------------------


def test_two_spots_a_process_can_choose_between_are_a_class(tmp_path):
    # `target` may run on either of station_1's two spots, for the same duration,
    # and the same move reaches both. Nothing tells them apart.
    classes = _classes(
        tmp_path,
        st_env(
            devices=[("station_0", ["core"]), ("station_1", ["core", "side"])],
            transports=[
                ("station_0.core", "station_1.core", 2),
                ("station_0.core", "station_1.side", 2),
            ],
            target_modes=(("station_1", "station_1.core"), ("station_1", "station_1.side")),
        ),
    )
    assert _summary(classes) == [(SPOT, ("station_1.core", "station_1.side"))]


def test_two_spots_whose_modes_differ_in_duration_are_not_a_class(tmp_path):
    # One spot is slower. Which one the material goes to changes the schedule, so
    # the choice is real and must not be reported as free.
    classes = _classes(
        tmp_path,
        st_env(
            devices=[("station_0", ["core"]), ("station_1", ["core", "side"])],
            transports=[
                ("station_0.core", "station_1.core", 2),
                ("station_0.core", "station_1.side", 2),
            ],
            target_modes=(
                ("station_1", "station_1.core", 2),
                ("station_1", "station_1.side", 5),
            ),
        ),
    )
    assert classes == ()


def test_two_spots_one_route_reaches_further_than_the_other_are_not_a_class(tmp_path):
    # The same mode duration, but getting to `side` takes longer. The transport
    # table is part of what tells two spots apart.
    classes = _classes(
        tmp_path,
        st_env(
            devices=[("station_0", ["core"]), ("station_1", ["core", "side"])],
            transports=[
                ("station_0.core", "station_1.core", 2),
                ("station_0.core", "station_1.side", 7),
            ],
            target_modes=(("station_1", "station_1.core"), ("station_1", "station_1.side")),
        ),
    )
    assert classes == ()


def test_a_spot_no_route_reaches_at_all_is_not_a_class(tmp_path):
    # `side` has a mode but nothing can deliver to it. The instance offers the
    # choice in name only, and the two spots are not interchangeable.
    classes = _classes(
        tmp_path,
        st_env(
            devices=[("station_0", ["core"]), ("station_1", ["core", "side"])],
            transports=[("station_0.core", "station_1.core", 2)],
            target_modes=(("station_1", "station_1.core"), ("station_1", "station_1.side")),
        ),
    )
    assert classes == ()


def test_spots_nothing_chooses_between_are_a_class_that_costs_nothing(tmp_path):
    # station_2 is in the laboratory and idle. Its two spots are interchangeable in
    # exactly the same sense, and saying so would be noise: no mode and no route
    # chooses between them, so the model pays nothing for them. Found, not reported.
    classes = _classes(
        tmp_path,
        st_env(
            devices=[
                ("station_0", ["core"]),
                ("station_1", ["core"]),
                ("station_2", ["bay_a", "bay_b"]),
            ],
            transports=[("station_0.core", "station_1.core", 2)],
        ),
    )
    assert _summary(classes) == [(SPOT, ("station_2.bay_a", "station_2.bay_b"))]
    assert [(c.modes, c.options) for c in classes] == [(0, 0)]


# --- whole devices --------------------------------------------------------


def test_two_identical_devices_are_a_class(tmp_path):
    # Two stations of the same model, reached the same way, offering `target` the
    # same mode. The pool shape: the choice of machine is free.
    classes = _classes(
        tmp_path,
        st_env(
            devices=[("station_0", ["core"]), ("station_1", ["core"]), ("station_2", ["core"])],
            transports=[
                ("station_0.core", "station_1.core", 2),
                ("station_0.core", "station_2.core", 2),
            ],
            target_modes=(("station_1", "station_1.core"), ("station_2", "station_2.core")),
        ),
    )
    assert _summary(classes) == [(DEVICE, ("station_1", "station_2"))]


def test_two_devices_of_different_speed_are_not_a_class(tmp_path):
    classes = _classes(
        tmp_path,
        st_env(
            devices=[("station_0", ["core"]), ("station_1", ["core"]), ("station_2", ["core"])],
            transports=[
                ("station_0.core", "station_1.core", 2),
                ("station_0.core", "station_2.core", 2),
            ],
            target_modes=(
                ("station_1", "station_1.core", 2),
                ("station_2", "station_2.core", 5),
            ),
        ),
    )
    assert classes == ()


# --- transporters ---------------------------------------------------------

_TWO_ARMS = """
time: {{ unit: second }}
devices:
  - {{ id: station_0, spots: [core] }}
  - {{ id: station_1, spots: [core] }}
transporters: [ {{ id: arm_0 }}, {{ id: arm_1 }} ]
transports:
  - {{ transporter: arm_0, from: station_0.core, to: station_1.core, duration: {first} }}
  - {{ transporter: arm_1, from: station_0.core, to: station_1.core, duration: {second} }}
processes:
  source: {{ modes: [ {{ devices: [station_0], duration: 2,
    output_spots: {{ source_out: station_0.core }} }} ] }}
  target: {{ modes: [ {{ devices: [station_1], duration: 2,
    input_spots: {{ target_in: station_1.core }} }} ] }}
"""


def test_two_arms_that_make_the_same_move_in_the_same_time_are_a_class(tmp_path):
    classes = _classes(tmp_path, _TWO_ARMS.format(first=2, second=2))
    assert _summary(classes) == [(TRANSPORTER, ("arm_0", "arm_1"))]


def test_two_arms_of_different_speed_are_not_a_class(tmp_path):
    classes = _classes(tmp_path, _TWO_ARMS.format(first=2, second=3))
    assert classes == ()


def test_the_two_arms_example_is_not_a_class():
    # The example exists to show a transporter that is faster on one route and
    # cannot make another (`examples/two_arms.env.yaml`). That asymmetry is its
    # whole point, and the detector has to see it.
    wf, _ = parse_workflow(EXAMPLES / "two_arms.workflow.yaml")
    env, _ = load_environment(EXAMPLES / "two_arms.env.yaml")
    instance, _ = build_instance(wf, env)
    assert instance is not None
    assert interchangeable_classes(instance) == ()


# --- what the document has put somewhere ----------------------------------


def test_a_spot_reported_history_pinned_is_not_claimed(tmp_path):
    # A completed leg delivered to station_1.core. That spot now carries a fact,
    # and swapping it for the free one is not a relabelling anybody may make -- so
    # the class the initial plan had is gone on the replan.
    env_text = st_env(
        devices=[("station_0", ["core"]), ("station_1", ["core", "side"])],
        transports=[
            ("station_0.core", "station_1.core", 2),
            ("station_0.core", "station_1.side", 2),
        ],
        target_modes=(("station_1", "station_1.core"), ("station_1", "station_1.side")),
    )
    env_path = write(tmp_path, "env.yaml", env_text)
    assert _summary(_classes(tmp_path, env_text)) == [
        (SPOT, ("station_1.core", "station_1.side"))
    ]

    status = write(tmp_path, "status.yaml", committed_source_and_leg(now=3))
    report = schedule(SIMPLE_WF, env_path, document_path=status)
    assert report.plan is not None, [d.code for d in report.diagnostics]
    assert CODE not in [d.code for d in report.diagnostics]


# --- the way out: through `schedule()` ------------------------------------


def test_the_consumable_examples_bench_is_reported_as_a_warning():
    # Two slots on a bench, either of which `make_plate` may use: an ordinary
    # laboratory, and a real instance of the finding.
    report = schedule(
        EXAMPLES / "consumable.workflow.yaml",
        EXAMPLES / "consumable.env.yaml",
        document_path=EXAMPLES / "consumable.document.yaml",
    )
    found = [d for d in report.diagnostics if d.code == CODE]
    assert len(found) == 1
    assert found[0].severity == WARNING
    assert "bench.slot_a" in found[0].message and "bench.slot_b" in found[0].message
    # A warning and nothing more: the plan is produced exactly as before.
    assert report.plan is not None
    assert report.makespan is not None


def test_the_storage_example_reports_both_of_its_classes():
    report = schedule(EXAMPLES / "storage.workflow.yaml", EXAMPLES / "storage.env.yaml")
    messages = sorted(d.message for d in report.diagnostics if d.code == CODE)
    assert len(messages) == 2
    assert "fridge.slot_1" in messages[0] and "fridge.slot_2" in messages[0]
    assert "prep.bench_1" in messages[1] and "prep.bench_2" in messages[1]
    assert report.plan is not None


def test_it_never_makes_a_plan_invalid():
    # The diagnostic is a warning, so a report carrying it is still `ok`.
    report = schedule(
        EXAMPLES / "consumable.workflow.yaml",
        EXAMPLES / "consumable.env.yaml",
        document_path=EXAMPLES / "consumable.document.yaml",
    )
    assert any(d.code == CODE for d in report.diagnostics)
    assert report.ok


def test_the_simple_example_says_nothing():
    # One spot each: nothing to choose between, and no diagnostic.
    report = schedule(SIMPLE_WF, EXAMPLES / "simple.env.yaml")
    assert [d.code for d in report.diagnostics if d.code == CODE] == []


def test_an_environment_whose_spots_differ_is_left_alone(tmp_path):
    # The negative through the front door too, so that the detector's silence is
    # pinned where a caller would see it.
    env = yaml.safe_load((EXAMPLES / "consumable.env.yaml").read_text(encoding="utf-8"))
    env = copy.deepcopy(env)
    # Make the bench's two slots genuinely different: one is slower to load.
    env["processes"]["make_plate"]["modes"][1]["duration"] = 9
    env_path = write(tmp_path, "env.yaml", yaml.safe_dump(env))
    report = schedule(
        EXAMPLES / "consumable.workflow.yaml",
        env_path,
        document_path=EXAMPLES / "consumable.document.yaml",
    )
    assert [d.code for d in report.diagnostics if d.code == CODE] == []


# --- what a class is reported to cost ------------------------------------


def test_the_cost_is_the_quotient_not_one_alternative_per_activity(tmp_path):
    # Two interchangeable spots, and an activity that offers *two* groups of modes
    # over them -- one per duration it may run for. Only each group collapses, so
    # four modes become two. Counting "every mode that names a member but one"
    # would say three.
    classes = _classes(
        tmp_path,
        st_env(
            devices=[("station_0", ["core"]), ("station_1", ["core", "side"])],
            transports=[
                ("station_0.core", "station_1.core", 2),
                ("station_0.core", "station_1.side", 2),
            ],
            target_modes=(
                ("station_1", "station_1.core", 2),
                ("station_1", "station_1.side", 2),
                ("station_1", "station_1.core", 7),
                ("station_1", "station_1.side", 7),
            ),
        ),
    )
    assert _summary(classes) == [(SPOT, ("station_1.core", "station_1.side"))]
    # Four target modes -> two, and four routes -> two.
    assert (classes[0].modes, classes[0].options) == (2, 2)


def test_a_cost_never_exceeds_what_the_instance_has(tmp_path):
    # The loose count could report more removable routes than an instance had at
    # all, which is how the error was found.
    report = schedule(EXAMPLES / "storage.workflow.yaml", EXAMPLES / "storage.env.yaml")
    assert report.stats is not None
    total_modes = report.stats.model.modes
    total_routes = report.stats.model.transport_options
    wf, _ = parse_workflow(EXAMPLES / "storage.workflow.yaml")
    env, _ = load_environment(EXAMPLES / "storage.env.yaml")
    instance, _ = build_instance(wf, env)
    assert instance is not None
    for found in interchangeable_classes(instance):
        assert found.modes < total_modes
        assert found.options < total_routes
