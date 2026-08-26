"""The `objective` checks shared by the two schema validators (§5.8, §6.1, §9).

Both validators accept the same `kind`: the environment *declares* the objective
and the execution document *reports* it, and §6.2 lets a plan be fed straight back
in as the next input -- so a shape one accepts and the other rejects would break
that round trip. Only `value` differs, and only because the environment has none.
"""

from __future__ import annotations

from ofplang.schedule.core import objective as stages
from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.yamlnode import YMap, YNode
from ofplang.schedule.validation import _shape as shape
from ofplang.schedule.validation import errors


def check_kind(node: YNode, diags) -> tuple[str, ...] | None:
    """Validate `objective.kind`, returning the stages it names or None.

    One code covers every way of getting it wrong (§10.1): naming a stage v0 does
    not define, naming none at all (an empty list), repeating one, or writing
    something that is neither a string nor a list of strings. They are all the same
    mistake from the reader's side -- this is not a set of stages -- and the
    message says which stages exist.
    """
    parsed = stages.normalize(yamlnode.to_plain(node))
    if parsed is None:
        diags.error(
            errors.UNKNOWN_OBJECTIVE_KIND,
            "objective.kind must name v0 stages "
            f"({', '.join(stages.STAGES)}), as a single name or as a "
            "non-empty list of distinct names",
            "objective.kind",
            at=node,
        )
    return parsed


def check_value(omap: YMap, kind: tuple[str, ...] | None, diags) -> None:
    """Validate `objective.value` against the `kind` it accompanies (§6.1).

    `value` takes the shape of its `kind`: a non-negative integer for a single
    stage, a list of them in the same order for several. When `kind` itself did not
    parse, `value` is checked as a scalar and nothing more -- the document already
    has one diagnostic for this field, and guessing which shape was meant would
    only add a second one saying the same thing.
    """
    node = omap.get("value")
    if node is None:
        return
    if kind is None or len(kind) == 1:
        shape.nonneg_int(node, "objective.value", diags)
        return

    seq = shape.as_seq(node, "objective.value", diags)
    if seq is None:
        return
    if len(seq.items) != len(kind):
        diags.error(
            errors.WRONG_TYPE,
            f"objective.value must have one entry per stage "
            f"({len(kind)}), not {len(seq.items)}",
            "objective.value",
            at=node,
        )
        return
    for index, item in enumerate(seq.items):
        shape.nonneg_int(item, f"objective.value[{index}]", diags)
