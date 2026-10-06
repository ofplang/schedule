"""An entry Object returned untouched (design.md D60 J2).

A workflow may return an Object exactly as it came in -- directly (`returns: {b: {from:
inputs.b}}`), through a composite, as one element of an Array, or as the carry of a
`fold` that ran no invocation. Nothing works on it, so it crosses the boundary in and
straight back out: a *through arc*, entry boundary -> exit boundary. The planner moves it
to the spot its output is bound to, or -- bound to where it came in, or not bound at all
-- leaves it there.

Until D60 this was out of scope: the flattener recorded nothing for it, and the instance
refused a binding of it.
"""

from __future__ import annotations

import copy

import yaml

from ofplang.schedule import schedule, validate_document
from ofplang.schedule.scheduler.model import Arc, Endpoint, SourceRef
from ofplang.schedule.scheduler.workflow import fingerprint, parse_workflow

WF = """\
spec_version: "0.4"
types: {Plate: {domain: object}}
processes:
  heat:
    kind: atomic
    inputs: {plate: {type: Plate, phase: data}}
    outputs: {plate: {type: Plate, phase: data}}
    objects: {map: {outputs.plate: inputs.plate}}
  main:
    kind: composite
    inputs: {a: {type: Plate, phase: data}, b: {type: Plate, phase: data}}
    outputs: {a: {type: Plate, phase: data}, b: {type: Plate, phase: data}}
    body:
      nodes:
        - {id: H, process: heat, state: {plate: {from: inputs.a}}}
      returns: {a: {from: H.plate}, b: {from: inputs.b}}
entry: main
"""

ENV = """\
time: {unit: second}
devices:
  - {id: hotel, spots: [a, b]}
  - {id: oven, spots: [tray]}
  - {id: rack, spots: [a, b]}
transporters: [{id: arm}]
transports:
  - {transporter: arm, from: hotel.a, to: oven.tray, duration: 2}
  - {transporter: arm, from: oven.tray, to: rack.a, duration: 2}
  - {transporter: arm, from: hotel.b, to: rack.b, duration: 3}
processes:
  heat:
    modes:
      - {devices: [oven], duration: 5, input_spots: {plate: oven.tray},
         output_spots: {plate: oven.tray}}
"""

INPUTS = {"a": "hotel.a", "b": "hotel.b"}


def _schedule(outputs, workflow=WF):
    return schedule(
        yaml.safe_load(workflow), yaml.safe_load(ENV),
        document_path={"interface": {"inputs": INPUTS, "outputs": outputs}, "activities": []},
        random_seed=0,
    )


def _moves_of_b(report):
    return [
        (a["from_spot"], a["to_spot"])
        for a in report.plan["activities"]
        if a["kind"] == "transport" and a["arc"]["to"] == {"node": [], "port": "b"}
    ]


def test_the_flattener_records_a_through_arc_and_a_source():
    wf, diags = parse_workflow(yaml.safe_load(WF))
    assert not diags.items
    assert wf.through_arcs == (Arc(Endpoint((), "b"), Endpoint((), "b")),)
    assert wf.output_sources["b"] == SourceRef((), "b")
    # The planner's own exits are the produced Objects only.
    assert [arc.dst.port for arc in wf.exit_arcs] == ["a"]


def test_bound_elsewhere_it_is_moved(tmp_path):
    report = _schedule({"a": "rack.a", "b": "rack.b"})
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]
    assert _moves_of_b(report) == [("hotel.b", "rack.b")]
    out = tmp_path / "plan.yaml"
    out.write_text(yaml.safe_dump(report.plan), encoding="utf-8")
    assert validate_document(out).ok


def test_bound_where_it_came_in_it_stays():
    report = _schedule({"a": "rack.a", "b": "hotel.b"})
    assert report.ok, [d.code for d in report.diagnostics]
    assert _moves_of_b(report) == [("hotel.b", "hotel.b")]  # a same-spot no-op


