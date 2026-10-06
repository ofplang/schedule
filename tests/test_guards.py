"""What the workflow reader refuses rather than silently leave out (design.md D60 Q2).

The reader assumes valid v0 but cannot assume it was given it -- `--no-validate` and API
callers hand it whatever they have. Where its own reading would then drop something
without a word (an unbound input, a reference to nothing, an output never returned), it
stops, and says so with the code `ofplang-validate` gives the same document (D60 V1):
these tests hold the two to the same words, case by case.

Each case names the one code the reader gives, and validate must give it too. Validate
may say more about the same document -- `object_output_unused` where an unbound Object
input leaves its producer's Object with no fate, a type finding about a section it would
not take -- and those are its to say: the reader loses nothing over them. One difference
is deliberate: a binding that is not a mapping is refused by the reader's older shape
guard as `wrong_type`, alongside the other shapes it cannot walk.
"""

from __future__ import annotations

import copy

import pytest
import yaml
from ofplang.validate import validate

from ofplang.schedule.core.diagnostics import ERROR
from ofplang.schedule.scheduler.workflow import parse_workflow

BASE = yaml.safe_load("""\
spec_version: "0.5"
types: {Plate: {domain: object}}
processes:
  heat:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}, t: {type: Float, phase: run}}
    outputs: {plate: {type: Plate, phase: data}, r: {type: Float, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  wrap:
    kind: composite
    inputs: {plate: {type: Plate, phase: data}, t: {type: Float, phase: run}}
    outputs: {plate: {type: Plate, phase: data}, r: {type: Float, phase: data}}
    body:
      nodes:
        - {id: h, process: heat, state: {plate: {from: inputs.plate}}, bind: {t: {from: inputs.t}}}
      returns: {plate: {from: h.plate}, r: {from: h.r}}
  main:
    kind: composite
    inputs: {p: {type: Plate, phase: data}, t: {type: Float, phase: run}}
    outputs: {p: {type: Plate, phase: data}, r: {type: Float, phase: data}}
    body:
      nodes:
        - {id: W, process: wrap, state: {plate: {from: inputs.p}}, bind: {t: {from: inputs.t}}}
      returns: {p: {from: W.plate}, r: {from: W.r}}
entry: main
""")


def _node(doc, proc):
    return doc["processes"][proc]["body"]["nodes"][0]


def _returns(doc, proc):
    return doc["processes"][proc]["body"]["returns"]


def _bind(proc, port, binding):
    return lambda d: _node(d, proc)["bind"].__setitem__(port, binding)


def _return(proc, port, binding):
    return lambda d: _returns(d, proc).__setitem__(port, binding)


# what -> (the one code the reader gives, how to break the base document)
CASES = {
    "unbound Pure Data input of an atomic":
        ("data_indegree", lambda d: _node(d, "wrap").pop("bind")),
    "unbound Object input of an atomic":
        ("object_input_no_source", lambda d: _node(d, "wrap").pop("state")),
    "unbound Pure Data input of a composite":
        ("data_indegree", lambda d: _node(d, "main").pop("bind")),
    "a reference to no node":
        ("unknown_reference", _bind("main", "t", {"from": "X.r"})),
    "a reference to no input":
        ("unknown_reference", _bind("wrap", "t", {"from": "inputs.zz"})),
    "a reference to no output of an atomic":
        ("unknown_reference", _return("wrap", "r", {"from": "h.zz"})),
    "a reference to no output of a composite":
        ("unknown_reference", _return("main", "r", {"from": "W.zz"})),
    "a malformed reference":
        ("malformed_reference", _bind("wrap", "t", {"from": "nodot"})),
    "a binding with neither from nor value":
        ("binding_source_arity", _bind("wrap", "t", {})),
    "a binding of a port the target lacks":
        ("binding_port_not_found", _bind("wrap", "zz", {"value": 1.0})),
    "a literal on an Object-bearing port":
        ("literal_on_object_port",
         lambda d: _node(d, "wrap")["state"].__setitem__("plate", {"value": {}})),
    "a composite output never returned":
        ("output_not_returned", lambda d: _returns(d, "wrap").pop("r")),
    "an entry output never returned":
        ("output_not_returned", lambda d: _returns(d, "main").pop("r")),
    "a return of no declared output":
        ("return_port_not_found", _return("wrap", "zz", {"from": "h.r"})),
    "a section the node kind does not take":
        ("section_not_valid_for_kind",
         lambda d: _node(d, "wrap").__setitem__("each", {"t": {"from": "inputs.t"}})),
}


def _codes(mutate):
    doc = copy.deepcopy(BASE)
    mutate(doc)
    said = {d.code for d in validate(copy.deepcopy(doc)).diagnostics if d.severity == "error"}
    _, diags = parse_workflow(copy.deepcopy(doc))
    return said, {d.code for d in diags.items if d.severity == ERROR}


def test_the_base_document_is_valid_and_reads_cleanly():
    assert validate(copy.deepcopy(BASE)).ok
    wf, diags = parse_workflow(copy.deepcopy(BASE))
    assert wf is not None and not diags.items


@pytest.mark.parametrize("what", sorted(CASES))
def test_the_reader_refuses_with_validates_code(what):
    code, mutate = CASES[what]
    by_validate, by_reader = _codes(mutate)
    assert by_reader == {code}, (what, by_reader)
    assert code in by_validate, (what, by_validate)


def test_a_binding_that_is_not_a_mapping_stays_a_shape_error():
    by_validate, by_reader = _codes(
        lambda d: _node(d, "wrap")["bind"].__setitem__("t", "inputs.t")
    )
    assert "wrong_value_kind" in by_validate and by_reader == {"wrong_type"}


def test_each_body_is_checked_once_however_often_it_is_invoked():
    # `wrap` is invoked twice; its missing return is one finding, not two.
    doc = copy.deepcopy(BASE)
    doc["processes"]["main"]["inputs"]["q"] = {"type": "Plate", "phase": "data"}
    doc["processes"]["main"]["outputs"]["q"] = {"type": "Plate", "phase": "data"}
    doc["processes"]["main"]["body"]["nodes"].append(
        {"id": "W2", "process": "wrap", "state": {"plate": {"from": "inputs.q"}},
         "bind": {"t": {"from": "inputs.t"}}}
    )
    doc["processes"]["main"]["body"]["returns"]["q"] = {"from": "W2.plate"}
    doc["processes"]["wrap"]["body"]["returns"].pop("r")
    doc["processes"]["main"]["body"]["returns"]["r"] = {"from": "W.plate"}  # keep main whole
    doc["processes"]["main"]["outputs"]["r"]["type"] = "Plate"
    _, diags = parse_workflow(doc)
    assert [d.code for d in diags.items if d.code == "output_not_returned"] == [
        "output_not_returned"
    ]


def test_a_map_with_no_each_source_says_so():
    doc = yaml.safe_load("""\
spec_version: "0.5"
processes:
  tick:
    kind: atomic
    inputs: {}
    outputs: {n: {type: Int, phase: data}}
  main:
    kind: composite
    inputs: {}
    outputs: {ns: {type: "Array<Int>", phase: data}}
    body:
      nodes:
        - {id: T, kind: map, process: tick}
      returns: {ns: {from: T.n}}
entry: main
""")
    _, diags = parse_workflow(doc)
    assert [d.code for d in diags.items if d.severity == ERROR] == ["missing_each_source"]
