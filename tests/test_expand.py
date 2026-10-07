"""Expanding `map` and `fold` before planning (design.md D57).

A `map` / `fold` node becomes its invocations, invocation `i` under the path
`node_id, i`. How many there are is the length of the `each` sources, known before the
run: an Array of Objects bound in `interface` (as many elements as spots), a literal,
or another map / fold's output. These tests read the expansion -- activities, arcs,
precedence, what the runner is told -- and plan, render and replan the result.

Every workflow here is valid v0 (checked with ofplang-validate when written).
"""

from __future__ import annotations

import copy

import yaml

from ofplang.schedule import schedule, validate_document
from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.model import (
    Arc,
    Endpoint,
    LengthCheck,
    SourceLiteral,
    SourceRef,
    SourceSeq,
)
from ofplang.schedule.scheduler.workflow import fingerprint, parse_workflow

MAP_HEAT = """\
spec_version: "0.4"
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
  main:
    kind: composite
    inputs:
      plates: {type: "Array<Plate>", phase: data}
      t: {type: Float, phase: run}
    outputs:
      plates: {type: "Array<Plate>", phase: data}
    body:
      nodes:
        - id: Heat
          kind: map
          process: heat
          each: {plate: {from: inputs.plates}}
          bind: {t: {from: inputs.t}}
      returns:
        plates: {from: Heat.plate}
entry: main
"""

# A composite target: heat then cool, per plate.
MAP_COMPOSITE = """\
spec_version: "0.4"
types:
  Plate: {domain: object}
processes:
  heat:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  cool:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  treat:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    body:
      nodes:
        - {id: h, process: heat, state: {plate: {from: inputs.plate}}}
        - {id: c, process: cool, state: {plate: {from: h.plate}}}
      returns: {plate: {from: c.plate}}
  main:
    kind: composite
    inputs: {plates: {type: "Array<Plate>", phase: data}}
    outputs: {plates: {type: "Array<Plate>", phase: data}}
    body:
      nodes:
        - {id: Each, kind: map, process: treat, each: {plate: {from: inputs.plates}}}
      returns: {plates: {from: Each.plate}}
entry: main
"""

# Nested: a rack of rows of plates, a map over rows whose target maps over plates.
MAP_NESTED = """\
spec_version: "0.4"
types:
  Plate: {domain: object}
processes:
  heat:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  row:
    kind: composite
    inputs: {plates: {type: "Array<Plate>", phase: data}}
    outputs: {plates: {type: "Array<Plate>", phase: data}}
    body:
      nodes:
        - {id: Inner, kind: map, process: heat, each: {plate: {from: inputs.plates}}}
      returns: {plates: {from: Inner.plate}}
  main:
    kind: composite
    inputs: {rows: {type: "Array<Array<Plate>>", phase: data}}
    outputs: {rows: {type: "Array<Array<Plate>>", phase: data}}
    body:
      nodes:
        - {id: Outer, kind: map, process: row, each: {plates: {from: inputs.rows}}}
      returns: {rows: {from: Outer.plates}}
entry: main
"""

# Cups created from a literal list of labels: L comes from the literal.
MAP_LITERAL = """\
spec_version: "0.4"
types:
  Cup: {domain: object}
processes:
  make:
    kind: atomic
    inputs: {label: {type: String, phase: run}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {create: [outputs.cup]}
  main:
    kind: composite
    outputs: {cups: {type: "Array<Cup>", phase: data}}
    body:
      nodes:
        - id: Make
          kind: map
          process: make
          each: {label: {value: ["a", "b", "c"]}}
      returns: {cups: {from: Make.cup}}
entry: main
"""

# A run-phase list of temperatures zipped with the plates: its length is a value.
MAP_ZIP = """\
spec_version: "0.4"
types:
  Plate: {domain: object}
processes:
  heat:
    kind: atomic
    inputs:
      plate: {type: Plate, phase: data}
      t: {type: Float, phase: run}
    outputs: {plate: {type: Plate, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  main:
    kind: composite
    inputs:
      plates: {type: "Array<Plate>", phase: data}
      temps: {type: "Array<Float>", phase: run}
    outputs: {plates: {type: "Array<Plate>", phase: data}}
    body:
      nodes:
        - id: Heat
          kind: map
          process: heat
          each:
            plate: {from: inputs.plates}
            t: {from: inputs.temps}
      returns: {plates: {from: Heat.plate}}
entry: main
"""

