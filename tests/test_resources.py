"""Device-local consumable resources: what a mode draws, and what is left (§4.7).

The slice under test is consumption alone — nothing refills a stock yet — so a
level only ever falls. Two properties carry most of the weight here: an environment
that declares a stock nobody draws on must stay as undemanding as one that declares
none, and a started activity must remain readable after a replan has withdrawn the
very mode it used.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ofplang.schedule import schedule, validate_document, validate_environment

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _env(**overrides):
    env = yaml.safe_load((EXAMPLES / "consumable.env.yaml").read_text(encoding="utf-8"))
    env.update(copy.deepcopy(overrides))
    return env


def _reader(env):
    return next(d for d in env["devices"] if d["id"] == "reader")


def _assay_mode(env):
    return env["processes"]["assay"]["modes"][0]


def _env_codes(env):
    return sorted({d.code for d in validate_environment(env).errors})


def _doc_codes(doc):
    return sorted({d.code for d in validate_document(doc).errors})


# --- the environment declares capacity and consumption -------------------


def test_the_example_environment_is_valid():
    assert _env_codes(_env()) == []


def test_capacity_must_be_positive():
    env = _env()
    _reader(env)["resources"]["reagent"]["capacity"] = 0
    # Not `nonpositive_duration`: a capacity of 0 is not a duration, and a
    # diagnostic that called it one would send the reader to the wrong field.
    assert _env_codes(env) == ["nonpositive_value"]


def test_consumption_must_be_positive():
    env = _env()
    # A resource a mode does not draw on is left out, not written as `0`.
    _assay_mode(env)["consumption"]["reader.reagent"] = 0
    assert _env_codes(env) == ["nonpositive_value"]


def test_consumption_must_be_qualified():
    env = _env()
    _assay_mode(env)["consumption"] = {"reagent": 2}
    assert _env_codes(env) == ["malformed_qualified_resource"]


def test_consumption_names_a_declared_resource():
    env = _env()
    _assay_mode(env)["consumption"] = {"reader.buffer": 2}
    assert _env_codes(env) == ["unknown_resource"]


def test_consumption_device_must_be_one_of_the_modes_devices():
    env = _env()
    _reader(env)  # `bench` holds no stock and is not this mode's device either
    env["devices"][0]["resources"] = {"reagent": {"capacity": 5}}
    _assay_mode(env)["consumption"] = {"bench.reagent": 2}
    assert _env_codes(env) == ["resource_device_not_in_mode"]


def test_a_mode_may_not_consume_more_than_the_device_can_hold():
    env = _env()
    _assay_mode(env)["consumption"]["reader.reagent"] = 7  # capacity is 6
    # Decidable from the environment alone, so it is settled there rather than
    # surfacing later as an unexplained infeasibility.
    assert _env_codes(env) == ["consumption_exceeds_capacity"]


def test_a_repeated_resource_is_a_repeated_mapping_key(tmp_path):
    # Resources are keys of a mapping, so a repeat needs no rule of its own. It has
    # to be validated from the text: parsing collapses the repeat last-wins, which
    # is precisely why the document would otherwise say something it does not mean.
    text = (EXAMPLES / "consumable.env.yaml").read_text(encoding="utf-8")
    text = text.replace(
        "      reagent: { capacity: 6 }",
        "      reagent: { capacity: 6 }\n      reagent: { capacity: 3 }",
    )
    path = tmp_path / "env.yaml"
    path.write_text(text, encoding="utf-8")
    assert [d.code for d in validate_environment(path).errors] == ["duplicate_key"]


def test_a_resource_sharing_a_machine_id_is_only_a_warning():
    env = _env()
    _reader(env)["resources"] = {"arm": {"capacity": 6}}  # `arm` is a transporter
    _assay_mode(env)["consumption"] = {"reader.arm": 2}
    result = validate_environment(env)
    assert [d.code for d in result.errors] == []
    assert [d.code for d in result.warnings] == ["cross_kind_id_coincidence"]


# --- the document says what the run started with -------------------------


def _document(initial=None, activities=None):
    doc: dict = {"activities": activities if activities is not None else []}
    if initial is not None:
        doc["inventories"] = {"initial": initial}
    return doc


def test_inventories_shape():
    assert _doc_codes(_document({"reader": {"reagent": 4}})) == []
    assert _doc_codes(_document({"reader": {"reagent": -1}})) == ["negative_value"]
    assert "unknown_key" in _doc_codes({"activities": [], "inventories": {"now": {}}})


def test_a_processing_consumption_echo_is_shape_checked():
    doc = _document(
        {"reader": {"reagent": 4}},
        [
            {
                "kind": "processing",
                "start": 0,
                "end": 1,
                "process": "assay",
                "mode": "0",
                "node": ["AssayA"],
                "consumption": {"reagent": 2},  # not qualified
            }
        ],
    )
    assert _doc_codes(doc) == ["malformed_qualified_resource"]


# --- planning ------------------------------------------------------------


def _plan(env=None, document=None):
    return schedule(
        EXAMPLES / "consumable.workflow.yaml",
        env if env is not None else EXAMPLES / "consumable.env.yaml",
        document_path=document if document is not None else EXAMPLES / "consumable.document.yaml",
    )


def test_the_example_plans():
    report = _plan()
    assert report.plan is not None, [d.code for d in report.diagnostics]
    assert report.plan["outcome"] == "optimal"


def test_a_stock_that_cannot_cover_the_work_is_infeasible():
    # Two assays at 2 units need 4; nothing refills, so 3 is simply not enough.
    report = _plan(document=_document({"reader": {"reagent": 3}}))
    assert report.plan is None
    assert "infeasible" in [d.code for d in report.diagnostics]


def test_the_plan_echoes_what_each_activity_consumed():
    plan = _plan().plan
    assays = [a for a in plan["activities"] if a.get("process") == "assay"]
    assert len(assays) == 2
    assert all(a["consumption"] == {"reader.reagent": 2} for a in assays)


def test_the_plan_carries_the_starting_levels_through():
    # `inventories` says what the run started with, so it round-trips unchanged and
    # the plan can be fed back as the next document.
    plan = _plan().plan
    assert plan["inventories"] == {"initial": {"reader": {"reagent": 4}}}


def test_the_plan_is_itself_a_valid_document():
    assert _doc_codes(_plan().plan) == []


# --- what puts the resource model in effect ------------------------------


def test_declaring_a_stock_nobody_draws_on_demands_nothing():
    # The trigger is a mode that consumes, not a device that declares. An
    # environment must be free to describe what a device holds without obliging
    # every document written against it to state a level.
    env = _env()
    del _assay_mode(env)["consumption"]
    report = _plan(env=env, document=_document())  # no `inventories` at all
    assert report.plan is not None, [d.code for d in report.diagnostics]


def test_a_consuming_environment_requires_the_starting_levels():
    report = _plan(document=_document())
    assert report.plan is None
    assert [d.code for d in report.diagnostics] == ["missing_inventories"]


def test_an_empty_initial_means_every_stock_starts_empty():
    report = _plan(document=_document({}))
    assert report.plan is None
    assert "infeasible" in [d.code for d in report.diagnostics]


@pytest.mark.parametrize(
    "initial,expected",
    [
        ({"freezer": {"reagent": 1}}, "unknown_device"),
        ({"reader": {"buffer": 1}}, "unknown_resource"),
        ({"reader": {"reagent": 7}}, "inventory_exceeds_capacity"),  # capacity is 6
    ],
)
def test_starting_levels_are_resolved_against_the_environment(initial, expected):
    report = _plan(document=_document(initial))
    assert report.plan is None
    assert expected in [d.code for d in report.diagnostics]


# --- replanning ----------------------------------------------------------


def _replan_document(consumption_echo: bool = True):
    """A status taken from the plan at the moment the first assay finishes.

    Everything that has ended by then is marked `completed`; the rest is left out
    and re-derived (§6.2). Cutting the plan at a real instant matters -- a status
    that completed one activity while leaving what fed it pending would be
    inconsistent history, not a test of consumption.
    """
    plan = _plan().plan
    now = min(a["end"] for a in plan["activities"] if a.get("process") == "assay")
    activities = []
    for activity in plan["activities"]:
        if activity["end"] > now:
            continue
        entry = dict(activity)
        entry["status"] = "completed"
        if not consumption_echo:
            entry.pop("consumption", None)
        activities.append(entry)
    return {"now": now, "inventories": plan["inventories"], "activities": activities}


def test_a_completed_activity_draws_down_the_stock():
    # 4 to begin with, one completed assay took 2, and the remaining assay needs 2.
    report = _plan(document=_replan_document(consumption_echo=True))
    assert report.plan is not None, [d.code for d in report.diagnostics]

    # With only 3 to begin with there is nothing left for the second assay.
    doc = _replan_document(consumption_echo=True)
    doc["inventories"] = {"initial": {"reader": {"reagent": 3}}}
    assert _plan(document=doc).plan is None


def test_the_echo_survives_the_mode_being_withdrawn():
    # A replan may drop the very mode a completed activity used -- that is how a
    # re-route is triggered -- and a fixed activity is never re-read against the
    # current environment. Its consumption therefore has to travel with it.
    env = _env()
    env["processes"]["assay"]["modes"] = [
        {
            "id": "backup",
            "devices": ["reader"],
            "duration": 10,
            "input_spots": {"plate": "reader.stage"},
            "output_spots": {"out": "reader.stage"},
            "consumption": {"reader.reagent": 2},
        }
    ]
    report = _plan(env=env, document=_replan_document(consumption_echo=True))
    assert report.plan is not None, [d.code for d in report.diagnostics]


def test_a_history_that_contradicts_the_environment_is_rejected():
    doc = _replan_document(consumption_echo=True)
    # One completed assay drew 2, but the run is said to have started with none.
    doc["inventories"] = {"initial": {"reader": {"reagent": 0}}}
    report = _plan(document=doc)
    assert report.plan is None
    assert "status_inventory_inconsistent" in [d.code for d in report.diagnostics]


# --- switching the model off (§4.7.3) ------------------------------------


def _off(document=None, env=None):
    return schedule(
        EXAMPLES / "consumable.workflow.yaml",
        env if env is not None else EXAMPLES / "consumable.env.yaml",
        document_path=document if document is not None else EXAMPLES / "consumable.document.yaml",
        ignore_resources=True,
    )


def test_switching_off_is_a_relaxation():
    # A stock too small to cover the work is infeasible with the model on, and
    # simply not looked at with it off. Off can never lose a schedule.
    assert _plan(document=_document({"reader": {"reagent": 3}})).plan is None
    assert _off(document=_document({"reader": {"reagent": 3}})).plan is not None


def test_switching_off_needs_no_starting_levels():
    report = _off(document=_document())
    assert report.plan is not None, [d.code for d in report.diagnostics]


def test_switching_off_adds_nothing_a_resource_free_reader_cannot_read():
    # The point of the switch: what the scheduler *adds* must not betray that the
    # environment declared a resource, so a consumer predating them still reads it.
    plan = _off().plan
    assert not any("consumption" in a for a in plan["activities"])
    assert plan["objective"] == {"kind": "makespan", "value": 32}


def test_switching_off_still_echoes_what_the_caller_supplied():
    # `inventories` is the caller's own input; dropping it would lose it from the
    # document rather than leave the plan unmarked.
    assert _off().plan["inventories"] == {"initial": {"reader": {"reagent": 4}}}


def test_switching_off_does_not_change_the_schedule_it_finds():
    on = _plan().plan
    off = _off().plan
    assert off["objective"] == on["objective"]


def test_switching_off_stops_checking_what_it_stopped_applying():
    # What is switched off is not checked: a level above its capacity, or a device
    # the environment does not declare, is simply not looked at.
    for initial in ({"reader": {"reagent": 99}}, {"freezer": {"reagent": 1}}):
        report = _off(document=_document(initial))
        assert report.plan is not None, [d.code for d in report.diagnostics]


def test_switching_off_says_so():
    assert [d.code for d in _off().diagnostics] == ["resources_ignored"]


def test_switching_off_a_stock_nobody_draws_on_says_nothing():
    # Nothing was in effect, so nothing was ignored. A warning here would fire on
    # environments that merely describe what a device holds.
    env = _env()
    del _assay_mode(env)["consumption"]
    assert [d.code for d in _off(env=env, document=_document()).diagnostics] == []


def test_a_plan_made_with_resources_off_is_a_valid_document():
    assert _doc_codes(_off().plan) == []
