"""An Array of Objects at the workflow boundary, one spot per element (SPECIFICATIONS.md
§6.8, design.md D57).

An `Array<Plate>` entry input is as many plates as it has elements, each on a spot of
its own, and its `interface` binding is the list of those spots in element order. Each
element crosses the boundary on an arc of its own, `Endpoint((), port, index)`.

Nothing expands a `map` yet, so the workflow is built by hand in the shape expansion
will produce: the `interface_load` example's one `heat` step, twice, as invocations 0
and 1 of a node `Each`, each heating one element of `samples` and returning one element
of `results`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ofplang.schedule import validate_document
from ofplang.schedule.core import yamlnode
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.model import Arc, Endpoint, NodeInvocation, Workflow
from ofplang.schedule.scheduler.normalize import normalize
from ofplang.schedule.scheduler.plan import render_plan, to_yaml
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

HEAT = [("Each", i, "Heat") for i in range(2)]

ENV = """
time: { unit: second }
devices:
  - { id: loader, spots: [a, b, c] }
  - { id: heater, spots: [stage] }
  - { id: output, spots: [a, b] }
transporters: [{ id: arm }]
transports:
  - { transporter: arm, from: loader.a, to: heater.stage, duration: 2 }
  - { transporter: arm, from: loader.b, to: heater.stage, duration: 2 }
  - { transporter: arm, from: heater.stage, to: output.a, duration: 2 }
  - { transporter: arm, from: heater.stage, to: output.b, duration: 2 }
processes:
  heat:
    modes:
      - devices: [heater]
        duration: 10
        input_spots:  { plate: heater.stage }
        output_spots: { out: heater.stage }
