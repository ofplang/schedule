"""Pure Data ports are routed by their type, and every port has a source.

Two defects had the same root: the flattener decided whether a binding moves an
Object by the section it is written under (`state` / `bind`) instead of by the
port's type, and the single-atomic entry recorded only its Object-bearing ports.
Valid documents were then planned wrongly or handed to the runner incomplete,
without a word. These tests pin the type-based reading and the invariant that
catches the whole class: spec v0 §11 binds every input port exactly once and
defines no default, so after flattening every input of every atomic activity has a
source, and so does every final output (an Object-bearing pass-through excepted,
which is out of scope).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ofplang.schedule import schedule
from ofplang.schedule.scheduler.model import Arc, Endpoint, Workflow
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# `limit` (Int) is an entry input written under `state`; `t` (Float) is passed
# between two atomics under `state`; `k` is a literal under `state`. All three are
# Pure Data, whatever section they were written in.
_STATE_WIRED = """\
spec_version: "0.0"
types:
  Plate: {domain: object}
processes:
  heat:
    kind: atomic
    inputs:
      plate: {type: Plate, phase: data}
      limit: {type: Int, phase: data}
      k: {type: Float, phase: data}
    outputs:
      plate: {type: Plate, phase: data}
      t: {type: Float, phase: data}
    objects: {map: {outputs.plate: inputs.plate}}
  cool:
    kind: atomic
    inputs:
      plate: {type: Plate, phase: data}
      t: {type: Float, phase: data}
    outputs:
      plate: {type: Plate, phase: data}
    objects: {map: {outputs.plate: inputs.plate}}
  main:
    kind: composite
    inputs:
      sample: {type: Plate, phase: data}
      limit: {type: Int, phase: data}
    outputs:
      result: {type: Plate, phase: data}
    body:
      nodes:
        - id: Heat
          process: heat
          state:
            plate: {from: inputs.sample}
            limit: {from: inputs.limit}
            k: {value: 2.5}
        - id: Cool
          process: cool
          state:
            plate: {from: Heat.plate}
            t: {from: Heat.t}
      returns:
        result: {from: Cool.plate}
entry: main
"""

_ENV = """
time: { unit: second }
devices:
  - { id: loader, spots: [stage] }
  - { id: heater, spots: [stage] }
  - { id: chiller, spots: [stage] }
transporters: [{ id: arm }]
transports:
  - { transporter: arm, from: loader.stage, to: heater.stage, duration: 2 }
  - { transporter: arm, from: heater.stage, to: chiller.stage, duration: 2 }
processes:
  heat:
    modes:
      - devices: [heater]
        duration: 5
        input_spots:  { plate: heater.stage }
        output_spots: { plate: heater.stage }
  cool:
    modes:
      - devices: [chiller]
        duration: 3
        input_spots:  { plate: chiller.stage }
        output_spots: { plate: chiller.stage }
