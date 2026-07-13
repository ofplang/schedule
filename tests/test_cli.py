"""CLI tests: the `validate` and `schedule` subcommands and their exit codes."""

from pathlib import Path

from ofplang.schedule import cli, validate_document

CASES = Path(__file__).parent / "conformance" / "cases"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_missing_file_is_usage_error():
    assert cli.main(["validate", "does-not-exist.yaml"]) == cli.EXIT_USAGE


def test_valid_environment_is_ok():
    assert cli.main(["validate", str(CASES / "env" / "_baseline.yaml")]) == cli.EXIT_OK


def test_invalid_environment_is_invalid():
    assert cli.main(["validate", str(CASES / "env" / "shape" / "empty_devices.yaml")]) == cli.EXIT_INVALID


def test_valid_document_is_ok():
    assert cli.main(["validate", str(CASES / "doc" / "_baseline.yaml")]) == cli.EXIT_OK


def test_schedule_missing_file_is_usage_error():
    assert cli.main(["schedule", "nope.yaml", "--env", "also-nope.yaml"]) == cli.EXIT_USAGE


def test_schedule_produces_valid_plan(tmp_path):
    out = tmp_path / "plan.yaml"
    code = cli.main(
        [
            "schedule",
            str(EXAMPLES / "simple.workflow.yaml"),
            "--env",
            str(EXAMPLES / "simple.env.yaml"),
            "-o",
            str(out),
        ]
    )
    assert code == cli.EXIT_OK
    # The emitted plan must itself validate as an execution document.
    assert validate_document(out).ok


def test_schedule_stdout_yaml(capsys):
    code = cli.main(
        [
            "schedule",
            str(EXAMPLES / "simple.workflow.yaml"),
            "--env",
            str(EXAMPLES / "simple.env.yaml"),
        ]
    )
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "outcome: optimal" in out
    assert "makespan" in out