# Only a run-phase Pure Data list: nothing the scheduler sees gives L.
MAP_DATA_ONLY = """\
spec_version: "0.4"
types:
  Cup: {domain: object}
processes:
  make:
    kind: atomic
    inputs: {label: {type: String, phase: run}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {create: [outputs.cup]}
  main:
    kind: composite
    inputs: {labels: {type: "Array<String>", phase: run}}
    outputs: {cups: {type: "Array<Cup>", phase: data}}
    body:
      nodes:
        - {id: Make, kind: map, process: make, each: {label: {from: inputs.labels}}}
      returns: {cups: {from: Make.cup}}
entry: main
"""

# One reagent container dispensed into each plate in turn (spec 21.0's example), and
# a running count carried as Pure Data.
FOLD_DISPENSE = """\
spec_version: "0.4"
types:
  Plate: {domain: object}
  Reagent: {domain: object}
processes:
  dispense:
    kind: atomic
    inputs:
      reagent: {type: Reagent, phase: data}
      plate: {type: Plate, phase: data}
      count: {type: Int, phase: data}
    outputs:
      reagent: {type: Reagent, phase: data}
      plate: {type: Plate, phase: data}
      count: {type: Int, phase: data}
    objects:
      map:
        outputs.reagent: inputs.reagent
        outputs.plate: inputs.plate
  main:
    kind: composite
    inputs:
      reagent: {type: Reagent, phase: data}
      plates: {type: "Array<Plate>", phase: data}
    outputs:
      reagent: {type: Reagent, phase: data}
      plates: {type: "Array<Plate>", phase: data}
      count: {type: Int, phase: data}
    body:
      nodes:
        - id: Run
          kind: fold
          process: dispense
          carry:
            reagent: {from: inputs.reagent}
            count: {value: 0}
          each:
            plate: {from: inputs.plates}
          outputs:
            reagent: {mode: carry}
            count: {mode: carry}
            plate: {mode: collect}
      returns:
        reagent: {from: Run.reagent}
        plates: {from: Run.plate}
        count: {from: Run.count}
entry: main
"""

# An atomic process with an Array of Objects as a port: refused in this stage.
ARRAY_PORT = """\
spec_version: "0.4"
types:
  Plate: {domain: object}
processes:
  stack:
    kind: atomic
    inputs: {plates: {type: "Array<Plate>", phase: data}}
    outputs: {plates: {type: "Array<Plate>", phase: data}}
    objects: {map: {outputs.plates: inputs.plates}}
  main:
    kind: composite
    inputs: {plates: {type: "Array<Plate>", phase: data}}
    outputs: {plates: {type: "Array<Plate>", phase: data}}
    body:
      nodes:
        - {id: S, process: stack, state: {plates: {from: inputs.plates}}}
      returns: {plates: {from: S.plates}}
entry: main
"""

# A map's Pure Data output gathered and read whole by a later step.
MAP_GATHER = """\
spec_version: "0.4"
types:
  Plate: {domain: object}
processes:
  measure:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}}
    outputs:
      plate: {type: Plate, phase: data}
      od: {type: Float, phase: data}
    objects: {map: {outputs.plate: inputs.plate}}
  summarize:
    kind: atomic
    inputs: {ods: {type: "Array<Float>", phase: data}}
    outputs: {mean: {type: Float, phase: data}}
  main:
    kind: composite
    inputs: {plates: {type: "Array<Plate>", phase: data}}
    outputs:
      plates: {type: "Array<Plate>", phase: data}
      mean: {type: Float, phase: data}
    body:
      nodes:
        - {id: M, kind: map, process: measure, each: {plate: {from: inputs.plates}}}
        - {id: S, process: summarize, bind: {ods: {from: M.od}}}
      returns:
        plates: {from: M.plate}
        mean: {from: S.mean}
entry: main
"""


