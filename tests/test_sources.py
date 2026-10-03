"""The runner-facing `Source` trees (D57) say what the per-kind fields say.

`input_sources` / `output_sources` / `CompositeIO.*_sources` are meant to replace
`arcs` + `data_arcs` + `entry_inputs` + `data_literals` (and their exit / composite
counterparts) for the runner. Until a `map` / `fold` is expanded they hold nothing
those fields cannot, so for every workflow without one the two must agree exactly,
in both directions: that is what lets the runner move over without a change in
what it is told.
"""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule.scheduler.model import (
    Endpoint,
    SourceLiteral,
    SourceRef,
    Workflow,
)
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# Every routing shape at once: an Object entry input and an Object arc; a Pure Data
# arc and a Pure Data entry input fanned out to three atomics, one through a
# composite; literals bound directly and through a composite; and final outputs that
# are an atomic's, a pass-through (direct and through a composite) and a literal.
_EVERY_SHAPE = """\
spec_version: "0.0"
types:
  Plate: {domain: object}
processes:
  heat:
    kind: atomic
    inputs:
      plate: {type: Plate, phase: data}
      t: {type: Float, phase: run}
    outputs:
      plate: {type: Plate, phase: data}
      r: {type: Float, phase: data}
    objects: {map: {outputs.plate: inputs.plate}}
  read:
    kind: atomic
    inputs:
      t: {type: Float, phase: run}
      k: {type: Float, phase: run}
    outputs: {r: {type: Float, phase: data}}
  wrap:
    kind: composite
    inputs:
      t: {type: Float, phase: run}
      k: {type: Float, phase: run}
    outputs: {r: {type: Float, phase: data}}
    body:
      nodes:
        - {id: inner, process: read, bind: {t: {from: inputs.t}, k: {from: inputs.k}}}
      returns: {r: {from: inner.r}}
  echo:
    kind: composite
    inputs: {k: {type: Float, phase: run}}
    outputs: {k: {type: Float, phase: run}}
    body:
      nodes: []
      returns: {k: {from: inputs.k}}
  main:
    kind: composite
    inputs:
      p: {type: Plate, phase: data}
      t: {type: Float, phase: run}
    outputs:
      p: {type: Plate, phase: data}
      r: {type: Float, phase: data}
      t_echo: {type: Float, phase: run}
      t_wrapped: {type: Float, phase: run}
      k: {type: Float, phase: run}
    body:
      nodes:
        - {id: A, process: heat, state: {plate: {from: inputs.p}}, bind: {t: {from: inputs.t}}}
        - {id: B, process: heat, state: {plate: {from: A.plate}}, bind: {t: {from: A.r}}}
        - {id: R, process: read, bind: {t: {from: inputs.t}, k: {value: 2.0}}}
        - {id: W, process: wrap, bind: {t: {from: inputs.t}, k: {value: 1.0}}}
        - {id: E, process: echo, bind: {k: {from: inputs.t}}}
        - {id: C, process: echo, bind: {k: {value: 3.0}}}
      returns:
        p: {from: B.plate}
        r: {from: W.r}
        t_echo: {from: inputs.t}
        t_wrapped: {from: E.k}
        k: {from: C.k}
entry: main
"""


def _workflows(tmp_path):
    paths = sorted(EXAMPLES.glob("*.workflow.yaml")) + [
        EXAMPLES / "outputs" / "plate_batch.workflow.yaml"
    ]
    every = tmp_path / "every_shape.yaml"
    every.write_text(_EVERY_SHAPE, encoding="utf-8")
    paths.append(every)
    for path in paths:
        wf, _ = parse_workflow(path)
        assert wf is not None, path
        yield path.name, wf


def _input_sources_from_old_fields(wf: Workflow) -> dict:
    """What the per-kind fields say about each atomic input, as Sources."""
    said: dict = {}
    for arc in wf.arcs + wf.data_arcs:
        said[arc.dst] = SourceRef(arc.src.node, arc.src.port)
    for name, consumer in {**wf.entry_inputs, **wf.data_entry_inputs}.items():
        said[consumer] = SourceRef((), name)
    for consumer, value in wf.data_literals.items():
        said[consumer] = SourceLiteral(value)
    return said


