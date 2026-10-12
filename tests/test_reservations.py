"""What an undecided region keeps for itself (design.md D66, stage 4).

While a branch or loop decided only during the run is open, the plan keeps the spots
it may yet need free of everything else: a branch from its decision to its exit, a
loop from its first start to its exit. Then whichever outcome the run sees, nothing
outside the region is standing where the region has to go, and a replan only has to
make things later. These tests pin that on the plan: the `reservation` entries, what
nothing else does inside them, and the regions that cannot be kept.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ofplang.schedule import JobInput, schedule, schedule_jobs, validate_document
from tests.schedutil import kinds

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _example(name: str):
    def load(kind: str):
        return yaml.safe_load((EXAMPLES / f"{name}.{kind}.yaml").read_text(encoding="utf-8"))

    return load("workflow"), load("env"), load("document")


def _two_rinses():
    """Two jobs of the rinse-until-clean loop on one sink and one reader, with
    nowhere to wait -- the laboratory where planning on an assumed end used to lock."""
    workflow, env, document = _example("rinse_until_clean")
    env = copy.deepcopy(env)
    env["devices"] = [
        d if d["id"] != "rack" else {**d, "spots": [*d["spots"], "in2", "out2"]}
        for d in env["devices"]
    ]
    extra = []
    for t in env["transports"]:
        if t["from"] == "rack.in":
            extra.append({**t, "from": "rack.in2"})
        if t["to"] == "rack.out":
            extra.append({**t, "to": "rack.out2"})
    env["transports"] += extra
    jobs = [
        JobInput("a", copy.deepcopy(workflow)),
        JobInput("b", copy.deepcopy(workflow)),
    ]
    roster = {
        "activities": [],
        "jobs": [
            {"id": "a", "release": 0, "interface": document["interface"]},
            {"id": "b", "release": 0, "interface": {
                "inputs": {"plate": "rack.in2"}, "outputs": {"plate": "rack.out2"}}},
        ],
    }
    return jobs, env, roster


def _occupies(entry: dict, spot: str) -> bool:
    if entry["kind"] == "processing":
        spots = {*entry.get("input_spots", {}).values(), *entry.get("output_spots", {}).values()}
        return spot in spots
    if entry["kind"] == "transport":
        moved = entry["from_spot"] != entry["to_spot"]
        return moved and spot in (entry["from_spot"], entry["to_spot"])
    return False


def _inside(entry: dict, start: int, end: int) -> bool:
    return entry["start"] < end and start < entry["end"]


# --- loops -----------------------------------------------------------------------------


def test_each_loop_keeps_its_spots_and_nothing_else_is_on_them():
    jobs, env, roster = _two_rinses()
    report = schedule_jobs(jobs, env, document_path=roster, random_seed=0)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    kept = kinds(report.plan, "reservation")
    assert sorted(entry["job"] for entry in kept) == ["a", "b"]
    for entry in kept:
        assert entry["node"] == ["Clean"]
        assert "sink.basin" in entry["spots"] and "reader.slot" in entry["spots"]
        # Nothing of the other job is on a kept spot inside the window.
        for other in report.plan["activities"]:
            if other.get("job") == entry["job"] or "start" not in other:
                continue
            for spot in entry["spots"]:
                if _occupies(other, spot):
                    assert not _inside(other, entry["start"], entry["end"]), (entry, other)
    # Two loops keeping the one sink take turns.
    (first, second) = sorted(kept, key=lambda e: e["start"])
    assert first["end"] <= second["start"]


def test_the_plan_with_reservations_is_a_valid_document():
    jobs, env, roster = _two_rinses()
    plan = schedule_jobs(jobs, env, document_path=roster, random_seed=0).plan
    result = validate_document(plan)
    assert result.ok, [(d.code, d.message) for d in result.diagnostics]


def test_a_loop_that_has_ended_keeps_nothing():
    workflow, env, document = _example("rinse_until_clean")
    ended = {**document, "expansion": {"loops": [{"node": ["Clean"], "count": 2, "ended": True}]}}
    report = schedule(workflow, env, document_path=ended, random_seed=0)
    assert report.ok and not kinds(report.plan, "reservation")


# --- branches ----------------------------------------------------------------------------


def test_each_undecided_branch_keeps_both_arms_and_its_exit():
    workflow, env, document = _example("inspect_then_wash")
    report = schedule(workflow, env, document_path=document, random_seed=0)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    kept = {tuple(entry["node"]): entry for entry in kinds(report.plan, "reservation")}
    assert set(kept) == {("Each", i, "Choose") for i in range(3)}
    for i, entry in enumerate(kept[("Each", i, "Choose")] for i in range(3)):
        # The arm planned (the sink), the other (the buffer), and the rack slot the
        # cup will be delivered to, which both arms reach in one move.
        assert {"sink.basin", "buffer.pad", f"rack.{'abc'[i]}"} <= set(entry["spots"])
    windows = sorted((e["start"], e["end"]) for e in kept.values())
    for (_s, end), (start, _e) in zip(windows, windows[1:], strict=False):
        assert end <= start  # the sink is kept by one cup at a time


def test_a_branch_decided_keeps_nothing():
    workflow, env, document = _example("inspect_then_wash")
    stated = {**document, "expansion": {"arms": [
        {"node": ["Each", i, "Choose"], "arm": "then"} for i in range(3)
    ]}}
    report = schedule(workflow, env, document_path=stated, random_seed=0)
    assert report.ok and not kinds(report.plan, "reservation")


@pytest.mark.parametrize("name", ["simple", "wash_or_polish", "storage"])
def test_a_plan_with_nothing_undecided_keeps_nothing(name):
    workflow, env, document = (
        _example(name) if (EXAMPLES / f"{name}.document.yaml").is_file()
        else (*_example_without_document(name), None)
    )
    report = schedule(workflow, env, document_path=document, random_seed=0)
    assert report.ok and not kinds(report.plan, "reservation")


def _example_without_document(name: str):
    load = lambda kind: yaml.safe_load(  # noqa: E731
        (EXAMPLES / f"{name}.{kind}.yaml").read_text(encoding="utf-8")
    )
    return load("workflow"), load("env")


# --- the document --------------------------------------------------------------------------


def test_a_reservation_entry_names_real_spots():
    document = {
        "time": {"unit": "second"},
        "activities": [
            {"kind": "reservation", "start": 0, "end": 5, "node": ["Clean"], "spots": []},
            {"kind": "reservation", "start": 0, "end": 5, "node": ["Clean"], "spots": ["sink"]},
        ],
    }
    codes = [d.code for d in validate_document(document).diagnostics]
    assert "wrong_type" in codes and "malformed_qualified_spot" in codes


# --- a job that has stopped -------------------------------------------------------------


def test_a_stopped_job_keeps_nothing_and_the_other_still_does():
    # Job a was stopped before its plate left the rack: its loop is going nowhere and
    # keeps nothing, and b's loop keeps what it keeps (design.md D66, stage 4c).
    jobs, env, roster = _two_rinses()
    first = schedule_jobs(jobs, env, document_path=roster, random_seed=0).plan
    into_a = next(
        a for a in first["activities"]
        if a.get("job") == "a" and a["kind"] == "transport" and a["from_spot"] == "rack.in"
    )
    document = copy.deepcopy(first)
    document["activities"] = [{**into_a, "status": "cancelled", "start": 0, "end": 0}]
    document["now"] = 0
    report = schedule_jobs(jobs, env, document_path=document, random_seed=0)
    assert report.plan is not None, [(d.code, d.message) for d in report.diagnostics]
    assert [entry["job"] for entry in kinds(report.plan, "reservation")] == ["b"]


# --- many regions ---------------------------------------------------------------------------


def _cups(n: int):
    """`inspect_then_wash` with `n` cups, each bound to a rack slot of its own."""
    workflow, env, document = _example("inspect_then_wash")
    env = copy.deepcopy(env)
    names = [f"s{i}" for i in range(n)]
    for device in env["devices"]:
        if device["id"] in ("tray", "rack"):
            device["spots"] = names
    env["transports"] = [
        t for t in env["transports"]
        if not (t["from"].startswith("tray.") or t["to"].startswith("rack."))
    ]
    for s in names:
        env["transports"].append(
            {"transporter": "arm", "from": f"tray.{s}", "to": "scope.stage", "duration": 2}
        )
        for src in ("sink.basin", "buffer.pad"):
            env["transports"].append(
                {"transporter": "arm", "from": src, "to": f"rack.{s}", "duration": 2}
            )
    document = {**document, "interface": {
        "inputs": {"cups": [f"tray.{s}" for s in names]},
        "outputs": {"cups": [f"rack.{s}" for s in names]},
    }}
    return workflow, env, document


def test_more_regions_than_candidates_a_region_offers_still_plan():
    # Twelve cups, each bound to its own slot: more slots than a region offers
    # candidates for. Each cup's exit is its own slot -- the only place what follows
    # can take it from -- so every one of them still has a candidate (measured: before
    # the exits were held to that, cups 8 to 11 had none and the plan was infeasible).
    workflow, env, document = _cups(12)
    report = schedule(workflow, env, document_path=document, random_seed=0, check_arms=False)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    kept = kinds(report.plan, "reservation")
    assert len(kept) == 12
    for entry in kept:
        slot = f"rack.s{entry['node'][1]}"
        assert slot in entry["spots"]


# --- work that overran into a window ---------------------------------------------------------

_INSPECT_PROCESSES = """\
  inspect:
    kind: atomic
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}, dirty: {type: Bool, phase: data}}
    objects: {map: {outputs.cup: inputs.cup}}
  wash:
    kind: atomic
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {map: {outputs.cup: inputs.cup}}
  polish:
    kind: atomic
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {map: {outputs.cup: inputs.cup}}
"""


def _cup_workflow(body: str, returns: str) -> dict:
    return yaml.safe_load(
        'spec_version: "0.5"\ntypes:\n  Cup: {domain: object}\nprocesses:\n'
        + _INSPECT_PROCESSES
        + "  main:\n    kind: composite\n    inputs: {cup: {type: Cup, phase: data}}\n"
        "    outputs: {cup: {type: Cup, phase: data}}\n    body:\n      nodes:\n"
        + body + f"      returns: {{cup: {{from: {returns}}}}}\nentry: main\n"
    )


_DECIDES = _cup_workflow(
    "        - {id: I, process: inspect, state: {cup: {from: inputs.cup}}}\n"
    "        - id: H\n          kind: branch\n          condition: {from: I.dirty}\n"
    "          args: {cup: {from: I.cup}}\n          then: {process: wash}\n"
    "          else: {process: polish}\n",
    "H.cup",
)
_POLISHES_FIRST = _cup_workflow(
    "        - {id: P, process: polish, state: {cup: {from: inputs.cup}}}\n"
    "        - {id: L, process: inspect, state: {cup: {from: P.cup}}}\n",
    "L.cup",
)
_PLACES = ["tray.a", "tray.b", "scope.stage", "sink.basin", "buffer.pad", "tub.bath",
           "rack.a", "rack.b"]
_CUPS_ENV = {
    "time": {"unit": "second"},
    "devices": [
        {"id": "tray", "spots": ["a", "b"]}, {"id": "scope", "spots": ["stage"]},
        {"id": "sink", "spots": ["basin"]}, {"id": "buffer", "spots": ["pad"]},
        {"id": "tub", "spots": ["bath"]}, {"id": "rack", "spots": ["a", "b"]},
    ],
    "transporters": [{"id": "arm"}],
    "transports": [
        {"transporter": "arm", "from": a, "to": b, "duration": 1}
        for a in _PLACES for b in _PLACES if a != b
    ],
    "processes": {
        name: {"modes": [{"devices": [device], "duration": duration,
                          "input_spots": {"cup": f"{device}.{spot}"},
                          "output_spots": {"cup": f"{device}.{spot}"}}]}
        for name, (device, spot, duration) in {
            "inspect": ("scope", "stage", 2), "wash": ("sink", "basin", 5),
            "polish": ("buffer", "pad", 3),
        }.items()
    },
}


def test_work_that_overran_onto_a_kept_spot_does_not_take_the_replan_away():
    # Cup b's polishing was planned to be over long before cup a is decided; it overran,
    # and is still on the buffer -- a spot a keeps, since a may yet be polished -- when
    # a's inspection ends. History is not the reservation's to move (design.md D66 F1):
    # the replan plans on, where holding the window against the running polish made it
    # infeasible.
    jobs = [JobInput("a", copy.deepcopy(_DECIDES)), JobInput("b", copy.deepcopy(_POLISHES_FIRST))]
    roster = {"activities": [], "jobs": [
        {"id": "a", "release": 0,
         "interface": {"inputs": {"cup": "tray.a"}, "outputs": {"cup": "rack.a"}}},
        {"id": "b", "release": 0,
         "interface": {"inputs": {"cup": "tray.b"}, "outputs": {"cup": "rack.b"}}},
    ]}
    plan = schedule_jobs(jobs, copy.deepcopy(_CUPS_ENV), document_path=roster, random_seed=0).plan
    (window,) = kinds(plan, "reservation")
    assert "buffer.pad" in window["spots"]
    polish = next(a for a in plan["activities"] if a.get("job") == "b" and a.get("node") == ["P"])
    assert polish["end"] <= window["start"]
    now = window["start"]
    document = copy.deepcopy(plan)
    history = []
    for entry in document["activities"]:
        if entry is polish or entry == polish:
            history.append({**entry, "status": "running"})
            continue
        moved_b_in = (
            entry["kind"] == "transport" and entry.get("job") == "b"
            and entry["end"] <= polish["start"]
        )
        done_by_a = (
            entry.get("job") == "a" and entry["kind"] in ("processing", "transport")
            and entry["end"] <= now
        )
        if moved_b_in or done_by_a:
            history.append({**entry, "status": "completed"})
    document["activities"] = history
    document["now"] = now
    report = schedule_jobs(jobs, copy.deepcopy(_CUPS_ENV), document_path=document,
                           random_seed=0, check_arms=False, running_task_margin=1)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
