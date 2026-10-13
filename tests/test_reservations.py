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


# --- an Object history left on a kept spot ---------------------------------------------------

# A dirty cup is washed and inspected again, and the second inspection decides between a
# second wash and a polish: a branch inside a branch's arm.
_WASHES_AGAIN = yaml.safe_load(
    'spec_version: "0.5"\ntypes:\n  Cup: {domain: object}\nprocesses:\n'
    + _INSPECT_PROCESSES
    + """  wash_check:
    kind: composite
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - {id: W, process: wash, state: {cup: {from: inputs.cup}}}
        - {id: J, process: inspect, state: {cup: {from: W.cup}}}
        - id: K
          kind: branch
          condition: {from: J.dirty}
          args: {cup: {from: J.cup}}
          then: {process: wash}
          else: {process: polish}
      returns: {cup: {from: K.cup}}
  main:
    kind: composite
    inputs: {cup: {type: Cup, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    body:
      nodes:
        - {id: I, process: inspect, state: {cup: {from: inputs.cup}}}
        - id: H
          kind: branch
          condition: {from: I.dirty}
          args: {cup: {from: I.cup}}
          then: {process: wash_check}
          else: {process: polish}
      returns: {cup: {from: H.cup}}
entry: main
"""
)


def _washes_again(document, **kwargs):
    jobs = [JobInput(j, copy.deepcopy(_WASHES_AGAIN)) for j in "ab"]
    return schedule_jobs(jobs, copy.deepcopy(_CUPS_ENV), document_path=document,
                         random_seed=0, check_arms=False, **kwargs)


def _stated(plan: dict, now: int, job: str, node: list) -> dict:
    """`plan` handed back at `now`, everything on time, with `job`'s branch at `node`
    stated `then` -- as a run does once the reading is in."""
    document = copy.deepcopy(plan)
    for entry in document["activities"]:
        if entry["kind"] in ("processing", "transport") and entry["start"] < now:
            entry["status"] = "completed" if entry["end"] <= now else "running"
    document["now"] = now
    held = next(j for j in document["jobs"] if j["id"] == job)
    held.setdefault("expansion", {}).setdefault("arms", []).append({"node": node, "arm": "then"})
    held.pop("bound", None)
    held.pop("fingerprint", None)
    return document


def test_an_object_history_left_on_a_kept_spot_waits_there_through_its_gates(monkeypatch):
    # One cup has been decided twice, `then` both times, and its second wash runs late on
    # the sink. The other cup's inspection runs late too, so its branch is still open --
    # and keeps the sink, since it may yet be washed. The first cup is still on the sink,
    # and leaves it through the two gates its branches hand it on by, both on the sink:
    # that stay is history's (design.md D66 F1, D67 hole 6), not new work inside the
    # window. Holding the window against those gates made the replan infeasible.
    roster = {"activities": [], "jobs": [
        {"id": j, "release": 0,
         "interface": {"inputs": {"cup": f"tray.{j}"}, "outputs": {"cup": f"rack.{j}"}}}
        for j in "ab"
    ]}
    plan = _washes_again(roster).plan
    first = min(kinds(plan, "decision"), key=lambda a: a["start"])
    job = first["job"]
    other = "b" if job == "a" else "a"
    plan = _washes_again(_stated(plan, first["start"], job, ["H"])).plan
    inner = next(a for a in kinds(plan, "decision") if a.get("job") == job)
    assert inner["node"] == ["H", "K"]
    plan = _washes_again(_stated(plan, inner["start"], job, ["H", "K"])).plan

    def of(who, node):
        return next(a for a in kinds(plan, "processing")
                    if a.get("job") == who and a["node"] == node)

    wash, look = of(job, ["H", "K"]), of(other, ["I"])
    assert wash["end"] < look["end"] and "sink.basin" in wash["input_spots"].values()
    now = look["end"] + 2
    document = copy.deepcopy(plan)
    for entry in document["activities"]:
        if entry["kind"] not in ("processing", "transport") or entry["start"] >= now:
            continue
        if entry.get("job") == job and entry["start"] >= wash["end"]:
            continue  # after the late wash: not started
        if entry.get("job") == other and entry["start"] >= look["end"]:
            continue  # after the late inspection: not started
        late = entry in (wash, look)
        entry["status"] = "running" if late or entry["end"] > now else "completed"
    document["now"] = now

    report = _washes_again(copy.deepcopy(document), running_task_margin=1)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    kept = [a for a in kinds(report.plan, "reservation") if a.get("job") == other]
    assert any("sink.basin" in a["spots"] for a in kept)

    # The constructed fallback, read back against the same rule, plans it too.
    from ofplang.schedule.scheduler import api
    from ofplang.schedule.scheduler.result import Solution

    monkeypatch.setattr(api, "_solver", lambda: lambda *a, **k: Solution("unknown", None, (), ()))
    constructed = _washes_again(copy.deepcopy(document), running_task_margin=1)
    assert constructed.ok, [(d.code, d.message) for d in constructed.diagnostics]
    assert "plan_constructed_after_solver" in [d.code for d in constructed.diagnostics]


