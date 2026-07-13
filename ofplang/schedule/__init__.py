"""ofplang.schedule -- scheduler for Object-flow Programming Language v0.

Currently the package provides the layer-1 schema validators (SPECIFICATIONS.md
§9) for the two documents the scheduler consumes; the scheduler itself is not
implemented yet.
"""

from ofplang.schedule.core.diagnostics import (
    ERROR,
    WARNING,
    Diagnostic,
    ValidationResult,
)
from ofplang.schedule.validation import validate_document, validate_environment

__all__ = [
    "validate_environment",
    "validate_document",
    "ValidationResult",
    "Diagnostic",
    "ERROR",
    "WARNING",
]
