"""Tests for the plan visualizer (self-contained HTML/SVG)."""

from __future__ import annotations

import re
from pathlib import Path

from ofplang.schedule import cli, schedule
from ofplang.schedule.scheduler.visualize import render_html, render_svg

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

_MARKER = 'marker-end="url(#ah)"'  # present iff dependency arrows were drawn


def _plan(name):
    report = schedule(EXAMPLES / f"{name}.workflow.yaml", EXAMPLES / f"{name}.env.yaml")
    assert report.plan is not None
    return report.plan


def test_device_view_has_device_lanes():
    html = render_html(_plan("simple"), view="device")
    assert "<svg" in html and "</svg>" in html
    assert "station_0" in html and "station_1" in html
    assert "transport (transporter)" in html
    assert _MARKER not in html  # no arrows in the device view


def test_device_view_has_one_lane_per_transporter():
    # Two transports on different transporters must yield a lane each. Built
    # inline so the assertion does not depend on any particular example.
    plan = {
        "activities": [
            {"kind": "transport", "start": 0, "end": 5,
             "from_spot": "d0.p", "to_spot": "d1.p", "transporter": "arm0",
             "arc": {"from": {"node": ["a"], "port": "o"}, "to": {"node": ["b"], "port": "i"}}},
            {"kind": "transport", "start": 0, "end": 3,
             "from_spot": "d2.p", "to_spot": "d3.p", "transporter": "arm1",
             "arc": {"from": {"node": ["c"], "port": "o"}, "to": {"node": ["d"], "port": "i"}}},
        ],
    }
    html = render_html(plan, view="device")
    assert "arm0 (transporter)" in html and "arm1 (transporter)" in html


def test_workflow_view_has_nodes_and_arrows():
    html = render_html(_plan("simple"), view="workflow")
    assert "SampleSource" in html and "SampleTarget" in html
    assert _MARKER in html  # the arc yields dependency arrows


def test_lane_view_packs_into_fewer_lanes():
    plan = _plan("reformatter")
    lane = render_html(plan, view="lane")
    workflow = render_html(plan, view="workflow")
    # A lane label is the only element with text-anchor="end".
    lane_lanes = lane.count('text-anchor="end"')
    workflow_lanes = workflow.count('text-anchor="end"')
    assert "lane 1" in lane and "lane 2" in lane  # parallel branches -> multiple lanes
    assert 1 < lane_lanes < workflow_lanes         # packed: fewer than one-per-activity
    assert _MARKER in lane                         # arcs still traced


def test_reformatter_all_views_render():
    plan = _plan("reformatter")
    for view in ("device", "workflow", "lane"):
        html = render_html(plan, view=view)
        assert "<svg" in html
        assert html.strip().startswith("<!doctype html>")


def test_light_svg_is_powerpoint_safe():
    # The default (light) SVG must use inline presentation attributes only —
    # nothing PowerPoint's renderer trips on.
    svg = render_svg(_plan("reformatter"), view="device", theme="light")
    assert svg.startswith("<?xml")
    assert "<style>" not in svg
    assert "var(" not in svg
    assert "prefers-color-scheme" not in svg
    # No 8-digit hex colours on fill/stroke (they'd render black).
    assert not re.search(r'(?:fill|stroke)="#[0-9a-fA-F]{8}"', svg)
    # Concrete colours, with opacity kept separate.
    assert 'fill="#3b82f6"' in svg          # processing
    assert 'fill="#f59e0b"' in svg          # transport
    assert 'fill="#f59e0b" fill-opacity="0.2"' in svg  # ghost (device occupancy)
    # Transparent: no full-canvas background rect.
    assert 'x="0" y="0" width=' not in svg


def test_dark_svg_is_fixed_and_inline():
    svg = render_svg(_plan("simple"), view="workflow", theme="dark")
    assert "<style>" not in svg and "var(" not in svg
    assert 'fill="#60a5fa"' in svg  # dark processing colour, painted explicitly
    assert 'x="0" y="0" width=' not in svg  # transparent background


