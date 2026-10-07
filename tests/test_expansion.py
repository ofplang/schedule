"""The lengths a run states for its Pure Data Arrays (`expansion.lengths`, design.md D62).

A `map` / `fold` over a Pure Data Array entry input -- labels to make one cup each,
spec §17 -- has no length the scheduler can see: the values are the run's. The runner
counts each Array it was given and states the count in `expansion`, and the expansion
reads it as it reads an `interface` binding's length for an Array of Objects. These
tests read that expansion, the refusals around it, and its round trip through a plan,
a replan and a joint plan.
"""

from __future__ import annotations

import copy

import yaml

from ofplang.schedule import JobInput, schedule, schedule_jobs, validate_document
from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.model import Endpoint, LengthCheck, SourceRef
from ofplang.schedule.scheduler.workflow import fingerprint, parse_workflow
from tests.test_expand import ENV, MAP_DATA_ONLY, MAP_HEAT, MAP_ZIP

# The whole Array handed to one atomic: nothing traverses it, so no length is read.
WHOLE = """\
spec_version: "0.4"
processes:
  summarize:
    kind: atomic
    inputs: {ods: {type: "Array<Float>", phase: data}}
    outputs: {mean: {type: Float, phase: data}}
  main:
    kind: composite
    inputs: {ods: {type: "Array<Float>", phase: run}}
    outputs: {mean: {type: Float, phase: data}}
    body:
      nodes:
        - {id: S, process: summarize, bind: {ods: {from: inputs.ods}}}
      returns: {mean: {from: S.mean}}
entry: main
"""

# The labels reach the map through a composite's input: the length is still the
# boundary's.
VIA_COMPOSITE = """\
spec_version: "0.4"
types:
  Cup: {domain: object}
processes:
  make:
    kind: atomic
    inputs: {label: {type: String, phase: run}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {create: [outputs.cup]}
  batch:
    kind: composite
    inputs: {labels: {type: "Array<String>", phase: run}}
    outputs: {cups: {type: "Array<Cup>", phase: data}}
    body:
      nodes:
        - {id: Make, kind: map, process: make, each: {label: {from: inputs.labels}}}
      returns: {cups: {from: Make.cup}}
  main:
    kind: composite
    inputs: {labels: {type: "Array<String>", phase: run}}
    outputs: {cups: {type: "Array<Cup>", phase: data}}
    body:
      nodes:
        - {id: B, process: batch, bind: {labels: {from: inputs.labels}}}
      returns: {cups: {from: B.cups}}
entry: main
"""

# Rows of labels: a map over the rows whose target maps over one row's labels. Only
# the outermost length is stated in this stage, so the inner map has none (D62 L2).
NESTED = """\
spec_version: "0.4"
types:
  Cup: {domain: object}
processes:
  make:
    kind: atomic
    inputs: {label: {type: String, phase: run}}
    outputs: {cup: {type: Cup, phase: data}}
    objects: {create: [outputs.cup]}
  row:
    kind: composite
    inputs: {labels: {type: "Array<String>", phase: run}}
    outputs: {cups: {type: "Array<Cup>", phase: data}}
    body:
      nodes:
        - {id: Make, kind: map, process: make, each: {label: {from: inputs.labels}}}
      returns: {cups: {from: Make.cup}}
  main:
    kind: composite
    inputs: {rows: {type: "Array<Array<String>>", phase: run}}
    outputs: {racks: {type: "Array<Array<Cup>>", phase: data}}
    body:
      nodes:
        - {id: Rows, kind: map, process: row, each: {labels: {from: inputs.rows}}}
      returns: {racks: {from: Rows.cups}}
entry: main
"""


def _lengths(**ports) -> dict:
    return {"lengths": [{"node": [], "port": port, "length": n} for port, n in ports.items()]}


def _parse(text, expansion=None, interface=None):
    wf, diags = parse_workflow(yaml.safe_load(text), interface=interface, expansion=expansion)
    return wf, [d.code for d in diags.items if d.severity == ERROR]


def _schedule(text, document):
    return schedule(yaml.safe_load(text), yaml.safe_load(ENV),
                    document_path=document, random_seed=0)


def _all_done(plan) -> dict:
    """A plan fed back with everything finished."""
    status = copy.deepcopy(plan)
    for activity in status["activities"]:
        activity["status"] = "completed"
    status["now"] = max(a["end"] for a in status["activities"])
    return status


# --- the stated length is the number of invocations ----------------------------------------


def test_a_stated_length_expands_a_map_over_a_pure_data_array():
    wf, errs = _parse(MAP_DATA_ONLY, _lengths(labels=3))
    assert errs == [] and wf is not None
    assert [a.path for a in wf.activities] == [("Make", i) for i in range(3)]
    assert wf.iterations == {("Make",): 3}
    # Element i of the labels reaches invocation i, from the boundary.
    assert wf.input_sources[Endpoint(("Make", 1), "label")] == SourceRef((), "labels", (1,))
    # The runner is held to the length it stated (a hand-written document may be wrong).
    assert wf.length_checks == (LengthCheck(("Make",), "label", SourceRef((), "labels"), 3),)