# --- a branch inside a loop, decided before the loop is -------------------------------------

_PLATE_STEPS = {
    name: {
        "kind": "atomic",
        "inputs": {"plate": {"type": "Plate", "phase": "data"}},
        "outputs": {"plate": {"type": "Plate", "phase": "data"},
                    **({flag: {"type": "Bool", "phase": "data"}} if flag else {})},
        "objects": {"map": {"outputs.plate": "inputs.plate"}},
    }
    for name, flag in (("rinse", None), ("soak", None), ("look", "stained"),
                       ("check", "dirty"), ("dry", None))
}
# Each invocation: rinse, look for stains, soak a stained plate, then check whether it is
# still dirty -- the loop's own condition, read only after the branch's.
_LOOKS_THEN_CHECKS = {
    "spec_version": "0.5",
    "types": {"Plate": {"domain": "object"}},
    "processes": {
        **_PLATE_STEPS,
        "once": {
            "kind": "composite",
            "inputs": {"plate": {"type": "Plate", "phase": "data"}},
            "outputs": {"plate": {"type": "Plate", "phase": "data"},
                        "dirty": {"type": "Bool", "phase": "data"}},
            "body": {"nodes": [
                {"id": "Rinse", "process": "rinse", "state": {"plate": {"from": "inputs.plate"}}},
                {"id": "Look", "process": "look", "state": {"plate": {"from": "Rinse.plate"}}},
                {"id": "Soak", "kind": "branch", "condition": {"from": "Look.stained"},
                 "args": {"plate": {"from": "Look.plate"}}, "then": {"process": "soak"}},
                {"id": "Check", "process": "check", "state": {"plate": {"from": "Soak.plate"}}},
            ], "returns": {"plate": {"from": "Check.plate"}, "dirty": {"from": "Check.dirty"}}},
        },
        "main": {
            "kind": "composite",
            "inputs": {"plate": {"type": "Plate", "phase": "data"}},
            "outputs": {"plate": {"type": "Plate", "phase": "data"}},
            "body": {"nodes": [
                {"id": "Clean", "kind": "do_while", "process": "once",
                 "carry": {"plate": {"from": "inputs.plate"}},
                 "condition": {"output": "dirty"}, "max_iterations": {"value": 4}},
                {"id": "Dry", "process": "dry", "state": {"plate": {"from": "Clean.plate"}}},
            ], "returns": {"plate": {"from": "Dry.plate"}}},
        },
    },
    "entry": "main",
}
# Another plate is only soaked and checked.
_SOAKS = {
    "spec_version": "0.5",
    "types": {"Plate": {"domain": "object"}},
    "processes": {
        "soak": _PLATE_STEPS["soak"], "check": _PLATE_STEPS["check"],
        "main": {
            "kind": "composite",
            "inputs": {"plate": {"type": "Plate", "phase": "data"}},
            "outputs": {"plate": {"type": "Plate", "phase": "data"}},
            "body": {"nodes": [
                {"id": "S", "process": "soak", "state": {"plate": {"from": "inputs.plate"}}},
                {"id": "C", "process": "check", "state": {"plate": {"from": "S.plate"}}},
            ], "returns": {"plate": {"from": "C.plate"}}},
        },
    },
    "entry": "main",
}


def _plate_mode(device: str, spot: str, duration: int) -> dict:
    return {"modes": [{"devices": [device], "duration": duration,
                       "input_spots": {"plate": f"{device}.{spot}"},
                       "output_spots": {"plate": f"{device}.{spot}"}}]}


_PLATES_ENV = {
    "time": {"unit": "second"},
    "devices": [{"id": "rack", "spots": ["in", "out", "in2", "out2"]},
                {"id": "sink", "spots": ["basin"]}, {"id": "reader", "spots": ["slot"]},
                {"id": "tub", "spots": ["bath"]}, {"id": "oven", "spots": ["shelf"]}],
    "transporters": [{"id": "arm"}],
    "transports": [
        {"transporter": "arm", "from": a, "to": b, "duration": 1} for a, b in (
            ("rack.in", "sink.basin"), ("sink.basin", "reader.slot"),
            ("reader.slot", "sink.basin"), ("reader.slot", "tub.bath"),
            ("tub.bath", "reader.slot"), ("reader.slot", "oven.shelf"),
            ("oven.shelf", "rack.out"), ("rack.in2", "tub.bath"), ("reader.slot", "rack.out2"),
        )
    ],
    "processes": {"rinse": _plate_mode("sink", "basin", 4), "soak": _plate_mode("tub", "bath", 3),
                  "look": _plate_mode("reader", "slot", 2),
                  "check": _plate_mode("reader", "slot", 6),
                  "dry": _plate_mode("oven", "shelf", 4)},
}


