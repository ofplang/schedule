"""Identifier and qualified-spot helpers (SPECIFICATIONS.md §8).

Environment-defined ids use the v0 identifier grammar, and spots are referenced
in the qualified form `<device>.<spot>`. Both validators (and later the scheduler)
share these two checks.
"""

from __future__ import annotations

import re

# v0 identifier grammar: ASCII, must start with a letter or underscore, no `.` and
# no `-` (SPECIFICATIONS.md §8.1).
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def is_identifier(value) -> bool:
    return isinstance(value, str) and _IDENTIFIER.match(value) is not None


def is_iteration_index(value) -> bool:
    """True for a node-path iteration index: a non-negative int, never a bool (YAML
    and Python both let `true` pass for an int)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def node_path_problem(elements) -> str | None:
    """Why a sequence is not a well-formed node path, or None if it is one
    (SPECIFICATIONS.md §6.3).

    A node path is a list of segments, each a node id optionally followed by one
    iteration index: the index says which invocation of a `map` / `fold` node the
    rest of the path lies in, so it can only come straight after that node's id.
    Hence an index never starts a path and two never stand side by side. A path
    without structured nodes has no index at all, which is every path written
    before indices existed. The empty path is not judged here: whether it is
    allowed (a boundary arc endpoint) or not (a processing `node`) depends on where
    it appears.
    """
    after_node_id = False
    for element in elements:
        if isinstance(element, str):
            if not is_identifier(element):
                return f"invalid node id {element!r}"
            after_node_id = True
        elif is_iteration_index(element):
            if not after_node_id:
                return f"iteration index {element} does not follow a node id"
            after_node_id = False
        else:
            return f"{element!r} is neither a node id nor an iteration index"
    return None


def format_node_path(path) -> str:
    """Render a node path as `a/b/c` for diagnostics and messages — the
    hierarchical node-path form (SPECIFICATIONS.md §6.3), readable where the raw
    tuple/list would leak Python syntax. An iteration index renders as its number
    (`Wash/2/aspirate`); a node id cannot start with a digit, so it reads back
    unambiguously."""
    return "/".join(str(element) for element in path)


def format_element(port, index=()) -> str:
    """A port, or one element of an Array-valued port, as `plates[2]` (`plates[1][0]`
    nested). The one spelling of an element: a mode's spot key for it
    (`model.slot_key`), its name in a message, in an `interface` binding's reading
    and on a chart all use this, so none of them can drift from the others."""
    return f"{port}" + "".join(f"[{i}]" for i in index)


def format_endpoint(node_path, port, index=()) -> str:
    """Render an arc endpoint (`node` path + `port`) as `a/b/c.port`, with the
    element `index` of an Array-valued port, if any, as `a/b/c.port[2][0]`."""
    return f"{format_node_path(node_path)}.{format_element(port, index)}"


def parse_qualified_resource(value) -> tuple[str, str] | None:
    """Split `<device>.<resource>` into (device, resource).

    A qualified resource has the same form as a qualified spot and passes the same
    check; the two are told apart by the section they appear in, not by their shape
    (SPECIFICATIONS.md §8.2). Named separately so a call site reads as the thing it
    is actually validating.
    """
    return parse_qualified_spot(value)


def parse_qualified_spot(value) -> tuple[str, str] | None:
    """Split `<device>.<spot>` into (device, spot).

    Returns None when the value is not a well-formed qualified spot: it must be a
    string with exactly one `.` whose two halves are each a valid identifier.
    """
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 2:
        return None
    device, spot = parts
    if not is_identifier(device) or not is_identifier(spot):
        return None
    return device, spot
