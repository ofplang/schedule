"""End-to-end CP-SAT solve on the example fixtures."""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.workflow import parse_workflow

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _instance(name):
    wf, _ = parse_workflow(EXAMPLES / f"{name}.workflow.yaml")
    env, _ = load_environment(EXAMPLES / f"{name}.env.yaml")
    inst, diags = build_instance(wf, env)
    assert inst is not None, [d.code for d in diags.items]
    return inst


def test_solve_job_sample_makespan_5():
    sol = solve(_instance("job_sample"))
    assert sol.outcome == "optimal"
    # source (2) -> transport (1) -> target (2) on the critical path.
    assert sol.makespan == 5

    by_node = {p.node: p for p in sol.processing}
    assert by_node[("SampleSource",)].start == 0
    assert by_node[("SampleSource",)].end == 2
    (t,) = sol.transport
    assert t.start >= 2 and t.end == t.start + 1
    assert by_node[("SampleTarget",)].start >= t.end
    assert by_node[("SampleTarget",)].end == sol.makespan == 5


def test_solve_reformatter_feasible():
    sol = solve(_instance("reformatter"))
    assert sol.outcome == "optimal"
    assert sol.makespan and sol.makespan > 0
    assert len(sol.processing) == 8
    assert len(sol.transport) == 12
    # Every scheduled interval is well-formed and respects arc ordering.
    for p in sol.processing:
        assert 0 <= p.start <= p.end <= sol.makespan
    for t in sol.transport:
        assert t.start <= t.end
