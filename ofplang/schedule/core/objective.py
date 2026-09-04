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

import math
from collections.abc import Mapping

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


def weights(stages: tuple[str, ...], bounds: Mapping[str, int]) -> tuple[int, ...]:
    """The multiplier each stage gets when the lexicographic order is expressed as a
    single weighted sum (FORMULATION "Objective"), in `stages` order.

    This is mixed-radix positional notation: a stage's weight is the product of
    `bound + 1` over every stage after it, so one unit of an earlier stage outweighs
    every value the later ones can reach together. That is what makes the single
    weighted solve *exact* rather than an approximation of the lexicographic order,
    and it is why `bounds` must be genuine upper bounds -- a bound a stage can exceed
    silently turns the order into an arbitrary trade-off.

    It lives here rather than in the solver because encoding and decoding have to
    agree, and the decoder (`decode`) is read by anything that looks at a solve in
    progress, where the only objective value on offer is the encoded one.
    """
    out = [0] * len(stages)
    weight = 1
    for i in range(len(stages) - 1, -1, -1):
        out[i] = weight
        weight *= bounds[stages[i]] + 1
    return tuple(out)


def decode(weights_: tuple[int, ...], value: int) -> tuple[int, ...]:
    """Split a weighted objective value back into one value per stage.

    Exact, given the same `weights_` the value was built with: each stage's
    contribution is smaller than the weight of the stage before it (that is what the
    mixed radix buys), so integer division and remainder recover the digits.
    """
    out = []
    rest = value
    for weight in weights_:
        out.append(rest // weight)
        rest %= weight
    return tuple(out)


def first_stage_bound(weights_: tuple[int, ...], bound: float) -> int:
    """A valid lower bound on the *first* stage, read off a lower bound on the whole
    weighted expression.

    Only the first stage gets one. The weighted sum cannot be split into per-stage
    bounds -- a bound on `w1*v1 + R` says nothing about how it divides between the
    two -- but the leading digit survives: with `0 <= R <= w1 - 1`, any expression
    value at least `bound` forces `v1 >= (bound - (w1 - 1)) / w1`.

    `bound` arrives as a float from CP-SAT and may be fractional; it is raised to the
    next integer first (with a slack for float error), since the expression is
    integral.
    """
    weight = weights_[0]
    # The expression is integral, so a fractional lower bound rounds up. The epsilon
    # keeps a bound that is an integer in exact arithmetic but 4.999999 in floating
    # point from being rounded up to 5.
    integral = math.ceil(bound - 1e-6)
    # ceil((integral - (weight - 1)) / weight) in integer arithmetic.
    numerator = integral - weight + 1
    return max(0, -((-numerator) // weight))


def render(stages: tuple[str, ...], values: tuple[int, ...]) -> dict:
    """The plan's `objective` mapping (§6.1).

    `value` takes the shape of the `kind` it accompanies: a scalar for a single
    stage, a list in the same order for several.
    """
    if len(stages) == 1:
        return {"kind": stages[0], "value": values[0]}
    return {"kind": list(stages), "value": list(values)}