ENV = """
time: { unit: second }
devices:
  - { id: loader, spots: [a, b, c, d] }
  - { id: heater, spots: [stage] }
  - { id: chiller, spots: [stage] }
  - { id: dispenser, spots: [stage, reagent] }
  - { id: reader, spots: [stage] }
  - { id: maker, spots: [out] }
  - { id: shelf, spots: [r] }
  - { id: output, spots: [a, b, c, d] }
transporters: [{ id: arm }]
transports:
  - { transporter: arm, from: loader.a, to: heater.stage, duration: 2 }
  - { transporter: arm, from: loader.b, to: heater.stage, duration: 2 }
  - { transporter: arm, from: loader.c, to: heater.stage, duration: 2 }
  - { transporter: arm, from: loader.d, to: heater.stage, duration: 2 }
  - { transporter: arm, from: loader.a, to: dispenser.stage, duration: 2 }
  - { transporter: arm, from: loader.b, to: dispenser.stage, duration: 2 }
  - { transporter: arm, from: loader.c, to: dispenser.stage, duration: 2 }
  - { transporter: arm, from: loader.a, to: reader.stage, duration: 2 }
  - { transporter: arm, from: loader.b, to: reader.stage, duration: 2 }
  - { transporter: arm, from: shelf.r, to: dispenser.reagent, duration: 2 }
  - { transporter: arm, from: dispenser.reagent, to: shelf.r, duration: 2 }
  - { transporter: arm, from: heater.stage, to: chiller.stage, duration: 2 }
  - { transporter: arm, from: heater.stage, to: output.a, duration: 2 }
  - { transporter: arm, from: heater.stage, to: output.b, duration: 2 }
  - { transporter: arm, from: heater.stage, to: output.c, duration: 2 }
  - { transporter: arm, from: heater.stage, to: output.d, duration: 2 }
  - { transporter: arm, from: chiller.stage, to: output.a, duration: 2 }
  - { transporter: arm, from: chiller.stage, to: output.b, duration: 2 }
  - { transporter: arm, from: dispenser.stage, to: output.a, duration: 2 }
  - { transporter: arm, from: dispenser.stage, to: output.b, duration: 2 }
  - { transporter: arm, from: dispenser.stage, to: output.c, duration: 2 }
  - { transporter: arm, from: reader.stage, to: output.a, duration: 2 }
  - { transporter: arm, from: reader.stage, to: output.b, duration: 2 }
  - { transporter: arm, from: maker.out, to: output.a, duration: 2 }
  - { transporter: arm, from: maker.out, to: output.b, duration: 2 }
  - { transporter: arm, from: maker.out, to: output.c, duration: 2 }
processes:
  heat:
    modes:
      - devices: [heater]
        duration: 10
        input_spots:  { plate: heater.stage }
        output_spots: { plate: heater.stage }
  cool:
    modes:
      - devices: [chiller]
        duration: 5
        input_spots:  { plate: chiller.stage }
        output_spots: { plate: chiller.stage }
  dispense:
    modes:
      - devices: [dispenser]
        duration: 4
        input_spots:  { plate: dispenser.stage, reagent: dispenser.reagent }
        output_spots: { plate: dispenser.stage, reagent: dispenser.reagent }
  measure:
    modes:
      - devices: [reader]
        duration: 3
        input_spots:  { plate: reader.stage }
        output_spots: { plate: reader.stage }
  summarize:
    modes:
      - { devices: [], duration: 1 }
  make:
    modes:
      - devices: [maker]
        duration: 2
        output_spots: { cup: maker.out }
"""


def _parse(text, interface=None):
    wf, diags = parse_workflow(yaml.safe_load(text), interface=interface)
    return wf, [d.code for d in diags.items if d.severity == ERROR]


def _schedule(text, interface):
    return schedule(yaml.safe_load(text), yaml.safe_load(ENV),
                    document_path={"interface": interface, "activities": []}, random_seed=0)


def _replan_all_done(report, text):
    """Feed a plan back as a status with everything finished: every activity of every
    invocation must be recognised (no unknown node, no unknown arc). The plan echoes the
    interface it was made with, so the replan reads the same lengths."""
    plan = copy.deepcopy(report.plan)
    for activity in plan["activities"]:
        activity["status"] = "completed"
    plan["now"] = max(a["end"] for a in plan["activities"])
    return schedule(yaml.safe_load(text), yaml.safe_load(ENV), document_path=plan,
                    random_seed=0)


PLATES3 = {"inputs": {"plates": ["loader.a", "loader.b", "loader.c"]},
           "outputs": {"plates": ["output.a", "output.b", "output.c"]}}


# --- map over an Array of Objects at the boundary ------------------------------------------


