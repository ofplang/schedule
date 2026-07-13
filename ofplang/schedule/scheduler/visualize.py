"""Render an execution plan (SPECIFICATIONS.md §6) as a self-contained HTML/SVG
Gantt chart. No third-party dependencies: the chart is inline SVG (see `render_svg`
/ `render_html`, and the `theme` colours in `_PALETTES`).

Three views, selected by the caller:

- ``device`` — one lane per device (plus one per transporter). A processing
  activity draws a bar on each device it occupies; a transport draws a solid bar
  on its transporter lane and ghost bars on its source/destination device lanes,
  reflecting the device occupancy of FORMULATION §7. Best for resource contention.
- ``workflow`` — one lane per activity (processing and transport), with arrows
  tracing each Object-bearing arc (source → transport → destination). Best for
  reading the dataflow one activity at a time.
- ``lane`` — dataflow swimlanes (ofp-scheduler style): a chain of processing
  activities shares one lane, a split branches into new lanes, and a merge
  rejoins to the lowest lane, so extra lanes appear only where work runs in
  parallel. Best for seeing concurrency compactly.

The input is the plan dict alone; the device/transporter set and the dataflow are
derived from the activities. A ``now`` marker is drawn when the document carries
one (replanning).
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


def render_svg(plan: dict, *, view: str = "station", theme: str = "light") -> str:
    """Render `plan` as a standalone SVG document (opens directly in a browser).

    `theme` is "light" or "dark" (fixed colours written as inline presentation
    attributes — compatible with PowerPoint's SVG renderer), or "auto" (a CSS
    `<style>` block that adapts to the viewer's light/dark preference; browsers
    only). The chart is always self-contained (no external CSS/fonts/scripts)."""
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + _svg_markup(plan, view, theme)


def render_html(plan: dict, *, view: str = "station", theme: str = "light") -> str:
    """Render `plan` as an HTML page wrapping the same self-contained SVG."""
    svg = _svg_markup(plan, view, theme)
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Schedule</title></head>\n"
        '<body style="margin:0">' + svg + "</body></html>\n"
    )


def _svg_markup(plan: dict, view: str, theme: str) -> str:
    if theme not in ("auto", "light", "dark"):
        theme = "light"
    activities = plan.get("activities") or []
    now = plan.get("now")
    unit = (plan.get("time") or {}).get("unit")
    makespan = (plan.get("objective") or {}).get("value")

    if view == "workflow":
        lanes, bars, arrows = _workflow_layout(activities)
    elif view == "lane":
        lanes, bars, arrows = _lane_layout(activities)
    else:
        view = "device"
        lanes, bars, arrows = _device_layout(activities)

    t_max = makespan if isinstance(makespan, (int, float)) else _max_end(activities)
    return _svg(lanes, bars, arrows, t_max=float(t_max or 0), now=now, unit=unit, view=view, makespan=makespan, theme=theme)


# --------------------------------------------------------------------------
# Lane layouts
# --------------------------------------------------------------------------


def _device_layout(activities):
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


def _lane_layout(activities):
    """Dataflow swimlanes (ofp-scheduler style). Follow the Object-bearing arcs:
    a straight chain of processing activities keeps one lane; a split (a step
    feeding several successors) branches into a new lane per extra output; a
    merge (a step with several predecessors) rejoins to the lowest incoming lane.
    So extra lanes appear only where work runs in parallel. Transports ride the
    lane of the branch they serve; arrows trace each arc through its transport."""
    proc = [a for a in activities if a.get("kind") == "processing"]
    xfer = [a for a in activities if a.get("kind") == "transport"]
    visible = {tuple(a.get("node") or []) for a in proc}

    # Predecessors / successors from the transport arcs.
    preds: dict[tuple, list[tuple]] = {}
    succs: dict[tuple, list[tuple]] = {}
    for t in xfer:
        arc = t.get("arc") or {}
        src = tuple((arc.get("from") or {}).get("node") or [])
        sport = (arc.get("from") or {}).get("port")
        dst = tuple((arc.get("to") or {}).get("node") or [])
        if src in visible and dst in visible:
            preds.setdefault(dst, []).append((src, sport))
            succs.setdefault(src, []).append((sport, dst))

    # Assign a lane to each processing node, in start-time order.
    lane_by_node: dict[tuple, int] = {}
    out_lane_by_port: dict[tuple, int] = {}
    next_lane = 0
    for a in sorted(proc, key=lambda a: (float(a.get("start", 0)), tuple(a.get("node") or []))):
        node = tuple(a.get("node") or [])
        pp = preds.get(node, [])
        if not pp:  # source: its own lane
            lane_by_node[node] = next_lane
            next_lane += 1
            continue
        if len(pp) > 1:  # merge: lowest predecessor lane
            cand = [lane_by_node[pn] for pn, _ in pp if pn in lane_by_node]
            lane_by_node[node] = min(cand) if cand else next_lane
            if not cand:
                next_lane += 1
            continue
        pred_node, pred_port = pp[0]
        pred_lane = lane_by_node.get(pred_node)
        if pred_lane is None:
            lane_by_node[node] = next_lane
            next_lane += 1
            continue
        # Distinct visible successor output ports of the predecessor.
        ports: list = []
        for sport, snode in succs.get(pred_node, []):
            if snode in visible and sport not in ports:
                ports.append(sport)
        if len(ports) <= 1:  # straight chain: stay in lane
            lane_by_node[node] = pred_lane
            continue
        # Split: the first output port keeps the lane, the rest branch off.
        for idx, op in enumerate(ports):
            key = (pred_node, op)
            if key not in out_lane_by_port:
                out_lane_by_port[key] = pred_lane if idx == 0 else next_lane
                if idx != 0:
                    next_lane += 1
        lane_by_node[node] = out_lane_by_port[(pred_node, pred_port)]

    proc_geom = {
        tuple(a.get("node") or []): (
            lane_by_node.get(tuple(a.get("node") or []), 0),
            float(a.get("start", 0)),
            float(a.get("end", 0)),
        )
        for a in proc
    }

    # Transport lanes: bias toward the endpoint on the branching side.
    extra = max(lane_by_node.values(), default=-1) + 1
    xfer_info = []  # (lane, start, end, src, dst)
    for t in xfer:
        arc = t.get("arc") or {}
        src = tuple((arc.get("from") or {}).get("node") or [])
        dst = tuple((arc.get("to") or {}).get("node") or [])
        s, e = float(t.get("start", 0)), float(t.get("end", 0))
        if src in lane_by_node and len(succs.get(src, [])) > 1 and dst in lane_by_node:
            lane = lane_by_node[dst]
        elif src in lane_by_node:
            lane = lane_by_node[src]
        else:
            lane = extra
            extra += 1
        xfer_info.append((lane, s, e, src, dst))

    # Remap the lanes actually used to contiguous indices.
    used = sorted({g[0] for g in proc_geom.values()} | {xi[0] for xi in xfer_info})
    remap = {lane: i for i, lane in enumerate(used)}
    labels = [f"lane {i + 1}" for i in range(len(used))]

    bars: list[_Bar] = []
    for a in proc:
        node = tuple(a.get("node") or [])
        bars.append(_Bar(remap[lane_by_node[node]], float(a.get("start", 0)), float(a.get("end", 0)), _proc_label(a), "proc"))
    for (lane, s, e, src, dst), t in zip(xfer_info, xfer):
        bars.append(_Bar(remap[lane], s, e, _xfer_label(t), "xfer"))

    # Arrows: source proc -> transport -> destination proc.
    arrows: list[_Arrow] = []
    for lane, s, e, src, dst in xfer_info:
        xlane = remap[lane]
        if src in proc_geom:
            pl, _ps, pe = proc_geom[src]
            arrows.append(_Arrow(pe, remap[pl], s, xlane))
        if dst in proc_geom:
            pl, ps, _pe = proc_geom[dst]
            arrows.append(_Arrow(e, xlane, ps, remap[pl]))
    return labels, bars, arrows


# --------------------------------------------------------------------------
# SVG / HTML
# --------------------------------------------------------------------------


_TITLE = 48  # top band for the title/subtitle

# Concrete colour palettes. `light` is the default and is what the
# PowerPoint-safe (inline-attribute) output uses; `dark` is the same idea with a
# dark fixed theme. Values mirror the CSS variables used by the "auto" theme.
_PALETTES = {
    "light": {
        "fg": "#1a1a1a", "muted": "#666666",
        "grid": "#b3b3b3", "lane": "#d9d9d9",
        "proc": "#3b82f6", "proc_fg": "#ffffff",
        "xfer": "#f59e0b", "xfer_fg": "#3a2a00",
        "ghost": "#f59e0b", "arrow": "#9333ea", "now": "#dc2626",
    },
    "dark": {
        "fg": "#e6e6e6", "muted": "#9aa0a6",
        "grid": "#3a3f47", "lane": "#2a2e35",
        "proc": "#60a5fa", "proc_fg": "#06131f",
        "xfer": "#fbbf24", "xfer_fg": "#241a00",
        "ghost": "#fbbf24", "arrow": "#c084fc", "now": "#f87171",
    },
}

# Each semantic role: fill vs stroke, its palette colour, and extra presentation
# attributes (opacity, stroke width, dash). Opacity is kept separate from the
# colour so no 8-digit hex is ever emitted (PowerPoint renders those as black).
_ROLE = {
    "title": ("fill", "fg", {}),
    "subtitle": ("fill", "muted", {}),
    "tick": ("fill", "muted", {}),
    "axis": ("fill", "muted", {}),
    "lanelabel": ("fill", "fg", {}),
    "nowlabel": ("fill", "now", {}),
    "grid": ("stroke", "grid", {"w": "1"}),
    "lane": ("stroke", "lane", {"w": "1"}),
    "now": ("stroke", "now", {"w": "1.5", "dash": "4 3"}),
    "proc": ("fill", "proc", {}),
    "xfer": ("fill", "xfer", {}),
    "ghost": ("fill", "ghost", {"op": "0.2"}),
    "barlabel-proc": ("fill", "proc_fg", {}),
    "barlabel-xfer": ("fill", "xfer_fg", {}),
    "arrow": ("stroke", "arrow", {"w": "1.3", "op": "0.8", "fillnone": True}),
    "arrowhead": ("fill", "arrow", {}),
}

# Class names for the CSS ("auto") theme, matching _STYLE.
_CLASS = {
    "title": "title", "subtitle": "subtitle", "tick": "tick",
    "axis": "axis", "lanelabel": "lanelabel", "nowlabel": "nowlabel",
    "grid": "grid", "lane": "lane", "now": "now",
    "proc": "bar proc", "xfer": "bar xfer", "ghost": "bar xfer-ghost",
    "barlabel-proc": "barlabel proc", "barlabel-xfer": "barlabel xfer",
    "arrow": "arrow", "arrowhead": "arrowhead",
}


def _attr(role: str, theme: str) -> str:
    """Attributes for one element's role. In the "auto" theme this is a CSS class
    (the <style> block colours it); otherwise it is explicit presentation
    attributes with concrete colours and separate opacity, so it survives
    PowerPoint's SVG renderer (no CSS variables, no 8-digit hex)."""
    if theme == "auto":
        return f'class="{_CLASS[role]}"'
    kind, key, ex = _ROLE[role]
    color = _PALETTES[theme][key]
    if kind == "fill":
        out = f'fill="{color}"'
        if "op" in ex:
            out += f' fill-opacity="{ex["op"]}"'
        return out
    tokens = []
    if ex.get("fillnone"):
        tokens.append('fill="none"')
    tokens.append(f'stroke="{color}"')
    tokens.append(f'stroke-width="{ex.get("w", "1")}"')
    if "op" in ex:
        tokens.append(f'stroke-opacity="{ex["op"]}"')
    if "dash" in ex:
        tokens.append(f'stroke-dasharray="{ex["dash"]}"')
    return " ".join(tokens)


