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


def test_parse_accepts_an_in_memory_document():
    # `parse_workflow` accepts an already-loaded document (a mapping), not only a path,
    # so a caller holding the workflow in memory need not round-trip it through a file.
    import yaml

    doc = yaml.safe_load((EXAMPLES / "simple.workflow.yaml").read_text(encoding="utf-8"))
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)
    assert wf is not None
    # Same result as parsing the file.
    from_path, _ = parse_workflow(EXAMPLES / "simple.workflow.yaml")
    assert {a.path for a in wf.activities} == {a.path for a in from_path.activities}
    assert wf.arcs == from_path.arcs


def test_parse_reformatter():
    wf, diags = parse_workflow(EXAMPLES / "reformatter.workflow.yaml")
    assert not _errors(diags)
    assert wf is not None

    assert len(wf.activities) == 8
    assert len(wf.arcs) == 12

    # A representative arc: Preparation feeds Reformatter12.
    assert (
        Arc(Endpoint(("Preparation",), "prep_out_rf12"), Endpoint(("Reformatter12",), "rf12_in"))
        in wf.arcs
    )
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


# --- Pure Data port-level dataflow (D26-0, for the ofplang-run runner) ---------
#
# `bind` bindings are Pure Data: the scheduler keeps only a node-level precedence
# edge, but the flattener also records the port-level output->input mapping in
# `data_arcs` / `data_entry_inputs` for the runner to route Pure Data values
# along. These must not affect the Object-bearing `arcs` / `entry_inputs` or the
# plan; they are additive metadata only.

_PURE_DATA = """\
spec_version: "0.0"
types:
  Sample: {domain: object}
  Reading: {domain: data}
processes:
  measure:
    kind: atomic
    inputs: {plate: {type: Sample, phase: data}}
    outputs:
      plate_out: {type: Sample, phase: data}
      reading: {type: Reading, phase: data}
    objects: {transform: [inputs.plate, outputs.plate_out]}
  analyze:
    kind: atomic
    inputs:
      reading: {type: Reading, phase: data}
      cfg: {type: Reading, phase: data}
    outputs: {score: {type: Reading, phase: data}}
  main:
    kind: composite
    inputs:
      sample: {type: Sample, phase: data}
      config: {type: Reading, phase: data}
    body:
      nodes:
        - {id: M, process: measure, state: {plate: {from: inputs.sample}}}
        - id: A
          process: analyze
          bind: {reading: {from: M.reading}, cfg: {from: inputs.config}}
      returns: {}
entry: main
"""


def test_pure_data_arcs_are_recorded_separately(tmp_path):
    doc = tmp_path / "pure_data.yaml"
    doc.write_text(_PURE_DATA, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)
    assert wf is not None

    # The Pure Data `bind` from M.reading -> A.reading is a port-level data arc,
    # NOT an Object-bearing `arc` (which stays empty: sample enters as a boundary
    # input, so there is no in-body Object arc here). The entry input `config` bound
    # into A.cfg is a data arc too, from the boundary node `()`.
    assert wf.data_arcs == (
        Arc(Endpoint(("M",), "reading"), Endpoint(("A",), "reading")),
        Arc(Endpoint((), "config"), Endpoint(("A",), "cfg")),
    )
    assert wf.arcs == ()
    # A Pure Data entry input (config) bound into A.cfg is recorded as a Pure Data
    # boundary, kept apart from the Object-bearing `entry_inputs` (sample -> M.plate).
    assert wf.data_entry_inputs == {"config": Endpoint(("A",), "cfg")}
    assert wf.entry_inputs == {"sample": Endpoint(("M",), "plate")}
    # The precedence edge still exists (the solver's view of the same dependency).
    assert (("M",), ("A",)) in wf.precedence


