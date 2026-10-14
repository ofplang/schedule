"""A `map` / `fold` whose length is a value made during the run (design.md D70).

An inspection says which wells to redo; a map redoes each. How many there are is the
inspection's output, so the traversal is expanded at one invocation -- assumed -- until
the run counts the list and states it in `expansion.lengths`. Nothing of it starts
before the inspection has ended, whichever length is planned: work of an invocation
that turns out not to exist must not have run. A fold that carries an Object hands it on
through a holding gate of its own, so what follows takes it from one place however many
invocations there turn out to be.
"""

from __future__ import annotations

import copy

import yaml

from ofplang.schedule import schedule
from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.api import ASSUMED_ARM, _switched_expansions
from ofplang.schedule.scheduler.model import Endpoint, LengthGate, SourceRef
from ofplang.schedule.scheduler.workflow import parse_workflow

_STEPS = """\
  inspect:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}, redo: {type: "Array<Int>", phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  prep:                                   # reads nothing: it could start at once
    kind: atomic
    inputs: {}
    outputs: {ready: {type: Bool, phase: data}}
  fix:
    kind: atomic
    inputs: {well: {type: Int, phase: data}, ready: {type: Bool, phase: data}}
    outputs: {ok: {type: Bool, phase: data}}
  fix_well:
    kind: composite
    inputs: {well: {type: Int, phase: data}}
    outputs: {ok: {type: Bool, phase: data}}
    body:
      nodes:
        - {id: P, process: prep}
        - {id: F, process: fix, bind: {well: {from: inputs.well}, ready: {from: P.ready}}}
      returns: {ok: {from: F.ok}}
  wash:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}, well: {type: Int, phase: data},
             ready: {type: Bool, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  wash_well:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}, well: {type: Int, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    body:
      nodes:
        - {id: P, process: prep}
        - {id: W, process: wash, state: {plate: {from: inputs.plate}},
           bind: {well: {from: inputs.well}, ready: {from: P.ready}}}
      returns: {plate: {from: W.plate}}
  store:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  make:
    kind: atomic
    inputs: {well: {type: Int, phase: data}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {create: [outputs.cup]}
"""


def _workflow(main: str) -> dict:
    return yaml.safe_load(
        'spec_version: "0.5"\ntypes:\n  Plate: {domain: object}\n  Cup: {domain: object}\n'
        "processes:\n" + _STEPS + main + "entry: main\n"
    )


# The inspection's list, a map over it, and the plate stored meanwhile.
REDO = _workflow("""\
  main:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}, oks: {type: "Array<Bool>", phase: data}}
    body:
      nodes:
        - {id: I, process: inspect, state: {plate: {from: inputs.plate}}}
        - {id: R, kind: map, process: fix_well, each: {well: {from: I.redo}}}
        - {id: S, process: store, state: {plate: {from: I.plate}}}
      returns: {plate: {from: S.plate}, oks: {from: R.ok}}
""")

# The plate washed once per well to redo, carried through a fold, then stored.
WASH = _workflow("""\
  main:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    body:
      nodes:
        - {id: I, process: inspect, state: {plate: {from: inputs.plate}}}
        - {id: W, kind: fold, process: wash_well, carry: {plate: {from: I.plate}},
           each: {well: {from: I.redo}}}
        - {id: S, process: store, state: {plate: {from: W.plate}}}
      returns: {plate: {from: S.plate}}
""")

# A second map over what the first gathered: as long as it turns out to be.
AGAIN = _workflow("""\
  again:
    kind: composite
    inputs: {ok: {type: Bool, phase: data}}
    outputs: {ok: {type: Bool, phase: data}}
    body:
      nodes:
        - {id: P, process: prep}
        - {id: F, process: fix, bind: {well: {value: 0}, ready: {from: P.ready}}}
      returns: {ok: {from: F.ok}}
  main:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}, oks: {type: "Array<Bool>", phase: data}}
    body:
      nodes:
        - {id: I, process: inspect, state: {plate: {from: inputs.plate}}}
        - {id: R, kind: map, process: fix_well, each: {well: {from: I.redo}}}
        - {id: A, kind: map, process: again, each: {ok: {from: R.ok}}}
        - {id: S, process: store, state: {plate: {from: I.plate}}}
      returns: {plate: {from: S.plate}, oks: {from: A.ok}}
""")