"""


def _workflow(length: int = 2) -> Workflow:
    """`samples: Array<Plate>` in, `results: Array<Plate>` out, element i heated by
    invocation i of `Each`."""
    base, _ = parse_workflow(EXAMPLES / "interface_load.workflow.yaml")
    assert base is not None
    paths = HEAT[:length]
    return Workflow(
        activities=tuple(NodeInvocation(p, "heat") for p in paths),
        arcs=(),
        precedence=(),
        processes={"heat": base.processes["heat"]},
        entry_input_ports={"samples": True},
        exit_output_ports={"results": True},
        entry_arcs=tuple(
            Arc(Endpoint((), "samples", (i,)), Endpoint(p, "plate")) for i, p in enumerate(paths)
        ),
        exit_arcs=tuple(
            Arc(Endpoint(p, "out"), Endpoint((), "results", (i,))) for i, p in enumerate(paths)
        ),
        entry_input_ranks={"samples": 1},
        exit_output_ranks={"results": 1},
    )


def _env(tmp_path):
    path = tmp_path / "env.yaml"
    path.write_text(ENV, encoding="utf-8")
    env, _ = load_environment(path)
    return env


def _build(tmp_path, interface, workflow=None):
    env = _env(tmp_path)
    inst, diags = build_instance(workflow or _workflow(), env, interface=interface)
    return inst, diags, env


def _codes(diags):
    return [d.code for d in diags.items]


BOUND = {
    "inputs": {"samples": ["loader.a", "loader.b"]},
    "outputs": {"results": ["output.a", "output.b"]},
}


# --- one Object per element, end to end -----------------------------------------------


def test_each_element_is_carried_on_its_own_arc(tmp_path):
    inst, diags, env = _build(tmp_path, BOUND)
    assert inst is not None and _codes(diags) == []
    solution = solve(inst, random_seed=0)
    assert solution.outcome == "optimal"
    plan = render_plan(inst, solution, interface=BOUND)
    assert validate_document(plan).ok

    moves = [a for a in plan["activities"] if a["kind"] == "transport"]
    entering = {
        tuple(m["arc"]["from"]["index"]): (m["from_spot"], m["arc"]["to"]["node"])
        for m in moves if m["arc"]["from"]["node"] == []
    }
    leaving = {
        tuple(m["arc"]["to"]["index"]): (m["to_spot"], m["arc"]["from"]["node"])
        for m in moves if m["arc"]["to"]["node"] == []
    }
    # Element i starts on the i-th spot of the list and goes to invocation i; it comes
    # back to the i-th spot of the output list. The index is the correspondence.
    assert entering == {(0,): ("loader.a", ["Each", 0, "Heat"]),
                        (1,): ("loader.b", ["Each", 1, "Heat"])}
    assert leaving == {(0,): ("output.a", ["Each", 0, "Heat"]),
                       (1,): ("output.b", ["Each", 1, "Heat"])}

    # Fed back as a status with everything done, every element's moves are recognised.
    for activity in plan["activities"]:
        activity["status"] = "completed"
    plan["now"] = max(a["end"] for a in plan["activities"])
    _, fixation, diags = normalize(inst, yamlnode.loads(to_yaml(plan)), env)
    assert _codes(diags) == []
    assert fixation is not None and len(fixation.arcs) == 4


def test_a_status_naming_another_element_is_not_this_arc(tmp_path):
    # Element 0 and element 1 of one port are two connections; history for one must
    # not be pinned onto the other, nor onto an element the workflow does not have.
    inst, _, env = _build(tmp_path, BOUND)
    plan = render_plan(inst, solve(inst, random_seed=0), interface=BOUND)
    for activity in plan["activities"]:
        activity["status"] = "completed"
        if activity["kind"] == "transport" and activity["arc"]["from"]["node"] == []:
            activity["arc"]["from"]["index"] = [activity["arc"]["from"]["index"][0] + 2]
    plan["now"] = max(a["end"] for a in plan["activities"])
    _, _, diags = normalize(inst, yamlnode.loads(yaml.safe_dump(plan)), env)
    assert "status_arc_unknown" in _codes(diags)


def test_a_replan_after_one_element_has_moved(tmp_path):
    # Element 0 has been carried in; nothing else has happened. The replan pins that
    # leg, re-plans the rest of element 0's move from where it is now, and plans
    # element 1 from scratch -- the element's own slot is what both are looked up by.
    inst, _, env = _build(tmp_path, BOUND)
    plan = render_plan(inst, solve(inst, random_seed=0), interface=BOUND)
    (first,) = [a for a in plan["activities"] if a["kind"] == "transport"
                and a["arc"]["from"]["node"] == [] and a["arc"]["from"]["index"] == [0]]
    status = {
        "interface": BOUND,
        "now": first["end"],
        "activities": [dict(first, status="completed")],
    }
    replanned, fixation, diags = normalize(inst, yamlnode.loads(yaml.safe_dump(status)), env)
    assert _codes(diags) == []
    assert replanned is not None and fixation is not None
    assert solve(replanned, fixation=fixation, random_seed=0).outcome == "optimal"


def test_an_empty_array_binds_nothing(tmp_path):
    # Length 0: no element crosses, and `[]` says exactly that.
    empty = _workflow(length=0)
    inst, diags, _ = _build(tmp_path, {"inputs": {"samples": []}, "outputs": {"results": []}},
                            workflow=empty)
    assert inst is not None and _codes(diags) == []
    assert not any(a.boundary for a in inst.activities)


# --- the binding is the Array's shape ---------------------------------------------------


def test_a_spot_for_an_array_port_is_a_shape_mismatch(tmp_path):
    _, diags, _ = _build(tmp_path, {"inputs": {"samples": "loader.a"}})
    assert "interface_shape_mismatch" in _codes(diags)


def test_a_list_for_a_scalar_port_is_a_shape_mismatch(tmp_path):
    base, _ = parse_workflow(EXAMPLES / "interface_load.workflow.yaml")
    _, diags, _ = _build(tmp_path, {"inputs": {"sample": ["loader.a"]}}, workflow=base)
    assert "interface_shape_mismatch" in _codes(diags)


def test_a_list_of_the_wrong_length_is_a_length_mismatch(tmp_path):
    for spots in (["loader.a"], ["loader.a", "loader.b", "loader.c"]):
        _, diags, _ = _build(tmp_path, {"inputs": {"samples": spots}})
        assert "interface_length_mismatch" in _codes(diags), spots


def test_two_elements_cannot_start_on_one_spot(tmp_path):
    _, diags, _ = _build(tmp_path, {"inputs": {"samples": ["loader.a", "loader.a"]}})
    assert "interface_duplicate_spot" in _codes(diags)


def test_an_unbound_element_is_one_missing_input(tmp_path):
    _, diags, _ = _build(tmp_path, {"inputs": {}})
    assert _codes(diags).count("interface_input_missing") == 1


# --- an unbound Array output ----------------------------------------------------------


def test_two_jobs_claiming_one_elements_spot_are_refused():
    # Each element of a bound Array is a claim on its spot, as a scalar port's binding
    # is (§6.11): delivering two jobs' results onto one shelf is refused by element.
    from ofplang.schedule.scheduler.api import _check_boundary_spots
    from ofplang.schedule.scheduler.model import JobSpec

    first = JobSpec(id="a", interface={"outputs": {"results": ["output.a", "output.b"]}})
    second = JobSpec(id="b", interface={"outputs": {"results": ["output.b"]}})
    (refusal,) = _check_boundary_spots((first, second))
    assert refusal.code == "interface_shared_output_spot"
    assert "results[1]" in refusal.message and "results[0]" in refusal.message


def test_a_stopped_job_holds_every_element_it_was_given():
    # Entry material is there from the job's release until something collects it, so
    # a job that stopped before touching it still holds every element's spot (§6.12).
    from ofplang.schedule.scheduler.api import _holds_of

    entries = {"a": {"release": 3, "interface": {"inputs": {"samples": ["loader.a", "loader.b"]}}}}
    assert _holds_of([], entries, ["a"], now=10) == {"loader.a": 3, "loader.b": 3}


def test_an_element_delivery_is_labelled_by_its_element():
    from ofplang.schedule.scheduler.visualize import _xfer_label

    element = {"arc": {"to": {"node": [], "port": "results", "index": [1]}}}
    whole = {"arc": {"to": {"node": [], "port": "result"}}}
    assert _xfer_label(element) == "> results[1]"
    assert _xfer_label(whole) == "> transport"  # unchanged where there is no element


def test_an_unbound_array_output_is_warned_about_once(tmp_path):
    inst, diags, _ = _build(tmp_path, {"inputs": BOUND["inputs"]})
    assert inst is not None
    assert _codes(diags) == ["interface_output_unbound"]
    # One output node per element, each choosing among its own resting spots.
    outputs = [a for a in inst.activities if a.boundary and a.boundary.kind == "output"]
    assert len(outputs) == 2
    assert solve(inst, random_seed=0).outcome == "optimal"
