"""A same-spot hand-off carries no transporter (SPEC §5.4/§6.4, design.md D54).

`from == to` is a physical no-op. Three things already said so -- the contract on
`TransportOption.transporter`, the plan renderer, which omits the field exactly
there, and the *committed* path, which reads that absent field back as null -- but
the route enumeration did not: `transport_duration` answers 0 for a same-spot pair
for every transporter alike, so a laboratory with K arms produced K such routes,
each holding its arm for zero time.

Zero time is not free in a non-overlap. CP-SAT refuses a point strictly inside
another interval, so a move that is physically nothing could not be placed while
that arm was busy -- and it stopped being unplaceable the moment it became
history, which is the disagreement these tests pin shut.
"""

from __future__ import annotations

import yaml

from ofplang.schedule import schedule
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance, routes
from ofplang.schedule.scheduler.workflow import parse_workflow
from tests.schedutil import write

# M -> T, both on one spot: the arc the workflow asks for begins and ends in the
# same place, which is the no-op this is about.
IN_PLACE_WF = """
spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  make: { kind: atomic, outputs: { o: { type: Sample, phase: data } },
          objects: { create: [outputs.o] } }
  take: { kind: atomic, inputs: { i: { type: Sample, phase: data } },
          objects: { consume: [inputs.i] } }
  main:
    kind: composite
    inputs: {}
    outputs: {}
    body:
      nodes:
        - { id: M, process: make }
        - { id: T, process: take, state: { i: { from: M.o } } }
      returns: {}
entry: main
"""


def in_place_env(arms: int) -> str:
    names = [f"arm_{k}" for k in range(1, arms + 1)]
    arms_line = ", ".join(f"{{ id: {n} }}" for n in names)
    return (
        "time: { unit: second }\n"
        "devices:\n  - { id: bench, spots: [only] }\n"
        + (f"transporters: [ {arms_line} ]\n" if names else "")
        + "transports: []\n"
        "processes:\n"
        "  make: { modes: [ { devices: [bench], duration: 1,\n"
        "          output_spots: { o: bench.only } } ] }\n"
        "  take: { modes: [ { devices: [bench], duration: 1,\n"
        "          input_spots: { i: bench.only } } ] }\n"
    )


def _options(tmp_path, env_text: str, workflow: str = IN_PLACE_WF):
    wf, _ = parse_workflow(write(tmp_path, "wf.yaml", workflow))
    env, _ = load_environment(write(tmp_path, "env.yaml", env_text))
    instance, _ = build_instance(wf, env)
    assert instance is not None
    return [
        (o.from_spot, o.to_spot, o.transporter, o.duration)
        for arc in instance.arcs
        for o in arc.options
    ]


# --- the route enumeration ------------------------------------------------


def test_a_same_spot_route_carries_no_transporter(tmp_path):
    assert _options(tmp_path, in_place_env(1)) == [
        ("bench.only", "bench.only", None, 0)
    ]


def test_it_is_one_route_however_many_arms_the_laboratory_has(tmp_path):
    # The point of the fix: K arms used to give K routes that the plan could not
    # tell apart, so the model carried a K-way choice that meant nothing.
    for arms in (1, 2, 5):
        assert _options(tmp_path, in_place_env(arms)) == [
            ("bench.only", "bench.only", None, 0)
        ], arms


def test_an_in_place_workflow_still_schedules_with_no_transporters_at_all(tmp_path):
    # What the fallback this replaced was for. A laboratory that moves nothing can
    # still run a workflow that moves nothing.
    env_path = write(tmp_path, "env.yaml", in_place_env(0))
    wf_path = write(tmp_path, "wf.yaml", IN_PLACE_WF)
    report = schedule(wf_path, env_path)
    assert report.plan is not None, [d.code for d in report.diagnostics]
    assert report.makespan == 2


