"""End-to-end replanning through the public API and the CLI.

Uses the `simple` example (source -> transport -> target). A status that marks
the source completed at now=3 leaves the transport and target to be re-optimised
at or after now, so the makespan grows from 5 (initial) to 6.
"""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule import cli, schedule, validate_document
from ofplang.schedule.scheduler.plan import to_yaml

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
OUTPUTS = EXAMPLES / "outputs"
WORKFLOW = EXAMPLES / "simple.workflow.yaml"
ENV = EXAMPLES / "simple.env.yaml"

_SOURCE_DONE = """
time: { unit: second }
now: 3
activities:
  - kind: processing
    status: completed
    start: 0
    end: 2
    process: source
    mode: '0'
    node: [SampleSource]
"""

_UNNORMALIZED = """
time: { unit: second }
now: 3
activities:
  - { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource] }
  - kind: transport
    status: running
    start: 2
    end: 3
    from_spot: station_0.core
    to_spot: station_1.core
    transporter: transport
    arc: { from: { node: [SampleSource], port: source_out }, to: { node: [SampleTarget], port: target_in } }
"""


_SOURCE_FAILED = """
time: { unit: second }
now: 3
activities:
  - kind: processing
    status: failed
    start: 0
    end: 3
    process: source
    mode: '0'
    node: [SampleSource]
"""


def _status_file(tmp_path, text):
    p = tmp_path / "status.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_replan_full_timeline(tmp_path):
    status = _status_file(tmp_path, _SOURCE_DONE)
    report = schedule(WORKFLOW, ENV, document_path=status)
    assert report.ok and report.outcome == "optimal"
    # Fixed history + re-optimised future: source ends at 2, everything else >= 3.
    assert report.makespan == 6
    assert report.plan["now"] == 3

    by_node = {tuple(a["node"]): a for a in report.plan["activities"] if a["kind"] == "processing"}
    assert by_node[("SampleSource",)]["status"] == "completed"
    assert by_node[("SampleSource",)]["end"] == 2
    # Pending activities carry no status and start at or after now.
    assert "status" not in by_node[("SampleTarget",)]
    assert by_node[("SampleTarget",)]["start"] >= 3
    transport = next(a for a in report.plan["activities"] if a["kind"] == "transport")
    assert "status" not in transport and transport["start"] >= 3


def test_replan_output_is_a_valid_document(tmp_path):
    status = _status_file(tmp_path, _SOURCE_DONE)
    report = schedule(WORKFLOW, ENV, document_path=status)
    out = tmp_path / "replan.yaml"
    out.write_text(to_yaml(report.plan), encoding="utf-8")
    assert validate_document(out).ok


def test_replan_output_round_trips_as_next_status(tmp_path):
    # Feeding the replan output straight back in yields the same optimum: its
    # `outcome` is ignored and its pending times are discarded and re-optimised.
    status = _status_file(tmp_path, _SOURCE_DONE)
    first = schedule(WORKFLOW, ENV, document_path=status)
    fed_back = tmp_path / "fed_back.yaml"
    fed_back.write_text(to_yaml(first.plan), encoding="utf-8")
    second = schedule(WORKFLOW, ENV, document_path=fed_back)
    assert second.ok and second.makespan == first.makespan == 6


def test_replan_started_transport_to_pending_folds_stay_put_relay(tmp_path):
    # A started transport feeding a pending processing is normalized into a relay
    # at the arrival spot plus a re-transport. Here the target still consumes at
    # that arrival spot, so the re-transport is a zero-distance no-op: it and its
    # relay are folded out of the output (§6.4.1), leaving only the committed leg
    # to deliver straight to the target.
    status = _status_file(tmp_path, _UNNORMALIZED)
    report = schedule(WORKFLOW, ENV, document_path=status)
    assert report.ok and report.outcome == "optimal"
    assert not [a for a in report.plan["activities"] if a["kind"] == "relay"]
    legs = [a for a in report.plan["activities"] if a["kind"] == "transport"]
    # Only the committed (running) real leg survives; the folded no-op is gone.
    assert len(legs) == 1 and legs[0].get("status") == "running"
    assert not any(a["from_spot"] == a["to_spot"] for a in legs)
    # The folded output still round-trips to the same optimum.
    fed = tmp_path / "fed.yaml"
    fed.write_text(to_yaml(report.plan), encoding="utf-8")
    assert validate_document(fed).ok
    second = schedule(WORKFLOW, ENV, document_path=fed)
    assert second.ok and second.makespan == report.makespan


def test_replan_rejects_terminal_status(tmp_path):
    # A terminal status (failed / cancelled) is a valid document shape but not a
    # replan input: a stopped run has no remaining work to plan (§6.2, §9.3).
    status = _status_file(tmp_path, _SOURCE_FAILED)
    assert validate_document(status).ok  # the shape itself is valid
    report = schedule(WORKFLOW, ENV, document_path=status)
    assert not report.ok
    assert "terminal_status_not_replannable" in {d.code for d in report.diagnostics}


def test_cli_replan_writes_valid_plan(tmp_path):
    status = _status_file(tmp_path, _SOURCE_DONE)
    out = tmp_path / "plan.yaml"
    code = cli.main(
        ["schedule", str(WORKFLOW), "--env", str(ENV), "--document", str(status), "-o", str(out)]
    )
    assert code == cli.EXIT_OK
    assert validate_document(out).ok


def test_cli_replan_missing_status_file_is_usage_error():
    code = cli.main(
        ["schedule", str(WORKFLOW), "--env", str(ENV), "--document", "no-such-status.yaml"]
    )
    assert code == cli.EXIT_USAGE


def test_cli_replan_started_transport_to_pending_is_ok(tmp_path):
    # The CLI now schedules a started-transport -> pending-successor input (via a
    # relay) instead of rejecting it.
    status = _status_file(tmp_path, _UNNORMALIZED)
    out = tmp_path / "plan.yaml"
    code = cli.main(["schedule", str(WORKFLOW), "--env", str(ENV), "--document", str(status), "-o", str(out)])
    assert code == cli.EXIT_OK
    assert validate_document(out).ok


# --- committed example (examples/simple.status.yaml) ----------------------
#
# Golden anchor for the tracked replan example, mirroring test_example_makespans
# for the initial plan and test_plan for the committed output.


def test_committed_simple_status_replans_to_6():
    report = schedule(WORKFLOW, ENV, document_path=EXAMPLES / "simple.status.yaml")
    assert report.outcome == "optimal"
    assert report.makespan == 6


def test_committed_replan_output_is_valid_document():
    path = OUTPUTS / "simple.replan.yaml"
    assert path.is_file(), f"missing committed replan: {path}"
    assert validate_document(path).ok