# A cup made per well to redo: an Array of Objects as long as the list.
CUPS = _workflow("""\
  main:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}, cups: {type: "Array<Cup>", phase: data}}
    body:
      nodes:
        - {id: I, process: inspect, state: {plate: {from: inputs.plate}}}
        - {id: M, kind: map, process: make, each: {well: {from: I.redo}}}
        - {id: S, process: store, state: {plate: {from: I.plate}}}
      returns: {plate: {from: S.plate}, cups: {from: M.cup}}
""")

_PLACES = ["rack.in", "scope.stage", "sink.basin", "rack.out", "shelf.a", "shelf.b"]
ENV = {
    "time": {"unit": "second"},
    "devices": [
        {"id": "rack", "spots": ["in", "out"]}, {"id": "scope", "spots": ["stage"]},
        {"id": "sink", "spots": ["basin"]}, {"id": "shelf", "spots": ["a", "b"]},
        {"id": "bench", "spots": []}, {"id": "robot", "spots": ["hand"]},
    ],
    "transporters": [{"id": "arm"}],
    "transports": [{"transporter": "arm", "from": a, "to": b, "duration": 1}
                   for a in _PLACES for b in _PLACES if a != b],
    "processes": {
        "inspect": {"modes": [{"devices": ["scope"], "duration": 3,
                               "input_spots": {"plate": "scope.stage"},
                               "output_spots": {"plate": "scope.stage"}}]},
        "prep": {"modes": [{"devices": ["bench"], "duration": 2}]},
        "fix": {"modes": [{"devices": ["bench"], "duration": 4}]},
        "wash": {"modes": [{"devices": ["sink"], "duration": 5,
                            "input_spots": {"plate": "sink.basin"},
                            "output_spots": {"plate": "sink.basin"}}]},
        "store": {"modes": [{"devices": ["rack"], "duration": 1,
                             "input_spots": {"plate": "rack.out"},
                             "output_spots": {"plate": "rack.out"}}]},
        "make": {"modes": [{"devices": ["robot"], "duration": 2,
                            "output_spots": {"cup": "robot.hand"}}]},
    },
}
DOCUMENT = {"interface": {"inputs": {"plate": "rack.in"}}, "activities": []}


def _made(n: int, node=("I",), port="redo") -> dict:
    return {"lengths": [{"node": list(node), "port": port, "length": n}]}


def _parse(workflow, expansion=None, assume=ASSUMED_ARM, interface=None):
    wf, diags = parse_workflow(copy.deepcopy(workflow), interface=interface,
                               expansion=expansion, assume=assume)
    return wf, [d.code for d in diags.items if d.severity == ERROR]


def _plan(workflow, expansion=None, document=DOCUMENT):
    doc = copy.deepcopy(document)
    if expansion is not None:
        doc["expansion"] = expansion
    return schedule(copy.deepcopy(workflow), copy.deepcopy(ENV), document_path=doc,
                    random_seed=0)


# --- expanded at one, until the run counts it ----------------------------------------------


def test_a_length_made_during_the_run_is_planned_at_one_and_says_so():
    wf, errs = _parse(REDO)
    assert errs == [] and wf is not None
    assert wf.lengths == {
        ("R",): LengthGate((SourceRef(("I",), "redo"),), (("I",),), 1, True)
    }
    assert [a.path for a in wf.activities if a.path[0] == "R"] == [("R", 0, "P"), ("R", 0, "F")]
    assert wf.length_checks == ()  # the run decides it, it does not check it


def test_without_leave_to_assume_it_is_refused():
    _, errs = _parse(REDO, assume=None)
    assert errs == ["array_length_unknown"]