# A Pure Data `bind` spliced across a composite boundary: main binds the analyzer's
# `a_in` from M.reading, and the analyzer's inner atomic binds its `reading` from
# `inputs.a_in`. The flattened data arc must connect M straight to the inner atomic.
_PURE_DATA_NESTED = """\
spec_version: "0.0"
types:
  Sample: {domain: object}
  Reading: {domain: data}
processes:
  measure:
    kind: atomic
    inputs: {plate: {type: Sample, phase: data}}
    outputs:
      plate_out: {type: Sample, phase: data}
      reading: {type: Reading, phase: data}
    objects: {transform: [inputs.plate, outputs.plate_out]}
  analyze:
    kind: atomic
    inputs: {reading: {type: Reading, phase: data}}
    outputs: {score: {type: Reading, phase: data}}
  analyzer:
    kind: composite
    inputs: {a_in: {type: Reading, phase: data}}
    outputs: {a_out: {type: Reading, phase: data}}
    body:
      nodes:
        - {id: A, process: analyze, bind: {reading: {from: inputs.a_in}}}
      returns: {a_out: {from: A.score}}
  main:
    kind: composite
    inputs: {sample: {type: Sample, phase: data}}
    body:
      nodes:
        - {id: M, process: measure, state: {plate: {from: inputs.sample}}}
        - {id: Az, process: analyzer, bind: {a_in: {from: M.reading}}}
      returns: {}
entry: main
"""


def test_pure_data_arc_spliced_across_composite_boundary(tmp_path):
    doc = tmp_path / "pure_data_nested.yaml"
    doc.write_text(_PURE_DATA_NESTED, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)
    assert wf is not None

    # The inner atomic gains a qualified path; the Pure Data arc is spliced across
    # the analyzer boundary from M straight to Az/A.
    assert {a.path for a in wf.activities} == {("M",), ("Az", "A")}
    assert wf.data_arcs == (
        Arc(Endpoint(("M",), "reading"), Endpoint(("Az", "A"), "reading")),
    )
    assert (("M",), ("Az", "A")) in wf.precedence


# Static literal `bind` values (`bind: {port: {value: ...}}`, §11) are Pure Data
# constants with no in-body producer. The flattener records them in `data_literals`
# (keyed by the consuming atomic input) for the runner to seed; they add no arc,
# data_arc, or precedence, and the scheduler never reads them.

_LITERAL = """\
spec_version: "0.0"
types:
  Reading: {domain: data}
processes:
  source:
    kind: atomic
    outputs: {reading: {type: Reading, phase: data}}
  analyze:
    kind: atomic
    inputs:
      reading: {type: Reading, phase: data}
      cfg: {type: Int, phase: data}
    outputs: {score: {type: Reading, phase: data}}
  main:
    kind: composite
    inputs: {}
    body:
      nodes:
        - {id: S, process: source}
        - id: A
          process: analyze
          bind:
            reading: {from: S.reading}
            cfg: {value: 3}
      returns: {}
entry: main
"""


def test_static_literal_is_recorded_separately(tmp_path):
    doc = tmp_path / "literal.yaml"
    doc.write_text(_LITERAL, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)
    assert wf is not None

    # The literal `cfg: {value: 3}` is recorded against the consuming atomic input.
    assert wf.data_literals == {Endpoint(("A",), "cfg"): 3}
    # It adds no data_arc (that is only for producer->consumer bindings) and no
    # precedence edge (a constant imposes no ordering). The `from` bind still does.
    assert wf.data_arcs == (Arc(Endpoint(("S",), "reading"), Endpoint(("A",), "reading")),)
    assert (("S",), ("A",)) in wf.precedence
    assert wf.arcs == ()


# A literal bound into a composite invocation must reach the inner atomic that
# ultimately consumes it -- the `_Literal` marker propagates through the composite
# input environment, just like an entry-input marker.
_LITERAL_NESTED = """\
spec_version: "0.0"
processes:
  compute:
    kind: atomic
    inputs: {cfg: {type: Int, phase: data}}
    outputs: {out: {type: Int, phase: data}}
  wrap:
    kind: composite
    inputs: {w_in: {type: Int, phase: data}}
    outputs: {w_out: {type: Int, phase: data}}
    body:
      nodes:
        - {id: A, process: compute, bind: {cfg: {from: inputs.w_in}}}
      returns: {w_out: {from: A.out}}
  main:
    kind: composite
    inputs: {}
    body:
      nodes:
        - {id: W, process: wrap, bind: {w_in: {value: 7}}}
      returns: {}
entry: main
"""


