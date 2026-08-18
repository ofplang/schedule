"""Duplicate mapping keys are reported (SPECIFICATIONS.md §9, `duplicate_key`).

A repeated key is resolved last-wins by YAML itself, so a document that repeats
one is read as something other than what it appears to say. These tests cover
where the pass looks (nested mappings, inside sequences), where it deliberately
does not (`x-` payloads, §9.4), and how it reports (one diagnostic per key,
positioned at the last entry).
"""

from __future__ import annotations

from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.validation.document import validate_document_node
from ofplang.schedule.validation.duplicates import check_duplicate_keys
from ofplang.schedule.validation.environment import validate_environment_node


def _run(text: str) -> list:
    diags = Diagnostics()
    check_duplicate_keys(yamlnode.loads(text, "doc.yaml"), diags)
    return diags.items


def _paths(text: str) -> list[str]:
    return sorted(d.path for d in _run(text))


def test_reports_a_top_level_duplicate_once_at_its_last_entry():
    diags = _run("""a: 1
b: 2
a: 3
""")
    assert len(diags) == 1
    (diag,) = diags
    assert diag.code == "duplicate_key"
    assert diag.path == "a"
    assert diag.line == 3  # the entry that wins, and the one to delete
    assert "2 entries" in diag.message


def test_looks_inside_nested_mappings_and_sequences():
    assert _paths("""processes:
  compute:
    modes:
    - {duration: 1, duration: 2}
  compute:
    modes: []
""") == ["processes.compute", "processes.compute.modes[0].duration"]


def test_a_key_repeated_three_times_is_one_diagnostic():
    diags = _run("""a: 1
a: 2
a: 3
""")
    assert len(diags) == 1
    assert "3 entries" in diags[0].message


def test_extension_payloads_are_not_inspected_but_the_extension_key_is():
    """§9.4: the values under an `x-` key are never validated. That a key appears
    twice is a fact about the document, though, so a repeated `x-` key is reported
    -- only what is nested inside a payload is left alone (labcode finding L7)."""
    assert _paths("""x-labcode:
  script:
    a: 1
    a: 2
""") == []
    assert _paths("""x-labcode: {a: 1}
x-labcode: {a: 2}
""") == ["x-labcode"]


def test_no_duplicates_reports_nothing():
    assert _run("""a: 1
b: {c: 2, d: [1, 2, {e: 3}]}
""") == []


# --- through the validators ---------------------------------------------------

# The conformance baseline environment (cases/env/_baseline.yaml), inline.
_ENV = """time: {unit: second}
devices:
- id: reader_0
  spots: [stage]
processes:
  measure_od:
    modes:
    - devices: [reader_0]
      duration: 45
      input_spots: {plate: reader_0.stage}
"""

_DOC = """time: {unit: second}
now: 0
activities: []
"""


def _env(text: str):
    return validate_environment_node(yamlnode.loads(text, "env.yaml"))


def _doc(text: str):
    return validate_document_node(yamlnode.loads(text, "doc.yaml"))


def test_both_validators_reject_a_duplicate_key():
    """And report nothing else for it: the repeated section is itself well-formed,
    so the duplicate is the whole finding."""
    env = _env(_ENV + """objective: {kind: makespan}
objective: {kind: makespan}
""")
    assert not env.ok
    assert [d.code for d in env.diagnostics] == ["duplicate_key"]

    doc = _doc(_DOC + """outcome: optimal
outcome: feasible
""")
    assert not doc.ok
    assert [d.code for d in doc.diagnostics] == ["duplicate_key"]


def test_a_duplicate_does_not_hide_the_rest_of_the_document():
    """The pass reports and carries on, so an unrelated schema error in the same
    document still surfaces (one document, several independent problems)."""
    env = _env(_ENV + """transporters: [{id: arm}]
transporters: [{id: arm, bogus: 1}]
""")  # the second entry wins, and its unknown key is reported from there
    assert not env.ok
    assert sorted(d.code for d in env.diagnostics) == ["duplicate_key", "unknown_key"]


def test_the_baseline_documents_stay_valid():
    assert _env(_ENV).ok
    assert _doc(_DOC).ok