def test_nothing_of_it_starts_before_the_length_exists():
    # Prep reads nothing, and would otherwise be free to start at once: work of an
    # invocation that may turn out not to exist.
    wf, _ = _parse(REDO)
    assert (("I",), ("R", 0, "P")) in wf.precedence
    report = _plan(REDO)
    assert report.ok, [d.code for d in report.diagnostics]
    by_node = {tuple(a["node"]): a for a in report.plan["activities"]
               if a["kind"] == "processing"}
    assert by_node[("R", 0, "P")]["start"] >= by_node[("I",)]["end"]


def test_the_plan_marks_the_length_still_to_be_counted():
    report = _plan(REDO)
    (decision,) = [a for a in report.plan["activities"] if a["kind"] == "decision"]
    assert decision["node"] == ["R"] and decision["length"] == 1 and decision["assumed"]
    assert decision["condition"] == {"node": ["I"], "port": "redo"}


def test_the_length_the_run_states_is_the_one_expanded():
    wf, errs = _parse(REDO, _made(3))
    assert errs == []
    assert wf.lengths[("R",)].length == 3 and not wf.lengths[("R",)].assumed
    assert sorted(a.path for a in wf.activities if a.path[0] == "R" and a.path[2] == "F") == [
        ("R", i, "F") for i in range(3)
    ]
    # Still waiting for the inspection, so the shape around it does not change.
    assert (("I",), ("R", 2, "P")) in wf.precedence


def test_a_stated_zero_is_no_invocation():
    wf, errs = _parse(REDO, _made(0))
    assert errs == [] and not [a for a in wf.activities if a.path[0] == "R"]
    assert _plan(REDO, _made(0)).ok


def test_one_invocation_more_is_checked_without_a_solve():
    wf, _ = _parse(REDO)
    found = _switched_expansions(copy.deepcopy(REDO), None, None, wf)
    (chain, read, errs, paths) = next(f for f in found if f[3] == (("R",),))
    assert "'R' at 2 invocations" in chain[-1] and errs == []
    assert read.lengths[("R",)].length == 2


# --- what follows it -----------------------------------------------------------------------


def test_a_traversal_over_what_it_gathered_waits_as_it_does():
    wf, errs = _parse(AGAIN)
    assert errs == []
    # A's own Prep reads nothing of R: it waits for the inspection all the same, since
    # A has as many invocations as R turns out to.
    assert (("I",), ("A", 0, "P")) in wf.precedence
    assert ("A",) not in wf.lengths  # its length follows R's; it is not a decision of its own


def test_a_fold_hands_its_object_on_through_a_holding_gate():
    wf, errs = _parse(WASH)
    assert errs == []
    gate = ("W", "@out", "plate")
    assert any(g.path == gate and g.object_bearing for g in wf.gates)
    # What follows takes the plate from the gate, whichever invocation left it there.
    assert any(a.src.node == gate and a.dst == Endpoint(("S",), "plate") for a in wf.arcs)
    assert _plan(WASH).ok


def test_a_fold_with_no_invocation_passes_the_object_through_its_gate():
    wf, errs = _parse(WASH, _made(0))
    gate = ("W", "@out", "plate")
    assert errs == []
    # Straight from the inspection into the gate, and out of it as before.
    assert any(a.src.node == ("I",) and a.dst.node == gate for a in wf.arcs)
    assert any(a.src.node == gate and a.dst == Endpoint(("S",), "plate") for a in wf.arcs)
    assert _plan(WASH, _made(0)).ok


def test_its_objects_at_the_boundary_are_left_unbound():
    listed = {"inputs": {"plate": "rack.in"}, "outputs": {"cups": ["shelf.a", "shelf.b"]}}
    _, errs = _parse(CUPS, interface=listed)
    assert errs == ["interface_shape_mismatch"]
    _, errs = _parse(CUPS, interface={"inputs": {"plate": "rack.in"}})
    assert errs == []


# --- what a statement may name ---------------------------------------------------------------


def test_a_statement_naming_no_length_read_is_refused():
    _, errs = _parse(REDO, {"lengths": [{"node": ["S"], "port": "plate", "length": 2}]})
    assert errs == ["length_unknown_node"]
    _, errs = _parse(REDO, {"lengths": [{"node": ["Nope"], "port": "redo", "length": 2}]})
    assert errs == ["length_unknown_node"]


