"""Conformance runner for the schema validators.

Discovers every case under tests/conformance/cases/{env,doc}/, runs the matching
validator, and compares the produced error/warning codes to the case's
`.expected.yaml` (SPECIFICATIONS.md §9, §10). See tests/conformance/README.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ofplang.schedule import validate_document, validate_environment
from ofplang.schedule.validation.errors import ALL_CODES

CASES_DIR = Path(__file__).parent / "conformance" / "cases"
_SUFFIX = ".expected.yaml"


def _discover():
    cases = []
    for expected in sorted(CASES_DIR.rglob("*" + _SUFFIX)):
        doc = expected.with_name(expected.name[: -len(_SUFFIX)] + ".yaml")
        rel = doc.relative_to(CASES_DIR).as_posix()
        kind = "environment" if rel.startswith("env/") else "document"
        cases.append(pytest.param(doc, expected, kind, id=rel[: -len(".yaml")]))
    return cases


def _codes(items):
    return {d.code for d in items}


@pytest.mark.parametrize("doc,expected_path,kind", _discover())
def test_case(doc: Path, expected_path: Path, kind: str) -> None:
    expected = yaml.safe_load(expected_path.read_text(encoding="utf-8"))

    # A `pending` case documents behavior not satisfied yet (spec-first).
    if expected.get("pending"):
        pytest.xfail(expected["pending"])

    result = validate_environment(doc) if kind == "environment" else validate_document(doc)
    produced_errors = _codes(result.errors)
    produced_warnings = _codes(result.warnings)

    expected_errors = {e["code"] for e in (expected.get("errors") or [])}
    expected_warnings = {w["code"] for w in (expected.get("warnings") or [])}

    # Every expected code must be a declared code (§10) — guards against typos.
    for code in expected_errors | expected_warnings:
        assert code in ALL_CODES, f"expected code {code!r} is not in §10"

    if expected["outcome"] == "valid":
        assert result.ok, f"expected valid, got errors {sorted(produced_errors)}"
        assert not produced_errors
    else:
        assert not result.ok, "expected invalid, but no errors were produced"
        if expected.get("match", "exact") == "exact":
            assert produced_errors == expected_errors
        else:
            assert expected_errors <= produced_errors

    # Warnings are always compared as an exact set.
    assert produced_warnings == expected_warnings