def _plates(document, **kwargs):
    jobs = [JobInput("a", copy.deepcopy(_LOOKS_THEN_CHECKS)), JobInput("b", copy.deepcopy(_SOAKS))]
    return schedule_jobs(jobs, copy.deepcopy(_PLATES_ENV), document_path=document,
                         random_seed=0, check_arms=False, **kwargs)


def _at(plan: dict, now: int, **stated) -> dict:
    """`plan` handed back at `now`, everything on time, with job a's `stated` added."""
    document = copy.deepcopy(plan)
    for entry in document["activities"]:
        if entry["kind"] in ("processing", "transport") and entry["start"] < now:
            entry["status"] = "completed" if entry["end"] <= now else "running"
    document["now"] = now
    a = next(j for j in document["jobs"] if j["id"] == "a")
    expansion = a.setdefault("expansion", {})
    for key, entries in stated.items():
        expansion[key] = [*expansion.get(key, []), *entries]
    a.pop("bound", None)
    a.pop("fingerprint", None)
    return document


def test_a_loop_keeps_what_its_next_invocation_may_need_once_a_branch_inside_is_decided(
    monkeypatch,
):
    # The first invocation's plate is not stained (the branch: else) but still dirty (the
    # loop: goes on). Between the two readings the loop used to keep only what that
    # invocation did -- no tub -- so the other plate went into the tub and waited there for
    # the reader the loop keeps; when the loop went on, its next invocation, planned on a
    # soak, needed the tub, and the replan was infeasible. The next invocation decides the
    # branch again, so the loop keeps one of the branch's candidates throughout (design.md
    # D67, hole 7).
    roster = {"activities": [], "jobs": [
        {"id": "a", "release": 0,
         "interface": {"inputs": {"plate": "rack.in"}, "outputs": {"plate": "rack.out"}}},
        {"id": "b", "release": 9,
         "interface": {"inputs": {"plate": "rack.in2"}, "outputs": {"plate": "rack.out2"}}},
    ]}
    plan = _plates(roster).plan

    def decision(plan, node):
        return next(x for x in kinds(plan, "decision") if x.get("job") == "a" and x["node"] == node)

    inner = decision(plan, ["Clean", 0, "Soak"])
    unstained = [{"node": ["Clean", 0, "Soak"], "arm": "else"}]
    report = _plates(_at(plan, inner["start"], arms=unstained))
    assert report.ok, [d.code for d in report.diagnostics]
    plan = report.plan
    (loop,) = [x for x in kinds(plan, "reservation") if x["node"] == ["Clean"]]
    assert "tub.bath" in loop["spots"]
    outer = decision(plan, ["Clean"])
    document = _at(plan, outer["start"], loops=[{"node": ["Clean"], "count": 2, "ended": False}])
    report = _plates(copy.deepcopy(document), running_task_margin=1)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]

    from ofplang.schedule.scheduler import api
    from ofplang.schedule.scheduler.result import Solution

    monkeypatch.setattr(api, "_solver", lambda: lambda *a, **k: Solution("unknown", None, (), ()))
    replayed = _plates(_at(plan, outer["start"] - 1), running_task_margin=1)
    assert replayed.ok, [(d.code, d.message) for d in replayed.diagnostics]


# --- a loop that draws on a stock ------------------------------------------------------------


def test_a_loop_drawing_on_a_stock_warns_that_later_invocations_are_unchecked():
    # Each rinse draws water from the sink. The stock is checked for one rinse more than
    # planned, not up to max_iterations (design.md D66, C5/D4): said, not refused.
    workflow, env, document = _example("rinse_until_clean")
    env = copy.deepcopy(env)
    for device in env["devices"]:
        if device["id"] == "sink":
            device["resources"] = {"water": {"capacity": 10}}
    for mode in env["processes"]["rinse"]["modes"]:
        mode["consumption"] = {"sink.water": 1}
    document = {**document, "inventories": {"levels": {"sink": {"water": 10}}}}
    report = schedule(workflow, env, document_path=document, random_seed=0)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    (said,) = [d for d in report.diagnostics if d.code == "loop_stock_unchecked"]
    assert said.severity == "warning" and "Clean" in said.message


def test_a_loop_drawing_on_nothing_says_nothing():
    workflow, env, document = _example("rinse_until_clean")
    report = schedule(workflow, env, document_path=document, random_seed=0)
    assert "loop_stock_unchecked" not in [d.code for d in report.diagnostics]
