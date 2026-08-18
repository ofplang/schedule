"""Duplicate mapping-key detection, shared by both schema validators (§9).

YAML permits a mapping to repeat a key and resolves it last-wins, so a document
that repeats one is read as something other than what it appears to say. Nothing
in these schemas ever means to do that, so a repeat is an error (`duplicate_key`).

Reporting it is also what keeps the node layer and the value layer honest about
each other: keyed node access is last-wins (`YMap.get`) exactly so the validators
inspect the entry a model would be built from, and this pass makes sure such a
document does not reach a model at all.

Extension keys (`x-`, §9.4) are treated in two parts. A repeat **of** an `x-` key
is reported like any other: that a key appears twice is a fact about the document,
not an interpretation of the extension's payload. Inside that payload nothing is
reported, because §9.4 states the values under an `x-` key are never validated --
no type, reference or null check applies there. The one thing this leaves
unreported is a duplicate *within* an extension payload, which is precisely the
region the specification excludes; the implementation that defines the extension
is the one that can check it (ofplang-labcode finding L7).
"""

from __future__ import annotations

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.core.yamlnode import YEntry, YMap, YNode, YSeq
from ofplang.schedule.validation import errors
from ofplang.schedule.validation._shape import join


def check_duplicate_keys(root: YNode | None, diags: Diagnostics) -> None:
    """Report every duplicate mapping key in the document, `x-` payloads aside."""
    _walk(root, "", diags)


def _walk(node: YNode | None, path: str, diags: Diagnostics) -> None:
    if isinstance(node, YMap):
        _report(node, path, diags)
        for entry in node.entries:
            # Never descend into an extension's payload (§9.4). Note this walks the
            # value of *every* entry, repeats included, so a duplicate nested inside
            # a repeated key is still found; the repeat itself was reported above.
            if not entry.key.startswith("x-"):
                _walk(entry.value, join(path, entry.key), diags)
    elif isinstance(node, YSeq):
        for index, item in enumerate(node.items):
            _walk(item, f"{path}[{index}]", diags)


def _report(ymap: YMap, path: str, diags: Diagnostics) -> None:
    """One diagnostic per repeated key, positioned at its last entry -- the one
    that wins, and the one a reader has to delete to fix the document."""
    last: dict[str, YEntry] = {}
    counts: dict[str, int] = {}
    for entry in ymap.entries:
        counts[entry.key] = counts.get(entry.key, 0) + 1
        last[entry.key] = entry
    for key, count in counts.items():
        if count > 1:
            diags.error(
                errors.DUPLICATE_KEY,
                f"duplicate key {key!r} ({count} entries)",
                join(path, key),
                at=last[key],
            )