# --- inside other structures ---------------------------------------------------------------

# A survey lists, per row, the wells to redo: a map over the rows, and over each row's.
ROWS = _workflow("""\
  survey:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}, rows: {type: "Array<Array<Int>>", phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  row:
    kind: composite
    inputs: {wells: {type: "Array<Int>", phase: data}}
    outputs: {oks: {type: "Array<Bool>", phase: data}}
    body:
      nodes:
        - {id: R, kind: map, process: fix_well, each: {well: {from: inputs.wells}}}
      returns: {oks: {from: R.ok}}
  main:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    body:
      nodes:
        - {id: V, process: survey, state: {plate: {from: inputs.plate}}}
        - {id: Rows, kind: map, process: row, each: {wells: {from: V.rows}}}
        - {id: S, process: store, state: {plate: {from: V.plate}}}
      returns: {plate: {from: S.plate}}
""")


def test_each_row_of_a_made_list_has_a_length_of_its_own():
    wf, errs = _parse(ROWS)
    assert errs == [] and set(wf.lengths) == {("Rows",), ("Rows", 0, "R")}
    stated = {"lengths": [
        {"node": ["V"], "port": "rows", "length": 2},
        {"node": ["V"], "port": "rows", "index": [0], "length": 3},
        {"node": ["V"], "port": "rows", "index": [1], "length": 0},
    ]}
    wf, errs = _parse(ROWS, stated)
    assert errs == []
    assert {k: v.length for k, v in wf.lengths.items()} == {
        ("Rows",): 2, ("Rows", 0, "R"): 3, ("Rows", 1, "R"): 0,
    }
    assert not any(v.assumed for v in wf.lengths.values())


def test_inside_a_loop_each_invocation_waits_for_its_own_inspection():
    looped = _workflow("""\
  check:
    kind: atomic
    inputs: {oks: {type: "Array<Bool>", phase: data}}
    outputs: {again: {type: Bool, phase: data}}
  round:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}, again: {type: Bool, phase: data}}
    body:
      nodes:
        - {id: I, process: inspect, state: {plate: {from: inputs.plate}}}
        - {id: R, kind: map, process: fix_well, each: {well: {from: I.redo}}}
        - {id: C, process: check, bind: {oks: {from: R.ok}}}
      returns: {plate: {from: I.plate}, again: {from: C.again}}
  main:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    body:
      nodes:
        - {id: L, kind: do_while, process: round, carry: {plate: {from: inputs.plate}},
           condition: {output: again}, max_iterations: {value: 3}}
        - {id: S, process: store, state: {plate: {from: L.plate}}}
      returns: {plate: {from: S.plate}}
""")
    wf, errs = _parse(looped)
    assert errs == []
    assert wf.lengths[("L", 0, "R")].waits == (("L", 0, "I"),)
    env = copy.deepcopy(ENV)
    env["processes"]["check"] = {"modes": [{"devices": ["bench"], "duration": 1}]}
    report = schedule(copy.deepcopy(looped), env, document_path=copy.deepcopy(DOCUMENT),
                      random_seed=0)
    assert report.ok, [d.code for d in report.diagnostics]


def test_a_fold_carrying_an_object_keeps_its_spots_while_its_length_is_assumed():
    # The plate waits in the fold's gate until it is stored, so another invocation finds
    # nothing in its way only if its spots were kept for it, as a loop's next one's are.
    # A map carries nothing across invocations, and keeps nothing.
    fold = _plan(WASH)
    kept = [a for a in fold.plan["activities"] if a["kind"] == "reservation"]
    assert [a["node"] for a in kept] == [["W"]] and "sink.basin" in kept[0]["spots"]
    assert not [a for a in _plan(REDO).plan["activities"] if a["kind"] == "reservation"]
    # Once the length is counted there is nothing left to keep.
    assert not [a for a in _plan(WASH, _made(2)).plan["activities"]
                if a["kind"] == "reservation"]