def _svg(lane_labels, bars, arrows, *, t_max: float, now, unit, view, makespan, theme) -> str:
    n = len(lane_labels)
    span = t_max if t_max > 0 else 1.0
    plot_w = max(560.0, min(1200.0, span * 12.0))
    scale = plot_w / span
    width = _LEFT + plot_w + _RIGHT
    chart_h = _TOP + n * _ROW + _BOTTOM
    height = _TITLE + chart_h
    bottom = chart_h - _BOTTOM  # y of the last lane's edge, within the chart group

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
    # CSS is emitted only for the browser-adaptive "auto" theme; the fixed
    # light/dark themes paint every element with inline attributes instead.
    if theme == "auto":
        parts.append("<style>" + _STYLE + "</style>")
    # No background rect: the chart background is transparent so it blends into
    # whatever it is placed on (slide, page, dark/light UI).
    parts.append(
        f'<text {_attr("title", theme)} font-size="15" font-weight="600" x="16" y="24">'
        f'Schedule &#8212; {_esc(view)} view</text>'
    )
    if span_note:
        parts.append(f'<text {_attr("subtitle", theme)} x="16" y="40">{_esc(span_note)}</text>')
    parts.append(
        '<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">'
        f'<path d="M0,0 L7,3 L0,6 Z" {_attr("arrowhead", theme)}/></marker></defs>'
    )
    # All chart content lives below the title band.
    parts.append(f'<g transform="translate(0,{_TITLE})">')

    # Time grid + axis ticks.
    step = _tick_step(span)
    t = 0.0
    while t <= span + 1e-9:
        gx = x(t)
        parts.append(f'<line {_attr("grid", theme)} x1="{gx:.1f}" y1="{_TOP - 6:.1f}" x2="{gx:.1f}" y2="{bottom:.1f}"/>')
        parts.append(f'<text {_attr("tick", theme)} x="{gx:.1f}" y="{_TOP - 12:.1f}" text-anchor="middle">{_fmt(t)}</text>')
        t += step
    axis_label = f"time ({unit})" if unit else "time"
    parts.append(f'<text {_attr("axis", theme)} x="{_LEFT:.1f}" y="{chart_h - 8:.1f}">{_esc(axis_label)}</text>')

    # Lane labels + separators.
    for i, label in enumerate(lane_labels):
        y = lane_y(i)
        parts.append(f'<line {_attr("lane", theme)} x1="0" y1="{y + _ROW:.1f}" x2="{width:.1f}" y2="{y + _ROW:.1f}"/>')
        parts.append(
            f'<text {_attr("lanelabel", theme)} x="{_LEFT - 10:.1f}" y="{y + _ROW / 2 + 4:.1f}" text-anchor="end">{_esc(label)}</text>'
        )

    # `now` marker (replanning).
    if isinstance(now, (int, float)):
        nx = x(float(now))
        parts.append(f'<line {_attr("now", theme)} x1="{nx:.1f}" y1="{_TOP - 6:.1f}" x2="{nx:.1f}" y2="{bottom:.1f}"/>')
        parts.append(f'<text {_attr("nowlabel", theme)} x="{nx + 3:.1f}" y="{_TOP - 12:.1f}">now={_fmt(float(now))}</text>')

    # Arrows (workflow view).
    for a in arrows:
        y1 = lane_y(a.lane1) + _ROW / 2
        y2 = lane_y(a.lane2) + _ROW / 2
        parts.append(
            f'<path {_attr("arrow", theme)} d="M{x(a.x1):.1f},{y1:.1f} C{x(a.x1) + 16:.1f},{y1:.1f} '
            f'{x(a.x2) - 16:.1f},{y2:.1f} {x(a.x2):.1f},{y2:.1f}" marker-end="url(#ah)"/>'
        )

    # Bars.
    for b in bars:
        bx = x(b.start)
        bw = max(2.0, (b.end - b.start) * scale)
        by = lane_y(b.lane) + (_ROW - _BAR) / 2
        role = "ghost" if b.css == "xfer-ghost" else b.css  # proc | xfer | ghost
        parts.append(f'<rect {_attr(role, theme)} x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{_BAR}" rx="3"/>')
        if b.label and bw > 26:
            lbl_role = "barlabel-xfer" if b.css == "xfer" else "barlabel-proc"
            parts.append(
                f'<text {_attr(lbl_role, theme)} x="{bx + 4:.1f}" y="{by + _BAR - 6:.1f}">{_esc(_clip(b.label, bw))}</text>'
            )

    parts.append("</g></svg>")
    return "".join(parts)


# CSS embedded inside the SVG's <style> so the chart is self-contained and
# adapts to light/dark. SVG text is coloured via `fill`; lines via `stroke`.
_STYLE = """
  :root {
    --fg: #1a1a1a; --muted: #666; --grid: #b3b3b3; --lane: #d9d9d9;
    --proc: #3b82f6; --proc-fg: #ffffff; --xfer: #f59e0b; --xfer-fg: #3a2a00;
    --ghost: #f59e0b33; --arrow: #9333ea; --now: #dc2626;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --fg: #e6e6e6; --muted: #9aa0a6; --grid: #3a3f47; --lane: #2a2e35;
      --proc: #60a5fa; --proc-fg: #06131f; --xfer: #fbbf24; --xfer-fg: #241a00;
      --ghost: #fbbf2433; --arrow: #c084fc; --now: #f87171;
    }
  }
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
    # ASCII prefix (">") keeps output printable on any console encoding.
    return "> " + ("/".join(str(x) for x in node) if node else "transport")


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
