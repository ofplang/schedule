"""What a solve did, as opposed to what it decided.

The plan (SPEC §6) says what to run and when; nothing in it says how long the
search took, how close to proven the answer is, or when the first usable schedule
appeared. Those are properties of the *solve*, not of the schedule, so they are
reported alongside the plan (`ScheduleReport.stats`) and never written into the
document -- a plan is a portable v0 artifact and stays exactly what it was.

Who reads this: a benchmark measuring scheduler improvements cares less about the
proven optimum than about **how good a schedule is at time t**, which needs the
incumbent's history, the bound it was measured against, and a clock that does not
move when the machine is busy (`deterministic_time`).

Why the nesting. Today a solve is one CP-SAT call, so `SolveStats.phases` holds
exactly one `PhaseStats` named `"cpsat"`. The planned pipeline (design.md D37)
runs several phases in one `schedule()` -- a greedy construction, then CP-SAT
seeded from it, then an LNS loop -- each handing its solution to the next. Shaping
the record for that now costs one level of nesting and keeps the *meaning of every
field stable* when those phases arrive: a reader that plots `phases[-1]` keeps
working, and nothing has to be re-cut when the second phase lands.

Naming: `stage` is already taken by the objective's lexicographic stages
(`makespan`, `replenishment_count`, core/objective.py). A step of the solve
pipeline is a **phase**. The two are unrelated and both appear here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SolutionEvent:
    """One improving solution, recorded as the search found it.

    Deliberately *not* the schedule itself: rendering a plan per incumbent would
    cost more than the solve, and what an anytime measurement needs is the number
    (how good was the answer at this moment), not the assignment. Whoever wants the
    final schedule already has it in the plan.

    `objective_values` are solver-side values, decoded from the weighted objective
    (`core.objective.decode`). For `replenishment_count` that can differ by one or
    more from the count the finished plan reports: the plan counts the refills that
    survive normalisation, and a refill that ends up adding nothing is dropped
    (cpsat.py `_refill_results`). The history says what the solver was minimising.
    """

    wall_time: float
    deterministic_time: float
    objective_value: int  # the weighted objective (core.objective.weights)
    objective_bound: float  # lower bound on the same expression, at this moment
    objective_values: tuple[int, ...]  # per objective stage, in `objective_kind` order


@dataclass(frozen=True)
class PhaseStats:
    """One phase of the solve. Today there is only ever `"cpsat"` (see the module
    docstring); the fields are what any phase can honestly report.

    `objective_value` / `objective_bound` are the weighted expression, as CP-SAT
    reports it; `objective_values` is that value decoded per objective stage, and
    `first_stage_bound` is the only per-stage bound the weighted encoding permits
    (`core.objective.first_stage_bound`). All are None when the phase produced no
    solution -- an `infeasible` or `unknown` outcome still reports its timings,
    because how long it takes to *fail* is a measurement too.

    `workers` is what the search actually ran with, not what was asked for: passing
    `random_seed` pins it to 1 (cpsat.py), and a run compared against another must
    know which of those it was.
    """

    name: str
    outcome: str  # optimal | feasible | infeasible | unknown
    wall_time: float
    user_time: float
    deterministic_time: float
    workers: int
    num_branches: int
    num_conflicts: int
    num_booleans: int
    objective_value: int | None = None
    objective_bound: float | None = None
    objective_values: tuple[int, ...] | None = None
    first_stage_bound: int | None = None
    # Improving solutions in the order they were found, or None when the caller did
    # not ask for them (`schedule(collect_solutions=True)`). None and () mean
    # different things: "not recorded" versus "recorded, and there were none".
    #
    # This is the only part of the record that grows, and it is bounded twice over:
    # CP-SAT calls back on *improving* solutions only, so there can never be more
    # entries than there are distinct objective values (at most the horizon times
    # the refill count), and each entry is five numbers. Measured: a 60-second solve
    # of the 6x4 plate batch collected 7 entries -- improvements thin out as the
    # search turns to proving -- and a whole record with history came to ~2 KB
    # against a ~31 KB plan. A caller running thousands of solves should still write
    # each one out and drop it rather than accumulate them, but that is bookkeeping,
    # not a size problem here.
    history: tuple[SolutionEvent, ...] | None = None


@dataclass(frozen=True)
class ModelStats:
    """The size of what was handed to the solver.

    Solve time is bounded by model size at least as much as by search luck
    (dev-notes/report-solver-scalability.md), so a benchmark comparing two versions
    of the scheduler needs to know whether a change moved the search or the model.
    `horizon` is here for the same reason: it is the upper bound every time
    variable's domain is cut from, and a loose one is a known cause of slow solves
    (design.md D26).
    """

    variables: int
    constraints: int
    activities: int
    arcs: int
    transport_options: int
    modes: int
    replenishments: int
    horizon: int


@dataclass(frozen=True)
class SolveStats:
    """Everything measurable about one `schedule()` call's solve.

    Present whenever a solve ran -- including when it proved the instance
    infeasible, or gave up without an answer. Absent (None on the report) only when
    the pipeline stopped before solving, because there is then nothing to measure.
    """

    model: ModelStats
    # The objective stages actually minimised (`core.objective.effective`), which is
    # the order every `objective_values` tuple here follows.
    objective_kind: tuple[str, ...]
    phases: tuple[PhaseStats, ...] = field(default_factory=tuple)

    @property
    def wall_time(self) -> float:
        """Seconds of wall clock across every phase."""
        return sum(p.wall_time for p in self.phases)

    @property
    def deterministic_time(self) -> float:
        """Deterministic time across every phase: CP-SAT's machine-independent
        measure of search effort. Unlike `wall_time` it reproduces across machines
        and under load, which is what makes it usable as a regression signal."""
        return sum(p.deterministic_time for p in self.phases)

    @property
    def final(self) -> PhaseStats | None:
        """The phase whose answer was kept -- the last one to have run."""
        return self.phases[-1] if self.phases else None
