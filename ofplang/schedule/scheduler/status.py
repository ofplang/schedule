"""Fixation data model and low-level readers for the replanning status.

The fixation is what the solver consumes to pin the executed part of a schedule:
per activity / arc index (into the augmented instance) whether it is `completed`
or `running` and at what times. `normalize` builds it from the status document;
`cpsat` reads it. This module also holds the small position-tracking readers over a
status document -- node paths, arc keys, statuses, times -- which are public because
`normalize` reads status entries with them: they belong to whoever reads a status, not
to this module alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ofplang.schedule.core.yamlnode import YMap, YNode, YScalar, YSeq
from ofplang.schedule.scheduler.model import NodePath

# Arc identity key: (source node path, source port, dest node path, dest port).
ArcKey = tuple[NodePath, str, NodePath, str]


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
class RefillFixation:
    """A started refill read back from the status (§6.9), matched by its `id`.

    Its effect is where it differs from every other fixed activity. A `completed`
    one has already raised the level and is folded into the levels at `now`; a
    `running` one has not landed yet and enters the model as a fixed increase at its
    end. That split is the whole of its role -- nothing re-derives it, and nothing
    re-validates it against the current environment (§7).
    """

    status: str  # completed | running
    start: int
    end: int
    device: str
    replenisher: str
    amounts: dict[str, int]


@dataclass(frozen=True)
class Fixation:
    """Everything the solver needs to fix the executed part and re-optimise the
    rest at or after `now`. Keyed by the augmented instance's activity / arc
    indices (relays and legs included)."""

    now: int
    activities: dict[int, ActivityFixation]
    arcs: dict[int, ArcFixation]
    # Consumable levels at `now`, keyed by (device, resource). Derived, not
    # reported: the document gives what the run *started* with and the history says
    # what has been drawn since (§4.7.2), so this is the other half of what the
    # executed part settles -- which is why it belongs here rather than being passed
    # alongside. Empty when the resource model is not in effect.
    levels: dict[tuple[str, str], int] = field(default_factory=dict)
    # Started refills, by `id`. Only `running` ones reach the solver (as a fixed
    # future increase); a `completed` one is already inside `levels`. Both are kept
    # because both are history the plan has to carry back out.
    replenishments: dict[str, RefillFixation] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Small readers over the position-tracking node tree (shared with `normalize`).
# --------------------------------------------------------------------------


def status_of(item: YMap) -> str:
    node = item.get("status")
    return node.value if isinstance(node, YScalar) and node.is_str else "pending"


def text(node: YNode | None) -> str:
    return node.value if isinstance(node, YScalar) and node.is_str else ""


def times(item: YMap) -> tuple[int, int]:
    start = item.get("start")
    end = item.get("end")
    s = start.value if isinstance(start, YScalar) and start.is_int else 0
    e = end.value if isinstance(end, YScalar) and end.is_int else 0
    return s, e


def node_path(node: YNode | None) -> NodePath:
    if not isinstance(node, YSeq):
        return ()
    return tuple(x.value for x in node.items if isinstance(x, YScalar) and x.is_str)


def arc_key(node: YNode | None) -> ArcKey | None:
    if not isinstance(node, YMap):
        return None
    frm, to = node.get("from"), node.get("to")
    if not isinstance(frm, YMap) or not isinstance(to, YMap):
        return None
    return (
        node_path(frm.get("node")),
        text(frm.get("port")),
        node_path(to.get("node")),
        text(to.get("port")),
    )


