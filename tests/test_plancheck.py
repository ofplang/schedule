"""The self-check on a rendered plan (`scheduler/plancheck.py`).

The scheduler proves its schedule against a reservoir, then *derives* the refill
amounts for the document it hands out (§4.7.1) rather than reading them off the
solver. Two computations over one answer, so the document is checked before it
leaves rather than trusted to agree.

These tests drive the check directly with hand-written plans, because the point of
a safety net is to work when the thing above it does not: a plan the solver would
never produce is exactly what has to be caught. `test_resources.py` covers the
solver's side.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.plancheck import check_plan_inventories

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _env(capacity: int = 6):
    raw = yaml.safe_load((EXAMPLES / "consumable.env.yaml").read_text(encoding="utf-8"))
    next(d for d in raw["devices"] if d["id"] == "reader")["resources"] = {
        "reagent": {"capacity": capacity}
    }
    # A mode may not declare a draw larger than the stock could ever hold
    # (`consumption_exceeds_capacity`), so the declared draw follows the capacity
    # down. What each *plan* below draws is written into its own activities.
    mode = raw["processes"]["assay"]["modes"][0]
    mode["consumption"] = {"reader.reagent": min(2, capacity)}
    env, result = load_environment(raw)
    assert env is not None, [d.code for d in result.diagnostics]
    return env


def _levels(reagent: int) -> dict:
    return {"levels": {"reader": {"reagent": reagent}}}


def _draw(start: int, amount: int) -> dict:
    return {
        "kind": "processing",
        "start": start,
        "end": start + 10,
        "process": "assay",
        "consumption": {"reader.reagent": amount},
    }


def _fill(end: int, amount: int, identifier: str = "replenishment_0") -> dict:
    return {
        "kind": "replenishment",
        "id": identifier,
        "start": end - 4,
        "end": end,
        "device": "reader",
        "replenisher": "dispenser",
        "amounts": {"reagent": amount},
    }


def _check(activities, reagent=0, capacity=6):
    return check_plan_inventories({"activities": activities}, _env(capacity), _levels(reagent))


# -- what must pass ----------------------------------------------------------


def test_a_coherent_plan_has_nothing_to_report():
    assert _check([_fill(4, 6), _draw(5, 2), _draw(19, 2)]) == []


def test_a_plan_that_draws_only_what_it_started_with_needs_no_refill():
    assert _check([_draw(5, 2), _draw(19, 2)], reagent=4) == []


def test_a_stock_may_be_taken_to_exactly_zero():
    assert _check([_draw(5, 4)], reagent=4) == []


def test_a_refill_may_take_a_stock_to_exactly_its_capacity():
    assert _check([_fill(4, 6)], reagent=0, capacity=6) == []


def test_a_completion_and_a_start_at_one_instant_are_checked_in_turn():
    """A refill ending exactly when a draw starts is how a schedule packs work: a
    device is released at one end and taken at the next start, at the same instant.
    The completion is applied first and the level checked, then the start (§4.7) --
    the same order the solver was built under, so this replay agrees with it."""
    # Capacity 2, empty. The fill lands at 5 and the draw takes 2 at 5. Filling
    # first reaches 2 (the capacity, allowed) and the draw takes it back to 0.
    assert _check([_fill(5, 2), _draw(5, 2)], reagent=0, capacity=2) == []
    # And the refill is what makes the draw possible: without it the draw at 5 would
    # take an empty stock negative.
    assert _check([_draw(5, 2)], reagent=0, capacity=2) != []


def test_a_refill_onto_a_full_stock_is_caught_even_with_a_draw_at_that_instant():
    """The level a refill leaves behind is real -- it is what the device holds when
    the refill finishes -- so it must fit in the device. A simultaneous draw does not
    excuse it: netting the two would hide a plan that puts 4 units into a device
    holding 2. Such a plan is a defect in this package, which is what this module is
    here to catch."""
    messages = _check([_fill(5, 2), _draw(5, 2)], reagent=2, capacity=2)
    assert messages and "at 4" in messages[0] and "a refill" in messages[0]


def test_no_consumption_anywhere_means_nothing_to_check():
    """A resource-free environment, and `--ignore-resources`, both look like this:
    no `consumption` echo, so no level to get wrong (§4.7.3)."""
    assert _check([{"kind": "processing", "start": 0, "end": 5, "process": "make"}]) == []
    assert check_plan_inventories({"activities": []}, _env(), None) == []


def test_transports_and_relays_do_not_touch_a_stock():
    activities = [
        {"kind": "transport", "start": 0, "end": 2, "from_spot": "a.x", "to_spot": "b.y"},
        {"kind": "relay", "start": 2, "end": 2, "spot": "b.y", "seq": 0},
        _draw(5, 2),
    ]
    assert _check(activities, reagent=2) == []


# -- what must be caught -----------------------------------------------------


def test_a_plan_that_draws_more_than_it_has_is_reported():
    found = _check([_draw(5, 2)], reagent=1)
    assert len(found) == 1
    assert "reader.reagent" in found[0] and "-1" in found[0]


def test_the_bug_this_check_exists_for_is_caught():
    """The real shape: two draws of 1 from a stock that starts empty, and one refill
    of 1. The solver had selected two refills; the amount replay dropped one, and
    the plan went out adding half of what it takes."""
    found = _check([_fill(4, 1), _draw(5, 1), _draw(19, 1)], reagent=0, capacity=1)
    assert len(found) == 1
    assert "at time 19" in found[0]


def test_overfilling_past_the_capacity_is_reported():
    found = _check([_fill(4, 6), _draw(19, 1)], reagent=4, capacity=6)
    assert len(found) == 1
    assert "at 10" in found[0] and "[0, 6]" in found[0]


def test_the_first_moment_a_stock_goes_wrong_is_the_one_reported():
    """One mistake, one message: a stock that goes negative and stays negative is a
    single finding, not one per activity after it."""
    found = _check([_draw(5, 2), _draw(19, 2), _draw(30, 2)], reagent=1)
    assert len(found) == 1
    assert "at time 5" in found[0]


def test_each_offending_stock_is_reported_once():
    raw = yaml.safe_load((EXAMPLES / "consumable.env.yaml").read_text(encoding="utf-8"))
    next(d for d in raw["devices"] if d["id"] == "reader")["resources"] = {
        "reagent": {"capacity": 6},
        "tips": {"capacity": 6},
    }
    env, _ = load_environment(raw)
    plan = {
        "activities": [
            {
                "kind": "processing",
                "start": 5,
                "end": 15,
                "process": "assay",
                "consumption": {"reader.reagent": 1, "reader.tips": 1},
            }
        ]
    }
    found = check_plan_inventories(plan, env, {"levels": {}})
    assert len(found) == 2
    assert any("reagent" in m for m in found) and any("tips" in m for m in found)