def test_a_map_is_one_invocation_per_element():
    wf, errs = _parse(MAP_HEAT, PLATES3)
    assert errs == [] and wf is not None
    assert [a.path for a in wf.activities] == [("Heat", 0), ("Heat", 1), ("Heat", 2)]
    assert wf.iterations == {("Heat",): 3}
    # Element i enters invocation i, and invocation i's plate is element i of the output.
    assert wf.entry_arcs == tuple(
        Arc(Endpoint((), "plates", (i,)), Endpoint(("Heat", i), "plate")) for i in range(3)
    )
    assert wf.exit_arcs == tuple(
        Arc(Endpoint(("Heat", i), "plate"), Endpoint((), "plates", (i,))) for i in range(3)
    )
    # The invocations are independent: no precedence between them.
    assert wf.precedence == ()
    # The runner's view: each invocation's plate is element i of the entry input, its
    # temperature the whole `t`, and the output the Array of the invocations' plates.
    assert wf.input_sources[Endpoint(("Heat", 1), "plate")] == SourceRef((), "plates", (1,))
    assert wf.input_sources[Endpoint(("Heat", 1), "t")] == SourceRef((), "t")
    assert wf.output_sources["plates"] == SourceSeq(
        tuple(SourceRef(("Heat", i), "plate") for i in range(3))
    )


def test_a_map_plans_and_replans_end_to_end():
    report = _schedule(MAP_HEAT, PLATES3)
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]
    assert validate_document(report.plan).ok
    heats = sorted((a["start"], tuple(a["node"])) for a in report.plan["activities"]
                   if a["kind"] == "processing")
    # One heater: the three heats run one after another, whatever the order.
    assert [node for _, node in heats] != [] and len(heats) == 3
    starts = [start for start, _ in heats]
    assert all(b - a >= 10 for a, b in zip(starts, starts[1:], strict=False))
    again = _replan_all_done(report, MAP_HEAT)
    assert again.ok, [d.code for d in again.diagnostics]


def test_the_list_length_is_the_graph():
    # The same workflow with a longer list is a different graph, and says so.
    two = {"inputs": {"plates": ["loader.a", "loader.b"]}}
    wf2, _ = _parse(MAP_HEAT, two)
    wf3, _ = _parse(MAP_HEAT, PLATES3)
    assert len(wf2.activities) == 2 and fingerprint(wf2) != fingerprint(wf3)


def test_an_empty_list_is_no_invocation():
    wf, errs = _parse(MAP_HEAT, {"inputs": {"plates": []}, "outputs": {"plates": []}})
    assert errs == [] and wf.activities == () and wf.iterations == {("Heat",): 0}
    assert wf.output_sources["plates"] == SourceSeq(())


def test_no_binding_is_no_length():
    _, errs = _parse(MAP_HEAT, None)
    assert errs == ["interface_input_missing"]


# --- where L comes from -------------------------------------------------------------------


def test_a_literal_gives_the_length():
    wf, errs = _parse(MAP_LITERAL)
    assert errs == [] and [a.path for a in wf.activities] == [("Make", i) for i in range(3)]
    assert wf.input_sources[Endpoint(("Make", 2), "label")] == SourceLiteral("c")
    report = _schedule(MAP_LITERAL, {"outputs": {"cups": ["output.a", "output.b", "output.c"]}})
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]


def test_a_run_phase_list_alone_gives_no_length():
    # Its length is a value, which the scheduler is not given (design.md D57).
    _, errs = _parse(MAP_DATA_ONLY)
    assert errs == ["array_length_unknown"]


def test_a_zipped_run_phase_list_is_left_for_the_runner_to_check():
    wf, errs = _parse(MAP_ZIP, {"inputs": {"plates": ["loader.a", "loader.b"]}})
    assert errs == [] and len(wf.activities) == 2
    assert wf.length_checks == (LengthCheck(("Heat",), "t", SourceRef((), "temps"), 2),)
    # Element i of the temperatures reaches invocation i, from the boundary.
    assert wf.input_sources[Endpoint(("Heat", 1), "t")] == SourceRef((), "temps", (1,))


def test_known_lengths_that_disagree_are_refused():
    # The temperatures as a literal of three, the plates a binding of two: both
    # lengths are known before the run, and they differ.
    lit = MAP_ZIP.replace("t: {from: inputs.temps}", "t: {value: [1.0, 2.0, 3.0]}")
    assert lit != MAP_ZIP
    _, errs = _parse(lit, {"inputs": {"plates": ["loader.a", "loader.b"]}})
    assert errs == ["each_length_mismatch"]


