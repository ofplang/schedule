"""Render an execution plan (SPECIFICATIONS.md §6) as a self-contained HTML/SVG
Gantt chart. No third-party dependencies: the chart is inline SVG in a single
HTML page that opens in any browser (and adapts to light/dark).

Two views, selected by the caller:

- ``station`` — one lane per device (plus one per transporter). A processing
  activity draws a bar on each device it occupies; a transport draws a solid bar
  on its transporter lane and ghost bars on its source/destination device lanes,
  reflecting the device occupancy of FORMULATION §7. Best for seeing resource
  contention.
- ``workflow`` — one lane per activity (processing and transport), with arrows
  tracing each Object-bearing arc (source → transport → destination). Best for
  reading the dataflow.

The input is the plan dict alone; the device/transporter set is derived from the
activities. A ``now`` marker is drawn when the document carries one (replanning).
"""

from __future__ import annotations

from dataclasses import dataclass

# Layout constants (px).
_LEFT = 200          # label gutter width
_RIGHT = 24
_TOP = 40
_BOTTOM = 28
_ROW = 30            # lane height
_BAR = 20            # bar height within a lane


@dataclass(frozen=True)
class _Bar:
    lane: int
    start: float
    end: float
    label: str
    css: str  # "proc" | "xfer" | "xfer-ghost"


@dataclass(frozen=True)
class _Arrow:
    x1: float
    lane1: int
    x2: float
    lane2: int


def render_svg(plan: dict, *, view: str = "station") -> str:
    """Render `plan` as a standalone SVG document (opens directly in a browser).
    The chart carries its own CSS, background, and title, so it needs no wrapper."""
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + _svg_markup(plan, view)


def render_html(plan: dict, *, view: str = "station") -> str:
    """Render `plan` as an HTML page wrapping the same self-contained SVG."""
    svg = _svg_markup(plan, view)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Schedule</title></head>\n"
        '<body style="margin:0">' + svg + "</body></html>\n"
    )


def _svg_markup(plan: dict, view: str) -> str:
    activities = plan.get("activities") or []
    now = plan.get("now")
    unit = (plan.get("time") or {}).get("unit")
    makespan = (plan.get("objective") or {}).get("value")

    if view == "workflow":
        lanes, bars, arrows = _workflow_layout(activities)
    else:
        view = "station"
        lanes, bars, arrows = _station_layout(activities)

    t_max = makespan if isinstance(makespan, (int, float)) else _max_end(activities)
    return _svg(lanes, bars, arrows, t_max=float(t_max or 0), now=now, unit=unit, view=view, makespan=makespan)


# --------------------------------------------------------------------------
# Lane layouts
# --------------------------------------------------------------------------


def _station_layout(activities):
    """Device lanes then transporter lanes; processing on its devices, transport
    on transporter (solid) + source/destination devices (ghost)."""
    devices: set[str] = set()
    transporters: set[str] = set()
    for a in activities:
        if a.get("kind") == "processing":
            devices.update(a.get("devices") or [])
        elif a.get("kind") == "transport":
            devices.add(_device_of(a.get("from_spot")))
            devices.add(_device_of(a.get("to_spot")))
            if a.get("transporter"):
                transporters.add(a["transporter"])
    devices.discard("")

    lane_labels = [f"{d}" for d in sorted(devices)] + [f"{t} (transporter)" for t in sorted(transporters)]
    index = {}
    for d in sorted(devices):
        index[("dev", d)] = len(index)
    for t in sorted(transporters):
        index[("tr", t)] = len(index)

    bars: list[_Bar] = []
    for a in activities:
        kind = a.get("kind")
        s, e = float(a.get("start", 0)), float(a.get("end", 0))
        if kind == "processing":
            label = _proc_label(a)
            for d in a.get("devices") or []:
                bars.append(_Bar(index[("dev", d)], s, e, label, "proc"))
        elif kind == "transport":
            src, dst = _device_of(a.get("from_spot")), _device_of(a.get("to_spot"))
            tr = a.get("transporter")
            label = _xfer_label(a)
            if tr and ("tr", tr) in index:
                bars.append(_Bar(index[("tr", tr)], s, e, label, "xfer"))
            for d in (src, dst):
                if d and ("dev", d) in index:
                    bars.append(_Bar(index[("dev", d)], s, e, "", "xfer-ghost"))
    return lane_labels, bars, []