"""

_SINGLE_ATOMIC = """\
spec_version: "0.0"
types: {Plate: {domain: object}}
processes:
  main:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}, t: {type: Float, phase: run}}
    outputs: {plate: {type: Plate, phase: data}, r: {type: Float, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
entry: main
"""


def _parse(tmp_path, text, name="wf.yaml") -> Workflow:
    doc = tmp_path / name
    doc.write_text(text, encoding="utf-8")
    wf, diags = parse_workflow(doc)
    assert wf is not None, [d.code for d in diags.items]
    return wf


# --- routed by type, not by section ------------------------------------------------


def test_pure_data_under_state_is_routed_as_pure_data(tmp_path):
    wf = _parse(tmp_path, _STATE_WIRED)
    # The entry input is a Pure Data boundary input, not an Object one.
    assert "limit" not in wf.entry_inputs
    assert Arc(Endpoint((), "limit"), Endpoint(("Heat",), "limit")) in wf.data_arcs
    # The Heat.t -> Cool.t connection is a data arc, not an Object arc (which would
    # have become a transport), and still orders the two.
    assert Arc(Endpoint(("Heat",), "t"), Endpoint(("Cool",), "t")) in wf.data_arcs
    assert wf.arcs == (Arc(Endpoint(("Heat",), "plate"), Endpoint(("Cool",), "plate")),)
    assert (("Heat",), ("Cool",)) in wf.precedence
    # The literal is kept, where reading by section used to drop it.
    assert wf.data_literals == {Endpoint(("Heat",), "k"): 2.5}


def test_pure_data_under_state_needs_no_spot(tmp_path):
    # Reading by section, `limit` became an Object boundary input, and the plan was
    # refused for not placing it on a spot ("is Object-bearing and must be bound").
    # Placing it is refused too, as a Pure Data port, so the document could not be
    # planned at all.
    report = schedule(
        yaml.safe_load(_STATE_WIRED),
        yaml.safe_load(_ENV),
        document_path={"interface": {"inputs": {"sample": "loader.stage"}}, "activities": []},
        random_seed=0,
    )
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]
    assert report.makespan == 2 + 5 + 2 + 3


# --- the single-atomic entry -----------------------------------------------------------


def test_a_single_atomic_entry_records_its_pure_data_ports(tmp_path):
    wf = _parse(tmp_path, _SINGLE_ATOMIC)
    path = ("main",)
    assert wf.entry_inputs == {"plate": Endpoint(path, "plate")}
    assert wf.data_arcs == (Arc(Endpoint((), "t"), Endpoint(path, "t")),)
    assert wf.data_entry_inputs == {"t": Endpoint(path, "t")}
    assert wf.exit_outputs == {"plate": Endpoint(path, "plate"), "r": Endpoint(path, "r")}


# --- the invariant ----------------------------------------------------------------------


def _uncovered(wf: Workflow) -> list[str]:
    """Every input port of every activity, and every final output, that the
    runner-facing fields give no source for (an Object pass-through excepted)."""
    fed = {arc.dst for arc in wf.arcs + wf.data_arcs}
    fed.update(wf.entry_inputs.values())
    fed.update(wf.data_entry_inputs.values())
    fed.update(wf.data_literals)
    missing = [
        f"{'/'.join(map(str, a.path))}.{p.name}"
        for a in wf.activities
        for p in wf.processes[a.process].inputs
        if Endpoint(a.path, p.name) not in fed
    ]
    missing += [
        f"output {name}"
        for name, object_bearing in wf.exit_output_ports.items()
        if name not in wf.exit_outputs and name not in wf.exit_literals and not object_bearing
    ]
    return missing


def _uncovered_sources(wf: Workflow) -> list[str]:
    """The same invariant on the `Source` trees, which -- unlike the per-kind fields --
    can say where a gathered Array or one element of an Array comes from."""
    missing = [
        f"{'/'.join(map(str, a.path))}.{p.name}"
        for a in wf.activities
        for p in wf.processes[a.process].inputs
        if Endpoint(a.path, p.name) not in wf.input_sources
    ]
    missing += [
        f"output {name}"
        for name, object_bearing in wf.exit_output_ports.items()
        if name not in wf.output_sources and not object_bearing
    ]
    return missing


def _example_interface(path: Path):
    """The example's own `interface`, where it has a document: a workflow that
    traverses an Array of Objects at its boundary takes its length from there."""
    document = path.with_name(path.name.replace(".workflow.yaml", ".document.yaml"))
    if not document.is_file():
        return None
    return (yaml.safe_load(document.read_text(encoding="utf-8")) or {}).get("interface")


def test_every_port_has_a_source(tmp_path):
    workflows = {
        path.name: parse_workflow(path, interface=_example_interface(path))[0]
        for path in [*sorted(EXAMPLES.glob("*.workflow.yaml")),
                     EXAMPLES / "outputs" / "plate_batch.workflow.yaml"]
    }
    workflows["state_wired"] = _parse(tmp_path, _STATE_WIRED, "state_wired.yaml")
    workflows["single_atomic"] = _parse(tmp_path, _SINGLE_ATOMIC, "single_atomic.yaml")
    for name, wf in workflows.items():
        assert wf is not None, name
        # The Source trees are the complete record for every workflow. The per-kind
        # fields are too, wherever no map / fold gathered an Array they cannot hold.
        assert _uncovered_sources(wf) == [], name
        if not wf.iterations:
            assert _uncovered(wf) == [], name
