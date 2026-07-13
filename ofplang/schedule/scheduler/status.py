"""Match an execution status against an instance and build the solver fixation.

A replanning input is an execution document (SPECIFICATIONS.md §6/§7) with `now`
set and a `status` (`completed` / `running`) on the activities that have started.
This module reads that document — as the position-tracking node tree, so its
diagnostics carry `file:line:col` like the schema validators — and matches each
started activity against the built `Instance` by provenance: a processing
activity by its `node` path, a transport by its `arc` (§6.6). The result is a
`Fixation`: for each matched activity/arc, which mode/route it took and its
reported start/end, plus `now` and the carried-through `placements`.

Only `completed` / `running` entries are matched; `pending` (or status-less)
entries are ignored and re-derived from the workflow, so a prior plan can be fed
back verbatim as the next replan input (its pending times are simply discarded).

The document has already passed the §9.2 shape validator when this runs, so the
structure is trusted; what remains are the cross-document checks (§9.3):
provenance must resolve, the reported mode/route must exist, the reported times
must be consistent with `now`, no activity may be fixed twice, and — per the
FORMULATION §9 normalization assumption — a started transport must not feed a
pending processing activity.
"""

from __future__ import annotations

from dataclasses import dataclass

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.core.identifiers import format_node_path
from ofplang.schedule.core.yamlnode import YMap, YNode, YScalar, YSeq
from ofplang.schedule.scheduler.instance import Instance
from ofplang.schedule.scheduler.model import NodePath
from ofplang.schedule.validation import errors

# Arc identity key: (source node path, source port, dest node path, dest port).
_ArcKey = tuple[NodePath, str, NodePath, str]


@dataclass(frozen=True)
class ActivityFixation:
    """A fixed processing activity: its reported status, times, and mode index."""

    status: str  # completed | running
    start: int
    end: int  # actual (completed) or expected finish (running)
    mode_index: int


@dataclass(frozen=True)
class ArcFixation:
    """A fixed transport: its reported status, times, and transport-option index."""

    status: str  # completed | running
    start: int
    end: int
    option_index: int


@dataclass(frozen=True)
class Fixation:
    """Everything the solver needs to fix the executed part and re-optimise the
    rest at or after `now`. Keyed by the instance's activity / arc indices."""

    now: int
    activities: dict[int, ActivityFixation]
    arcs: dict[int, ArcFixation]
    # Carried through to the output verbatim; not consumed as a constraint here.
    placements: list


def build_fixation(root: YNode | None, instance: Instance) -> tuple[Fixation | None, Diagnostics]:
    """Match the status document `root` against `instance`. Returns the fixation
    (None if any error was reported) and the diagnostics."""
    diags = Diagnostics()
    if not isinstance(root, YMap):
        # The shape validator already rejects a non-mapping document; guard anyway.
        diags.error(errors.WRONG_TYPE, "status document must be a mapping", "")
        return None, diags

    now_node = root.get("now")
    if not (isinstance(now_node, YScalar) and now_node.is_int):
        diags.error(errors.STATUS_MISSING_NOW, "a replanning status must set now", "now", at=root)
        return None, diags
    now = now_node.value

    node_index = {act.node: i for i, act in enumerate(instance.activities)}
    arc_index = _arc_index(instance)

    activities: dict[int, ActivityFixation] = {}
    arcs: dict[int, ArcFixation] = {}

    activities_node = root.get("activities")
    items = activities_node.items if isinstance(activities_node, YSeq) else []
    for i, item in enumerate(items):
        if not isinstance(item, YMap):
            continue
        status = _status_of(item)
        if status not in ("completed", "running"):
            continue  # pending / status-less: re-derived from the workflow
        base = f"activities[{i}]"
        kind = _text(item.get("kind"))
        if kind == "processing":
            _match_processing(item, base, status, now, node_index, instance, activities, diags)
        elif kind == "transport":
            _match_transport(item, base, status, now, arc_index, instance, arcs, diags)

    # Normalization + route/mode agreement checks need every fixation collected.
    _check_transports_against_endpoints(instance, activities, arcs, items, diags)

    if any(d.severity == "error" for d in diags.items):
        return None, diags
    return Fixation(now, activities, arcs, _placements(root)), diags