def test_static_literal_spliced_across_composite_boundary(tmp_path):
    doc = tmp_path / "literal_nested.yaml"
    doc.write_text(_LITERAL_NESTED, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)
    assert wf is not None

    # The literal supplied to the composite's `w_in` reaches the inner atomic's `cfg`
    # at its qualified path -- the marker propagated across the composite boundary.
    assert {a.path for a in wf.activities} == {("W", "A")}
    assert wf.data_literals == {Endpoint(("W", "A"), "cfg"): 7}
    assert wf.data_arcs == ()


# The Pure Data boundary shapes that used to vanish from what the runner was handed:
#  - the entry input `t` feeds three atomics (A and B directly, and W.inner through a
#    composite), where a one-consumer map could hold only one of them;
#  - `t_echo` returns `t` verbatim, and `t_wrapped` returns it through a composite
#    that passes it straight back -- both pass-throughs with no producing activity;
#  - `k` returns the literal a nested composite was bound to.
# `p_echo` is the Object-bearing counterpart of the pass-through, which stays out of
# scope and so out of `exit_outputs`.
_PURE_DATA_BOUNDARY = """\
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
    objects: {map: {outputs.plate: inputs.plate}}
  read:
    kind: atomic
    inputs: {t: {type: Float, phase: run}}
    outputs: {r: {type: Float, phase: data}}
  wrap:
    kind: composite
    inputs: {t: {type: Float, phase: run}}
    outputs: {r: {type: Float, phase: data}}
    body:
      nodes:
        - {id: inner, process: read, bind: {t: {from: inputs.t}}}
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
      q: {type: Plate, phase: data}
      t: {type: Float, phase: run}
    outputs:
      p: {type: Plate, phase: data}
      p_echo: {type: Plate, phase: data}
      r: {type: Float, phase: data}
      t_echo: {type: Float, phase: run}
      t_wrapped: {type: Float, phase: run}
      k: {type: Float, phase: run}
    body:
      nodes:
        - {id: A, process: heat, state: {plate: {from: inputs.p}}, bind: {t: {from: inputs.t}}}
        - {id: B, process: heat, state: {plate: {from: A.plate}}, bind: {t: {from: inputs.t}}}
        - {id: W, process: wrap, bind: {t: {from: inputs.t}}}
        - {id: E, process: echo, bind: {k: {from: inputs.t}}}
        - {id: C, process: echo, bind: {k: {value: 3.0}}}
      returns:
        p: {from: B.plate}
        p_echo: {from: inputs.q}
        r: {from: W.r}
        t_echo: {from: inputs.t}
        t_wrapped: {from: E.k}
        k: {from: C.k}
entry: main
"""


def test_pure_data_entry_input_reaches_every_consumer(tmp_path):
    doc = tmp_path / "boundary.yaml"
    doc.write_text(_PURE_DATA_BOUNDARY, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)
    assert wf is not None

    # One boundary data arc per consuming atomic, including the one reached through
    # a composite. A runner that inverts `data_arcs` finds a source for every one.
    boundary_arcs = {arc for arc in wf.data_arcs if arc.src == Endpoint((), "t")}
    assert boundary_arcs == {
        Arc(Endpoint((), "t"), Endpoint(("A",), "t")),
        Arc(Endpoint((), "t"), Endpoint(("B",), "t")),
        Arc(Endpoint((), "t"), Endpoint(("W", "inner"), "t")),
    }
    # The one-consumer map is kept as it was, for its existing readers: it can name
    # only one of the three.
    assert wf.data_entry_inputs["t"] in {arc.dst for arc in boundary_arcs}