def test_without_a_stated_length_there_is_still_none():
    _, errs = _parse(MAP_DATA_ONLY)
    assert errs == ["array_length_unknown"]


def test_a_stated_zero_is_no_invocation():
    wf, errs = _parse(MAP_DATA_ONLY, _lengths(labels=0))
    assert errs == [] and wf.activities == () and wf.iterations == {("Make",): 0}


def test_the_stated_length_is_the_graph():
    two, _ = _parse(MAP_DATA_ONLY, _lengths(labels=2))
    three, _ = _parse(MAP_DATA_ONLY, _lengths(labels=3))
    assert fingerprint(two) != fingerprint(three)


def test_a_length_reaches_a_map_through_a_composite_input():
    wf, errs = _parse(VIA_COMPOSITE, _lengths(labels=2))
    assert errs == [] and [a.path for a in wf.activities] == [("B", "Make", 0), ("B", "Make", 1)]


def test_an_inner_length_is_not_read_yet():
    # The rows expand (2 of them); each row's labels have no stated length.
    _, errs = _parse(NESTED, _lengths(rows=2))
    assert errs == ["array_length_unknown"] * 2


# --- zipped with a length the scheduler already knows ---------------------------------------


def test_a_stated_length_zipped_with_a_binding_must_agree():
    plates = {"inputs": {"plates": ["loader.a", "loader.b"]}}
    wf, errs = _parse(MAP_ZIP, _lengths(temps=2), plates)
    assert errs == [] and len(wf.activities) == 2
    assert wf.length_checks == (LengthCheck(("Heat",), "t", SourceRef((), "temps"), 2),)
    # Known to differ before the run, so refused before anything moves (D62 H1).
    _, errs = _parse(MAP_ZIP, _lengths(temps=3), plates)
    assert errs == ["each_length_mismatch"]


# --- a length no traversal reads ---------------------------------------------------------


def test_a_length_nothing_traverses_is_not_remarked_on():
    with_length, diags = parse_workflow(yaml.safe_load(WHOLE), expansion=_lengths(ods=4))
    without, _ = parse_workflow(yaml.safe_load(WHOLE))
    assert diags.items == []
    assert with_length.activities == without.activities
    assert fingerprint(with_length) == fingerprint(without)


def test_an_array_with_no_length_that_nothing_traverses_is_not_remarked_on():
    _, diags = parse_workflow(yaml.safe_load(WHOLE))
    assert diags.items == []


# --- refusals ------------------------------------------------------------------------------


def test_a_port_that_is_no_entry_input_is_refused():
    _, errs = _parse(MAP_DATA_ONLY, _lengths(labels=3, colours=3))
    assert errs == ["length_unknown_port"]


def test_a_port_that_is_not_an_array_is_refused():
    plates = {"inputs": {"plates": ["loader.a"]}}
    _, errs = _parse(MAP_HEAT, _lengths(t=1), plates)
    assert errs == ["length_unknown_port"]


def test_an_array_of_objects_is_refused():
    # Its length is its interface binding's; a second statement could only disagree.
    plates = {"inputs": {"plates": ["loader.a"]}}
    _, errs = _parse(MAP_HEAT, _lengths(plates=1), plates)
    assert errs == ["length_on_object_port"]


def test_a_length_inside_the_workflow_is_not_read_yet():
    expansion = {"lengths": [{"node": ["Make", 0], "port": "cup", "length": 2},
                             {"node": [], "port": "labels", "length": 1}]}
    _, errs = _parse(MAP_DATA_ONLY, expansion)
    assert errs == ["unsupported_feature"]


def test_an_inner_length_is_refused_rather_than_ignored():
    expansion = {"lengths": [{"node": [], "port": "rows", "length": 2},
                             {"node": [], "port": "rows", "index": [0], "length": 3}]}
    _, errs = _parse(NESTED, expansion)
    assert "unsupported_feature" in errs


def test_the_atomic_entry_checks_its_lengths_too():
    atomic = yaml.safe_load(WHOLE)
    atomic["entry"] = "summarize"
    del atomic["processes"]["main"]
    _, diags = parse_workflow(atomic, expansion=_lengths(ods=2, nope=1))
    assert [d.code for d in diags.items] == ["length_unknown_port"]


# --- the document's shape ------------------------------------------------------------------


def _shape_codes(expansion) -> list[str]:
    result = validate_document({"expansion": expansion, "activities": []})
    return [d.code for d in result.diagnostics]


