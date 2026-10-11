"""Gates of the regions decided only during the run (design.md D66, stage 1).

A branch or `do_while` whose condition is produced during the run hands everything
on through gate nodes of its own: an Object through a holding gate it is moved into by
at most one move, a Pure Data value through a gate that holds nothing. Both start after
the decision, so whatever reads past them does too -- read off the structure, never off
marks carried on values. This file pins what that adds to the plan and the document:
the gate's node path and its grammar, `kind: gate`, the shape staying put for regions
decided before the run, and replanning once the history has passed a gate.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ofplang.schedule import schedule, validate_document
from ofplang.schedule.core.identifiers import node_path_problem
from ofplang.schedule.scheduler.api import ASSUMED_ARM
from ofplang.schedule.scheduler.workflow import parse_workflow
from tests.schedutil import kinds

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _example(name: str):
    def load(kind: str):
        return yaml.safe_load((EXAMPLES / f"{name}.{kind}.yaml").read_text(encoding="utf-8"))

    return load("workflow"), load("env"), load("document")


def _plan(name: str, **kwargs) -> dict:
    workflow, env, document = _example(name)
    report = schedule(workflow, env, document_path=document, random_seed=0, **kwargs)
    assert report.plan is not None, [(d.code, d.message) for d in report.diagnostics]
    return report.plan


# --- the node path ---------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    ["H", "@out", "cup"],
    ["Clean", 0, "@out", "plate"],
    ["Clean", "@out", "exhausted"],
    ["H", "@out", "plates", 1],
    ["Tray", 2, "Handle", "Wash", "@out", "cup", 0, 3],
])
def test_a_gate_path_is_a_node_path(path):
    assert node_path_problem(path) is None


@pytest.mark.parametrize("path", [
    ["@out", "cup"],              # no region before it
    ["H", "@out"],                # no port after it
    ["H", "@out", 3],             # an index where the port goes
    ["H", "@out", "cup", "x"],    # a name after the port
    ["H", "@out", "cup", 0, "y"],
    ["H", "out", "cup", "@out"],  # nothing after it at all
])
def test_anything_else_after_a_gate_mark_is_not(path):
    assert node_path_problem(path) is not None


def test_a_gate_entry_has_to_name_a_gate():
    document = {
        "time": {"unit": "second"},
        "activities": [{"kind": "gate", "start": 0, "end": 0, "node": ["H", "cup"]}],
    }
    result = validate_document(document)
    assert "invalid_node_path" in [d.code for d in result.diagnostics]


# --- the plan --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["inspect_then_wash", "rinse_until_clean"])
def test_a_plan_with_gates_is_a_valid_document(name):
    plan = _plan(name)
    assert kinds(plan, "gate")
    result = validate_document(plan)
    assert result.ok, [(d.code, d.message) for d in result.diagnostics]


def test_an_objects_gate_says_where_it_waits_and_is_moved_into_at_most_once():
    plan = _plan("rinse_until_clean")
    gates = {tuple(g["node"]): g for g in kinds(plan, "gate")}
    plate = ("Clean", 0, "@out", "plate")
    assert gates[plate]["start"] == gates[plate]["end"]
    into = [
        t for t in kinds(plan, "transport") if tuple(t["arc"]["to"]["node"]) == plate
    ]
    assert len(into) == 1 and into[0]["to_spot"] == gates[plate]["spot"]
    # A Pure Data gate holds nothing.
    assert "spot" not in gates[("Clean", "@out", "exhausted")]


def test_nothing_leaves_a_gate_before_the_reading_it_waits_for():
    plan = _plan("rinse_until_clean")
    read = next(p for p in kinds(plan, "processing") if p["node"] == ["Clean", 0, "Read"])
    for gate in kinds(plan, "gate"):
        assert gate["start"] >= read["end"], gate
    for move in kinds(plan, "transport"):
        if "@out" in move["arc"]["from"]["node"]:
            assert move["start"] >= read["end"], move


# --- regions decided before the run keep their shape -------------------------------------


def test_a_branch_the_boundary_decides_plans_without_gates():
    # Each cup's arm comes from the boundary (`expansion.arms`, written by the run
    # before it starts): nothing is decided during the run, so nothing is gated.
    plan = _plan("wash_or_polish")
    assert not kinds(plan, "gate")


def test_a_branch_the_boundary_decides_has_no_gate():
    workflow, _env, document = _example("inspect_then_wash")
    # The same workflow with the inspection's verdict replaced by a literal: decided
    # in the workflow itself, so nothing is undecided and nothing is gated.
    literal = copy.deepcopy(workflow)
    body = literal["processes"]["handle"]["body"]
    branch = next(node for node in body["nodes"] if node.get("kind") == "branch")
    branch["condition"] = {"value": True}
    wf, diags = parse_workflow(
        literal, interface=document.get("interface"), expansion=document.get("expansion"),
        assume=ASSUMED_ARM,
    )
    assert not [d for d in diags.items if d.severity == "error"]
    assert wf.gates == () and not wf.branch_gates


# --- replanning once the history has passed a gate ----------------------------------------


def _passed(plan: dict, until: int) -> dict:
    """The plan as a run hands it back at `until`: everything that started before is
    completed or running, nothing else has started. Gates are never stamped."""
    document = copy.deepcopy(plan)
    for entry in document["activities"]:
        if entry["kind"] not in ("processing", "transport"):
            continue
        if entry["start"] < until or (entry["start"] == entry["end"] == until):
            entry["status"] = "completed" if entry["end"] <= until else "running"
    document["now"] = until
    return document


@pytest.mark.parametrize("name", ["inspect_then_wash", "rinse_until_clean", "wash_or_polish"])
def test_a_replan_reads_a_gate_as_passed_once_its_waits_are_done(name):
    workflow, env, _document = _example(name)
    plan = _plan(name)
    moments = sorted({entry["end"] for entry in plan["activities"] if "end" in entry})
    for until in moments:
        report = schedule(
            copy.deepcopy(workflow), copy.deepcopy(env), document_path=_passed(plan, until),
            random_seed=0, check_arms=False,
        )
        assert report.plan is not None, (until, [(d.code, d.message) for d in report.diagnostics])
        for gate in kinds(report.plan, "gate"):
            # A gate behind `now` is history; one ahead is planned at or after it.
            if gate.get("status") == "completed":
                assert gate["end"] <= until, (until, gate)
            else:
                assert gate["start"] >= until, (until, gate)
