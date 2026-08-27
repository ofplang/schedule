"""Device-local consumable resources: what a mode draws, what refills it, and what
is left (§4.7).

A few properties carry most of the weight here. An environment that declares a
stock nobody draws on must stay as undemanding as one that declares none. A started
activity must remain readable after a replan has withdrawn the very mode it used.
And the two halves of a refill have to stay apart: a *planned* one fills to
capacity, while a *started* one is history and is reported as it happened.

Where a test needs to see what a stock does on its own, it uses an environment with
no replenisher (`_env_no_refills`) — otherwise the answer to "not enough" is always
"refill it", and the stock itself is never under test.
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


def _env_no_refills():
    """The same environment with nothing that can refill the reader.

    A legitimate configuration, not a degenerate one (§5.6): it describes a stock an
    operator tops up outside the schedule. It is also the only way to see what a
    stock does on its own, since with a replenisher present the answer to "not
    enough" is always "refill it".
    """
    env = _env()
    env.pop("replenishers", None)
    env.pop("replenishments", None)
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
        doc["inventories"] = {"levels": initial}
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
    # Two assays at 2 units need 4; with nothing to refill it, 3 is not enough.
    report = _plan(env=_env_no_refills(), document=_document({"reader": {"reagent": 3}}))
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
    assert plan["inventories"] == {"levels": {"reader": {"reagent": 0}}}


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
    # Empty and nothing to refill it: the work cannot run.
    report = _plan(env=_env_no_refills(), document=_document({}))
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


def _replan_document(consumption_echo: bool = True, env=None, initial: int = 0):
    """A status taken from the plan at the moment the first assay finishes.

    Everything that has ended by then is marked `completed`; the rest is left out
    and re-derived (§6.2). Cutting the plan at a real instant matters -- a status
    that completed one activity while leaving what fed it pending would be
    inconsistent history, not a test of consumption.
    """
    plan = _plan(env=env, document=_document({"reader": {"reagent": initial}})).plan
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
    doc = _replan_document(env=_env_no_refills(), initial=4)
    assert _plan(env=_env_no_refills(), document=doc).plan is not None

    # With only 3 to begin with there is nothing left for the second assay, and
    # nothing here can refill it.
    doc["inventories"] = {"levels": {"reader": {"reagent": 3}}}
    assert _plan(env=_env_no_refills(), document=doc).plan is None


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
    report = _plan(env=env, document=_replan_document(initial=4))
    assert report.plan is not None, [d.code for d in report.diagnostics]


def test_a_history_that_contradicts_the_environment_is_rejected():
    doc = _replan_document(initial=4)
    # One completed assay drew 2, but the run is said to have started with none.
    doc["inventories"] = {"levels": {"reader": {"reagent": 0}}}
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
    thin = _document({"reader": {"reagent": 3}})
    assert _plan(env=_env_no_refills(), document=thin).plan is None
    assert _off(env=_env_no_refills(), document=thin).plan is not None


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
    assert _off().plan["inventories"] == {"levels": {"reader": {"reagent": 0}}}


def test_switching_off_does_not_change_the_schedule_it_finds():
    on = _plan(env=_env_no_refills(), document=_document({"reader": {"reagent": 4}}))
    off = _off(env=_env_no_refills(), document=_document({"reader": {"reagent": 4}}))
    assert off.plan["objective"] == on.plan["objective"]


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


# --- refills (§4.7.1) ----------------------------------------------------


def _refills(plan):
    return [a for a in plan["activities"] if a["kind"] == "replenishment"]


def test_a_stock_that_runs_out_is_refilled_rather_than_ending_the_run():
    # The reader starts empty. Without a replenisher this cannot be scheduled at
    # all; with one, the answer to "not enough" is "top it up".
    assert _plan(env=_env_no_refills()).plan is None
    plan = _plan().plan
    assert plan is not None
    assert len(_refills(plan)) == 1


def test_a_refill_fills_the_device_to_capacity():
    # Amounts are derived, not chosen (§4.7.1): capacity 6, starting from empty.
    refill = _refills(_plan().plan)[0]
    assert refill["amounts"] == {"reagent": 6}
    assert refill["device"] == "reader"
    assert refill["replenisher"] == "dispenser"


def test_one_refill_covers_work_that_fits_in_one_fill():
    # Two assays at 2 need 4, and a fill gives 6. Minimising the count is what stops
    # the plan carrying a second refill it does not need.
    assert len(_refills(_plan().plan)) == 1


def _disjoint(a: dict, b: dict) -> bool:
    return a["end"] <= b["start"] or b["end"] <= a["start"]


def _touching(plan: dict, machine: str) -> list[dict]:
    """Every activity in `plan` that holds `machine` -- a processing on it, a
    transport onto or off one of its spots, or a refill of it or by it."""
    held = []
    for activity in plan["activities"]:
        kind = activity.get("kind")
        if kind == "processing":
            devices = {s.split(".", 1)[0] for s in (activity.get("input_spots") or {}).values()}
            devices |= {s.split(".", 1)[0] for s in (activity.get("output_spots") or {}).values()}
            devices |= set(activity.get("devices") or [])
            if machine in devices:
                held.append(activity)
        elif kind == "transport":
            ends = {
                str(activity.get(side, "")).split(".", 1)[0] for side in ("from_spot", "to_spot")
            }
            if machine in ends or machine == activity.get("transporter"):
                held.append(activity)
        elif kind == "replenishment":
            if machine in (activity.get("device"), activity.get("replenisher")):
                held.append(activity)
    return held


def test_a_refill_holds_both_machines_so_it_cannot_overlap_the_work_it_feeds():
    """Not only the assays: a refill is exclusive on the device it fills *and* on the
    replenisher that fills it (§4.7.1), against every other activity that holds
    either. The model registers a refill's intervals like any other activity's, so
    this is a check that they reach the non-overlap constraint at all -- they did not,
    and a plan laid a refill straight over the reader it was filling."""
    plan = _plan().plan
    for refill in _refills(plan):
        for machine in (refill["device"], refill["replenisher"]):
            for other in _touching(plan, machine):
                if other is refill:
                    continue
                assert _disjoint(refill, other), (
                    f"{refill['id']} overlaps {other.get('kind')} "
                    f"{other.get('node') or other.get('id')} on {machine}"
                )


def test_the_count_is_reported_as_a_second_objective_stage():
    plan = _plan().plan
    # 33, not 32: the refill holds the reader while it works, so it cannot be laid
    # over the assays that use it. 32 was what came out while refills occupied
    # nothing -- a schedule no lab could run.
    assert plan["objective"] == {
        "kind": ["makespan", "replenishment_count"],
        "value": [33, 1],
    }


def test_a_plan_with_refills_is_a_valid_document():
    assert _doc_codes(_plan().plan) == []


def test_a_device_no_replenisher_reaches_gets_no_refill():
    # Reachability is presence in the table (§5.7). An empty table is a legitimate
    # environment, not a broken one -- the stock simply only falls.
    env = _env()
    env["replenishments"] = []
    assert _plan(env=env).plan is None


def test_switching_off_drops_the_refills_too():
    plan = _off().plan
    assert plan is not None  # the empty stock is simply not looked at
    assert _refills(plan) == []


# --- replanning a run that has refilled ----------------------------------


def _replan_after_refill():
    """A status cut just after the refill has completed."""
    plan = _plan().plan
    refill = _refills(plan)[0]
    now = refill["end"]
    activities = [dict(a, status="completed") for a in plan["activities"] if a["end"] <= now]
    return {"now": now, "inventories": plan["inventories"], "activities": activities}


def test_a_completed_refill_is_folded_into_the_level():
    # It has already raised the stock, so the remaining assays need no second one.
    report = _plan(document=_replan_after_refill())
    assert report.plan is not None, [d.code for d in report.diagnostics]
    planned = [r for r in _refills(report.plan) if r.get("status") is None]
    assert planned == []


def test_a_started_refill_is_carried_back_out_as_history():
    status = _replan_after_refill()
    was = next(a for a in status["activities"] if a["kind"] == "replenishment")
    plan = _plan(document=status).plan
    completed = [r for r in _refills(plan) if r.get("status") == "completed"]
    assert len(completed) == 1
    # Reported as it happened, keeping the id it was planned with (§6.9). Which id
    # that is, is not something to assert: a generated id is unique within one
    # document and no more, so pinning a literal here would be asserting the
    # opposite of what the spec promises.
    assert completed[0]["id"] == was["id"]
    assert completed[0]["amounts"] == was["amounts"]


def test_a_new_refill_does_not_reuse_an_id_the_history_holds():
    # Drain the stock so more refills are needed alongside the one in the history.
    doc = _replan_after_refill()
    doc["inventories"] = {"levels": {"reader": {"reagent": 0}}}
    for entry in doc["activities"]:
        if entry.get("process") == "assay":
            entry["consumption"] = {"reader.reagent": 6}
    plan = _plan(document=doc).plan
    assert plan is not None
    ids = [r["id"] for r in _refills(plan)]
    assert len(ids) == len(set(ids))
    kept = {r["id"] for r in _refills(plan) if r.get("status") is not None}
    assert kept  # the history's own refill is still there, under its own id


def test_a_pending_refill_in_the_status_is_refused():
    # How many refills to run is re-decided every solve, so one in the input
    # describes a decision that is not the caller's to make.
    doc = _document({"reader": {"reagent": 0}})
    doc["activities"] = [
        {
            "kind": "replenishment",
            "id": "replenishment_9",
            "start": 0,
            "end": 4,
            "device": "reader",
            "replenisher": "dispenser",
            "amounts": {"reagent": 6},
        }
    ]
    doc["now"] = 10
    report = _plan(document=doc)
    assert report.plan is None
    assert "pending_replenishment_in_status" in [d.code for d in report.diagnostics]


# -- the plan must be executable, not merely optimal -------------------------
#
# The failure these guard is silent: a plan whose refills add less than its draws
# take schedules cleanly, reports `optimal`, and runs dry in a real lab. It happened
# -- refills reached no non-overlap constraint, so a stock's events were not totally
# ordered, and the amount replay (which assumes they are) dropped a refill the solver
# was counting on. One case below is the smallest environment that showed it.


def _tight(capacity: int, draw: int):
    """The consumable example with the stock tightened: two assays, each drawing
    `draw` from a reader that holds `capacity`, starting empty."""
    env = _env()
    env["devices"][1]["resources"] = {"reagent": {"capacity": capacity}}
    env["processes"]["assay"]["modes"][0]["consumption"] = {"reader.reagent": draw}
    return env


def _added_and_drawn(plan: dict) -> tuple[int, int]:
    added = sum(v for a in _refills(plan) for v in a["amounts"].values())
    drawn = sum(v for a in plan["activities"] for v in (a.get("consumption") or {}).values())
    return added, drawn


@pytest.mark.parametrize(
    "capacity,draw",
    [
        (1, 1),  # one refill per assay; the smallest case that used to fail
        (2, 1),
        (2, 2),
        (3, 2),
        (4, 2),
        (6, 2),  # what the shipped example declares
    ],
)
def test_the_refills_a_plan_carries_cover_the_draws_it_carries(capacity, draw):
    report = _plan(env=_tight(capacity, draw), document=_document({"reader": {"reagent": 0}}))
    assert report.plan is not None, [d.code for d in report.diagnostics]
    added, drawn = _added_and_drawn(report.plan)
    assert added >= drawn, f"the plan adds {added} and takes {drawn}"


@pytest.mark.parametrize("capacity,draw", [(1, 1), (2, 2), (3, 2)])
def test_a_tight_stock_needs_more_than_one_refill(capacity, draw):
    """The cases above are only worth having if they force a second refill: with one
    refill per assay the plan has to carry two, which is what a single-refill plan
    used to claim was enough."""
    report = _plan(env=_tight(capacity, draw), document=_document({"reader": {"reagent": 0}}))
    assert len(_refills(report.plan)) == 2


def test_a_stock_never_exceeds_its_capacity_either():
    """A refill fills to capacity, so the level touches the bound and must not pass
    it -- the other half of `[0, capacity]`."""
    report = _plan(env=_tight(2, 2), document=_document({"reader": {"reagent": 0}}))
    plan = report.plan
    level, capacity = 0, 2
    events: dict[int, int] = {}
    for activity in plan["activities"]:
        if activity.get("kind") == "replenishment":
            end = activity["end"]
            events[end] = events.get(end, 0) + activity["amounts"]["reagent"]
        for amount in (activity.get("consumption") or {}).values():
            events[activity["start"]] = events.get(activity["start"], 0) - amount
    for time in sorted(events):
        level += events[time]
        assert 0 <= level <= capacity, f"level {level} at time {time}"


# -- a running refill and the margin ----------------------------------------
#
# A report of `running` carries the plan's *estimate* of the end, not the actual one:
# the reporter does not know that yet. So a running refill is held to at least
# `now + margin` like any other running activity (FORMULATION §9) -- and the plan says
# so, rather than repeating an estimate it did not schedule around.


def _running_refill(now: int, end: int) -> dict:
    return {
        "time": {"unit": "second"},
        "now": now,
        "inventories": {"levels": {"reader": {"reagent": 0}}},
        "activities": [
            {
                "kind": "replenishment",
                "id": "replenishment_0",
                "status": "running",
                "start": 0,
                "end": end,
                "device": "reader",
                "replenisher": "dispenser",
                "amounts": {"reagent": 6},
            }
        ],
    }


@pytest.mark.parametrize("margin", [0, 1, 3, 10])
def test_a_running_refill_holds_its_device_until_now_plus_margin(margin):
    """The failure this prevents is real and was hit on a real backend: the refill's
    child process was still running when the plan said the reader was free, and the run
    stopped with `device busy`."""
    report = schedule(
        EXAMPLES / "consumable.workflow.yaml", _env(), document_path=_running_refill(now=4, end=4),
        running_task_margin=margin,
    )
    assert report.plan is not None, [d.code for d in report.diagnostics]
    assays = [a for a in report.plan["activities"] if a.get("process") == "assay"]
    assert assays
    assert min(a["start"] for a in assays) >= 4 + margin


@pytest.mark.parametrize("margin,expected", [(0, 4), (1, 5), (3, 7)])
def test_the_plan_reports_the_end_it_scheduled_around(margin, expected):
    report = schedule(
        EXAMPLES / "consumable.workflow.yaml", _env(), document_path=_running_refill(now=4, end=4),
        running_task_margin=margin,
    )
    refill = next(a for a in report.plan["activities"] if a["kind"] == "replenishment")
    assert refill["end"] == expected
    assert refill["status"] == "running"


def test_a_refill_that_has_not_overrun_keeps_its_reported_end():
    report = schedule(
        EXAMPLES / "consumable.workflow.yaml", _env(), document_path=_running_refill(now=4, end=9),
        running_task_margin=0,
    )
    refill = next(a for a in report.plan["activities"] if a["kind"] == "replenishment")
    assert refill["end"] == 9


def test_a_completed_refill_keeps_its_end_whatever_the_margin():
    """A completed end is a fact the reporter does know, so the margin has no business
    with it -- as with the amounts, which are history either way."""
    document = _running_refill(now=9, end=4)
    document["activities"][0]["status"] = "completed"
    report = schedule(
        EXAMPLES / "consumable.workflow.yaml", _env(),
        document_path=document, running_task_margin=5,
    )
    refill = next(a for a in report.plan["activities"] if a["kind"] == "replenishment")
    assert (refill["end"], refill["amounts"]) == (4, {"reagent": 6})