def _entry_codes(**entry) -> list[str]:
    """The codes for a section of one length entry."""
    return _shape_codes({"lengths": [entry]})


def test_the_section_validates():
    assert _shape_codes(_lengths(labels=3)) == []
    assert _entry_codes(node=[], port="rows", index=[0], length=2) == []
    assert _entry_codes(node=["Make", 0], port="cup", length=2) == []
    assert _shape_codes({}) == []


def test_a_position_is_stated_once():
    twice = {"lengths": [{"node": [], "port": "labels", "length": 3},
                         {"node": [], "port": "labels", "length": 4}]}
    assert _shape_codes(twice) == ["duplicate_length"]
    # Different elements of one port are different positions.
    two_rows = {"lengths": [{"node": [], "port": "rows", "index": [0], "length": 3},
                            {"node": [], "port": "rows", "index": [1], "length": 4}]}
    assert _shape_codes(two_rows) == []


def test_malformed_entries():
    assert _entry_codes(node=[], port="labels", length=-1) == ["negative_value"]
    assert _entry_codes(node=[], port="labels", length="3") == ["wrong_type"]
    assert _entry_codes(node=[], port="labels") == ["missing_required_field"]
    assert _entry_codes(node=[], port="labels", index=[], length=1) == ["wrong_type"]
    assert _entry_codes(node="x", port="labels", length=1) == ["wrong_type"]
    assert _entry_codes(node=[0], port="labels", length=1) == ["invalid_node_path"]
    assert _entry_codes(node=[], port="a-b", length=1) == ["invalid_identifier"]
    assert _entry_codes(node=[], port="p", length=1, count=2) == ["unknown_key"]
    assert _shape_codes({"arms": []}) == ["unknown_key"]
    assert _shape_codes([1]) == ["wrong_type"]


def test_a_roster_entry_carries_its_own():
    doc = {"jobs": [{"id": "a", "expansion": _lengths(labels=2)},
                    {"id": "b", "expansion": {"lengths": 3}}],
           "activities": []}
    codes = [(d.code, d.path) for d in validate_document(doc).diagnostics]
    assert codes == [("wrong_type", "jobs[1].expansion.lengths")]


# --- plan, replan, joint plan --------------------------------------------------------------


def test_it_plans_echoes_and_replans():
    document = {"expansion": _lengths(labels=3), "activities": []}
    report = _schedule(MAP_DATA_ONLY, document)
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]
    makes = [a for a in report.plan["activities"] if a["kind"] == "processing"]
    assert sorted(a["node"] for a in makes) == [["Make", i] for i in range(3)]
    # Echoed, so the plan is the next document and expands the same way.
    assert report.plan["expansion"] == _lengths(labels=3)
    assert validate_document(report.plan).ok
    again = _schedule(MAP_DATA_ONLY, _all_done(report.plan))
    assert again.ok, [d.code for d in again.diagnostics]


def test_a_replan_without_the_section_does_not_know_the_length():
    report = _schedule(MAP_DATA_ONLY, {"expansion": _lengths(labels=2), "activities": []})
    status = _all_done(report.plan)
    del status["expansion"]
    again = _schedule(MAP_DATA_ONLY, status)
    assert not again.ok and "array_length_unknown" in [d.code for d in again.diagnostics]


def test_each_job_expands_by_its_own():
    document = {
        "jobs": [{"id": "a", "expansion": _lengths(labels=2)},
                 {"id": "b", "expansion": _lengths(labels=1)}],
        "activities": [],
    }
    workflow = yaml.safe_load(MAP_DATA_ONLY)
    report = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        yaml.safe_load(ENV), document_path=document, random_seed=0,
    )
    assert report.ok, [d.code for d in report.diagnostics]
    made = sorted((a["job"], tuple(a["node"])) for a in report.plan["activities"]
                  if a["kind"] == "processing")
    assert made == [("a", ("Make", 0)), ("a", ("Make", 1)), ("b", ("Make", 0))]
    assert [entry.get("expansion") for entry in report.plan["jobs"]] == [
        _lengths(labels=2), _lengths(labels=1)
    ]
    assert "expansion" not in report.plan
    again = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        yaml.safe_load(ENV), document_path=_all_done(report.plan), random_seed=0,
    )
    assert again.ok, [d.code for d in again.diagnostics]


def test_a_joint_plan_refuses_a_top_level_section():
    workflow = yaml.safe_load(MAP_DATA_ONLY)
    report = schedule_jobs(
        [JobInput("a", copy.deepcopy(workflow)), JobInput("b", copy.deepcopy(workflow))],
        yaml.safe_load(ENV),
        document_path={"expansion": _lengths(labels=2), "activities": []},
        random_seed=0,
    )
    assert not report.ok
    assert [d.code for d in report.diagnostics if d.severity == ERROR] == ["multi_job_expansion"]
