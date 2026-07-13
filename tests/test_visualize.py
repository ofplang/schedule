"""Tests for the plan visualizer (self-contained HTML/SVG)."""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule import cli, schedule
from ofplang.schedule.scheduler.visualize import render_html, render_svg

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _plan(name):
    report = schedule(EXAMPLES / f"{name}.workflow.yaml", EXAMPLES / f"{name}.env.yaml")
    assert report.plan is not None
    return report.plan


def test_station_view_has_device_lanes():
    html = render_html(_plan("job_sample"), view="station")
    assert "<svg" in html and "</svg>" in html
    assert "station_0" in html and "station_1" in html
    assert "transport (transporter)" in html
    # No dependency arrows in the station view.
    assert 'class="arrow"' not in html


def test_workflow_view_has_nodes_and_arrows():
    html = render_html(_plan("job_sample"), view="workflow")
    assert "SampleSource" in html and "SampleTarget" in html
    # The one arc yields dependency arrows (source -> transport -> destination).
    assert 'class="arrow"' in html


def test_reformatter_both_views_render():
    plan = _plan("reformatter")
    for view in ("station", "workflow"):
        html = render_html(plan, view=view)
        assert "<svg" in html
        assert html.strip().startswith("<!doctype html>")


def test_visualize_cli_writes_html(tmp_path):
    plan = tmp_path / "plan.yaml"
    assert cli.main(["schedule", str(EXAMPLES / "job_sample.workflow.yaml"),
                     "--env", str(EXAMPLES / "job_sample.env.yaml"), "-o", str(plan)]) == cli.EXIT_OK
    out = tmp_path / "gantt.html"
    assert cli.main(["visualize", str(plan), "--view", "workflow", "-o", str(out)]) == cli.EXIT_OK
    assert "<svg" in out.read_text(encoding="utf-8")


def test_render_svg_is_standalone():
    svg = render_svg(_plan("job_sample"), view="station")
    assert svg.startswith("<?xml")
    assert "<svg" in svg and "</svg>" in svg
    # Self-contained: styling and background travel with the SVG.
    assert "<style>" in svg
    assert 'class="bg"' in svg
    assert "station_0" in svg


def test_visualize_cli_svg_by_extension(tmp_path):
    plan = tmp_path / "plan.yaml"
    assert cli.main(["schedule", str(EXAMPLES / "job_sample.workflow.yaml"),
                     "--env", str(EXAMPLES / "job_sample.env.yaml"), "-o", str(plan)]) == cli.EXIT_OK
    out = tmp_path / "gantt.svg"  # .svg extension -> SVG format inferred
    assert cli.main(["visualize", str(plan), "-o", str(out)]) == cli.EXIT_OK
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<?xml") and "<svg" in text


def test_visualize_cli_rejects_non_document(tmp_path):
    bad = tmp_path / "env.yaml"
    bad.write_text("devices: []\n", encoding="utf-8")
    assert cli.main(["visualize", str(bad)]) == cli.EXIT_USAGE
