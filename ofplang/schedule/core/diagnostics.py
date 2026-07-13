"""Diagnostics and validation result.

Shared by both schema validators. A diagnostic pins a stable error code
(SPECIFICATIONS.md §10) to a source position; `severity` distinguishes hard
errors from warnings. Warnings never make a document invalid (§9), so
`ValidationResult.ok` looks only at errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ofplang.schedule.core.yamlnode import YNode

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str = ""
    path: str = ""
    file: str | None = None
    line: int | None = None
    col: int | None = None
    severity: str = ERROR

    @property
    def location(self) -> str | None:
        """`file:line:col`, or None when no source position is known."""
        if self.file is not None and self.line is not None:
            return f"{self.file}:{self.line}:{self.col}"
        return None


class Diagnostics:
    """Accumulates diagnostics during a validation pass."""

    def __init__(self) -> None:
        self._items: list[Diagnostic] = []

    def add(
        self,
        code: str,
        message: str = "",
        path: str = "",
        *,
        at: YNode | None = None,
        severity: str = ERROR,
    ) -> None:
        # A node `at` supplies the source position; without one the diagnostic is
        # still recorded but carries no location.
        file = at.file if at is not None else None
        line = at.line if at is not None else None
        col = at.col if at is not None else None
        self._items.append(Diagnostic(code, message, path, file, line, col, severity))

    def error(self, code: str, message: str = "", path: str = "", *, at: YNode | None = None) -> None:
        self.add(code, message, path, at=at, severity=ERROR)

    def warning(self, code: str, message: str = "", path: str = "", *, at: YNode | None = None) -> None:
        self.add(code, message, path, at=at, severity=WARNING)

    @property
    def items(self) -> list[Diagnostic]:
        return list(self._items)


@dataclass(frozen=True)
class ValidationResult:
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # Valid iff no error-severity diagnostics; warnings are tolerated.
        return not any(d.severity == ERROR for d in self.diagnostics)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == ERROR]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == WARNING]
