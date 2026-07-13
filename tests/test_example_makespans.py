"""Golden makespan regression for the committed example workflows.

Records the optimal makespan each committed example schedules to today, so a
later change that is *not* meant to alter the schedule (e.g. rewriting
plate_batch with nested composites, which must flatten to the same graph) is
caught if it does. CP-SAT's optimum is a unique value for a given instance, so
the makespan is a stable golden anchor even though the concrete schedule is not.

When a change is intended to change a schedule, update the expected value here
deliberately — that edit is the record that the change was expected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ofplang.schedule import schedule

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
OUTPUTS = EXAMPLES / "outputs"

# (name, workflow, environment, expected optimal makespan as of 2026-07-14).
CASES = [
    ("simple", EXAMPLES / "simple.workflow.yaml", EXAMPLES / "simple.env.yaml", 5),
    ("reformatter", EXAMPLES / "reformatter.workflow.yaml", EXAMPLES / "reformatter.env.yaml", 88),
    # two_arms: two independent jobs on a two-transporter fleet. Parallel transport
    # gives 20; a single transporter would serialise the two moves for 30.
    ("two_arms", EXAMPLES / "two_arms.workflow.yaml", EXAMPLES / "two_arms.env.yaml", 20),
    ("plate_batch", OUTPUTS / "plate_batch.workflow.yaml", OUTPUTS / "plate_batch.env.yaml", 50),
]


@pytest.mark.parametrize("name,workflow,env,expected", CASES, ids=[c[0] for c in CASES])
def test_example_makespan_is_stable(name, workflow, env, expected):
    report = schedule(workflow, env)
    assert report.outcome == "optimal", f"{name}: outcome={report.outcome}"
    assert report.makespan == expected, f"{name}: makespan={report.makespan}, expected {expected}"
