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

from ofplang.schedule import JobInput, schedule, schedule_jobs

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
OUTPUTS = EXAMPLES / "outputs"

# (name, workflow, environment, execution document or None, expected optimal
# makespan as of 2026-07-14; consumable added 2026-08-27).
CASES = [
    ("simple", EXAMPLES / "simple.workflow.yaml", EXAMPLES / "simple.env.yaml", None, 5),
    (
        "reformatter",
        EXAMPLES / "reformatter.workflow.yaml",
        EXAMPLES / "reformatter.env.yaml",
        None,
        88,
    ),
    # two_arms: two independent jobs on a two-transporter fleet. Parallel transport
    # gives 20; a single transporter would serialise the two moves for 30.
    ("two_arms", EXAMPLES / "two_arms.workflow.yaml", EXAMPLES / "two_arms.env.yaml", None, 20),
    (
        "plate_batch",
        OUTPUTS / "plate_batch.workflow.yaml",
        OUTPUTS / "plate_batch.env.yaml",
        None,
        50,
    ),
    # consumable: the reader starts empty, so a document is required (it says what
    # the run starts with) and one refill covers both assays. 33 rather than 32
    # because the refill holds the reader while it works and cannot be laid over the
    # assays it feeds -- 32 was the makespan while refills occupied nothing.
    # storage: three plates over a two-slot refrigerator whose `chill` mode does not
    # access the device (§4.4.2). 450; the same instance is 650 when chilling holds
    # the machine, which is what test_hold.py pins.
    (
        "storage",
        EXAMPLES / "storage.workflow.yaml",
        EXAMPLES / "storage.env.yaml",
        None,
        450,
    ),
    (
        "consumable",
        EXAMPLES / "consumable.workflow.yaml",
        EXAMPLES / "consumable.env.yaml",
        EXAMPLES / "consumable.document.yaml",
        33,
    ),
    # shared_refill on its own: one plate against a stock that exactly covers it, so
    # no refill is planned at all. Its two-job counterpart is pinned below.
    (
        "shared_refill",
        EXAMPLES / "shared_refill.workflow.yaml",
        EXAMPLES / "consumable.env.yaml",
        EXAMPLES / "shared_refill.document.yaml",
        18,
    ),
]


@pytest.mark.parametrize(
    "name,workflow,env,document,expected", CASES, ids=[c[0] for c in CASES]
)
def test_example_makespan_is_stable(name, workflow, env, document, expected):
    report = schedule(workflow, env, document_path=document)
    assert report.outcome == "optimal", f"{name}: outcome={report.outcome}"
    assert report.makespan == expected, f"{name}: makespan={report.makespan}, expected {expected}"


def test_shared_refill_two_jobs_makespan_is_stable():
    """The joint plan (§6.11) of the same example, anchored the same way.

    33 rather than twice the 18 above: the two jobs overlap where the environment
    lets them and serialise on the reader, and the refill they share holds that
    reader for its 4 seconds in between.
    """
    report = schedule_jobs(
        [
            JobInput("job1", EXAMPLES / "shared_refill.workflow.yaml"),
            JobInput("job2", EXAMPLES / "shared_refill.workflow.yaml"),
        ],
        EXAMPLES / "consumable.env.yaml",
        document_path=EXAMPLES / "shared_refill.document.yaml",
    )
    assert report.outcome == "optimal"
    assert report.makespan == 33
