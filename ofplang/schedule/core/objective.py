"""Objective stages (SPECIFICATIONS.md §4.8, §6.1).

The objective is a sequence of **stages** minimised lexicographically. v0 defines
two of them, and three places have to agree on what a stage list means: the
document schema validator (is this `kind` well formed?), the solver (what am I
minimising?), and the plan renderer (what shape does `objective` take?). That
agreement lives here.

Keeping it in one module is what made moving `objective` out of the environment
definition and into the execution document a local change: only the *caller* that
read the declaration had to move (0.2.1).
"""

from __future__ import annotations

MAKESPAN = "makespan"
REPLENISHMENT_COUNT = "replenishment_count"

# The stage names v0 defines (§4.8). The order here is the default order, not a
# constraint on what a document may write: `[replenishment_count, makespan]` is a
# legitimate lexicographic order too (minimise refills first, then time).
STAGES: tuple[str, ...] = (MAKESPAN, REPLENISHMENT_COUNT)

# The objective when the declaration is omitted (§6.1). Wherever replenishment
# cannot occur this is equivalent to `[makespan]`, which is what `effective` below
# makes true rather than something the reader has to remember.
DEFAULT: tuple[str, ...] = STAGES


def normalize(value) -> tuple[str, ...] | None:
    """Read a plain `objective.kind` value into a stage tuple, or None if v0 does
    not define what it says.

    A single stage may be written as a bare scalar or as a one-element list; both
    read as the same tuple. Rejected are an unknown stage name, an empty list (it
    names nothing to minimise), a repeated stage (its second occurrence is
    minimised subject to its own optimum and so could never change the outcome),
    and anything that is neither a string nor a list of strings.
    """
    if isinstance(value, str):
        names = [value]
    elif isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        names = list(value)
    else:
        return None
    if any(name not in STAGES for name in names):
        return None
    if len(set(names)) != len(names):
        return None
    return tuple(names)


def effective(
    stages: tuple[str, ...], *, replenishment_possible: bool
) -> tuple[str, ...]:
    """The stages that can actually tell two schedules of this instance apart.

    `replenishment_count` is identically zero wherever no replenishment can occur,
    so it is dropped there. That is what makes the default equivalent to
    `[makespan]` for an environment declaring no resources, and what keeps such a
    plan's `objective` shaped exactly as it always was (§6.1) instead of growing a
    list whose second entry is always 0.

    A document may name only stages that all drop out -- `kind:
    replenishment_count` against a resource-free environment. Something must still
    be minimised, or the schedule would be pinned in time by nothing but the
    horizon, so `makespan` remains.
    """
    kept = tuple(
        stage
        for stage in stages
        if stage != REPLENISHMENT_COUNT or replenishment_possible
    )
    return kept or (MAKESPAN,)


def render(stages: tuple[str, ...], values: tuple[int, ...]) -> dict:
    """The plan's `objective` mapping (§6.1).

    `value` takes the shape of the `kind` it accompanies: a scalar for a single
    stage, a list in the same order for several.
    """
    if len(stages) == 1:
        return {"kind": stages[0], "value": values[0]}
    return {"kind": list(stages), "value": list(values)}