def test_a_declared_zero_duration_move_between_two_spots_keeps_its_arm(tmp_path):
    # Not a no-op: the spots differ, so the plan writes the transporter and the
    # document says that arm carried it. §5.4 allows the duration to be zero, and
    # such a route goes on occupying its arm.
    env = (
        "time: { unit: second }\n"
        "devices:\n  - { id: bench, spots: [a, b] }\n"
        "transporters: [ { id: arm } ]\n"
        "transports:\n"
        "  - { transporter: arm, from: bench.a, to: bench.b, duration: 0 }\n"
        "processes:\n"
        "  make: { modes: [ { devices: [bench], duration: 1,\n"
        "          output_spots: { o: bench.a } } ] }\n"
        "  take: { modes: [ { devices: [bench], duration: 1,\n"
        "          input_spots: { i: bench.b } } ] }\n"
    )
    assert _options(tmp_path, env) == [("bench.a", "bench.b", "arm", 0)]


def test_routes_is_asked_directly_too(tmp_path):
    # `normalize` re-uses `routes` to derive a re-transport, so the rule has to live
    # there rather than in one caller.
    env, _ = load_environment(write(tmp_path, "env.yaml", in_place_env(3)))
    assert routes(env, "bench.only", "bench.only") == [(None, 0)]


# --- what it changes in the model ----------------------------------------


def test_a_no_op_no_longer_holds_an_arm_that_is_busy(tmp_path):
    # The behaviour this fixes. One arm is carrying a long move for another chain;
    # the in-place chain's no-op has to sit somewhere, and it used to be barred from
    # the interior of that move. Now the whole run fits in the long move's shadow.
    workflow = """
spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  make: { kind: atomic, outputs: { o: { type: Sample, phase: data } },
          objects: { create: [outputs.o] } }
  take: { kind: atomic, inputs: { i: { type: Sample, phase: data } },
          objects: { consume: [inputs.i] } }
  main:
    kind: composite
    inputs: {}
    outputs: {}
    body:
      nodes:
        - { id: M, process: make }
        - { id: T, process: take, state: { i: { from: M.o } } }
        - { id: M2, process: make2 }
        - { id: T2, process: take2, state: { i: { from: M2.o } } }
      returns: {}
  make2: { kind: atomic, outputs: { o: { type: Sample, phase: data } },
           objects: { create: [outputs.o] } }
  take2: { kind: atomic, inputs: { i: { type: Sample, phase: data } },
           objects: { consume: [inputs.i] } }
entry: main
"""
    env = (
        "time: { unit: second }\n"
        "devices:\n"
        "  - { id: bench, spots: [only] }\n"
        "  - { id: left,  spots: [only] }\n"
        "  - { id: right, spots: [only] }\n"
        "transporters: [ { id: arm } ]\n"
        "transports:\n"
        "  - { transporter: arm, from: left.only, to: right.only, duration: 30 }\n"
        "processes:\n"
        "  make:  { modes: [ { devices: [bench], duration: 1,\n"
        "           output_spots: { o: bench.only } } ] }\n"
        "  take:  { modes: [ { devices: [bench], duration: 1,\n"
        "           input_spots: { i: bench.only } } ] }\n"
        "  make2: { modes: [ { devices: [left], duration: 1,\n"
        "           output_spots: { o: left.only } } ] }\n"
        "  take2: { modes: [ { devices: [right], duration: 1,\n"
        "           input_spots: { i: right.only } } ] }\n"
    )
    env_path = write(tmp_path, "env.yaml", env)
    wf_path = write(tmp_path, "wf.yaml", workflow)
    report = schedule(wf_path, env_path)
    assert report.plan is not None, [d.code for d in report.diagnostics]
    # The long chain is 1 + 30 + 1 = 32 and nothing can shorten it; the in-place
    # chain fits inside it, so that is the whole makespan.
    assert report.makespan == 32


