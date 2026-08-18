"""In-memory inputs: `schedule()` and both validators accept an already-loaded
document (a mapping) wherever they accept a path.

The point of these tests is equivalence: an in-memory document must be read
*exactly* as the file it would otherwise have been written to and read back --
same plan, same diagnostics codes -- plus the two things that differ by nature:
provenance (there is no path to record) and source positions (there are none, so
diagnostics locate by `path` alone). A fixed `random_seed` pins the solve to one
worker so whole-plan comparison is meaningful (see `cpsat.solve`).
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ofplang.schedule import schedule, validate_document, validate_environment

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
WORKFLOW = EXAMPLES / "simple.workflow.yaml"
ENV = EXAMPLES / "simple.env.yaml"
STATUS = EXAMPLES / "simple.status.yaml"
# The interface example is the one whose document carries a section that is echoed
# verbatim into the plan (§6.8), so it is what the aliasing tests use.
IF_WORKFLOW = EXAMPLES / "interface_load.workflow.yaml"
IF_ENV = EXAMPLES / "interface_load.env.yaml"
IF_DOC = EXAMPLES / "interface_load.document.yaml"


def load(path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def codes(diagnostics) -> set[str]:
    return {d.code for d in diagnostics}


# --- equivalence: the same plan whichever form the inputs arrive in -----------


def test_environment_dict_plans_exactly_as_the_file():
    from_path = schedule(WORKFLOW, ENV, random_seed=0)
    from_dict = schedule(WORKFLOW, load(ENV), random_seed=0, environment_source=str(ENV))
    assert from_path.ok and from_dict.ok
    # `environment_source` names the original file, so even `meta` matches.
    assert from_dict.plan == from_path.plan


def test_document_dict_plans_exactly_as_the_file():
    from_path = schedule(WORKFLOW, ENV, document_path=STATUS, random_seed=0)
    from_dict = schedule(
        WORKFLOW,
        ENV,
        document_path=load(STATUS),
        random_seed=0,
        document_source=str(STATUS),
    )
    assert from_path.ok and from_dict.ok
    assert from_dict.makespan == 6  # the replan the file drives (see test_replan)
    assert from_dict.plan == from_path.plan


def test_every_input_in_memory_records_in_memory_provenance():
    from_path = schedule(WORKFLOW, ENV, document_path=STATUS, random_seed=0)
    from_dicts = schedule(
        load(WORKFLOW), load(ENV), document_path=load(STATUS), random_seed=0
    )
    assert from_dicts.ok
    assert from_dicts.plan["meta"] == {
        "workflow": "<in-memory>",
        "environment": "<in-memory>",
        "status": "<in-memory>",
    }
    # Everything but the provenance is identical to the all-paths plan.
    assert {k: v for k, v in from_dicts.plan.items() if k != "meta"} == {
        k: v for k, v in from_path.plan.items() if k != "meta"
    }


def test_mixed_forms_are_allowed():
    """A path environment with an in-memory document, and the other way round."""
    baseline = schedule(WORKFLOW, ENV, document_path=STATUS, random_seed=0)
    for env, doc in ((ENV, load(STATUS)), (load(ENV), STATUS)):
        report = schedule(WORKFLOW, env, document_path=doc, random_seed=0)
        assert report.ok
        assert report.plan["activities"] == baseline.plan["activities"]


# --- diagnostics: same codes, no source position ------------------------------


def test_invalid_environment_dict_reports_the_same_codes(tmp_path):
    broken = load(ENV)
    del broken["time"]
    as_file = tmp_path / "broken.env.yaml"
    as_file.write_text(yaml.safe_dump(broken, sort_keys=False), encoding="utf-8")

    from_path = schedule(WORKFLOW, as_file)
    from_dict = schedule(WORKFLOW, broken)
    assert not from_path.ok and not from_dict.ok
    assert codes(from_dict.diagnostics) == codes(from_path.diagnostics)
    assert "missing_required_section" in codes(from_dict.diagnostics)
    # No positions to report, so the diagnostic locates by `path` alone.
    for diag in from_dict.diagnostics:
        assert diag.location is None
    assert any(d.path == "time" for d in from_dict.diagnostics)
    # ... while the same document read from a file still points at a line.
    assert any(d.location is not None for d in from_path.diagnostics)


def test_terminal_status_dict_is_rejected_like_the_file():
    status = load(STATUS)
    status["activities"][0]["status"] = "failed"
    report = schedule(WORKFLOW, ENV, document_path=status)
    assert not report.ok
    assert "terminal_status_not_replannable" in codes(report.diagnostics)


def test_empty_document_dict_is_a_document_not_an_absent_one():
    """`document_path=None` means "no document" (an initial plan); an empty mapping
    is a document that happens to be missing its required `activities`."""
    assert schedule(WORKFLOW, ENV, document_path=None, random_seed=0).ok
    report = schedule(WORKFLOW, ENV, document_path={}, random_seed=0)
    assert not report.ok
    assert "missing_activities" in codes(report.diagnostics)


def test_validators_accept_a_mapping_directly():
    assert validate_environment(load(ENV)).ok
    assert validate_document(load(STATUS)).ok
    assert not validate_environment({}).ok
    assert not validate_document({}).ok


# --- normalization: an in-memory document is read like its YAML form ----------


def test_tuples_and_non_string_keys_are_read_as_yaml_would_write_them(tmp_path):
    """`safe_dump` turns a tuple into a sequence and an int key into a string, so
    reading the mapping directly must do the same -- otherwise a document behaves
    differently depending on whether it went through a file."""
    status = load(STATUS)
    status["activities"][0]["node"] = tuple(status["activities"][0]["node"])
    round_tripped = tmp_path / "status.yaml"
    round_tripped.write_text(yaml.safe_dump(status, sort_keys=False), encoding="utf-8")

    from_dict = schedule(WORKFLOW, ENV, document_path=status, random_seed=0)
    from_file = schedule(WORKFLOW, ENV, document_path=round_tripped, random_seed=0)
    assert from_dict.ok and from_file.ok
    assert from_dict.plan["activities"] == from_file.plan["activities"]

    # An int key is reported under its string form, exactly as the file would be.
    keyed = load(STATUS)
    keyed[1] = "stray"
    report = validate_document(keyed)
    assert "unknown_key" in codes(report.diagnostics)
    assert any(d.path == "1" for d in report.diagnostics if d.code == "unknown_key")


# --- ownership: inputs are read-only, and the plan shares nothing with them ----


def test_the_plan_does_not_alias_the_input_document():
    doc = load(IF_DOC)
    report = schedule(IF_WORKFLOW, IF_ENV, document_path=doc, random_seed=0)
    assert report.ok
    assert report.plan["interface"] == doc["interface"]
    assert report.plan["interface"] is not doc["interface"]
    # Mutating the returned plan cannot reach back into the caller's document.
    report.plan["interface"]["inputs"]["sample"] = "somewhere.else"
    assert doc["interface"]["inputs"]["sample"] == "loader.stage"


def test_in_memory_inputs_are_not_modified():
    workflow, env, doc = load(WORKFLOW), load(ENV), load(STATUS)
    before = copy.deepcopy((workflow, env, doc))
    assert schedule(workflow, env, document_path=doc, random_seed=0).ok
    assert (workflow, env, doc) == before
