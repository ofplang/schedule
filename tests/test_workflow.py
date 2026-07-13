"""Tests for the minimal v0 workflow reader."""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.model import Arc, Endpoint
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _errors(diags):
    return [d for d in diags.items if d.severity == ERROR]


def test_parse_simple():
    wf, diags = parse_workflow(EXAMPLES / "simple.workflow.yaml")
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


# --- nested composite flattening (boundary splicing) --------------------
#
# A composite invoked as a node is structural: its body is spliced into the
# parent graph. An enclosing `Child.out` reference resolves through the child's
# `returns` to the producing atomic (outward splice); a child's `inputs.p`
# reference resolves to whatever the invocation bound to p (inward splice). Node
# ids gain a qualified path so identities stay unique.

# source -> target, but each wrapped in its own composite: `producer` returns the
# source's output, `consumer` forwards its input to the target. Process/port names
# match `simple` so the flattened graph schedules against examples/simple.env.yaml.
_NESTED = """\
spec_version: "0.0"
types:
  Sample: {domain: object}
processes:
  source:
    kind: atomic
    outputs: {source_out: {type: Sample, phase: data}}
    objects: {create: [outputs.source_out]}
  target:
    kind: atomic
    inputs: {target_in: {type: Sample, phase: data}}
    objects: {consume: [inputs.target_in]}
  producer:
    kind: composite
    outputs: {p_out: {type: Sample, phase: data}}
    body:
      nodes:
        - {id: S, process: source}
      returns: {p_out: {from: S.source_out}}
  consumer:
    kind: composite
    inputs: {c_in: {type: Sample, phase: data}}
    body:
      nodes:
        - id: T
          process: target
          state: {target_in: {from: inputs.c_in}}
  main:
    kind: composite
    body:
      nodes:
        - {id: Prod, process: producer}
        - id: Cons
          process: consumer
          state: {c_in: {from: Prod.p_out}}
entry: main
"""


def test_nested_composite_is_flattened(tmp_path):
    doc = tmp_path / "nested.yaml"
    doc.write_text(_NESTED, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)
    assert wf is not None

    # Inner atomics gain a qualified node path through their enclosing composite.
    paths = {a.path: a.process for a in wf.activities}
    assert paths == {("Prod", "S"): "source", ("Cons", "T"): "target"}

    # The arc is spliced across both boundaries: producer's return (Prod/S.source_out)
    # into the consumer's forwarded input (Cons/T.target_in).
    assert wf.arcs == (
        Arc(Endpoint(("Prod", "S"), "source_out"), Endpoint(("Cons", "T"), "target_in")),
    )
    assert (("Prod", "S"), ("Cons", "T")) in wf.precedence
    # Only the invoked atomics are recorded as used process signatures.
    assert set(wf.processes) == {"source", "target"}


def test_recursive_composite_is_reported(tmp_path):
    # `loop` invokes itself -> the expander must stop and report, not recurse.
    doc = tmp_path / "recursive.yaml"
    doc.write_text(
        "processes:\n"
        "  loop:\n"
        "    kind: composite\n"
        "    body: {nodes: [{id: inner, process: loop}]}\n"
        "  main:\n"
        "    kind: composite\n"
        "    body: {nodes: [{id: L, process: loop}]}\n"
        "entry: main\n",
        encoding="utf-8",
    )
    wf, diags = parse_workflow(doc)
    assert "recursive_composite" in {d.code for d in _errors(diags)}


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