# --- composite targets and nesting -----------------------------------------------------


def test_a_composite_target_is_expanded_per_invocation():
    wf, errs = _parse(MAP_COMPOSITE, {"inputs": {"plates": ["loader.a", "loader.b"]}})
    assert errs == []
    assert [a.path for a in wf.activities] == [
        ("Each", 0, "h"), ("Each", 0, "c"), ("Each", 1, "h"), ("Each", 1, "c")
    ]
    assert Arc(Endpoint(("Each", 1, "h"), "plate"), Endpoint(("Each", 1, "c"), "plate")) in wf.arcs
    # Each invocation of the composite is a composite boundary of its own (D34).
    assert set(wf.composites) == {("Each", 0), ("Each", 1)}
    assert wf.composites[("Each", 1)].input_sources == {"plate": SourceRef((), "plates", (1,))}
    report = _schedule(MAP_COMPOSITE, {"inputs": {"plates": ["loader.a", "loader.b"]},
                                       "outputs": {"plates": ["output.a", "output.b"]}})
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]
    assert _replan_all_done(report, MAP_COMPOSITE).ok


def test_a_nested_map_follows_the_nested_list():
    rows = {"inputs": {"rows": [["loader.a", "loader.b"], ["loader.c"]]}}
    wf, errs = _parse(MAP_NESTED, rows)
    assert errs == []
    assert [a.path for a in wf.activities] == [
        ("Outer", 0, "Inner", 0), ("Outer", 0, "Inner", 1), ("Outer", 1, "Inner", 0)
    ]
    assert wf.iterations == {("Outer",): 2, ("Outer", 0, "Inner"): 2, ("Outer", 1, "Inner"): 1}
    assert Arc(Endpoint((), "rows", (0, 1)), Endpoint(("Outer", 0, "Inner", 1), "plate")) \
        in wf.entry_arcs
    assert Arc(Endpoint(("Outer", 1, "Inner", 0), "plate"), Endpoint((), "rows", (1, 0))) \
        in wf.exit_arcs
    report = _schedule(MAP_NESTED, {**rows, "outputs": {"rows": [["output.a", "output.b"],
                                                                 ["output.c"]]}})
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]


def test_a_gathered_output_read_whole_waits_for_every_invocation():
    wf, errs = _parse(MAP_GATHER, {"inputs": {"plates": ["loader.a", "loader.b"]}})
    assert errs == []
    assert {(("M", 0), ("S",)), (("M", 1), ("S",))} <= set(wf.precedence)
    assert wf.input_sources[Endpoint(("S",), "ods")] == SourceSeq(
        (SourceRef(("M", 0), "od"), SourceRef(("M", 1), "od"))
    )


# --- fold ----------------------------------------------------------------------------


def test_a_fold_threads_its_carry_and_collects():
    interface = {"inputs": {"reagent": "shelf.r", "plates": ["loader.a", "loader.b", "loader.c"]}}
    wf, errs = _parse(FOLD_DISPENSE, interface)
    assert errs == []
    run = [("Run", i) for i in range(3)]
    assert [a.path for a in wf.activities] == run
    # The container goes from the boundary into invocation 0, from each invocation into
    # the next, and out of the last; each plate goes in and out on its own.
    assert Arc(Endpoint((), "reagent"), Endpoint(run[0], "reagent")) in wf.entry_arcs
    assert [a for a in wf.arcs if a.src.port == "reagent"] == [
        Arc(Endpoint(run[0], "reagent"), Endpoint(run[1], "reagent")),
        Arc(Endpoint(run[1], "reagent"), Endpoint(run[2], "reagent")),
    ]
    assert Arc(Endpoint(run[2], "reagent"), Endpoint((), "reagent")) in wf.exit_arcs
    assert Arc(Endpoint(run[1], "plate"), Endpoint((), "plates", (1,))) in wf.exit_arcs
    # The count is carried as data: each invocation reads the last one's, and is
    # ordered after it.
    assert wf.input_sources[Endpoint(run[0], "count")] == SourceLiteral(0)
    assert wf.input_sources[Endpoint(run[1], "count")] == SourceRef(run[0], "count")
    assert (run[0], run[1]) in wf.precedence
    assert wf.output_sources["count"] == SourceRef(run[2], "count")
    report = _schedule(FOLD_DISPENSE, {**interface, "outputs": {
        "reagent": "shelf.r", "plates": ["output.a", "output.b", "output.c"]}})
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]
    assert _replan_all_done(report, FOLD_DISPENSE).ok