def test_planned_and_committed_say_the_same_thing(tmp_path):
    # The disagreement that made this a defect rather than a choice: the same no-op
    # used to carry an arm while planned and none once reported, so a replan
    # silently changed whether it occupied anything.
    env_path = write(tmp_path, "env.yaml", in_place_env(2))
    wf_path = write(tmp_path, "wf.yaml", IN_PLACE_WF)
    planned = schedule(wf_path, env_path)
    assert planned.plan is not None, [d.code for d in planned.diagnostics]
    move = next(a for a in planned.plan["activities"] if a["kind"] == "transport")
    # The plan omits the field for a same-spot move (§6.4), which is what the
    # committed path reads back as "nothing carried this".
    assert "transporter" not in move

    status = write(
        tmp_path,
        "status.yaml",
        "time: { unit: second }\n"
        "now: 2\n"
        "activities:\n"
        "- { kind: processing, status: completed, start: 0, end: 1, process: make,\n"
        "    mode: '0', node: [M], output_spots: { o: bench.only } }\n"
        "- kind: transport\n"
        "  status: completed\n"
        "  start: 1\n"
        "  end: 1\n"
        "  from_spot: bench.only\n"
        "  to_spot: bench.only\n"
        "  arc: { from: { node: [M], port: o }, to: { node: [T], port: i } }\n",
    )
    replanned = schedule(wf_path, env_path, document_path=status)
    assert replanned.plan is not None, [d.code for d in replanned.diagnostics]
    for entry in replanned.plan["activities"]:
        if entry["kind"] == "transport" and entry["from_spot"] == entry["to_spot"]:
            assert "transporter" not in entry


# --- and what it unblocks ------------------------------------------------


def test_a_same_spot_hand_off_no_longer_blocks_a_transporter_collapse(tmp_path):
    # Why this was reached for. A class of interchangeable arms is left alone if any
    # of its routes takes no time, and a single same-spot hand-off anywhere used to
    # put one such route on every arm -- which is every real laboratory measured.
    from ofplang.schedule.scheduler.symmetry import (
        aggregatable_transporters,
        interchangeable_classes,
    )

    env = (
        "time: { unit: second }\n"
        "devices:\n"
        "  - { id: bench, spots: [only] }\n"
        "  - { id: left,  spots: [only] }\n"
        "  - { id: right, spots: [only] }\n"
        "transporters: [ { id: arm_1 }, { id: arm_2 } ]\n"
        "transports:\n"
        "  - { transporter: arm_1, from: left.only, to: right.only, duration: 5 }\n"
        "  - { transporter: arm_2, from: left.only, to: right.only, duration: 5 }\n"
        "processes:\n"
        "  make:  { modes: [ { devices: [bench], duration: 1,\n"
        "           output_spots: { o: bench.only } } ] }\n"
        "  take:  { modes: [ { devices: [bench], duration: 1,\n"
        "           input_spots: { i: bench.only } } ] }\n"
        "  make2: { modes: [ { devices: [left], duration: 1,\n"
        "           output_spots: { o: left.only } } ] }\n"
        "  take2: { modes: [ { devices: [right], duration: 1,\n"
        "           input_spots: { i: right.only } } ] }\n"
    )
    workflow = yaml.safe_load(IN_PLACE_WF)
    body = workflow["processes"]["main"]["body"]
    body["nodes"] += [
        {"id": "M2", "process": "make2"},
        {"id": "T2", "process": "take2", "state": {"i": {"from": "M2.o"}}},
    ]
    for name in ("make2", "take2"):
        workflow["processes"][name] = {
            "kind": "atomic",
            **(
                {"outputs": {"o": {"type": "Sample", "phase": "data"}},
                 "objects": {"create": ["outputs.o"]}}
                if name == "make2"
                else {"inputs": {"i": {"type": "Sample", "phase": "data"}},
                      "objects": {"consume": ["inputs.i"]}}
            ),
        }
    wf, _ = parse_workflow(write(tmp_path, "wf.yaml", yaml.safe_dump(workflow)))
    environment, _ = load_environment(write(tmp_path, "env.yaml", env))
    instance, _ = build_instance(wf, environment)
    assert instance is not None
    classes = interchangeable_classes(instance)
    assert [c.members for c in classes if c.scope == "transporter"] == [
        ("arm_1", "arm_2")
    ]
    assert aggregatable_transporters(instance, classes) == {
        "arm_1": ("arm_1", 2),
        "arm_2": ("arm_1", 2),
    }
