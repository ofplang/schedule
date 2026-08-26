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


def test_validate_malformed_yaml_under_auto_detect_is_usage_error(tmp_path, capsys):
    # Without --kind there is nothing to detect a kind from, so the failure is
    # reported as the undetermined kind rather than the parse error (both exit 2).
    bad = tmp_path / "broken.yaml"
    bad.write_text(_BROKEN_YAML, encoding="utf-8")
    assert cli.main(["validate", str(bad)]) == cli.EXIT_USAGE
    assert "cannot determine kind" in capsys.readouterr().err


def test_schedule_malformed_workflow_is_caught_by_front_door(tmp_path, capsys):
    # A malformed workflow is rejected by the ofplang-validate front door as an
    # invalid document (EXIT_INVALID), reported with its validate error code.
    bad = tmp_path / "broken.workflow.yaml"
    bad.write_text(_BROKEN_YAML, encoding="utf-8")
    code = cli.main(["schedule", str(bad), "--env", str(EXAMPLES / "simple.env.yaml")])
    assert code == cli.EXIT_INVALID
    assert "wrong_value_kind" in capsys.readouterr().err


def test_schedule_no_validate_bypasses_front_door_on_malformed(tmp_path, capsys):
    # --no-validate skips the validation pass, but `$import` expansion is a
    # structural step (spec 2.2 step 1) that still runs, so a malformed workflow
    # fails there instead: still an input error (EXIT_USAGE).
    bad = tmp_path / "broken.workflow.yaml"
    bad.write_text(_BROKEN_YAML, encoding="utf-8")
    code = cli.main(
        ["schedule", str(bad), "--env", str(EXAMPLES / "simple.env.yaml"), "--no-validate"]
    )
    assert code == cli.EXIT_USAGE
    assert "cannot expand" in capsys.readouterr().err


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


_GENERIC_WORKFLOW = """\
spec_version: "0.0"
types:
  Plate:
    domain: object
processes:
  make_plate:
    kind: atomic
    inputs: {}
    outputs:
      plate: {type: Plate, phase: data}
    objects:
      create: [outputs.plate]
  wash:
    kind: atomic
    type_params:
      O: {domain: object}
    inputs:
      item: {type: O, phase: data}
    outputs:
      item: {type: O, phase: data}
    objects:
      map: {outputs.item: inputs.item}
  main:
    kind: composite
    inputs: {}
    body:
      nodes:
        - {id: mk, process: make_plate}
        - {id: w, process: wash, state: {item: {from: mk.plate}}}
      returns:
        plate: {from: w.item}
    outputs:
      plate: {type: Plate, phase: data}
entry: main
"""


def test_schedule_generic_workflow_is_unsupported(tmp_path, capsys):
    # Generics are valid v0, so the front door passes; the capability gate then
    # rejects them (the scheduler does not support generic_processes) rather than
    # silently misreading the generic Object port and dropping its transport arc.
    wf = tmp_path / "generic.workflow.yaml"
    wf.write_text(_GENERIC_WORKFLOW, encoding="utf-8")
    code = cli.main(["schedule", str(wf), "--env", str(EXAMPLES / "simple.env.yaml")])
    assert code == cli.EXIT_INVALID
    assert "unsupported_feature" in capsys.readouterr().err


def test_schedule_no_validate_still_gates_generics(tmp_path, capsys):
    # The capability gate is independent of the front door: even with
    # --no-validate, a generic workflow is rejected (it can't be scheduled).
    wf = tmp_path / "generic.workflow.yaml"
    wf.write_text(_GENERIC_WORKFLOW, encoding="utf-8")
    code = cli.main(
        ["schedule", str(wf), "--env", str(EXAMPLES / "simple.env.yaml"), "--no-validate"]
    )
    assert code == cli.EXIT_INVALID
    assert "unsupported_feature" in capsys.readouterr().err


def _import_workflow(tmp_path: Path) -> Path:
    """A copy of the `simple` workflow whose `types` come from a `$import`
    fragment — valid v0 that the front door must expand before scheduling."""
    src = (EXAMPLES / "simple.workflow.yaml").read_text(encoding="utf-8")
    # Replace the inline `types:` block with a mapping-position import of it.
    body = src[src.index("processes:") :]
    (tmp_path / "types_frag.yaml").write_text("Sample:\n  domain: object\n", encoding="utf-8")
    wf = tmp_path / "main.workflow.yaml"
    wf.write_text(
        'spec_version: "0.0"\ntypes:\n  $import: ./types_frag.yaml\n' + body,
        encoding="utf-8",
    )
    return wf


def test_schedule_expands_import_and_schedules(tmp_path, capsys):
    # A `$import` workflow is no longer rejected: the front door resolves it and
    # schedules the expanded document (the Sample transport arc is recovered from
    # the imported type). `meta.workflow` still names the original file.
    wf = _import_workflow(tmp_path)
    code = cli.main(["schedule", str(wf), "--env", str(EXAMPLES / "simple.env.yaml")])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert str(wf) in out  # meta.workflow is the source path, not "<in-memory>"


def test_schedule_no_validate_expands_import(tmp_path):
    # Expansion is structural, so it runs under --no-validate too: the same
    # `$import` workflow schedules rather than tripping the library import guard.
    wf = _import_workflow(tmp_path)
    code = cli.main(
        ["schedule", str(wf), "--env", str(EXAMPLES / "simple.env.yaml"), "--no-validate"]
    )
    assert code == cli.EXIT_OK


def test_schedule_ignore_resources_warns_on_stderr(capsys):
    # A warning says a feature was accepted and then not applied, which changes what
    # the plan means. A *successful* schedule is exactly the case where nothing else
    # would ever mention it, so it has to go out even though the command succeeded.
    code = cli.main(
        [
            "schedule",
            str(EXAMPLES / "consumable.workflow.yaml"),
            "--env",
            str(EXAMPLES / "consumable.env.yaml"),
            "--document",
            str(EXAMPLES / "consumable.document.yaml"),
            "--ignore-resources",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "warning resources_ignored" in captured.err
    assert "consumption" not in captured.out