def test_unbound_it_stays_and_nothing_is_warned():
    # Unlike an unbound produced output, whose resting place the schedule chooses (and
    # warns about), this one's place is the input binding's: nothing moves it.
    report = _schedule({"a": "rack.a"})
    assert report.ok, [d.code for d in report.diagnostics]
    assert _moves_of_b(report) == [("hotel.b", "hotel.b")]
    assert "interface_output_unbound" not in {d.code for d in report.diagnostics}


def test_its_input_still_has_to_be_bound():
    report = schedule(
        yaml.safe_load(WF), yaml.safe_load(ENV),
        document_path={"interface": {"inputs": {"a": "hotel.a"}}, "activities": []},
        random_seed=0,
    )
    assert not report.ok
    assert "interface_input_missing" in {d.code for d in report.diagnostics}


def test_a_replan_carries_the_move_through_its_history():
    # Halfway through the move: the plan fed back with what has started marked so. The
    # through arc is matched against its history like any other arc.
    report = _schedule({"a": "rack.a", "b": "rack.b"})
    plan = copy.deepcopy(report.plan)
    now = 3
    for activity in plan["activities"]:
        if activity["end"] <= now:
            activity["status"] = "completed"
        elif activity["start"] < now:
            activity["status"] = "running"
    plan["now"] = now
    again = schedule(yaml.safe_load(WF), yaml.safe_load(ENV), document_path=plan, random_seed=0)
    assert again.ok, [(d.code, d.message) for d in again.diagnostics]
    assert _moves_of_b(again) == [("hotel.b", "rack.b")]
    # And once everything has finished.
    for activity in plan["activities"]:
        activity["status"] = "completed"
    plan["now"] = max(a["end"] for a in plan["activities"])
    done = schedule(yaml.safe_load(WF), yaml.safe_load(ENV), document_path=plan, random_seed=0)
    assert done.ok, [(d.code, d.message) for d in done.diagnostics]


def test_which_input_goes_to_which_output_is_part_of_the_fingerprint():
    swapped = WF.replace(
        "inputs: {a: {type: Plate, phase: data}, b: {type: Plate, phase: data}}",
        "inputs: {a: {type: Plate, phase: data}, b: {type: Plate, phase: data},"
        " c: {type: Plate, phase: data}}",
    ).replace(
        "outputs: {a: {type: Plate, phase: data}, b: {type: Plate, phase: data}}\n    body",
        "outputs: {a: {type: Plate, phase: data}, b: {type: Plate, phase: data},"
        " c: {type: Plate, phase: data}}\n    body",
    )
    straight = swapped.replace("b: {from: inputs.b}}", "b: {from: inputs.b}, c: {from: inputs.c}}")
    crossed = swapped.replace("b: {from: inputs.b}}", "b: {from: inputs.c}, c: {from: inputs.b}}")
    first, _ = parse_workflow(yaml.safe_load(straight))
    second, _ = parse_workflow(yaml.safe_load(crossed))
    assert fingerprint(first) != fingerprint(second)


def test_a_workflow_without_one_keeps_its_fingerprint():
    # The through arcs enter the digest only where there are some, so every digest
    # written before D60 is still the one a replan computes.
    plain = WF.replace("b: {from: inputs.b}}", "b: {from: H2.plate}}").replace(
        "        - {id: H, process: heat, state: {plate: {from: inputs.a}}}\n",
        "        - {id: H, process: heat, state: {plate: {from: inputs.a}}}\n"
        "        - {id: H2, process: heat, state: {plate: {from: inputs.b}}}\n",
    )
    wf, _ = parse_workflow(yaml.safe_load(plain))
    assert wf.through_arcs == ()
    # Recomputed the way it was before D60: the five parts and nothing else.
    import hashlib
    import json

    from ofplang.schedule.scheduler.workflow import _typed

    parts = [
        sorted(((list(a.path), a.process) for a in wf.activities), key=_typed),
        sorted(((list(arc.src.node), arc.src.port, list(arc.dst.node), arc.dst.port)
                for arc in wf.arcs), key=_typed),
        sorted(((list(s), list(d)) for s, d in wf.precedence), key=_typed),
        sorted(wf.entry_input_ports.items()),
        sorted(wf.exit_output_ports.items()),
    ]
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    assert fingerprint(wf) == hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
