"""Tests for the minimal v0 workflow reader."""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.model import Arc, Endpoint
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _errors(diags):
    return [d for d in diags.items if d.severity == ERROR]


def test_parse_job_sample():
    wf, diags = parse_workflow(EXAMPLES / "job_sample.workflow.yaml")
    assert not _errors(diags)
    assert wf is not None

    paths = {a.path for a in wf.activities}
    assert paths == {("SampleSource",), ("SampleTarget",)}
    assert {a.process for a in wf.activities} == {"source", "target"}

    assert wf.arcs == (
        Arc(Endpoint(("SampleSource",), "source_out"), Endpoint(("SampleTarget",), "target_in")),
    )
    assert (("SampleSource",), ("SampleTarget",)) in wf.precedence

    assert wf.processes["source"].object_output_names() == ("source_out",)
    assert wf.processes["source"].object_input_names() == ()
    assert wf.processes["target"].object_input_names() == ("target_in",)


def test_parse_reformatter():
    wf, diags = parse_workflow(EXAMPLES / "reformatter.workflow.yaml")
    assert not _errors(diags)
    assert wf is not None

    assert len(wf.activities) == 8
    assert len(wf.arcs) == 12

    # A representative arc: Preparation feeds Reformatter12.
    assert Arc(Endpoint(("Preparation",), "prep_out_rf12"), Endpoint(("Reformatter12",), "rf12_in")) in wf.arcs
    # Reformatter20 has three inputs and two outputs, all Object-bearing.
    assert wf.processes["reformatter_20"].object_input_names() == (
        "rf20_in_rf12",
        "rf20_in_motoman",
        "rf20_in_a3_24",
    )
    assert wf.processes["reformatter_20"].object_output_names() == (
        "rf20_out_rf3",
        "rf20_out_a3_19",
    )
    # Reformatter3 is a pure sink (no outputs).
    assert wf.processes["reformatter_3"].object_output_names() == ()


def test_structured_node_is_unsupported(tmp_path):
    doc = tmp_path / "wf.yaml"
    doc.write_text(
        "types: {Cup: {domain: object}}\n"
        "processes:\n"
        "  make: {kind: atomic, outputs: {cup: {type: Cup, phase: data}}, objects: {create: [outputs.cup]}}\n"
        "  main:\n"
        "    kind: composite\n"
        "    body:\n"
        "      nodes:\n"
        "        - {id: m, kind: map, process: make, each: {x: {from: inputs.xs}}}\n"
        "entry: main\n",
        encoding="utf-8",
    )
    wf, diags = parse_workflow(doc)
    codes = {d.code for d in _errors(diags)}
    assert "unsupported_feature" in codes
