"""Command-line interface for ofplang.schedule.

Thin presentation layer over the library. Two subcommands:

    ofp-schedule validate [--kind ...] [--format ...] <file>...
    ofp-schedule schedule <file>...          # not implemented yet (stub)

`validate` runs the schema validators (SPECIFICATIONS.md §9) and renders their
diagnostics as `file:line:col: <severity> <code>` lines, matching the
ofplang.validate convention. All validation logic lives in the library so the
CLI cannot drift from it.

Exit codes:
    0  every file is valid (warnings do not count as failure)
    1  at least one file has validation errors
    2  usage / input error
    3  not implemented yet (the `schedule` command)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from ofplang.schedule import validate_document, validate_environment
from ofplang.schedule.core.diagnostics import ERROR, ValidationResult

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3

_RED = "\033[31m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ofp-schedule", description="Schedule ofplang v0 documents.")
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="validate an environment definition or execution document")
    v.add_argument("paths", nargs="+", metavar="FILE")
    v.add_argument(
        "--kind",
        choices=["auto", "environment", "document"],
        default="auto",
        help="which schema to validate against (default: auto-detect)",
    )
    v.add_argument("--format", choices=["text", "json"], default="text", help="output format")
    v.add_argument("-q", "--quiet", action="store_true", help="show only the summary")
    v.add_argument("--no-color", action="store_true", help="disable ANSI color")

    s = sub.add_parser("schedule", help="produce an execution plan (not implemented yet)")
    s.add_argument("paths", nargs="+", metavar="FILE")

    return parser


def _detect_kind(path: str) -> str | None:
    """Guess the document kind from its top-level keys; None if ambiguous."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    if "activities" in data:
        return "document"
    if "devices" in data or "processes" in data:
        return "environment"
    return None


def _validate_one(path: str, kind: str) -> tuple[str, ValidationResult]:
    result = validate_environment(path) if kind == "environment" else validate_document(path)
    return path, result


def _color_enabled(no_color: bool) -> bool:
    return not no_color and sys.stdout.isatty()


def _render_text(results: list[tuple[str, ValidationResult]], quiet: bool, color: bool) -> str:
    def c(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    lines: list[str] = []
    total_errors = 0
    total_warnings = 0
    multi = len(results) > 1

    for path, result in results:
        for diag in result.diagnostics:
            if diag.severity == ERROR:
                total_errors += 1
            else:
                total_warnings += 1
        if result.ok and not result.diagnostics:
            if not quiet and multi:
                lines.append(f"{path}: {c('OK', _GREEN)}")
            continue
        if not quiet:
            for diag in result.diagnostics:
                locator = diag.location or (diag.path or "<root>")
                tag = c("error", _RED) if diag.severity == ERROR else c("warning", _YELLOW)
                detail = f"  {c(diag.path, _DIM)}" if diag.location and diag.path else ""
                message = f"  {diag.message}" if diag.message else ""
                lines.append(f"{locator}: {tag} {diag.code}{detail}{message}")

    if total_errors == 0:
        summary = f"all valid ({len(results)} file{'s' if len(results) != 1 else ''})"
        if total_warnings:
            summary += f", {total_warnings} warning{'s' if total_warnings != 1 else ''}"
        lines.append(c(summary, _GREEN))
    else:
        lines.append(c(f"{total_errors} error{'s' if total_errors != 1 else ''}", _RED))
    return "\n".join(lines)


def _render_json(results: list[tuple[str, ValidationResult]]) -> str:
    payload = {
        "ok": all(r.ok for _, r in results),
        "results": [
            {
                "file": path,
                "ok": result.ok,
                "diagnostics": [
                    {
                        "code": d.code,
                        "severity": d.severity,
                        "path": d.path,
                        "message": d.message,
                        "file": d.file,
                        "line": d.line,
                        "col": d.col,
                    }
                    for d in result.diagnostics
                ],
            }
            for path, result in results
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _cmd_validate(args) -> int:
    missing = [p for p in args.paths if not Path(p).is_file()]
    if missing:
        for p in missing:
            print(f"ofp-schedule: cannot open {p!r}: no such file", file=sys.stderr)
        return EXIT_USAGE

    results: list[tuple[str, ValidationResult]] = []
    for path in args.paths:
        kind = args.kind
        if kind == "auto":
            kind = _detect_kind(path)
            if kind is None:
                print(f"ofp-schedule: cannot determine kind of {path!r}; pass --kind", file=sys.stderr)
                return EXIT_USAGE
        results.append(_validate_one(path, kind))

    if args.format == "json":
        print(_render_json(results))
    else:
        print(_render_text(results, args.quiet, _color_enabled(args.no_color)))

    return EXIT_OK if all(r.ok for _, r in results) else EXIT_INVALID


def _cmd_schedule(args) -> int:
    missing = [p for p in args.paths if not Path(p).is_file()]
    if missing:
        for p in missing:
            print(f"ofp-schedule: cannot open {p!r}: no such file", file=sys.stderr)
        return EXIT_USAGE
    print("ofp-schedule: scheduling is not implemented yet", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "schedule":
        return _cmd_schedule(args)
    return EXIT_USAGE  # pragma: no cover - argparse enforces a subcommand


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
