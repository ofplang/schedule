"""What a solve returns.

These are the scheduler's answer, not CP-SAT's: an outcome, times, and the
concrete mode, route, spot and machine chosen for each activity and move. They
live apart from `cpsat` because more than one thing produces them -- the solver,
and the constructive first schedule of `greedy` -- and a second producer that had
to import the first would be a second producer in name only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ofplang.schedule.core import objective as objective_stages
from ofplang.schedule.scheduler.instance import BoundaryInfo, RelayInfo, TransportOption
from ofplang.schedule.scheduler.model import Arc, Mode, NodePath
from ofplang.schedule.scheduler.stats import SolveStats


@dataclass(frozen=True)
class ProcessingResult:
    activity: int
    node: NodePath
    process: str
    mode: Mode
    start: int
    end: int
    # On a replan, the reported status of a fixed activity; None when pending.
    status: str | None = None
    # Set (opaquely, by the solver) when this activity is a relay junction; drives
    # rendering (`kind: relay`). None for a normal processing activity.
    relay: RelayInfo | None = None
    # Set when this activity is a synthetic boundary node (§6.8); rendering skips it.
    boundary: BoundaryInfo | None = None


@dataclass(frozen=True)
class TransportResult:
    arc: Arc
    option: TransportOption
    start: int
    end: int
    status: str | None = None
    # A leg's chain position (§6.6); None for a single-leg transport.
    seq: int | None = None


@dataclass(frozen=True)
class RefillResult:
    """A refill in the plan (§6.9). `amounts` is what it adds, keyed by bare
    resource name -- `device` already says whose stock it is."""

    id: str
    device: str
    replenisher: str
    amounts: dict[str, int]
    start: int
    end: int
    status: str | None = None


@dataclass(frozen=True)
class Solution:
    outcome: str  # optimal | feasible | infeasible | unknown
    makespan: int | None
    processing: tuple[ProcessingResult, ...]
    transport: tuple[TransportResult, ...]
    replenishment: tuple[RefillResult, ...] = ()
    # The objective actually minimised, and what each of its stages reached. These
    # are the *effective* stages (`core.objective.effective`): a stage this
    # instance cannot tell two schedules apart by is dropped, so a plan from an
    # environment without resources reports the bare makespan it always did.
    # Defaulted so the infeasible return below stays a four-argument call.
    objective_kind: tuple[str, ...] = (objective_stages.MAKESPAN,)
    objective_values: tuple[int, ...] = ()
    # What the solve cost and how it got there (stats.py). Set on every path that
    # reached the solver, including the infeasible one -- how long it takes to
    # prove an instance unschedulable is as much a measurement as how long it takes
    # to schedule one. None only where no solve ran.
    stats: SolveStats | None = None
    # What each job of a joint plan (§6.11) finished at, by id. This is where a job's
    # promised bound B_j comes from -- a bound is the completion the solve achieved,
    # not a value derived some other way (design.md D38) -- so the caller needs it per
    # job rather than folded into `objective_values`. Empty for a single workflow.
    job_completions: dict[str, int] = field(default_factory=dict)
