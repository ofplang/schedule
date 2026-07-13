"""CLI tests: the `validate` and `schedule` subcommands and their exit codes."""

from pathlib import Path

from ofplang.schedule import cli

CASES = Path(__file__).parent / "conformance" / "cases"


def test_missing_file_is_usage_error():
    assert cli.main(["validate", "does-not-exist.yaml"]) == cli.EXIT_USAGE


def test_valid_environment_is_ok():
    assert cli.main(["validate", str(CASES / "env" / "_baseline.yaml")]) == cli.EXIT_OK


def test_invalid_environment_is_invalid():
    assert cli.main(["validate", str(CASES / "env" / "shape" / "empty_devices.yaml")]) == cli.EXIT_INVALID


def test_valid_document_is_ok():
    assert cli.main(["validate", str(CASES / "doc" / "_baseline.yaml")]) == cli.EXIT_OK


def test_schedule_is_not_implemented(tmp_path):
    doc = tmp_path / "workflow.yaml"
    doc.write_text("activities: []\n", encoding="utf-8")
    assert cli.main(["schedule", str(doc)]) == cli.EXIT_NOT_IMPLEMENTED