def _match_processing(
    item: YMap,
    base: str,
    status: str,
    now: int,
    node_index: dict[NodePath, int],
    instance: Instance,
    activities: dict[int, ActivityFixation],
    diags: Diagnostics,
) -> None:
    node_node = item.get("node")
    path = _node_path(node_node)
    idx = node_index.get(path)
    if idx is None:
        diags.error(
            errors.STATUS_NODE_UNKNOWN,
            f"status references a processing node not in the workflow: {format_node_path(path)}",
            f"{base}.node",
            at=node_node or item,
        )
        return
    if idx in activities:
        diags.error(errors.STATUS_DUPLICATE, f"processing node {format_node_path(path)} is fixed more than once", base, at=item)
        return

    mode_node = item.get("mode")
    mode_id = _text(mode_node)
    mode_index = _mode_index(instance, idx, mode_id)
    if mode_index is None:
        diags.error(
            errors.STATUS_MODE_UNKNOWN,
            f"process does not offer mode {mode_id!r} for node {format_node_path(path)}",
            f"{base}.mode",
            at=mode_node or item,
        )
        return

    start, end = _times(item)
    _check_times(status, start, end, now, item, base, diags)
    activities[idx] = ActivityFixation(status, start, end, mode_index)


def _match_transport(
    item: YMap,
    base: str,
    status: str,
    now: int,
    arc_index: dict[_ArcKey, int],
    instance: Instance,
    arcs: dict[int, ArcFixation],
    diags: Diagnostics,
) -> None:
    key = _arc_key(item.get("arc"))
    idx = arc_index.get(key) if key is not None else None
    if idx is None:
        diags.error(
            errors.STATUS_ARC_UNKNOWN,
            "status references a transport arc not in the workflow",
            f"{base}.arc",
            at=item.get("arc") or item,
        )
        return
    if idx in arcs:
        diags.error(errors.STATUS_DUPLICATE, "transport arc is fixed more than once", base, at=item)
        return

    transporter = _text(item.get("transporter"))
    from_spot = _text(item.get("from_spot"))
    to_spot = _text(item.get("to_spot"))
    option_index = _option_index(instance, idx, transporter, from_spot, to_spot)
    if option_index is None:
        diags.error(
            errors.STATUS_ROUTE_UNKNOWN,
            f"no transport option matches {transporter} {from_spot} -> {to_spot} for this arc",
            base,
            at=item,
        )
        return

    start, end = _times(item)
    _check_times(status, start, end, now, item, base, diags)
    arcs[idx] = ArcFixation(status, start, end, option_index)


def _check_transports_against_endpoints(
    instance: Instance,
    activities: dict[int, ActivityFixation],
    arcs: dict[int, ArcFixation],
    items: list[YNode],
    diags: Diagnostics,
) -> None:
    """For every fixed transport, enforce the normalization assumption and the
    route/endpoint-mode agreement. The destination processing must be fixed too
    (else the input is unnormalized, §9); if an endpoint is fixed, its mode must
    be the one the fixed route implies. Errors are anchored to the transport
    entry, found by its arc index among the status activities."""
    node_for_arc = _transport_nodes(items, arcs, instance)
    for arc_idx, fix in arcs.items():
        arc = instance.arcs[arc_idx]
        option = arc.options[fix.option_index]
        at = node_for_arc.get(arc_idx)

        dst_fix = activities.get(arc.dst_activity)
        if dst_fix is None:
            diags.error(
                errors.STATUS_UNNORMALIZED,
                "a started transport feeds a pending processing activity; normalize before replanning",
                "",
                at=at,
            )
        elif dst_fix.mode_index != option.dst_mode_index:
            diags.error(
                errors.STATUS_ROUTE_INCONSISTENT,
                "fixed transport route implies a destination mode other than the one fixed on that activity",
                "",
                at=at,
            )

        src_fix = activities.get(arc.src_activity)
        if src_fix is not None and src_fix.mode_index != option.src_mode_index:
            diags.error(
                errors.STATUS_ROUTE_INCONSISTENT,
                "fixed transport route implies a source mode other than the one fixed on that activity",
                "",
                at=at,
            )