def test_pure_data_pass_through_and_literal_returns_are_recorded(tmp_path):
    doc = tmp_path / "boundary.yaml"
    doc.write_text(_PURE_DATA_BOUNDARY, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)
    assert wf is not None

    # A Pure Data pass-through -- direct, or through a composite that returns its
    # input -- is produced by the boundary node `()`, where the entry input is seeded.
    assert wf.exit_outputs["t_echo"] == Endpoint((), "t")
    assert wf.exit_outputs["t_wrapped"] == Endpoint((), "t")
    # A return with no producer at all: the literal a nested composite was bound to.
    assert wf.exit_literals == {"k": 3.0}
    assert "k" not in wf.exit_outputs
    # The ordinary returns are unchanged, and an Object pass-through stays out.
    assert wf.exit_outputs["p"] == Endpoint(("B",), "plate")
    assert wf.exit_outputs["r"] == Endpoint(("W", "inner"), "r")
    assert "p_echo" not in wf.exit_outputs


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
        "  make: {kind: atomic, outputs: {cup: {type: Cup, phase: data}}, "
        "objects: {create: [outputs.cup]}}\n"
        "  main:\n"
        "    kind: composite\n"
        "    body:\n"
        "      nodes:\n"
        "        - {id: m, kind: do_while, process: make, max_iterations: {value: 3}}\n"
        "entry: main\n",
        encoding="utf-8",
    )
    wf, diags = parse_workflow(doc)
    codes = {d.code for d in _errors(diags)}
    # `map` / `fold` are expanded (design.md D57); `do_while` and `branch` are not.
    assert "unsupported_feature" in codes


def test_generic_process_is_unsupported(tmp_path):
    # A generic process (type_params) is valid v0 but out of scope for the
    # scheduler: reject it cleanly (spec 4.4) instead of misreading a generic
    # Object port as Pure Data and dropping its transport arc.
    doc = tmp_path / "wf.yaml"
    doc.write_text(
        "types: {Plate: {domain: object}}\n"
        "processes:\n"
        "  make: {kind: atomic, outputs: {plate: {type: Plate, phase: data}}, "
        "objects: {create: [outputs.plate]}}\n"
        "  wash:\n"
        "    kind: atomic\n"
        "    type_params: {O: {domain: object}}\n"
        "    inputs: {item: {type: O, phase: data}}\n"
        "    outputs: {item: {type: O, phase: data}}\n"
        "    objects: {map: {outputs.item: inputs.item}}\n"
        "  main:\n"
        "    kind: composite\n"
        "    body:\n"
        "      nodes:\n"
        "        - {id: mk, process: make}\n"
        "        - {id: w, process: wash, state: {item: {from: mk.plate}}}\n"
        "      returns: {}\n"
        "entry: main\n",
        encoding="utf-8",
    )
    wf, diags = parse_workflow(doc)
    assert wf is None  # not silently mis-scheduled
    assert "unsupported_feature" in {d.code for d in _errors(diags)}


def test_import_is_unsupported(tmp_path):
    # An unexpanded `$import` is valid v0 (resolved before feature derivation),
    # but the scheduler does not resolve imports: reject rather than silently
    # ignore the key and mis-read the graph.
    doc = tmp_path / "wf.yaml"
    doc.write_text(
        "processes:\n"
        "  $import: shared.yaml\n"
        "  main: {kind: composite, body: {nodes: [], returns: {}}}\n"
        "entry: main\n",
        encoding="utf-8",
    )
    wf, diags = parse_workflow(doc)
    assert wf is None
    assert "unsupported_feature" in {d.code for d in _errors(diags)}


