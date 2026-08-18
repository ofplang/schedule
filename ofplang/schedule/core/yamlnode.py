"""Position-tracking YAML wrapper.

The schema validators report diagnostics with a `file:line:col` source position
(SPECIFICATIONS.md §9). PyYAML's ordinary loaders discard node positions, so this
module composes the YAML *node* tree (which keeps a `start_mark`) and wraps it in
lightweight nodes that carry the originating file and a 1-based line/column.

Only what the validators need is exposed: mapping/sequence/scalar nodes with
positions, ordered access, and scalar type predicates. This is deliberately not a
general YAML data model.

A document that is already loaded in memory (a caller that holds plain dicts, e.g.
the rolling-horizon runner rendering its history each replan) is wrapped by
`from_object` instead of being round-tripped through a file: the same node types,
but without source positions. `load_source` picks the right one for either form.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class YNode:
    """Base for every wrapped node; carries the source position."""

    def __init__(self, file: str | None, line: int, col: int):
        self.file = file
        self.line = line  # 1-based
        self.col = col  # 1-based


class YScalar(YNode):
    """A scalar leaf, with the constructed Python value and its resolved tag."""

    def __init__(self, value, tag: str, file: str | None, line: int, col: int):
        super().__init__(file, line, col)
        self.value = value
        self.tag = tag

    # Type predicates. `bool` is excluded from `is_int` because in Python `bool`
    # is a subclass of `int`, and the validators treat them as distinct kinds.
    @property
    def is_str(self) -> bool:
        return isinstance(self.value, str)

    @property
    def is_bool(self) -> bool:
        return isinstance(self.value, bool)

    @property
    def is_int(self) -> bool:
        return isinstance(self.value, int) and not isinstance(self.value, bool)

    @property
    def is_null(self) -> bool:
        return self.value is None

    @property
    def text(self) -> str:
        """The value as a string (empty string if not a string scalar)."""
        return self.value if isinstance(self.value, str) else ""


@dataclass(frozen=True)
class YEntry:
    """One mapping entry, keeping the key's own position for key-level diagnostics.

    Carries `file`/`line`/`col` so an entry can be passed directly as the `at`
    argument of a diagnostic (to point at the offending key).
    """

    key: str
    file: str | None
    line: int
    col: int
    value: YNode


class YMap(YNode):
    """A mapping with ordered entries and first-wins keyed access."""

    def __init__(self, entries: list[YEntry], by_key: dict[str, YNode], file, line, col):
        super().__init__(file, line, col)
        self.entries = entries
        self._by_key = by_key

    def get(self, key: str) -> YNode | None:
        return self._by_key.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self._by_key

    def keys(self) -> list[str]:
        # Insertion order, first occurrence only.
        seen: dict[str, None] = {}
        for e in self.entries:
            seen.setdefault(e.key, None)
        return list(seen)


class YSeq(YNode):
    """A sequence of wrapped items."""

    def __init__(self, items: list[YNode], file, line, col):
        super().__init__(file, line, col)
        self.items = items


def _wrap(node, loader: yaml.SafeLoader, file: str | None) -> YNode:
    """Recursively convert a composed PyYAML node into wrapped nodes."""
    line = node.start_mark.line + 1
    col = node.start_mark.column + 1

    if isinstance(node, yaml.MappingNode):
        entries: list[YEntry] = []
        by_key: dict[str, YNode] = {}
        for key_node, value_node in node.value:
            # Keys are expected to be scalars; coerce to str for lookup. A
            # non-scalar key is unusual and simply keyed by its string form.
            key = str(loader.construct_object(key_node, deep=True))
            child = _wrap(value_node, loader, file)
            entries.append(
                YEntry(
                    key,
                    file,
                    key_node.start_mark.line + 1,
                    key_node.start_mark.column + 1,
                    child,
                )
            )
            by_key.setdefault(key, child)
        return YMap(entries, by_key, file, line, col)

    if isinstance(node, yaml.SequenceNode):
        items = [_wrap(item, loader, file) for item in node.value]
        return YSeq(items, file, line, col)

    # Scalar: construct the Python value so callers get int/float/bool/None/str.
    value = loader.construct_object(node, deep=True)
    return YScalar(value, node.tag, file, line, col)


def loads(text: str, file: str | None = None) -> YNode | None:
    """Wrap a single YAML document from text; None for an empty document."""
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        return _wrap(node, loader, file)
    finally:
        loader.dispose()


def load_file(path) -> YNode | None:
    """Wrap the YAML document at `path`, recording the path as the source file."""
    p = Path(path)
    return loads(p.read_text(encoding="utf-8"), str(path))


# The resolved tag of an in-memory scalar, looked up by *exact* Python type -- which
# is what keeps `bool` and `int` apart here (`bool` is a subclass of `int`, the same
# distinction `is_int` makes). Anything else -- a type YAML's safe dumper would not
# emit -- carries no tag rather than a made-up one; the validators read a scalar's
# value and its predicates, never its tag.
_SCALAR_TAGS = {
    bool: "tag:yaml.org,2002:bool",
    int: "tag:yaml.org,2002:int",
    float: "tag:yaml.org,2002:float",
    str: "tag:yaml.org,2002:str",
    type(None): "tag:yaml.org,2002:null",
}


def from_object(data, file: str | None = None) -> YNode:
    """Wrap an already-loaded document (plain dicts / sequences / scalars).

    The result carries no source position (`file` defaults to None, so a
    diagnostic raised at one of these nodes has no `location` and locates by its
    `path` alone). Normalizes what a YAML round-trip would have normalized, so an
    in-memory document is read exactly like the file it would have been written
    to: a tuple becomes a sequence and every mapping key becomes a string
    (first occurrence wins for keyed access, as in `_wrap`).
    """
    if isinstance(data, dict):
        entries: list[YEntry] = []
        by_key: dict[str, YNode] = {}
        for key, value in data.items():
            name = str(key)
            child = from_object(value, file)
            entries.append(YEntry(name, file, 0, 0, child))
            by_key.setdefault(name, child)
        return YMap(entries, by_key, file, 0, 0)

    if isinstance(data, (list, tuple)):
        return YSeq([from_object(item, file) for item in data], file, 0, 0)

    return YScalar(data, _SCALAR_TAGS.get(type(data), ""), file, 0, 0)


def load_source(source) -> YNode | None:
    """Wrap `source`: a path to a YAML file, or an already-loaded document.

    The single place the two input forms are told apart (`isinstance(source, dict)`,
    as the workflow parser does), so every caller -- both validators, the
    environment loader and the scheduling entry point -- agrees on what counts as
    an in-memory document.
    """
    if isinstance(source, dict):
        return from_object(source)
    return load_file(source)
