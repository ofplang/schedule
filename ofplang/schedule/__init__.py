"""ofplang.schedule -- scheduler for Object-flow Programming Language v0.

The package provides the layer-1 schema validators (SPECIFICATIONS.md §9) for the
two documents the scheduler consumes, and the scheduler itself: `schedule` turns
a v0 workflow plus an execution environment into an execution plan (makespan,
transport) and, given an execution status, replans it. `schedule_jobs` does the
same for several workflows planned together against one environment (§6.11),
where they compete for its machines and share its consumable stocks.
"""

from ofplang.schedule.core.diagnostics import (
    ERROR,
    WARNING,
    Diagnostic,
    ValidationResult,
)
from ofplang.schedule.scheduler.api import (
    JobInput,
    ScheduleReport,
    schedule,
    schedule_jobs,
)
from ofplang.schedule.scheduler.stats import (
    ModelStats,
    PhaseStats,
    SolutionEvent,
    SolveStats,
)
from ofplang.schedule.validation import validate_document, validate_environment

__all__ = [
    "validate_environment",
    "validate_document",
    "schedule",
    "schedule_jobs",
    "JobInput",
    "ScheduleReport",
    "SolveStats",
    "PhaseStats",
    "SolutionEvent",
    "ModelStats",
    "ValidationResult",
    "Diagnostic",
    "ERROR",
    "WARNING",
]