# A nested composite invocation's value-layer boundary is recorded in `composites`
# for the runner's composite contract checks (D34). It maps each of the composite's
# own input / output ports to the value-store key that supplies it -- a producing
# atomic, the workflow boundary `((), name)`, or a static literal -- even though the
# composite itself is flattened away. Value-independent metadata: the scheduler
# never reads it, so the plan is unaffected (the rest of the suite pins that).
_COMPOSITE_IO = """
processes:
  add:
    kind: atomic
    inputs: {x: {type: Int, phase: data}, y: {type: Int, phase: data}}
    outputs: {s: {type: Int, phase: data}}
  wrap:
    kind: composite
    inputs: {base: {type: Int, phase: data}, cfg: {type: Int, phase: data}}
    outputs: {out: {type: Int, phase: data}}
    body:
      nodes:
        - id: A
          process: add
          bind: {x: {from: inputs.base}, y: {from: inputs.cfg}}
      returns: {out: {from: A.s}}
  main:
    kind: composite
    inputs: {a: {type: Int, phase: data}}
    outputs: {r: {type: Int, phase: data}}
    body:
      nodes:
        - id: W
          process: wrap
          bind: {base: {from: inputs.a}, cfg: {value: 5}}
      returns: {r: {from: W.out}}
entry: main
"""


def test_nested_composite_boundary_is_recorded(tmp_path):
    doc = tmp_path / "composite_io.yaml"
    doc.write_text(_COMPOSITE_IO, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)

    # Only the nested composite `W` is recorded (the entry `main` `()` is omitted --
    # the runner checks it via its whole-workflow handles, D33).
    assert set(wf.composites) == {("W",)}
    io = wf.composites[("W",)]
    assert io.process == "wrap"
    # `base` comes from the workflow boundary input `a`; `cfg` is a static literal.
    assert io.inputs == {"base": Endpoint((), "a")}
    assert io.input_literals == {"cfg": 5}
    # `out` is produced by the inner atomic `A` (path matches the plan's activity).
    assert io.outputs == {"out": Endpoint(("W", "A"), "s")}
    assert io.output_literals == {}
    # The recorded output endpoint is an actual activity path.
    assert ("W", "A") in {a.path for a in wf.activities}


# A unit-annotated numeric port (v0 §28). The reader needs no change for it: a
# suffix sits only on `Int` or `Float` (§28.2), both Pure Data, so every verdict
# it reaches is the one it would reach without the suffix. That is a property of
# where units are allowed, not of how `_object_bearing` is written, so it is
# pinned here rather than left to hold by accident.
_UNIT_ANNOTATED = """\
spec_version: "0.3"
units:
  s: {}
  uL: {}
types:
  Sample: {domain: object}
processes:
  measure:
    kind: atomic
    inputs:
      plate: {type: Sample, phase: data}
      duration: {type: "Float[s]", phase: data}
    outputs:
      plate_out: {type: Sample, phase: data}
      volumes: {type: "Array<Float[uL]>", phase: data}
    objects: {transform: [inputs.plate, outputs.plate_out]}
  main:
    kind: composite
    inputs:
      sample: {type: Sample, phase: data}
      t: {type: "Float[s]", phase: data}
    outputs:
      sample_out: {type: Sample, phase: data}
    body:
      nodes:
        - id: M
          process: measure
          state: {plate: {from: inputs.sample}}
          bind: {duration: {from: inputs.t}}
      returns:
        sample_out: {from: M.plate_out}
entry: main
"""


def test_a_unit_annotated_port_is_pure_data(tmp_path):
    doc = tmp_path / "units.yaml"
    doc.write_text(_UNIT_ANNOTATED, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert not _errors(diags)
    assert wf is not None

    # The Object-bearing boundary is the Sample alone: neither Float[s] nor
    # Array<Float[uL]> is Object-bearing (§5.2), so the unit-annotated entry
    # input is recorded as Pure Data.
    assert wf.entry_inputs == {"sample": Endpoint(("M",), "plate")}
    assert wf.data_entry_inputs == {"t": Endpoint(("M",), "duration")}


def test_object_bearing_reads_a_unit_annotated_type_as_pure_data():
    # Directly, since the verdict above rests on this one function.
    from ofplang.schedule.scheduler.workflow import _object_bearing

    domains = {"Sample": "object", "Reading": "data"}
    for expr in ("Float[s]", "Int[count_]", "Array<Float[uL]>", "Float[mg/mL]", "Float[1]"):
        assert not _object_bearing(expr, domains), expr
    assert _object_bearing("Sample", domains)
    assert _object_bearing("Array<Sample>", domains)
