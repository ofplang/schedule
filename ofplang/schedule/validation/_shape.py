"""Small shape-checking helpers shared by the two schema validators.

These wrap the recurring "is this the right kind of node, and is this key
present" checks so `environment.py` and `document.py` stay readable. Each helper
emits at most one diagnostic and returns either the narrowed node or None, so
callers can short-circuit and avoid cascading errors.
"""

from __future__ import annotations

from ofplang.schedule.core.yamlnode import YMap, YNode, YScalar, YSeq
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
    """Report every key outside `allowed` (strict; SPECIFICATIONS.md §9).

    Keys using the reserved `x-` extension prefix are tolerated and ignored:
    they are implementation extension points, never validated, and carry no
    portable v0 meaning (mirrors the workflow `x-` convention, ofplang-spec §26).
    Because this helper is only ever called at *closed* mapping positions, this
    admits `x-` keys exactly there and nowhere in the open name/port maps
    (`processes`, `input_spots`/`output_spots`, `interface.inputs`/`outputs`),
    whose keys are user-chosen and indistinguishable from an extension.
    """
    for entry in ymap.entries:
        if entry.key.startswith("x-"):
            continue
        if entry.key not in allowed:
            diags.error(
                errors.UNKNOWN_KEY,
                f"unknown key {entry.key!r}",
                join(path, entry.key),
                at=entry,
            )


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
