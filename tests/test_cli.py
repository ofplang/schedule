"""Smoke tests for the CLI scaffold.

These pin the entry-point wiring and exit-code contract while the scheduler
itself is under construction; replace/expand them once real scheduling lands.
"""

from ofplang.schedule import cli


def test_missing_file_is_usage_error():
    assert cli.main(["does-not-exist.yaml"]) == cli.EXIT_USAGE


def test_existing_file_is_not_implemented(tmp_path):
    doc = tmp_path / "workflow.yaml"
    doc.write_text("processes: {}\n", encoding="utf-8")
    assert cli.main([str(doc)]) == cli.EXIT_NOT_IMPLEMENTED
