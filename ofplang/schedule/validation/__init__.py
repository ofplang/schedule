"""Layer-1 schema validators (SPECIFICATIONS.md §9).

Two standalone, shape-only validators — one for the execution environment
definition (§9.1) and one for the execution document / plan or status (§9.2) —
plus the stable error-code catalog (§10). Neither reads the workflow.
"""

from ofplang.schedule.validation.document import validate_document
from ofplang.schedule.validation.environment import validate_environment

__all__ = ["validate_environment", "validate_document"]
