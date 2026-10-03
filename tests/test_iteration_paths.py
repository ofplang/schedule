"""Node paths with iteration indices, and arc endpoints with element indices
(SPECIFICATIONS.md §6.3, §6.4).

Nothing expands a `map` / `fold` yet, so these tests make the shapes expansion will
produce by rewriting the `simple` example's flattened workflow: both activities move
into invocation 0 of a node `Rep`, and the arc between them is given an element
index. What is checked is that every layer carries such a path and such an arc
unchanged -- the solver, the rendered plan, the document validator, the status read
back on a replan, the job prefix of a joint plan, and the fingerprint -- with an
index kept as an int all the way.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import yaml

from ofplang.schedule import validate_document
from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.identifiers import (
    format_endpoint,
    format_node_path,
    node_path_problem,
)
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance, prefix_instance
from ofplang.schedule.scheduler.model import Arc, Endpoint, NodeInvocation
from ofplang.schedule.scheduler.normalize import normalize
from ofplang.schedule.scheduler.plan import render_plan, to_yaml
from ofplang.schedule.scheduler.status import arc_key, node_path
from ofplang.schedule.scheduler.workflow import fingerprint, parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

SOURCE = ("Rep", 0, "SampleSource")
TARGET = ("Rep", 0, "SampleTarget")


def _iterated_workflow(src_index=(1,)):
    """The `simple` workflow as if both of its nodes sat inside invocation 0 of a
    structured node `Rep`, with the one arc carrying element `src_index` of the
    source port."""
    wf, _ = parse_workflow(EXAMPLES / "simple.workflow.yaml")
    assert wf is not None
    moved = {("SampleSource",): SOURCE, ("SampleTarget",): TARGET}
    (arc,) = wf.arcs
    return replace(
        wf,
        activities=tuple(NodeInvocation(moved[a.path], a.process) for a in wf.activities),
        arcs=(
            Arc(
                Endpoint(moved[arc.src.node], arc.src.port, src_index),
                Endpoint(moved[arc.dst.node], arc.dst.port),
            ),
        ),
        precedence=tuple((moved[s], moved[d]) for s, d in wf.precedence),
    )


def _instance(workflow):
    env, _ = load_environment(EXAMPLES / "simple.env.yaml")
    inst, diags = build_instance(workflow, env, check_reachability=False)
    assert inst is not None, [d.code for d in diags.items]
    return inst, env


# --- the grammar ------------------------------------------------------------


def test_node_path_grammar():
    # A node id, optionally followed by one index, per segment.
    assert node_path_problem(["Wash"]) is None
    assert node_path_problem(["Wash", 2]) is None
    assert node_path_problem(["Wash", 2, "aspirate"]) is None
    assert node_path_problem(["Outer", 0, "Inner", 3, "step"]) is None
    # An index never starts a path, never follows another, and is never negative.
    assert node_path_problem([0, "Wash"]) is not None
    assert node_path_problem(["Wash", 2, 3]) is not None
    assert node_path_problem(["Wash", -1]) is not None
    # A bool is not an index, though Python and YAML both let it pass for an int.
    assert node_path_problem(["Wash", True]) is not None
    assert node_path_problem(["2nd"]) is not None


def test_formatting_renders_an_index_as_its_number():
    # A node id cannot start with a digit, so `2` reads back as an index.
    assert format_node_path(("Wash", 2, "aspirate")) == "Wash/2/aspirate"
    assert format_endpoint(("Wash", 2, "aspirate"), "plate") == "Wash/2/aspirate.plate"
    assert format_endpoint((), "plates", (2,)) == ".plates[2]"
    assert format_endpoint(("Mix",), "rows", (1, 0)) == "Mix.rows[1][0]"


# --- the document validator ---------------------------------------------------


def _document(node, arc_from):
    return {
        "activities": [
            {"kind": "processing", "start": 0, "end": 2, "process": "source", "mode": "0",
             "node": node},
            {"kind": "transport", "start": 2, "end": 3, "from_spot": "a.b", "to_spot": "c.d",
             "transporter": "arm",
             "arc": {"from": arc_from, "to": {"node": ["Rep", 0, "SampleTarget"], "port": "p"}}},
        ]
    }


def _codes(document):
    return sorted(d.code for d in validate_document(document).diagnostics)


def test_validator_accepts_indices_where_they_belong():
    good_arc = {"node": ["Rep", 0, "SampleSource"], "port": "p", "index": [1]}
    assert _codes(_document(["Rep", 0, "SampleSource"], good_arc)) == []
    # A boundary endpoint may carry an element index too.
    boundary = {"node": [], "port": "plates", "index": [2, 0]}
    assert _codes(_document(["Rep", 0, "SampleSource"], boundary)) == []


def test_validator_rejects_a_misplaced_or_malformed_index():
    arc = {"node": ["Rep", 0, "SampleSource"], "port": "p"}
    assert _codes(_document([0, "SampleSource"], arc)) == ["invalid_node_path"]
    assert _codes(_document(["Rep", 0, 1], arc)) == ["invalid_node_path"]
    assert _codes(_document(["Rep", -1, "SampleSource"], arc)) == ["invalid_node_path"]
    assert _codes(_document(["Rep", True, "SampleSource"], arc)) == ["wrong_type"]
    node = ["Rep", 0, "SampleSource"]
    for bad in (
        {"node": [0, "SampleSource"], "port": "p"},       # index first
        {"node": ["Rep", 0, "SampleSource"], "port": "p", "index": []},  # empty
        {"node": ["Rep", 0, "SampleSource"], "port": "p", "index": [-1]},
        {"node": ["Rep", 0, "SampleSource"], "port": "p", "index": ["a"]},
        {"node": ["Rep", 0, "SampleSource"], "port": "p", "index": 2},  # not a list
    ):
        assert _codes(_document(node, bad)) == ["malformed_arc"], bad


# --- the status reader ---------------------------------------------------------


def test_status_reads_an_index_back_as_an_int():
    root = yamlnode.loads(
        "node: [Rep, 0, SampleSource]\n"
        "arc: {from: {node: [Rep, 0, SampleSource], port: p, index: [1]},"
        " to: {node: [], port: out}}\n"
    )
    assert node_path(root.get("node")) == ("Rep", 0, "SampleSource")
    assert arc_key(root.get("arc")) == (("Rep", 0, "SampleSource"), "p", (1,), (), "out", ())


# --- the round trip ---------------------------------------------------------------


def test_iterated_paths_survive_solve_render_validate_and_replan():
    inst, env = _instance(_iterated_workflow())
    solution = solve(inst, random_seed=0)
    assert solution.outcome == "optimal"
    plan = render_plan(inst, solution)

    # The plan carries the paths and the element index as written, ints as ints.
    processing = {tuple(a["node"]) for a in plan["activities"] if a["kind"] == "processing"}
    assert processing == {SOURCE, TARGET}
    (move,) = [a for a in plan["activities"] if a["kind"] == "transport"]
    assert move["arc"]["from"] == {"node": list(SOURCE), "port": "source_out", "index": [1]}
    assert move["arc"]["to"] == {"node": list(TARGET), "port": "target_in"}  # no `index` key
    assert _codes(plan) == []

    # Fed back as a status with everything done, every activity is recognised and
    # fixed: nothing is unknown, nothing is matched twice.
    for activity in plan["activities"]:
        activity["status"] = "completed"
    plan["now"] = max(a["end"] for a in plan["activities"])
    status = yamlnode.loads(to_yaml(plan))
    _, fixation, diags = normalize(inst, status, env)
    assert [d.code for d in diags.items] == []
    assert fixation is not None
    assert len(fixation.activities) == 2 and len(fixation.arcs) == 1


def test_a_status_naming_another_element_is_not_this_arc():
    # Element 1 and element 2 of one port are two connections; history for one must
    # not be pinned onto the other.
    inst, env = _instance(_iterated_workflow(src_index=(1,)))
    plan = render_plan(inst, solve(inst, random_seed=0))
    for activity in plan["activities"]:
        activity["status"] = "completed"
        if activity["kind"] == "transport":
            activity["arc"]["from"]["index"] = [2]
    plan["now"] = max(a["end"] for a in plan["activities"])
    _, _, diags = normalize(inst, yamlnode.loads(yaml.safe_dump(plan)), env)
    assert "status_arc_unknown" in [d.code for d in diags.items]


def test_a_joint_plan_prefix_keeps_the_element_index():
    inst, _ = _instance(_iterated_workflow())
    prefixed = prefix_instance(inst, ("job1",))
    (arc,) = [r.arc for r in prefixed.arcs if r.arc.src.node[:1] == ("job1",)]
    assert arc.src == Endpoint(("job1", *SOURCE), "source_out", (1,))


# --- the fingerprint ------------------------------------------------------------------


def test_fingerprint_reads_indices():
    plain = _iterated_workflow(src_index=())
    one = _iterated_workflow(src_index=(1,))
    two = _iterated_workflow(src_index=(2,))
    # Computing it at all is the first check: sorting a path that holds an int must
    # not compare it with a str.
    assert fingerprint(one) == fingerprint(_iterated_workflow(src_index=(1,)))
    assert len({fingerprint(plain), fingerprint(one), fingerprint(two)}) == 3
