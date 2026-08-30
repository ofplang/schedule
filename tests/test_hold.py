"""A mode that holds a spot without holding its device (SPEC §4.4.2).

`device_access: false` says the material merely rests on the machine -- a plate
chilling in a refrigerator, a rack waiting in a hotel. The spot is bound like any
other mode's, so it stays exclusive; the device is not, so several such activities
run side by side and a transport can reach in while one of them is resting.

Driven by the `storage` example: three plates, a refrigerator with two slots.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ofplang.schedule import schedule

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load(name: str) -> dict:
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


def _storage() -> tuple[dict, dict]:
    return _load("storage.workflow.yaml"), _load("storage.env.yaml")


def _occupying(env: dict) -> dict:
    """The same environment with `chill` occupying the fridge after all."""
    env = copy.deepcopy(env)
    for mode in env["processes"]["chill"]["modes"]:
        del mode["device_access"]
    return env


def _chills(plan: dict) -> list[dict]:
    return [a for a in plan["activities"] if a.get("process") == "chill"]


def test_holds_share_one_device():
    """Three 180s chills on one fridge, two slots: they overlap."""
    workflow, env = _storage()
    report = schedule(workflow, env, random_seed=0)
    assert report.outcome == "optimal"
    assert report.makespan == 450

    chills = _chills(report.plan)
    assert len(chills) == 3
    assert all(a["devices"] == ["fridge"] for a in chills)
    assert all(a["end"] - a["start"] == 180 for a in chills)
    # Some pair runs at the same time -- which is the whole point.
    assert any(
        a["start"] < b["end"] and b["start"] < a["end"]
        for a in chills
        for b in chills
        if a is not b
    )


def test_the_same_instance_serialises_when_the_device_is_accessed():
    """Deleting the key restores the old meaning, and costs 200 seconds."""
    workflow, env = _storage()
    report = schedule(workflow, _occupying(env), random_seed=0)
    assert report.outcome == "optimal"
    assert report.makespan == 650

    chills = sorted(_chills(report.plan), key=lambda a: a["start"])
    assert all(a["end"] <= b["start"] for a, b in zip(chills, chills[1:], strict=False))


def test_the_spot_is_still_exclusive():
    """The device is free; the slot is not. One slot serialises the three."""
    workflow, env = _storage()
    env = copy.deepcopy(env)
    env["devices"][1]["spots"] = ["slot_1"]
    env["processes"]["chill"]["modes"] = [env["processes"]["chill"]["modes"][0]]
    env["transports"] = [
        t for t in env["transports"] if "fridge.slot_2" not in (t["from"], t["to"])
    ]

    report = schedule(workflow, env, random_seed=0)
    assert report.outcome == "optimal"
    chills = sorted(_chills(report.plan), key=lambda a: a["start"])
    assert all(a["end"] <= b["start"] for a, b in zip(chills, chills[1:], strict=False))


def test_the_plan_echoes_it_only_where_it_is_false():
    """§6.3: written where the mode does not access, absent everywhere else -- so a
    plan from an environment that never says `device_access` is shaped as before."""
    workflow, env = _storage()
    plan = schedule(workflow, env, random_seed=0).plan

    for a in plan["activities"]:
        if a.get("process") == "chill":
            assert a["device_access"] is False
        else:
            assert "device_access" not in a

    unchanged = schedule(workflow, _occupying(env), random_seed=0).plan
    assert all("device_access" not in a for a in unchanged["activities"])


def _history(plan: dict, now: int) -> dict:
    """The plan's own activities as a status at `now`: what has finished is
    completed, what spans `now` is running, and what has not started is left out
    for the replan to place again (§7)."""
    activities = []
    for a in plan["activities"]:
        if a["end"] <= now:
            activities.append({**a, "status": "completed"})
        elif a["start"] <= now:
            activities.append({**a, "status": "running"})
    return {"time": {"unit": "second"}, "now": now, "activities": activities}


def test_a_running_hold_does_not_block_its_device():
    """The echo is what makes this true (§6.3).

    A fixed activity is read back from its echo and never re-read against the
    environment (§7). Without `device_access` in the echo, a running chill would be
    pinned as one that *occupies* the fridge, and nothing could be moved into the
    other slot until it finished.
    """
    workflow, env = _storage()
    plan = schedule(workflow, env, random_seed=0).plan

    running = [a for a in _chills(plan) if a["start"] <= 100 < a["end"]]
    assert len(running) == 1, "the fixture expects exactly one chill running at 100"
    blocked_until = running[0]["end"]

    status = _history(plan, 100)
    replan = schedule(workflow, env, document_path=status, random_seed=0)
    assert replan.outcome == "optimal"
    pending = [a for a in _chills(replan.plan) if a.get("status") is None]
    assert pending, "the other chills are still to be placed"
    assert min(a["start"] for a in pending) < blocked_until

    # And the contrast: strip the echo, and the running activity holds the fridge.
    stripped = copy.deepcopy(status)
    for a in stripped["activities"]:
        a.pop("device_access", None)
    held = schedule(workflow, env, document_path=stripped, random_seed=0)
    assert held.outcome == "optimal"
    pending = [a for a in _chills(held.plan) if a.get("status") is None]
    assert min(a["start"] for a in pending) >= blocked_until
