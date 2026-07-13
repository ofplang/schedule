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


def format_node_path(path) -> str:
    """Render a node path as `a/b/c` for diagnostics and messages — the
    hierarchical node-path form (SPECIFICATIONS.md §6.3), readable where the raw
    tuple/list would leak Python syntax."""
    return "/".join(path)


def format_endpoint(node_path, port) -> str:
    """Render an arc endpoint (`node` path + `port`) as `a/b/c.port`."""
    return f"{format_node_path(node_path)}.{port}"


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
