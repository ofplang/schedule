"""Command-line interface for ofplang.schedule.

Thin presentation layer over the library. Subcommands:

    ofp-schedule validate [--kind ...] [--format ...] <file>...
    ofp-schedule schedule <workflow> --env <env> [--document <doc>] [-o <file>] [--format yaml|json]
    ofp-schedule visualize <plan> [--view device|workflow|lane] [-o <file>]

`validate` runs the schema validators (SPECIFICATIONS.md §9); `schedule` produces
an execution plan (§6) from a v0 workflow and an execution environment, and with
`--document` (an execution document that sets `now`) replans from a prior status
(§7); `visualize` renders a plan as a self-contained HTML/SVG Gantt chart. All
logic lives in the library so the CLI cannot drift from it.

`schedule` first runs the workflow through `ofplang-validate` as a one-shot front
door (extension-tolerant) so a malformed workflow fails with clear diagnostics
rather than being silently mis-scheduled; the scheduler library itself trusts its
input. Pass `--no-validate` to skip this (e.g. when already validated upstream).

Exit codes:
    0  success (valid, or a plan was produced)
    1  validation errors, or no feasible schedule
    2  usage / input error
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

import yaml
from ofplang.validate import EXTENSION_TOLERANT
from ofplang.validate import validate as validate_workflow

from ofplang.schedule import schedule as run_schedule
from ofplang.schedule import validate_document, validate_environment
from ofplang.schedule.core.diagnostics import ERROR, ValidationResult
from ofplang.schedule.scheduler.plan import to_yaml
from ofplang.schedule.scheduler.visualize import render_html, render_svg

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2

_RED = "\033[31m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ofp-schedule", description="Schedule ofplang v0 documents."
    )
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

    s = sub.add_parser("schedule", help="produce an execution plan from a workflow and environment")
    s.add_argument("workflow", metavar="WORKFLOW", help="ofplang v0 workflow YAML")
    s.add_argument(
        "--env", required=True, metavar="ENV", help="execution environment definition YAML"
    )
    s.add_argument(
        "--document",
        metavar="DOC",
        help="execution document (§6): carries the interface boundary constraint (§6.8), and — "
        "when it sets `now` — the prior status to replan from "
        "(fix completed/running, re-optimise the rest)",
    )
    s.add_argument(
        "--running-margin",
        type=int,
        default=0,
        metavar="N",
        help="safety margin: a running activity's fixed end is clamped up to now + N (default: 0)",
    )
    s.add_argument(
        "--seed",
        type=int,
        default=None,
        metavar="N",
        help="reproducible solve: fix the CP-SAT seed to N and use a single worker "
        "(default: non-deterministic multi-worker search)",
    )
    s.add_argument("-o", "--out", metavar="FILE", help="write the plan here (default: stdout)")
    s.add_argument("--format", choices=["yaml", "json"], default="yaml", help="plan output format")
    s.add_argument(
        "--no-validate",
        action="store_true",
        help="skip the one-shot ofplang-validate front-door check of the workflow "
        "(use when it was already validated upstream, e.g. by the `ofp` umbrella CLI)",
    )

    z = sub.add_parser("visualize", help="render an execution plan as an HTML/SVG Gantt chart")
    z.add_argument("plan", metavar="PLAN", help="execution plan/document YAML")
    z.add_argument(
        "--view", choices=["device", "workflow", "lane"], default="device", help="lane layout"
    )
    z.add_argument(
        "--theme",
        choices=["light", "dark", "auto"],
        default="light",
        help="light/dark = fixed colours (PowerPoint-safe); auto = adapts to the viewer "
        "(browsers only)",
    )
    z.add_argument(
        "--format",
        choices=["html", "svg"],
        default=None,
        help="output format (default: svg, or html when -o ends in .html)",
    )
    z.add_argument("-o", "--out", metavar="FILE", help="write the chart here (default: stdout)")

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
                print(
                    f"ofp-schedule: cannot determine kind of {path!r}; pass --kind",
                    file=sys.stderr,
                )
                return EXIT_USAGE
        try:
            results.append(_validate_one(path, kind))
        except yaml.YAMLError as exc:
            # A file that does not parse as YAML is an input error, not a schema
            # validation result (auto-detect already handles this; an explicit
            # --kind must not slip past it). Mirrors _cmd_visualize.
            print(f"ofp-schedule: cannot parse {path!r}: {exc}", file=sys.stderr)
            return EXIT_USAGE

    if args.format == "json":
        print(_render_json(results))
    else:
        print(_render_text(results, args.quiet, _color_enabled(args.no_color)))

    return EXIT_OK if all(r.ok for _, r in results) else EXIT_INVALID


def _front_door_validate(workflow_path: str) -> bool:
    """Validate a workflow as portable v0 before scheduling (spec §2/§3, etc.).

    The scheduler library trusts its input; this CLI front door runs the full
    ofplang-validate pass once so a malformed workflow fails with clear
    diagnostics instead of being silently mis-scheduled. Returns True if the
    workflow is valid; otherwise prints each error and returns False. Uses
    extension-tolerant mode so `x-` extension keys — which the scheduler itself
    tolerates — are not rejected at the door.
    """
    result = validate_workflow(workflow_path, mode=EXTENSION_TOLERANT)
    if result.ok:
        return True
    for diag in result.diagnostics:
        if diag.file and diag.line:
            locator = f"{diag.file}:{diag.line}:{diag.col}"
        else:
            locator = diag.path or "<root>"
        detail = f"  {diag.path}" if diag.file and diag.path else ""
        message = f"  {diag.message}" if diag.message else ""
        print(f"{locator}: error {diag.code}{detail}{message}", file=sys.stderr)
    return False


def _cmd_schedule(args) -> int:
    doc = args.document
    inputs = [args.workflow, args.env] + ([doc] if doc else [])
    for p in inputs:
        if not Path(p).is_file():
            print(f"ofp-schedule: cannot open {p!r}: no such file", file=sys.stderr)
            return EXIT_USAGE

    # Front door: validate the workflow as portable v0 once, unless suppressed.
    # A validation failure is an invalid document (EXIT_INVALID), mirroring the
    # `validate` subcommand; the scheduler library is not invoked in that case.
    if not args.no_validate and not _front_door_validate(args.workflow):
        return EXIT_INVALID

    try:
        report = run_schedule(
            args.workflow,
            args.env,
            document_path=doc,
            running_task_margin=args.running_margin,
            random_seed=args.seed,
        )
    except yaml.YAMLError as exc:
        # Malformed workflow / environment / document YAML is an input error.
        print(f"ofp-schedule: cannot parse input: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not report.ok:
        # Surface every error diagnostic (missing location falls back to a path).
        for diag in report.diagnostics:
            if diag.severity != ERROR:
                continue
            locator = diag.location or diag.path or "<input>"
            message = f"  {diag.message}" if diag.message else ""
            print(f"{locator}: error {diag.code}{message}", file=sys.stderr)
        return EXIT_INVALID

    assert report.plan is not None  # report.ok (checked above) implies a plan
    text = (
        to_yaml(report.plan)
        if args.format == "yaml"
        else json.dumps(report.plan, indent=2, ensure_ascii=False)
    )
    if args.out:
        Path(args.out).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        print(
            f"ofp-schedule: wrote plan to {args.out} "
            f"(outcome={report.outcome}, makespan={report.makespan})",
            file=sys.stderr,
        )
    else:
        print(text if not text.endswith("\n") else text, end="" if text.endswith("\n") else "\n")
    return EXIT_OK


def _cmd_visualize(args) -> int:
    if not Path(args.plan).is_file():
        print(f"ofp-schedule: cannot open {args.plan!r}: no such file", file=sys.stderr)
        return EXIT_USAGE
    try:
        plan = yaml.safe_load(Path(args.plan).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(f"ofp-schedule: cannot parse {args.plan!r}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not isinstance(plan, dict) or "activities" not in plan:
        print(
            f"ofp-schedule: {args.plan!r} is not an execution document (no 'activities')",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Format: explicit --format wins; otherwise the default is svg, except when
    # the -o path clearly asks for html (.html / .htm).
    fmt = args.format
    if fmt is None:
        fmt = "html" if (args.out and args.out.lower().endswith((".html", ".htm"))) else "svg"

    render = render_svg if fmt == "svg" else render_html
    text = render(plan, view=args.view, theme=args.theme)
    if args.out:
        Path(args.out).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        print(
            f"ofp-schedule: wrote {args.view} view ({fmt}, {args.theme}) to {args.out}",
            file=sys.stderr,
        )
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    # Emit UTF-8 to stdout regardless of the console's default encoding (e.g. a
    # cp932 Windows console), so piped SVG/YAML never hits an encode error.
    # AttributeError/ValueError when stdout is not a real TextIO (e.g. under capture).
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    args = _build_parser().parse_args(argv)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "schedule":
        return _cmd_schedule(args)
    if args.command == "visualize":
        return _cmd_visualize(args)
    return EXIT_USAGE  # pragma: no cover - argparse enforces a subcommand


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