def test_an_empty_fold_hands_its_carry_through():
    wf, errs = _parse(FOLD_DISPENSE, {"inputs": {"reagent": "shelf.r", "plates": []}})
    assert errs == [] and wf.activities == ()
    # Nothing was invoked: the count is the literal it started as.
    assert wf.output_sources["count"] == SourceLiteral(0)
    # ... and the container is the one that came in, returned untouched (D60): it used
    # to be out of scope, and a plan with no plates was refused for it.
    assert wf.output_sources["reagent"] == SourceRef((), "reagent")
    assert wf.through_arcs == (Arc(Endpoint((), "reagent"), Endpoint((), "reagent")),)
    report = _schedule(FOLD_DISPENSE, {"inputs": {"reagent": "shelf.r", "plates": []},
                                       "outputs": {"reagent": "shelf.r"}})
    assert report.ok, [d.code for d in report.diagnostics]


# --- refused ----------------------------------------------------------------------------


def test_an_atomic_with_an_array_of_objects_is_refused():
    _, errs = _parse(ARRAY_PORT, {"inputs": {"plates": ["loader.a"]}})
    assert errs == ["unsupported_feature"]


# --- joint plans -------------------------------------------------------------------


def test_each_job_is_expanded_with_its_own_binding():
    # Two jobs of one workflow, three plates and one: each job's roster entry gives
    # its own length, so the two are different graphs -- and a replan reads them the
    # same way again, which is what lets their fingerprints be checked.
    from ofplang.schedule import JobInput, schedule_jobs

    roster = [
        {"id": "a", "interface": {"inputs": {"plates": ["loader.a", "loader.b", "loader.c"]}}},
        {"id": "b", "interface": {"inputs": {"plates": ["loader.d"]}}},
    ]
    jobs = [JobInput("a", yaml.safe_load(MAP_HEAT)), JobInput("b", yaml.safe_load(MAP_HEAT))]
    report = schedule_jobs(jobs, yaml.safe_load(ENV),
                           document_path={"jobs": roster, "activities": []}, random_seed=0)
    assert report.ok, [d.code for d in report.diagnostics]
    heats = {(a["job"], tuple(a["node"])) for a in report.plan["activities"]
             if a["kind"] == "processing"}
    assert heats == {("a", ("Heat", 0)), ("a", ("Heat", 1)), ("a", ("Heat", 2)),
                     ("b", ("Heat", 0))}
    again = schedule_jobs(jobs, yaml.safe_load(ENV), document_path=copy.deepcopy(report.plan),
                          random_seed=0)
    assert again.ok, [d.code for d in again.diagnostics]


# --- read before the document is validated (code review, 2026-10-05) ------------------
#
# A workflow is read with its `interface` before the document is validated, so a
# binding that is wrong must still come out as the document mistake it is.


def test_a_misshapen_interface_does_not_break_the_reading():
    # `inputs` that is not a mapping: no crash, and the document's own validation says
    # what is wrong with it.
    report = schedule(yaml.safe_load(MAP_HEAT), yaml.safe_load(ENV),
                      document_path={"interface": {"inputs": "loader.a"}, "activities": []})
    assert not report.ok
    assert "wrong_type" in {d.code for d in report.diagnostics}


def test_one_spot_for_an_array_of_objects_is_a_shape_mismatch():
    _, errs = _parse(MAP_HEAT, {"inputs": {"plates": "loader.a"}})
    assert errs == ["interface_shape_mismatch"]


def test_a_joint_plan_with_a_top_level_binding_is_told_so():
    from ofplang.schedule import JobInput, schedule_jobs

    report = schedule_jobs([JobInput("a", yaml.safe_load(MAP_HEAT))], yaml.safe_load(ENV),
                           document_path={"interface": PLATES3, "activities": []})
    assert "multi_job_interface" in {d.code for d in report.diagnostics}
    assert "interface_input_missing" not in {d.code for d in report.diagnostics}


