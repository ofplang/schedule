"""The objective is a lexicographic stage list (§4.8), declared in the execution
document and reported in the plan.

Two things these tests exist to hold. A run that cannot replenish reports the bare
`makespan` it always did, whatever stage list was declared -- that is what lets a
v0-era plan and a plan written today be the same document, and it is easy to lose
the moment a second stage becomes reachable. And there is exactly one declaration
site: an environment that declares an objective is refused, by a code that says
where the section went rather than one that says the key means nothing.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ofplang.schedule import schedule, validate_document, validate_environment
from ofplang.schedule.core import objective as stages

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


# --- the stage vocabulary ------------------------------------------------


@pytest.mark.parametrize(
    "declared,expected",
    [
        ("makespan", ("makespan",)),
        ("replenishment_count", ("replenishment_count",)),
        (["makespan"], ("makespan",)),
        (["makespan", "replenishment_count"], ("makespan", "replenishment_count")),
        # Order is the caller's choice: minimising refills first and breaking ties
        # on time is a different objective, not a malformed one.
        (["replenishment_count", "makespan"], ("replenishment_count", "makespan")),
    ],
)
def test_normalize_accepts(declared, expected):
    assert stages.normalize(declared) == expected


@pytest.mark.parametrize(
    "declared",
    [
        "latency",  # not a stage v0 defines
        ["latency"],
        [],  # names nothing to minimise
        ["makespan", "makespan"],  # the repeat could never change the outcome
        ["makespan", 3],  # a stage name is a string
        5,
        {"kind": "makespan"},
        None,
    ],
)
def test_normalize_rejects(declared):
    assert stages.normalize(declared) is None


def test_effective_drops_the_unreachable_stage():
    declared = ("makespan", "replenishment_count")
    assert stages.effective(declared, replenishment_possible=True) == declared
    assert stages.effective(declared, replenishment_possible=False) == ("makespan",)


def test_effective_always_leaves_something_to_minimise():
    # Naming only a stage that drops out would otherwise leave the schedule pinned
    # in time by nothing but the horizon.
    assert stages.effective(("replenishment_count",), replenishment_possible=False) == (
        "makespan",
    )


def test_render_shape_follows_the_stage_count():
    assert stages.render(("makespan",), (12,)) == {"kind": "makespan", "value": 12}
    assert stages.render(("makespan", "replenishment_count"), (12, 2)) == {
        "kind": ["makespan", "replenishment_count"],
        "value": [12, 2],
    }


# --- what a declaration does to the plan ---------------------------------


def _plan_objective(declared):
    # Declared in the *document* -- the declaration site (§6.1).
    env = yaml.safe_load((EXAMPLES / "interface_load.env.yaml").read_text(encoding="utf-8"))
    env.pop("objective", None)
    document = yaml.safe_load(
        (EXAMPLES / "interface_load.document.yaml").read_text(encoding="utf-8")
    )
    if declared is not None:
        document["objective"] = {"kind": declared}
    report = schedule(EXAMPLES / "interface_load.workflow.yaml", env, document_path=document)
    assert report.plan is not None, [d.code for d in report.diagnostics]
    return report.plan["objective"]


@pytest.mark.parametrize(
    "declared",
    [
        None,  # omitted -> the default, [makespan, replenishment_count]
        "makespan",
        ["makespan"],
        ["makespan", "replenishment_count"],
        ["replenishment_count", "makespan"],
        "replenishment_count",
    ],
)
def test_plan_reports_the_scalar_makespan_without_replenishment(declared):
    # No environment can replenish yet, so every declaration above names the same
    # single reachable stage and the plan is shaped as it always was.
    assert _plan_objective(declared) == {"kind": "makespan", "value": 14}


def test_declaring_the_default_explicitly_changes_nothing():
    assert _plan_objective(["makespan", "replenishment_count"]) == _plan_objective(None)


# --- validation ----------------------------------------------------------


def _env_errors(kind):
    env = yaml.safe_load((EXAMPLES / "interface_load.env.yaml").read_text(encoding="utf-8"))
    env["objective"] = {"kind": kind}
    return [d.code for d in validate_environment(env).errors]


@pytest.mark.parametrize(
    "kind",
    [
        "makespan",  # what an environment written before the move says
        ["makespan", "replenishment_count"],
        "latency",  # malformed, but that is not the finding
        [],
        5,
    ],
)
def test_an_environment_declaring_an_objective_is_refused_whatever_it_says(kind):
    """One code for every shape: the section is not read, so there is no point
    telling an author their stage list is malformed in a place nothing looks."""
    assert _env_errors(kind) == ["objective_in_environment"]


def test_the_refusal_says_where_the_section_went():
    """`unknown_key` would be true and useless -- it cannot name the document."""
    env = yaml.safe_load((EXAMPLES / "interface_load.env.yaml").read_text(encoding="utf-8"))
    env["objective"] = {"kind": "makespan"}
    message = validate_environment(env).errors[0].message
    assert "execution document" in message


def _doc_errors(objective):
    doc = {
        "activities": [],
        "objective": copy.deepcopy(objective),
    }
    return [d.code for d in validate_document(doc).errors]


def test_document_value_takes_the_shape_of_its_kind():
    assert _doc_errors({"kind": "makespan", "value": 5}) == []
    assert _doc_errors({"kind": ["makespan", "replenishment_count"], "value": [5, 1]}) == []


def test_document_rejects_a_value_that_does_not_match_its_kind():
    # One entry short of the stages it accompanies: it does not say what each stage
    # reached.
    assert _doc_errors({"kind": ["makespan", "replenishment_count"], "value": [5]}) == [
        "wrong_type"
    ]
    # A scalar where the stages are a list.
    assert _doc_errors({"kind": ["makespan", "replenishment_count"], "value": 5}) == [
        "wrong_type"
    ]


def test_a_malformed_kind_does_not_also_fault_its_value():
    # The field already has a diagnostic; guessing which shape `value` was meant to
    # take would only add a second one saying the same thing.
    assert _doc_errors({"kind": "latency", "value": 1}) == ["unknown_objective_kind"]


# --- where the objective is declared -------------------------------------
#
# It says how *this run* is to be optimised, so it belongs with the run's other
# planning inputs rather than with the description of the lab. The environment was
# the other declaration site until 0.2.1 -- first the only one, then a deprecated
# fallback. These pin that there is now one.


def _report(env_kind=None, doc_kind=None):
    env = yaml.safe_load((EXAMPLES / "interface_load.env.yaml").read_text(encoding="utf-8"))
    env.pop("objective", None)
    if env_kind is not None:
        env["objective"] = {"kind": env_kind}
    document = yaml.safe_load(
        (EXAMPLES / "interface_load.document.yaml").read_text(encoding="utf-8")
    )
    if doc_kind is not None:
        document["objective"] = {"kind": doc_kind}
    return schedule(EXAMPLES / "interface_load.workflow.yaml", env, document_path=document)


def _codes(report):
    return [d.code for d in report.diagnostics]


def test_the_document_declares_the_objective():
    report = _report(doc_kind=["makespan"])
    assert report.plan is not None, _codes(report)
    assert _codes(report) == []


def test_an_environment_that_declares_one_stops_the_schedule():
    report = _report(env_kind="makespan")
    assert report.plan is None
    assert _codes(report) == ["objective_in_environment"]


def test_the_environment_is_refused_even_when_the_document_declares_one_too():
    # There is nothing to fall back to any more, so "the document said it as well"
    # does not make the environment's declaration harmless -- it makes it a second
    # answer to a question with one place to answer it.
    report = _report(env_kind=["replenishment_count", "makespan"], doc_kind="makespan")
    assert report.plan is None
    assert _codes(report) == ["objective_in_environment"]


def test_declaring_it_nowhere_is_the_default_and_says_nothing():
    report = _report()
    assert report.plan is not None, _codes(report)
    assert _codes(report) == []
    # No replenishment is possible here, so the default's second stage drops out.
    assert report.plan["objective"] == {"kind": "makespan", "value": 14}


def test_a_malformed_declaration_in_the_document_is_rejected():
    report = _report(doc_kind="latency")
    assert report.plan is None
    assert "unknown_objective_kind" in _codes(report)
