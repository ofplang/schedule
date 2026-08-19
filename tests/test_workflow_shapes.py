"""What the workflow reader does with a document that is not shaped as v0 requires.

It assumes valid v0 -- validating is `ofplang-validate`'s job, run once at each CLI's
front door -- but it cannot assume it was *given* valid v0: `--no-validate` says the
caller already validated, and a caller holding a document in memory may not have.

Two outcomes are unacceptable and are what these tests pin. Raising, because the
function's contract is to return diagnostics and both CLIs catch only `YAMLError`, so
an exception arrives as a traceback. And reading the document *partially*, because a
workflow with fewer activities than the document describes schedules successfully and
says nothing about what it dropped.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

VALID = {
    "spec_version": "0.0",
    "types": {"Cup": {"domain": "object"}},
    "processes": {
        "make": {
            "kind": "atomic",
            "outputs": {"o": {"type": "Cup", "phase": "data"}},
            "objects": {"create": ["outputs.o"]},
        },
        "take": {
            "kind": "atomic",
            "inputs": {"i": {"type": "Cup", "phase": "data"}},
            "objects": {"consume": ["inputs.i"]},
        },
        "main": {
            "kind": "composite",
            "inputs": {},
            "outputs": {"out": {"type": "Cup", "phase": "data"}},
            "body": {
                "nodes": [
                    {"id": "M", "process": "make"},
                    {"id": "T", "process": "take", "state": {"i": {"from": "M.o"}}},
                ],
                "returns": {"out": {"from": "M.o"}},
            },
        },
    },
    "entry": "main",
}

# Shapes the reader would otherwise read partially: it carries on with less than the
# document holds. Each names the position the diagnostic must point at.
READ_PARTIALLY = {
    "a type definition": ("types.Cup", lambda d: d["types"].__setitem__("Cup", "x")),
    "a body": (
        "processes.main.body",
        lambda d: d["processes"]["main"].__setitem__("body", "x"),
    ),
    "a body written as a sequence": (
        "processes.main.body",
        lambda d: d["processes"]["main"].__setitem__("body", []),
    ),
    "a node list": (
        "processes.main.body.nodes",
        lambda d: d["processes"]["main"]["body"].__setitem__("nodes", "x"),
    ),
    "a node list written as a mapping": (
        "processes.main.body.nodes",
        lambda d: d["processes"]["main"]["body"].__setitem__("nodes", {"M": {}}),
    ),
    "a node": (
        "processes.main.body.nodes[0]",
        lambda d: d["processes"]["main"]["body"]["nodes"].__setitem__(0, "x"),
    ),
    "a node id": (
        "processes.main.body.nodes[0].id",
        lambda d: d["processes"]["main"]["body"]["nodes"][0].pop("id"),
    ),
    "a binding source": (
        "processes.main.body.nodes[1].state.i",
        lambda d: d["processes"]["main"]["body"]["nodes"][1]["state"].__setitem__("i", "x"),
    ),
    "a return source": (
        "processes.main.body.returns.out",
        lambda d: d["processes"]["main"]["body"]["returns"].__setitem__("out", "x"),
    ),
}

# Shapes the reader cannot use at all: it would raise, and the translation reports it.
UNREADABLE = {
    "types": lambda d: d.__setitem__("types", "x"),
    "processes": lambda d: d.__setitem__("processes", "x"),
    "the entry process": lambda d: d["processes"].__setitem__("main", "x"),
    "an atomic process": lambda d: d["processes"].__setitem__("make", "x"),
    "the entry name": lambda d: d.__setitem__("entry", ["main"]),
    "a port map": lambda d: d["processes"]["make"].__setitem__("outputs", "x"),
    "a port declaration": lambda d: d["processes"]["make"]["outputs"].__setitem__("o", "x"),
    "a node kind": lambda d: d["processes"]["main"]["body"]["nodes"][0].__setitem__("kind", ["m"]),
    "a binding section": lambda d: d["processes"]["main"]["body"]["nodes"][1].__setitem__(
        "state", "x"
    ),
    "the returns map": lambda d: d["processes"]["main"]["body"].__setitem__("returns", "x"),
}


def _mutated(mutate) -> dict:
    doc = copy.deepcopy(VALID)
    mutate(doc)
    return doc


def test_the_valid_document_parses() -> None:
    workflow, diags = parse_workflow(copy.deepcopy(VALID))
    assert workflow is not None and not diags.items
    assert len(workflow.activities) == 2


@pytest.mark.parametrize("what", sorted(READ_PARTIALLY))
def test_a_partially_readable_shape_is_reported_at_its_position(what: str) -> None:
    path, mutate = READ_PARTIALLY[what]
    workflow, diags = parse_workflow(_mutated(mutate))
    assert workflow is None, f"{what}: a partially read document must not be scheduled"
    assert [d.code for d in diags.items] == ["wrong_type"]
    assert diags.items[0].path == path


@pytest.mark.parametrize("what", sorted(UNREADABLE))
def test_an_unreadable_shape_becomes_a_diagnostic(what: str) -> None:
    workflow, diags = parse_workflow(_mutated(UNREADABLE[what]))
    assert workflow is None
    codes = {d.code for d in diags.items}
    assert "wrong_type" in codes
    # The exception's own type and text survive into the message, so a genuine bug in
    # the reader is not disguised as a malformed document.
    message = next(d.message for d in diags.items if d.code == "wrong_type")
    assert "Error" in message or "error" in message


def test_every_malformed_shape_is_answered_with_diagnostics() -> None:
    """The whole survey at once: none of these may raise, and none may pass silently."""
    shapes = [m for _, m in READ_PARTIALLY.values()] + list(UNREADABLE.values())
    for mutate in shapes:
        workflow, diags = parse_workflow(_mutated(mutate))  # must not raise
        assert diags.items, "a malformed document must produce a diagnostic"
        assert workflow is None


def test_a_structured_node_is_still_reported_as_unsupported() -> None:
    """Unchanged: a valid v0 workflow the scheduler does not support is a capability
    finding, not a shape one.

    Note what this one does *not* assert: the reader still returns the workflow it
    built around the unsupported node. That is how it behaved before, and it is safe
    because the entry point refuses any document with an error diagnostic
    (`api.schedule`); the guards did not change it.
    """
    doc = copy.deepcopy(VALID)
    doc["processes"]["main"]["body"]["nodes"][0]["kind"] = "map"
    _workflow, diags = parse_workflow(doc)
    assert {d.code for d in diags.items} == {"unsupported_feature"}


@pytest.mark.parametrize(
    "path", sorted(EXAMPLES.glob("*.workflow.yaml")), ids=lambda p: p.stem
)
def test_the_examples_still_parse_without_a_diagnostic(path: Path) -> None:
    """The guards must reject nothing that was read before."""
    workflow, diags = parse_workflow(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert workflow is not None
    assert not diags.items