# --------------------------------------------------------------------------
# Index builders and small readers over the position-tracking node tree.
# --------------------------------------------------------------------------


def _arc_index(instance: Instance) -> dict[_ArcKey, int]:
    index: dict[_ArcKey, int] = {}
    for i, arc in enumerate(instance.arcs):
        index[(arc.arc.src.node, arc.arc.src.port, arc.arc.dst.node, arc.arc.dst.port)] = i
    return index


def _mode_index(instance: Instance, activity: int, mode_id: str) -> int | None:
    for m, mode in enumerate(instance.activities[activity].modes):
        if mode.id == mode_id:
            return m
    return None


def _option_index(instance: Instance, arc: int, transporter: str, from_spot: str, to_spot: str) -> int | None:
    for k, opt in enumerate(instance.arcs[arc].options):
        if opt.transporter == transporter and opt.from_spot == from_spot and opt.to_spot == to_spot:
            return k
    return None


def _transport_nodes(items: list[YNode], arcs: dict[int, ArcFixation], instance: Instance) -> dict[int, YNode]:
    """Map each fixed arc index back to its status entry node (for diagnostics)."""
    arc_index = _arc_index(instance)
    found: dict[int, YNode] = {}
    for item in items:
        if not isinstance(item, YMap) or _text(item.get("kind")) != "transport":
            continue
        key = _arc_key(item.get("arc"))
        idx = arc_index.get(key) if key is not None else None
        if idx is not None and idx in arcs:
            found.setdefault(idx, item)
    return found


def _status_of(item: YMap) -> str:
    node = item.get("status")
    return node.value if isinstance(node, YScalar) and node.is_str else "pending"


def _text(node: YNode | None) -> str:
    return node.value if isinstance(node, YScalar) and node.is_str else ""


def _times(item: YMap) -> tuple[int, int]:
    start = item.get("start")
    end = item.get("end")
    s = start.value if isinstance(start, YScalar) and start.is_int else 0
    e = end.value if isinstance(end, YScalar) and end.is_int else 0
    return s, e


def _check_times(status: str, start: int, end: int, now: int, item: YMap, base: str, diags: Diagnostics) -> None:
    # A completed activity must have finished by now; a running one must have
    # started by now. A running activity whose expected end is already past now
    # is a legitimate overrun (the solver clamps it to now + margin), so `end`
    # is not constrained here.
    if status == "completed" and end > now:
        diags.error(errors.STATUS_TIME_INCONSISTENT, "completed activity ends after now", f"{base}.end", at=item.get("end") or item)
    elif status == "running" and start > now:
        diags.error(errors.STATUS_TIME_INCONSISTENT, "running activity starts after now", f"{base}.start", at=item.get("start") or item)


def _node_path(node: YNode | None) -> NodePath:
    if not isinstance(node, YSeq):
        return ()
    return tuple(x.value for x in node.items if isinstance(x, YScalar) and x.is_str)


def _arc_key(node: YNode | None) -> _ArcKey | None:
    if not isinstance(node, YMap):
        return None
    frm, to = node.get("from"), node.get("to")
    if not isinstance(frm, YMap) or not isinstance(to, YMap):
        return None
    return (_node_path(frm.get("node")), _text(frm.get("port")), _node_path(to.get("node")), _text(to.get("port")))


def _placements(root: YMap) -> list:
    """Carry `placements` through to the output as plain Python, verbatim."""
    node = root.get("placements")
    plain = _to_plain(node)
    return plain if isinstance(plain, list) else []


def _to_plain(node: YNode | None):
    if isinstance(node, YScalar):
        return node.value
    if isinstance(node, YSeq):
        return [_to_plain(x) for x in node.items]
    if isinstance(node, YMap):
        return {e.key: _to_plain(e.value) for e in node.entries}
    return None
