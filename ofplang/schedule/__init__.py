"""ofplang.schedule -- scheduler for Object-flow Programming Language v0.

The package provides the layer-1 schema validators (SPECIFICATIONS.md §9) for the
two documents the scheduler consumes, and the scheduler itself: `schedule` turns
a v0 workflow plus an execution environment into an execution plan (initial-plan
slice — single workflow, makespan, transport, no replanning yet).
"""

from ofplang.schedule.core.diagnostics import (
    ERROR,
    WARNING,
    Diagnostic,
    ValidationResult,
)
from ofplang.schedule.scheduler.api import ScheduleReport, schedule
from ofplang.schedule.validation import validate_document, validate_environment

__all__ = [
    "validate_environment",
    "validate_document",
    "schedule",
    "ScheduleReport",
    "ValidationResult",
    "Diagnostic",
    "ERROR",
    "WARNING",
]