def test_a_fold_section_that_drops_an_object_is_refused():
    # An Object-bearing target output left out, written without a mode, or dropped:
    # each would plan the plates to vanish (spec 18.1 rules 6, 7, 9). Each is refused
    # with the code ofplang-validate gives it (D60 V1).
    interface = {"inputs": {"reagent": "shelf.r", "plates": ["loader.a"]}}
    for outputs, code in (
        ("            reagent: {mode: carry}\n            count: {mode: carry}\n",
         "output_not_listed"),
        ("            reagent: {mode: carry}\n            count: {mode: carry}\n"
         "            plate: {}\n", "missing_required_key"),
        ("            reagent: {mode: carry}\n            count: {mode: carry}\n"
         "            plate: {mode: drop}\n", "object_output_bad_mode"),
    ):
        text = FOLD_DISPENSE.replace(
            "            reagent: {mode: carry}\n            count: {mode: carry}\n"
            "            plate: {mode: collect}\n",
            outputs,
        )
        assert text != FOLD_DISPENSE
        _, errs = _parse(text, interface)
        # Refused for the section, and only for it: the body's return of `Run.plate`
        # reads the refused section, so it is the refusal's consequence and is not
        # reported a second time -- as ofplang-validate does.
        assert errs == [code], (outputs, errs)


# A fold over plain values, with a Pure Data output that is not carried.
FOLD_NOTE = """\
spec_version: "0.5"
processes:
  step:
    kind: atomic
    inputs: {acc: {type: Int, phase: data}, x: {type: Int, phase: data}}
    outputs: {acc: {type: Int, phase: data}, note: {type: String, phase: data}}
  main:
    kind: composite
    inputs: {xs: {type: "Array<Int>", phase: data}}
    outputs: {acc: {type: Int, phase: data}, note: {type: String, phase: data}}
    body:
      nodes:
        - id: F
          kind: fold
          process: step
          carry: {acc: {value: 0}}
          each: {x: {value: [1, 2]}}
          outputs: {acc: {mode: carry}, note: {mode: drop}}
      returns: {acc: {from: F.acc}, note: {from: F.note}}
entry: main
"""


def test_a_reference_to_an_output_the_fold_does_not_expose_is_refused():
    # Dropped by the section, dropped by default (no section: only the carry is
    # exposed, spec 18.2), and -- the section being fully explicit -- left out of it.
    # Each with the code ofplang-validate gives the same document.
    from ofplang.validate import validate

    section = "          outputs: {acc: {mode: carry}, note: {mode: drop}}\n"
    no_section = FOLD_NOTE.replace(section, "")
    left_out = FOLD_NOTE.replace(", note: {mode: drop}}", "}")
    assert no_section != FOLD_NOTE and left_out != FOLD_NOTE
    for text, code in ((FOLD_NOTE, "output_not_exposed"), (no_section, "output_not_exposed"),
                       (left_out, "output_not_listed")):
        _, errs = _parse(text)
        by_validate = sorted({d.code for d in validate(yaml.safe_load(text)).diagnostics
                              if d.severity == "error"})
        assert errs == [code] == by_validate, (code, errs, by_validate)


def test_an_array_passed_straight_through_is_one_through_arc_per_element():
    # A map whose target hands its plate straight back: every element of the output is
    # the boundary input's element, untouched -- one through arc each (D60), in order.
    keep = MAP_COMPOSITE.replace(
        "        - {id: h, process: heat, state: {plate: {from: inputs.plate}}}\n"
        "        - {id: c, process: cool, state: {plate: {from: h.plate}}}\n"
        "      returns: {plate: {from: c.plate}}",
        "        []\n      returns: {plate: {from: inputs.plate}}",
    ).replace("      nodes:\n        []", "      nodes: []")
    assert keep != MAP_COMPOSITE
    wf, errs = _parse(keep, {"inputs": {"plates": ["loader.a", "loader.b"]}})
    assert errs == [] and wf.exit_arcs == () and wf.activities == ()
    assert wf.through_arcs == tuple(
        Arc(Endpoint((), "plates", (i,)), Endpoint((), "plates", (i,))) for i in range(2)
    )
    assert wf.output_sources["plates"] == SourceSeq(
        (SourceRef((), "plates", (0,)), SourceRef((), "plates", (1,)))
    )
