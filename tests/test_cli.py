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
    assert (
        cli.main(["validate", str(CASES / "env" / "shape" / "empty_devices.yaml")])
        == cli.EXIT_INVALID
    )


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


_BROKEN_YAML = "a: [1, 2\nb: :::\n"


def test_validate_malformed_yaml_is_usage_error(tmp_path, capsys):
    # A file that does not parse as YAML is an input error (exit 2), even when
    # --kind is explicit (which skips the auto-detect that also catches it).
    bad = tmp_path / "broken.yaml"
    bad.write_text(_BROKEN_YAML, encoding="utf-8")
    assert cli.main(["validate", "--kind", "environment", str(bad)]) == cli.EXIT_USAGE
    assert "cannot parse" in capsys.readouterr().err


def test_schedule_malformed_workflow_is_caught_by_front_door(tmp_path, capsys):
    # A malformed workflow is rejected by the ofplang-validate front door as an
    # invalid document (EXIT_INVALID), reported with its validate error code.
    bad = tmp_path / "broken.workflow.yaml"
    bad.write_text(_BROKEN_YAML, encoding="utf-8")
    code = cli.main(["schedule", str(bad), "--env", str(EXAMPLES / "simple.env.yaml")])
    assert code == cli.EXIT_INVALID
    assert "wrong_value_kind" in capsys.readouterr().err


def test_schedule_no_validate_bypasses_front_door_on_malformed(tmp_path, capsys):
    # With --no-validate the front door is skipped, so a malformed workflow now
    # falls through to the scheduler's own YAML guard: still an input error.
    bad = tmp_path / "broken.workflow.yaml"
    bad.write_text(_BROKEN_YAML, encoding="utf-8")
    code = cli.main(
        ["schedule", str(bad), "--env", str(EXAMPLES / "simple.env.yaml"), "--no-validate"]
    )
    assert code == cli.EXIT_USAGE
    assert "cannot parse" in capsys.readouterr().err


def _workflow_with_bogus_key(tmp_path) -> Path:
    """A parseable workflow that is invalid v0 (an unknown top-level key) but that
    the scheduler still tolerates — it ignores unknown keys."""
    src = (EXAMPLES / "simple.workflow.yaml").read_text(encoding="utf-8")
    wf = tmp_path / "bogus.workflow.yaml"
    wf.write_text("bogus_key: 1\n" + src, encoding="utf-8")
    return wf


def test_schedule_invalid_v0_workflow_is_invalid(tmp_path, capsys):
    # A parseable-but-invalid-v0 workflow (not just malformed YAML) is caught by
    # the front door too, with the specific validate code.
    wf = _workflow_with_bogus_key(tmp_path)
    code = cli.main(["schedule", str(wf), "--env", str(EXAMPLES / "simple.env.yaml")])
    assert code == cli.EXIT_INVALID
    assert "unknown_key" in capsys.readouterr().err


def test_schedule_no_validate_skips_front_door(tmp_path):
    # --no-validate lets an invalid-v0-but-schedulable workflow through: the
    # scheduler ignores the unknown key and produces a plan.
    wf = _workflow_with_bogus_key(tmp_path)
    code = cli.main(
        ["schedule", str(wf), "--env", str(EXAMPLES / "simple.env.yaml"), "--no-validate"]
    )
    assert code == cli.EXIT_OK
