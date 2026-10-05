"""Reading one `interface` binding (SPECIFICATIONS.md §6.8).

A binding pins the Objects of one boundary port to spots. A scalar port carries one
Object, so its binding is one qualified spot. An `Array<T>` port carries one Object
per element, each on a spot of its own, so its binding is a list of spots in element
order -- and `Array<Array<T>>` a list of such lists. The list *is* the Array's
shape: its length is how many elements there are, and an inner list may be as long
as that element is.

Everything that reads a binding -- building the boundary nodes, checking that two
jobs do not claim one spot, deriving what a stopped job still holds -- reads it
through here, so a spot list means the same thing to all of them.
"""

from __future__ import annotations

from collections.abc import Iterator

from ofplang.schedule.core.identifiers import format_element


def binding_matches(value, rank: int) -> bool:
    """Whether `value` has the shape a port nesting Arrays `rank` deep takes: a spot
    (a string) at rank 0, and at rank k a list whose items each have rank k - 1.

    An empty list matches every positive rank: it says the Array has no elements,
    which is a shape every Array can have. Nothing is coerced -- a spot for an Array
    port is not a one-element list, nor a list for a scalar port one spot."""
    if rank == 0:
        return isinstance(value, str)
    return isinstance(value, list) and all(binding_matches(item, rank - 1) for item in value)


def binding_elements(value, index: tuple[int, ...] = ()) -> Iterator[tuple[tuple[int, ...], str]]:
    """Every (element index, spot) a binding names, in element order -- `((), spot)`
    for a scalar port's one spot. The index is the path through the nested lists,
    outermost first, which is the arc endpoint's `index` (§6.4).

    Shape-agnostic: it walks whatever nesting is there and yields only strings, so a
    caller that has not checked the shape against the port (`binding_matches`) still
    sees every spot named. Anything else in the tree is skipped; the document
    validator has already reported it."""
    if isinstance(value, str):
        yield index, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from binding_elements(item, (*index, i))


def element_label(port: str, index: tuple[int, ...]) -> str:
    """How one element of a binding is named in a message: `plates[2]`, or the port
    itself for a scalar one (`identifiers.format_element`, the one spelling)."""
    return format_element(port, index)
