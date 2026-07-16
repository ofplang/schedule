"""Fixation data model and low-level readers for the replanning status.

The fixation is what the solver consumes to pin the executed part of a schedule:
per activity / arc index (into the augmented instance) whether it is `completed`
or `running` and at what times. `normalize` builds it from the status document;
`cpsat` reads it. This module also holds the small position-tracking readers
shared by `normalize` (node paths, arc keys, times).
"""

from __future__ import annotations

from dataclasses import dataclass

from ofplang.schedule.core.yamlnode import YMap, YNode, YScalar, YSeq
from ofplang.schedule.scheduler.model import NodePath

# Arc identity key: (source node path, source port, dest node path, dest port).
_ArcKey = tuple[NodePath, str, NodePath, str]


@dataclass(frozen=True)
class ActivityFixation:
    """A fixed processing (or relay) activity: reported status, times, and the
    index of the mode it took (always 0 — a fixed activity has one frozen mode)."""

    status: str  # completed | running
    start: int
    end: int  # actual (completed) or expected finish (running)
    mode_index: int


@dataclass(frozen=True)
class ArcFixation:
    """A fixed transport leg: reported status, times, and the index of the
    transport option it took (always 0 — a fixed leg has one frozen route)."""

    status: str  # completed | running
    start: int
    end: int
    option_index: int


@dataclass(frozen=True)
class Fixation:
    """Everything the solver needs to fix the executed part and re-optimise the
    rest at or after `now`. Keyed by the augmented instance's activity / arc
    indices (relays and legs included)."""

    now: int
    activities: dict[int, ActivityFixation]
    arcs: dict[int, ArcFixation]


# --------------------------------------------------------------------------
# Small readers over the position-tracking node tree (shared with `normalize`).
# --------------------------------------------------------------------------


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


def _to_plain(node: YNode | None):
    if isinstance(node, YScalar):
        return node.value
    if isinstance(node, YSeq):
        return [_to_plain(x) for x in node.items]
    if isinstance(node, YMap):
        return {e.key: _to_plain(e.value) for e in node.entries}
    return None