def test_auto_svg_uses_css_for_browsers():
    svg = render_svg(_plan("simple"), view="device", theme="auto")
    assert "<style>" in svg
    assert "prefers-color-scheme" in svg


def test_visualize_cli_writes_html(tmp_path):
    plan = tmp_path / "plan.yaml"
    assert cli.main(["schedule", str(EXAMPLES / "simple.workflow.yaml"),
                     "--env", str(EXAMPLES / "simple.env.yaml"), "-o", str(plan)]) == cli.EXIT_OK
    out = tmp_path / "gantt.html"  # .html extension -> HTML format inferred
    assert cli.main(["visualize", str(plan), "--view", "workflow", "-o", str(out)]) == cli.EXIT_OK
    assert "<svg" in out.read_text(encoding="utf-8")


def test_visualize_cli_svg_by_extension_is_powerpoint_safe(tmp_path):
    plan = tmp_path / "plan.yaml"
    assert cli.main(["schedule", str(EXAMPLES / "simple.workflow.yaml"),
                     "--env", str(EXAMPLES / "simple.env.yaml"), "-o", str(plan)]) == cli.EXIT_OK
    out = tmp_path / "gantt.svg"  # .svg -> SVG; default theme light
    assert cli.main(["visualize", str(plan), "-o", str(out)]) == cli.EXIT_OK
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<?xml") and "<svg" in text
    assert "var(" not in text and "<style>" not in text


def test_visualize_cli_rejects_non_document(tmp_path):
    bad = tmp_path / "env.yaml"
    bad.write_text("devices: []\n", encoding="utf-8")
    assert cli.main(["visualize", str(bad)]) == cli.EXIT_USAGE


# -- refills, in every view --------------------------------------------------
#
# A refill is real work that occupies two machines, so every view has to account
# for it. It reached only the device view: the lane view dropped it silently, and
# the workflow view -- treating "not a processing" as a transport -- gave it a lane
# labelled `> transport`, present and describing something it is not.


def _refill_plan(count: int = 1) -> dict:
    activities: list[dict] = [
        {"kind": "processing", "start": 0, "end": 4, "process": "assay", "mode": "0",
         "node": ["A"], "devices": ["reader"],
         "input_spots": {"plate": "reader.stage"},
         "consumption": {"reader.reagent": 2}},
    ]
    for i in range(count):
        activities.append({
            "kind": "replenishment", "id": f"replenishment_{i}",
            "start": 10 + i * 5, "end": 14 + i * 5,
            "device": "reader", "replenisher": f"dispenser_{i}",
            "amounts": {"reagent": 6},
        })
    return {"activities": activities}


def test_the_device_view_puts_a_refill_on_both_of_its_machines():
    html = render_html(_refill_plan(), view="device")
    assert "dispenser_0 (replenisher)" in html  # solid, the machine doing the work
    assert "reader" in html  # ghosted, the machine it is done to
    assert "+6 reagent" in html


def test_the_lane_view_gives_a_refill_a_lane_of_its_own():
    """Not dataflow -- a refill has no arc and no node -- but leaving it out left the
    picture with an unexplained hole where the device it fills stands idle."""
    html = render_html(_refill_plan(), view="lane")
    assert "+6 reagent" in html


def _lane_count(html: str) -> int:
    return len(re.findall(r">lane \d+</text>", html))


def test_the_lane_view_gives_each_refill_its_own_lane():
    """Two that overlap in a bigger plan have to stay readable, so they do not share."""
    # One dataflow lane for the assay, then one lane per refill.
    assert _lane_count(render_html(_refill_plan(1), view="lane")) == 2
    assert _lane_count(render_html(_refill_plan(2), view="lane")) == 3
    assert render_html(_refill_plan(2), view="lane").count("+6 reagent") == 2


def test_the_workflow_view_names_a_refill_for_what_it_is():
    html = render_html(_refill_plan(), view="workflow")
    assert "+6 reagent" in html
    assert "&gt; transport" not in html and "> transport" not in html


def test_a_refill_draws_no_arrows():
    """It connects nothing to anything: there is no arc to trace."""
    for view in ("device", "lane", "workflow"):
        assert _MARKER not in render_html(_refill_plan(), view=view), view