def _workflow_layout(activities):
    """One lane per activity (time-ordered); arrows follow each arc from its
    source processing activity through the transport to the destination."""
    ordered = sorted(
        activities,
        key=lambda a: (float(a.get("start", 0)), float(a.get("end", 0)), a.get("kind", "")),
    )
    lane_labels: list[str] = []
    geom: list[tuple[float, float]] = []  # (start, end) per lane
    proc_lane: dict[tuple, int] = {}      # node path -> lane
    transports: list[tuple[int, tuple, tuple]] = []  # (lane, src_node, dst_node)
    bars: list[_Bar] = []

    for a in ordered:
        lane = len(lane_labels)
        s, e = float(a.get("start", 0)), float(a.get("end", 0))
        geom.append((s, e))
        if a.get("kind") == "processing":
            lane_labels.append(_proc_label(a))
            bars.append(_Bar(lane, s, e, _proc_label(a), "proc"))
            proc_lane[tuple(a.get("node") or [])] = lane
        else:
            arc = a.get("arc") or {}
            src = tuple((arc.get("from") or {}).get("node") or [])
            dst = tuple((arc.get("to") or {}).get("node") or [])
            lane_labels.append(_xfer_label(a))
            bars.append(_Bar(lane, s, e, _xfer_label(a), "xfer"))
            transports.append((lane, src, dst))

    arrows: list[_Arrow] = []
    for lane, src, dst in transports:
        s_start, s_end = geom[lane]
        if src in proc_lane:
            src_lane = proc_lane[src]
            arrows.append(_Arrow(geom[src_lane][1], src_lane, s_start, lane))
        if dst in proc_lane:
            dst_lane = proc_lane[dst]
            arrows.append(_Arrow(s_end, lane, geom[dst_lane][0], dst_lane))
    return lane_labels, bars, arrows


# --------------------------------------------------------------------------
# SVG / HTML
# --------------------------------------------------------------------------


_TITLE = 48  # top band for the title/subtitle


def _svg(lane_labels, bars, arrows, *, t_max: float, now, unit, view, makespan) -> str:
    n = len(lane_labels)
    span = t_max if t_max > 0 else 1.0
    plot_w = max(560.0, min(1200.0, span * 12.0))
    scale = plot_w / span
    width = _LEFT + plot_w + _RIGHT
    chart_h = _TOP + n * _ROW + _BOTTOM
    height = _TITLE + chart_h

    def x(t: float) -> float:
        return _LEFT + t * scale

    def lane_y(i: int) -> float:
        return _TOP + i * _ROW

    span_note = ""
    if makespan is not None:
        span_note = f"makespan {makespan}" + (f" {unit}" if unit else "")

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" font-family="system-ui, sans-serif" font-size="12">'
    )
    # Self-contained styling, background, and title so the SVG stands alone.
    parts.append("<style>" + _STYLE + "</style>")
    parts.append(f'<rect class="bg" x="0" y="0" width="{width:.0f}" height="{height:.0f}"/>')
    parts.append(f'<text class="title" x="16" y="24">Schedule &#8212; {_esc(view)} view</text>')
    if span_note:
        parts.append(f'<text class="subtitle" x="16" y="40">{_esc(span_note)}</text>')
    parts.append(
        '<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">'
        '<path d="M0,0 L7,3 L0,6 Z" class="arrowhead"/></marker></defs>'
    )
    # All chart content lives below the title band.
    parts.append(f'<g transform="translate(0,{_TITLE})">')

    # Time grid + axis ticks.
    step = _tick_step(span)
    t = 0.0
    while t <= span + 1e-9:
        gx = x(t)
        parts.append(f'<line class="grid" x1="{gx:.1f}" y1="{_TOP - 6:.1f}" x2="{gx:.1f}" y2="{height - _BOTTOM:.1f}"/>')
        parts.append(f'<text class="tick" x="{gx:.1f}" y="{_TOP - 12:.1f}" text-anchor="middle">{_fmt(t)}</text>')
        t += step
    axis_label = f"time ({unit})" if unit else "time"
    parts.append(f'<text class="axis" x="{_LEFT:.1f}" y="{height - 8:.1f}">{_esc(axis_label)}</text>')

    # Lane labels + separators.
    for i, label in enumerate(lane_labels):
        y = lane_y(i)
        parts.append(f'<line class="lane" x1="0" y1="{y + _ROW:.1f}" x2="{width:.1f}" y2="{y + _ROW:.1f}"/>')
        parts.append(
            f'<text class="lanelabel" x="{_LEFT - 10:.1f}" y="{y + _ROW / 2 + 4:.1f}" text-anchor="end">{_esc(label)}</text>'
        )

    # `now` marker (replanning).
    if isinstance(now, (int, float)):
        nx = x(float(now))
        parts.append(f'<line class="now" x1="{nx:.1f}" y1="{_TOP - 6:.1f}" x2="{nx:.1f}" y2="{height - _BOTTOM:.1f}"/>')
        parts.append(f'<text class="nowlabel" x="{nx + 3:.1f}" y="{_TOP - 12:.1f}">now={_fmt(float(now))}</text>')

    # Arrows (workflow view).
    for a in arrows:
        y1 = lane_y(a.lane1) + _ROW / 2
        y2 = lane_y(a.lane2) + _ROW / 2
        parts.append(
            f'<path class="arrow" d="M{x(a.x1):.1f},{y1:.1f} C{x(a.x1) + 16:.1f},{y1:.1f} '
            f'{x(a.x2) - 16:.1f},{y2:.1f} {x(a.x2):.1f},{y2:.1f}" marker-end="url(#ah)"/>'
        )

    # Bars.
    for b in bars:
        bx = x(b.start)
        bw = max(2.0, (b.end - b.start) * scale)
        by = lane_y(b.lane) + (_ROW - _BAR) / 2
        parts.append(
            f'<rect class="bar {b.css}" x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{_BAR}" rx="3"/>'
        )
        if b.label and bw > 26:
            parts.append(
                f'<text class="barlabel {b.css}" x="{bx + 4:.1f}" y="{by + _BAR - 6:.1f}">{_esc(_clip(b.label, bw))}</text>'
            )

    parts.append("</g></svg>")
    return "".join(parts)


