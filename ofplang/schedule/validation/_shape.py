"""Small shape-checking helpers shared by the two schema validators.

These wrap the recurring "is this the right kind of node, and is this key
present" checks so `environment.py` and `document.py` stay readable. Each helper
emits at most one diagnostic and returns either the narrowed node or None, so
callers can short-circuit and avoid cascading errors.
"""

from __future__ import annotations

from ofplang.schedule.core.yamlnode import YMap, YScalar, YSeq, YNode
from ofplang.schedule.validation import errors


def join(base: str, key) -> str:
    """Dotted diagnostic path, e.g. `devices[0].id`."""
    return f"{base}.{key}" if base else str(key)


def as_map(node: YNode | None, path: str, diags) -> YMap | None:
    """Require a mapping; emit wrong_type otherwise. None passes through (a
    missing value is the caller's concern, reported separately)."""
    if node is None:
        return None
    if isinstance(node, YMap):
        return node
    diags.error(errors.WRONG_TYPE, f"expected a mapping at {path}", path, at=node)
    return None


def as_seq(node: YNode | None, path: str, diags) -> YSeq | None:
    if node is None:
        return None
    if isinstance(node, YSeq):
        return node
    diags.error(errors.WRONG_TYPE, f"expected a list at {path}", path, at=node)
    return None


def require(ymap: YMap, key: str, path: str, diags) -> YNode | None:
    """Require a field to be present; emit missing_required_field otherwise."""
    if key in ymap:
        return ymap.get(key)
    diags.error(errors.MISSING_REQUIRED_FIELD, f"missing {key!r}", join(path, key), at=ymap)
    return None


def unknown_keys(ymap: YMap, allowed: set[str], path: str, diags) -> None:
    """Report every key outside `allowed` (strict; SPECIFICATIONS.md §9)."""
    for entry in ymap.entries:
        if entry.key not in allowed:
            diags.error(errors.UNKNOWN_KEY, f"unknown key {entry.key!r}", join(path, entry.key), at=entry)


def nonneg_int(node: YNode | None, path: str, diags) -> None:
    """Check a value is a non-negative integer: wrong_type if not an int,
    negative_value if negative. Absent/None is left to the caller."""
    if node is None:
        return
    if not (isinstance(node, YScalar) and node.is_int):
        diags.error(errors.WRONG_TYPE, f"expected an integer at {path}", path, at=node)
        return
    if node.value < 0:
        diags.error(errors.NEGATIVE_VALUE, f"{path} must be non-negative", path, at=node)
