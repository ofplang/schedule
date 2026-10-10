"""A `do_while` (design.md D65).

Invocation i of a loop L is at [L, i]. How many there are is what the conditions
decide so far: a literal one here, one the run states in `expansion.loops`, and -- for
a condition settled only during the run -- the loop is planned to end at the
invocation it is in, growing by one each time the run says it goes on. Nothing of the
next invocation, and nothing the loop hands on, starts before that condition is
settled. `exhausted` is the node's own value, at [L].exhausted.

Every base workflow here is valid v0 (checked against ofplang-validate below).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from ofplang.validate import validate

from ofplang.schedule import schedule, validate_document
from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.api import ASSUMED_ARM, _arm_expansions
from ofplang.schedule.scheduler.model import SourceLiteral, SourceRef
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load(kind: str):
    return yaml.safe_load(
        (EXAMPLES / f"rinse_until_clean.{kind}.yaml").read_text(encoding="utf-8")
    )


RINSE = _load("workflow")
ENV = _load("env")
DOCUMENT = _load("document")
INTERFACE = DOCUMENT["interface"]


def _parse(workflow, expansion=None, interface=INTERFACE, assume=ASSUMED_ARM):
    wf, diags = parse_workflow(copy.deepcopy(workflow), interface=interface,
                               expansion=expansion, assume=assume)
    return wf, [d.code for d in diags.items if d.severity == ERROR]


def loops(*entries) -> dict:
    return {"loops": [{"node": list(n), "count": c, "ended": e} for n, c, e in entries]}


def _with(workflow, *, condition=None, limit=None, carry=None, target=None):
    """RINSE with its loop node changed."""
    doc = copy.deepcopy(workflow)
    node = doc["processes"]["main"]["body"]["nodes"][0]
    if condition is not None:
        node["condition"] = condition
    if limit is not None:
        node["max_iterations"] = limit
    if carry is not None:
        node["carry"] = carry
    if target is not None:
        node["process"] = target
    return doc


# The same rinse, its loop's condition carried in from the boundary (`soiled`) and
# handed back untouched by each invocation: settled before the run, by the run.
PASSED_ON = copy.deepcopy(RINSE)
PASSED_ON["processes"]["rinse_only"] = {
    "kind": "composite",
    "inputs": {"plate": {"type": "Plate", "phase": "data"},
               "dirty": {"type": "Bool", "phase": "run"}},
    "outputs": {"plate": {"type": "Plate", "phase": "data"},
                "dirty": {"type": "Bool", "phase": "run"}},
    "body": {"nodes": [{"id": "Rinse", "process": "rinse",
                        "state": {"plate": {"from": "inputs.plate"}}}],
             "returns": {"plate": {"from": "Rinse.plate"}, "dirty": {"from": "inputs.dirty"}}},
}
PASSED_ON["processes"]["main"]["inputs"]["soiled"] = {"type": "Bool", "phase": "run"}
PASSED_ON = _with(PASSED_ON, target="rinse_only",
                  carry={"plate": {"from": "inputs.plate"}, "dirty": {"from": "inputs.soiled"}})


def test_the_fixtures_are_valid_v0():
    for workflow in (RINSE, PASSED_ON, NESTED, BRANCH_IN_LOOP, LOOP_IN_BRANCH, COLLECTED):
        result = validate(copy.deepcopy(workflow))
        assert result.ok, [(d.code, d.message) for d in result.diagnostics]


# --- the reading -------------------------------------------------------------------------


def test_a_measured_loop_is_planned_to_end_where_it_is():
    wf, errs = _parse(RINSE)
    assert errs == []
    loop = wf.loops[("Clean",)]
    assert (loop.count, loop.ended, loop.assumed) == (1, False, True)
    assert loop.waits == (("Clean", 0, "Read"),)
    assert loop.condition == SourceRef(("Clean", 0, "Read"), "dirty")
    assert loop.limit == SourceLiteral(5)
    # What follows the loop waits for the reading, and so does the plate's move to it.
    assert (("Clean", 0, "Read"), ("Dry",)) in wf.precedence
    gated = [arc for arc in wf.arcs if arc.dst.node == ("Dry",)]
    assert [wf.arc_gates.get(arc) for arc in gated] == [frozenset({("Clean", 0, "Read")})]
    # `exhausted` is the node's own value, waited for the same way.
    assert wf.output_sources["gave_up"] == SourceRef(("Clean",), "exhausted")


def test_each_invocation_waits_for_the_last_reading():
    wf, errs = _parse(RINSE, loops((("Clean",), 3, False)))
    assert errs == []
    assert wf.loops[("Clean",)].count == 3 and wf.loops[("Clean",)].assumed
    for i in (1, 2):
        before = ("Clean", i - 1, "Read")
        assert (before, ("Clean", i, "Rinse")) in wf.precedence
        into = [arc for arc in wf.arcs if arc.dst.node == ("Clean", i, "Rinse")]
        assert [wf.arc_gates.get(arc) for arc in into] == [frozenset({before})]


def test_a_loop_stated_ended_is_not_assumed():
    wf, errs = _parse(RINSE, loops((("Clean",), 2, True)))
    assert errs == []
    loop = wf.loops[("Clean",)]
    assert (loop.count, loop.ended, loop.assumed) == (2, True, False)


@pytest.mark.parametrize("stated", [
    (("Clean",), 6, True),    # more than max_iterations
    (("Clean",), 5, False),   # at the limit, and not ended
])
def test_a_count_the_loop_cannot_have_is_refused(stated):
    assert _parse(RINSE, loops(stated))[1] == ["loop_count_mismatch"]


def test_a_loop_that_is_not_there_is_named():
    assert _parse(RINSE, loops((("Dry",), 1, True)))[1] == ["loop_unknown_node"]
    assert _parse(RINSE, loops((("Nope",), 1, True)))[1] == ["loop_unknown_node"]


def test_without_an_arm_to_assume_a_measured_loop_is_refused():
    wf, errs = _parse(RINSE, assume=None)
    assert errs == ["loop_count_unknown"]


def test_the_document_states_each_loop_once():
    def codes(expansion):
        result = validate_document({"expansion": expansion, "activities": []})
        return [d.code for d in result.diagnostics]

    assert codes(loops((("Clean",), 2, False))) == []
    assert codes(loops((("Clean",), 2, False), (("Clean",), 3, True))) == ["duplicate_loop"]
    assert codes({"loops": [{"node": ["Clean"], "count": 0, "ended": True}]}) == ["wrong_type"]


# --- literal and boundary conditions -----------------------------------------------------


def test_a_literal_condition_decides_here():
    # The flag each invocation hands back is the one it was given: a literal here.
    always = copy.deepcopy(PASSED_ON)
    always["processes"]["main"]["body"]["nodes"][0]["carry"]["dirty"] = {"value": True}
    wf, errs = _parse(always)
    assert errs == [] and wf.loops[("Clean",)].count == 5 and wf.loops[("Clean",)].ended
    assert not wf.loops[("Clean",)].waits  # nothing to wait for
    never = copy.deepcopy(always)
    never["processes"]["main"]["body"]["nodes"][0]["carry"]["dirty"] = {"value": False}
    wf, errs = _parse(never)
    assert errs == [] and wf.loops[("Clean",)].count == 1 and wf.loops[("Clean",)].ended
    # A literal `true` up to a bound the scheduler is not given: the run states it.
    unbounded = _with(always, limit={"from": "inputs.rounds"})
    unbounded["processes"]["main"]["inputs"]["rounds"] = {"type": "Int", "phase": "run"}
    wf, errs = _parse(unbounded)
    assert errs == ["loop_count_unknown"]
    assert wf.undecided_loops[("Clean",)] == (
        1, SourceLiteral(True), SourceRef((), "rounds")
    )
    assert _parse(unbounded, loops((("Clean",), 4, True)))[1] == []


def test_a_condition_from_the_boundary_is_the_runs_to_state():
    wf, errs = _parse(PASSED_ON)
    assert errs == ["loop_count_unknown"]
    assert wf.undecided_loops[("Clean",)] == (1, SourceRef((), "soiled"), SourceLiteral(5))
    wf, errs = _parse(PASSED_ON, loops((("Clean",), 3, True)))
    assert errs == [] and wf.loops[("Clean",)].count == 3


# --- shapes ------------------------------------------------------------------------------

# A loop inside a loop: each rinse is several dips, as many as the reader asks for.
NESTED = copy.deepcopy(RINSE)
NESTED["processes"]["dip_until_wet"] = {
    "kind": "composite",
    "inputs": {"plate": {"type": "Plate", "phase": "data"}},
    "outputs": {"plate": {"type": "Plate", "phase": "data"},
                "dirty": {"type": "Bool", "phase": "data"}},
    "body": {
        "nodes": [{"id": "Dip", "kind": "do_while", "process": "rinse_and_read",
                   "carry": {"plate": {"from": "inputs.plate"}},
                   "condition": {"output": "dirty"}, "max_iterations": {"value": 3}},
                  {"id": "Check", "process": "read", "state": {"plate": {"from": "Dip.plate"}}}],
        "returns": {"plate": {"from": "Check.plate"}, "dirty": {"from": "Check.dirty"}},
    },
}
NESTED = _with(NESTED, target="dip_until_wet")


def test_a_loop_inside_a_loop_grows_on_its_own():
    wf, errs = _parse(NESTED, loops((("Clean",), 2, False), (("Clean", 0, "Dip"), 2, True)))
    assert errs == []
    assert wf.loops[("Clean", 0, "Dip")].count == 2
    assert wf.loops[("Clean", 1, "Dip")].count == 1  # not stated: assumed to end
    # The inner loop of an invocation the outer one has not made is not an error.
    assert _parse(NESTED, loops((("Clean", 4, "Dip"), 1, True)))[1] == []
    # The outer one's next invocation waits for its last check, not for the inner loop.
    assert (("Clean", 0, "Check"), ("Clean", 1, "Dip", 0, "Rinse")) in wf.precedence


# A branch inside each invocation: a dirty plate is soaked before the next rinse.
BRANCH_IN_LOOP = copy.deepcopy(RINSE)
BRANCH_IN_LOOP["processes"]["soak"] = copy.deepcopy(RINSE["processes"]["rinse"])
BRANCH_IN_LOOP["processes"]["rinse_read_soak"] = {
    "kind": "composite",
    "inputs": {"plate": {"type": "Plate", "phase": "data"}},
    "outputs": {"plate": {"type": "Plate", "phase": "data"},
                "dirty": {"type": "Bool", "phase": "data"}},
    "body": {
        "nodes": [{"id": "Rinse", "process": "rinse", "state": {"plate": {"from": "inputs.plate"}}},
                  {"id": "Read", "process": "read", "state": {"plate": {"from": "Rinse.plate"}}},
                  {"id": "Soak", "kind": "branch", "condition": {"from": "Read.dirty"},
                   "args": {"plate": {"from": "Read.plate"}}, "then": {"process": "soak"}}],
        "returns": {"plate": {"from": "Soak.plate"}, "dirty": {"from": "Read.dirty"}},
    },
}
BRANCH_IN_LOOP = _with(BRANCH_IN_LOOP, target="rinse_read_soak")


def test_a_branch_inside_an_invocation_waits_for_its_own_reading():
    wf, errs = _parse(BRANCH_IN_LOOP, loops((("Clean",), 2, False)))
    assert errs == []
    assert set(wf.branch_gates) == {("Clean", 0, "Soak"), ("Clean", 1, "Soak")}
    assert wf.branch_gates[("Clean", 1, "Soak")].waits == (("Clean", 1, "Read"),)


# A loop inside a branch's arm: only a plate the first reading finds dirty is rinsed.
LOOP_IN_BRANCH = copy.deepcopy(RINSE)
LOOP_IN_BRANCH["processes"]["clean_it"] = {
    "kind": "composite",
    "inputs": {"plate": {"type": "Plate", "phase": "data"}},
    "outputs": {"plate": {"type": "Plate", "phase": "data"}},
    "body": {
        "nodes": [{"id": "Again", "kind": "do_while", "process": "rinse_and_read",
                   "carry": {"plate": {"from": "inputs.plate"}},
                   "condition": {"output": "dirty"}, "max_iterations": {"value": 5}}],
        "returns": {"plate": {"from": "Again.plate"}},
    },
}
LOOP_IN_BRANCH["processes"]["main"]["body"] = {
    "nodes": [{"id": "First", "process": "read", "state": {"plate": {"from": "inputs.plate"}}},
              {"id": "Maybe", "kind": "branch", "condition": {"from": "First.dirty"},
               "args": {"plate": {"from": "First.plate"}}, "then": {"process": "clean_it"}},
              {"id": "Dry", "process": "dry", "state": {"plate": {"from": "Maybe.plate"}}}],
    "returns": {"plate": {"from": "Dry.plate"}, "gave_up": {"value": False}},
}


def test_a_loop_inside_an_arm_is_expanded_with_the_arm():
    wf, errs = _parse(LOOP_IN_BRANCH)
    assert errs == []
    assert set(wf.loops) == {("Maybe", "Again")}
    # Stated for the arm not taken, it is not an error: it does not occur.
    assert _parse(LOOP_IN_BRANCH, {**arms_of("Maybe", "else"),
                                   **loops((("Maybe", "Again"), 2, True))})[1] == []


def arms_of(node, arm):
    return {"arms": [{"node": [node], "arm": arm}]}


# Every reading collected, and each one logged by a map over the list.
COLLECTED = copy.deepcopy(RINSE)
COLLECTED["processes"]["log"] = {
    "kind": "atomic", "inputs": {"reading": {"type": "Bool", "phase": "data"}},
    "outputs": {"line": {"type": "Bool", "phase": "data"}},
}
COLLECTED["processes"]["main"]["outputs"]["entries"] = {"type": "Array<Bool>", "phase": "data"}
COLLECTED["processes"]["main"]["body"]["nodes"][0]["outputs"] = {
    "plate": {"mode": "carry"}, "dirty": {"mode": "collect"},
}
COLLECTED["processes"]["main"]["body"]["nodes"].append(
    {"id": "Log", "kind": "map", "process": "log", "each": {"reading": {"from": "Clean.dirty"}}}
)
COLLECTED["processes"]["main"]["body"]["returns"]["entries"] = {"from": "Log.line"}
COLLECTED_ENV = copy.deepcopy(ENV)
COLLECTED_ENV["devices"].append({"id": "logger", "spots": []})
COLLECTED_ENV["processes"]["log"] = {"modes": [{"devices": ["logger"], "duration": 1}]}


def test_a_collected_output_is_as_long_as_the_loop_and_waits_for_it():
    wf, errs = _parse(COLLECTED, loops((("Clean",), 3, False)))
    assert errs == []
    assert wf.iterations[("Log",)] == 3
    for i in range(3):
        assert (("Clean", 2, "Read"), ("Log", i)) in wf.precedence


# --- the plan --------------------------------------------------------------------------


@pytest.mark.parametrize("planner", ["cpsat", "greedy"])
def test_nothing_after_an_invocation_starts_before_its_reading(planner):
    report = schedule(copy.deepcopy(RINSE), copy.deepcopy(ENV),
                      document_path={**DOCUMENT, **{"expansion": loops((("Clean",), 2, False))}},
                      planner=planner, random_seed=0)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    acts = report.plan["activities"]

    def end_of(node):
        return next(a["end"] for a in acts if a["kind"] == "processing" and a["node"] == node)

    first, last = end_of(["Clean", 0, "Read"]), end_of(["Clean", 1, "Read"])
    for a in acts:
        arc_to = (a.get("arc") or {}).get("to", {})
        target = a.get("node") if a["kind"] == "processing" else arc_to.get("node")
        if target and target[:2] == ["Clean", 1]:
            assert a["start"] >= first, a
        if target == ["Dry"]:
            assert a["start"] >= last, a
    (decision,) = [a for a in acts if a["kind"] == "decision"]
    assert decision["node"] == ["Clean"] and decision["iteration"] == 1
    assert decision["arm"] == "exit" and decision["assumed"] is True
    assert decision["start"] == last
    assert validate_document(report.plan).ok


def test_the_plan_replans_as_it_was():
    first = schedule(copy.deepcopy(RINSE), copy.deepcopy(ENV),
                     document_path=copy.deepcopy(DOCUMENT))
    again = schedule(copy.deepcopy(RINSE), copy.deepcopy(ENV), document_path=first.plan)
    assert first.ok and again.ok and again.makespan == first.makespan


def test_once_the_reading_is_in_the_decision_is_gone():
    plan = schedule(copy.deepcopy(RINSE), copy.deepcopy(ENV), document_path=copy.deepcopy(DOCUMENT),
                    random_seed=0).plan
    done = [{**a, "status": "completed"} for a in plan["activities"]
            if a["kind"] in ("processing", "transport") and a["end"] <= 10]
    status = {**DOCUMENT, "now": 10, "activities": done,
              "expansion": loops((("Clean",), 2, False))}
    report = schedule(copy.deepcopy(RINSE), copy.deepcopy(ENV), document_path=status)
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]
    assert [a["iteration"] for a in report.plan["activities"] if a["kind"] == "decision"] == [1]


# --- the arm not planned ----------------------------------------------------------------


def test_going_on_is_checked_one_invocation_more():
    wf, _ = _parse(RINSE)
    found = _arm_expansions(RINSE, INTERFACE, None, wf)
    assert [chain for chain, _read, _errs in found] == [
        ("loop 'Clean' going on to invocation 2",),
    ]
    stuck = copy.deepcopy(ENV)
    stuck["transports"] = [t for t in stuck["transports"]
                           if not (t["from"] == "reader.slot" and t["to"] == "sink.basin")]
    report = schedule(copy.deepcopy(RINSE), stuck, document_path=copy.deepcopy(DOCUMENT))
    (found_one,) = [d.message for d in report.diagnostics if d.code == "arm_unplannable"]
    assert found_one.startswith("loop 'Clean' going on to invocation 2 could not be planned")
    assert schedule(copy.deepcopy(RINSE), stuck, document_path=copy.deepcopy(DOCUMENT),
                    check_arms=False).ok


def test_a_loop_inside_the_arm_planned_is_checked_going_on():
    wf, _errs = _parse(LOOP_IN_BRANCH)
    found = _arm_expansions(LOOP_IN_BRANCH, INTERFACE, None, wf)
    assert [chain for chain, _read, errs in found if not errs] == [
        ("branch 'Maybe' on its else arm",),
        ("loop 'Maybe/Again' going on to invocation 2",),
    ]


# A clean plate is rinsed as often as the boundary's `soiled` flag keeps saying: a
# loop whose every decision is the boundary's, on the arm the plan does not take.
LOOP_ON_ELSE = copy.deepcopy(PASSED_ON)
LOOP_ON_ELSE["processes"]["rinse_soiled"] = {
    "kind": "composite",
    "inputs": {"plate": {"type": "Plate", "phase": "data"},
               "soiled": {"type": "Bool", "phase": "run"}},
    "outputs": {"plate": {"type": "Plate", "phase": "data"}},
    "body": {
        "nodes": [{"id": "Again", "kind": "do_while", "process": "rinse_only",
                   "carry": {"plate": {"from": "inputs.plate"}, "dirty": {"from": "inputs.soiled"}},
                   "condition": {"output": "dirty"}, "max_iterations": {"value": 4}}],
        "returns": {"plate": {"from": "Again.plate"}},
    },
}
LOOP_ON_ELSE["processes"]["dry_soiled"] = {
    "kind": "composite",
    "inputs": {"plate": {"type": "Plate", "phase": "data"},
               "soiled": {"type": "Bool", "phase": "run"}},
    "outputs": {"plate": {"type": "Plate", "phase": "data"}},
    "body": {"nodes": [{"id": "D", "process": "dry", "state": {"plate": {"from": "inputs.plate"}}}],
             "returns": {"plate": {"from": "D.plate"}}},
}
LOOP_ON_ELSE["processes"]["main"]["body"] = {
    "nodes": [{"id": "First", "process": "read", "state": {"plate": {"from": "inputs.plate"}}},
              {"id": "Maybe", "kind": "branch", "condition": {"from": "First.dirty"},
               "args": {"plate": {"from": "First.plate"}, "soiled": {"from": "inputs.soiled"}},
               "then": {"process": "dry_soiled"}, "else": {"process": "rinse_soiled"}}],
    "returns": {"plate": {"from": "Maybe.plate"}, "gave_up": {"value": False}},
}


def test_a_loop_the_boundary_decides_on_the_other_arm_is_checked_both_ways():
    # Only the run knows `soiled`: the check reads the loop ending after one rinse,
    # then going on to a second -- it does not refuse it as undecided.
    assert validate(copy.deepcopy(LOOP_ON_ELSE)).ok
    wf, errs = _parse(LOOP_ON_ELSE)
    assert errs == []
    found = _arm_expansions(LOOP_ON_ELSE, INTERFACE, None, wf)
    assert [(chain, errs) for chain, _read, errs in found] == [
        (("branch 'Maybe' on its else arm",), []),
        (("branch 'Maybe' on its else arm", "loop 'Maybe/Again' going on to invocation 2"), []),
    ]
    env = copy.deepcopy(ENV)  # the plate is read first, and may leave from the sink
    env["transports"] += [
        {"transporter": "arm", "from": "rack.in", "to": "reader.slot", "duration": 2},
        {"transporter": "arm", "from": "sink.basin", "to": "rack.out", "duration": 2},
    ]
    report = schedule(copy.deepcopy(LOOP_ON_ELSE), env, document_path=copy.deepcopy(DOCUMENT))
    assert report.ok, [(d.code, d.message) for d in report.diagnostics]


# --- what a condition waits for (the D64 hole) -------------------------------------------


def test_a_condition_handed_on_by_an_undecided_branch_waits_for_it():
    # The sample's verdict passes through a branch the first reading decides; a second
    # branch on that verdict cannot be decided before the first one is.
    doc = copy.deepcopy(LOOP_IN_BRANCH)
    doc["processes"]["main"]["body"]["nodes"].append(
        {"id": "Then", "kind": "branch", "condition": {"from": "Clean2.exhausted"},
         "args": {"plate": {"from": "Dry.plate"}}, "then": {"process": "dry"}}
    )
    doc["processes"]["main"]["body"]["nodes"].insert(
        0, {"id": "Clean2", "kind": "do_while", "process": "rinse_and_read",
            "carry": {"plate": {"from": "inputs.plate"}},
            "condition": {"output": "dirty"}, "max_iterations": {"value": 2}},
    )
    doc["processes"]["main"]["body"]["nodes"][1]["state"] = {"plate": {"from": "Clean2.plate"}}
    doc["processes"]["main"]["body"]["returns"]["plate"] = {"from": "Then.plate"}
    assert validate(copy.deepcopy(doc)).ok
    wf, errs = _parse(doc)
    assert errs == []
    gate = wf.branch_gates[("Then",)]
    assert gate.condition == SourceRef(("Clean2",), "exhausted")
    assert gate.waits == (("Clean2", 0, "Read"),)
    assert gate.after == (("Clean2",),)  # not before the loop has ended