# CSS embedded inside the SVG's <style> so the chart is self-contained and
# adapts to light/dark. SVG text is coloured via `fill`; lines via `stroke`.
_STYLE = """
  :root {
    --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --grid: #e3e3e3; --lane: #f0f0f0;
    --proc: #3b82f6; --proc-fg: #ffffff; --xfer: #f59e0b; --xfer-fg: #3a2a00;
    --ghost: #f59e0b33; --arrow: #9333ea; --now: #dc2626;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --fg: #e6e6e6; --muted: #9aa0a6; --grid: #262a31; --lane: #1a1d23;
      --proc: #60a5fa; --proc-fg: #06131f; --xfer: #fbbf24; --xfer-fg: #241a00;
      --ghost: #fbbf2433; --arrow: #c084fc; --now: #f87171;
    }
  }
  .bg { fill: var(--bg); }
  .title { font-size: 15px; font-weight: 600; fill: var(--fg); }
  .subtitle { font-size: 12px; fill: var(--muted); }
  .grid { stroke: var(--grid); stroke-width: 1; }
  .lane { stroke: var(--lane); stroke-width: 1; }
  .tick, .axis { fill: var(--muted); }
  .lanelabel { fill: var(--fg); }
  .bar.proc { fill: var(--proc); }
  .bar.xfer { fill: var(--xfer); }
  .bar.xfer-ghost { fill: var(--ghost); }
  .barlabel.proc { fill: var(--proc-fg); }
  .barlabel.xfer { fill: var(--xfer-fg); }
  .arrow { stroke: var(--arrow); stroke-width: 1.3; fill: none; opacity: 0.8; }
  .arrowhead { fill: var(--arrow); }
  .now { stroke: var(--now); stroke-width: 1.5; stroke-dasharray: 4 3; }
  .nowlabel { fill: var(--now); }
"""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _device_of(qualified_spot) -> str:
    if isinstance(qualified_spot, str) and "." in qualified_spot:
        return qualified_spot.split(".", 1)[0]
    return ""


def _proc_label(a: dict) -> str:
    node = a.get("node") or []
    return "/".join(str(x) for x in node) or str(a.get("process", "?"))


def _xfer_label(a: dict) -> str:
    to = (a.get("arc") or {}).get("to") or {}
    node = to.get("node") or []
    return "▸ " + ("/".join(str(x) for x in node) if node else "transport")


def _max_end(activities) -> float:
    return max((float(a.get("end", 0)) for a in activities), default=0.0)


def _tick_step(span: float) -> float:
    raw = span / 10 if span > 0 else 1
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000):
        if step >= raw:
            return float(step)
    return 10000.0


def _fmt(t: float) -> str:
    return str(int(t)) if float(t).is_integer() else f"{t:g}"


def _clip(text: str, width_px: float) -> str:
    max_chars = max(1, int((width_px - 8) / 6.5))
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