def test_input_sources_say_what_the_per_kind_fields_say(tmp_path):
    for name, wf in _workflows(tmp_path):
        assert wf.input_sources == _input_sources_from_old_fields(wf), name


def test_output_sources_say_what_exit_outputs_and_exit_literals_say(tmp_path):
    for name, wf in _workflows(tmp_path):
        said = {port: SourceRef(ep.node, ep.port) for port, ep in wf.exit_outputs.items()}
        said.update({port: SourceLiteral(v) for port, v in wf.exit_literals.items()})
        assert wf.output_sources == said, name


def test_composite_sources_say_what_its_four_maps_say(tmp_path):
    seen = 0
    for name, wf in _workflows(tmp_path):
        for path, io in wf.composites.items():
            seen += 1
            inputs = {p: SourceRef(ep.node, ep.port) for p, ep in io.inputs.items()}
            inputs.update({p: SourceLiteral(v) for p, v in io.input_literals.items()})
            outputs = {p: SourceRef(ep.node, ep.port) for p, ep in io.outputs.items()}
            outputs.update({p: SourceLiteral(v) for p, v in io.output_literals.items()})
            assert io.input_sources == inputs, (name, path)
            assert io.output_sources == outputs, (name, path)
    assert seen > 0  # the fixture has composites, so this compared something


def test_the_every_shape_fixture_reads_as_intended(tmp_path):
    # Spelled out once, so the equivalence above is known to cover each shape rather
    # than to agree on an empty map.
    (wf,) = [wf for name, wf in _workflows(tmp_path) if name == "every_shape.yaml"]
    assert wf.input_sources[Endpoint(("A",), "plate")] == SourceRef((), "p")
    assert wf.input_sources[Endpoint(("B",), "plate")] == SourceRef(("A",), "plate")
    assert wf.input_sources[Endpoint(("B",), "t")] == SourceRef(("A",), "r")
    for consumer in (("A",), ("R",), ("W", "inner")):
        assert wf.input_sources[Endpoint(consumer, "t")] == SourceRef((), "t")
    assert wf.input_sources[Endpoint(("R",), "k")] == SourceLiteral(2.0)
    assert wf.input_sources[Endpoint(("W", "inner"), "k")] == SourceLiteral(1.0)
    assert wf.output_sources == {
        "p": SourceRef(("B",), "plate"),
        "r": SourceRef(("W", "inner"), "r"),
        "t_echo": SourceRef((), "t"),
        "t_wrapped": SourceRef((), "t"),
        "k": SourceLiteral(3.0),
    }
    assert wf.composites[("W",)].input_sources == {
        "t": SourceRef((), "t"),
        "k": SourceLiteral(1.0),
    }
    # Nothing is expanded yet, so nothing is counted and nothing is left to check.
    assert wf.iterations == {} and wf.length_checks == ()


def test_a_single_atomic_entry_names_every_port(tmp_path):
    # The degenerate single-atomic workflow: every port is a boundary port, the Pure
    # Data ones too -- and the per-kind fields say the same (0.12.2 made them record
    # the Pure Data ports, which they used to leave out).
    doc = tmp_path / "single.yaml"
    doc.write_text(
        'spec_version: "0.0"\n'
        "types: {Plate: {domain: object}}\n"
        "processes:\n"
        "  main:\n"
        "    kind: atomic\n"
        "    inputs: {plate: {type: Plate, phase: data}, t: {type: Float, phase: run}}\n"
        "    outputs: {plate: {type: Plate, phase: data}, r: {type: Float, phase: data}}\n"
        "    objects: {map: {outputs.plate: inputs.plate}}\n"
        "entry: main\n",
        encoding="utf-8",
    )
    wf, _ = parse_workflow(doc)
    assert wf is not None
    assert wf.input_sources == {
        Endpoint(("main",), "plate"): SourceRef((), "plate"),
        Endpoint(("main",), "t"): SourceRef((), "t"),
    }
    assert wf.output_sources == {
        "plate": SourceRef(("main",), "plate"),
        "r": SourceRef(("main",), "r"),
    }
    assert wf.input_sources == _input_sources_from_old_fields(wf)
