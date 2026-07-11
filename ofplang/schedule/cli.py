"""Command-line interface for the ofplang v0 scheduler.

Intent: this is a thin presentation layer over the (not-yet-written) scheduling
library. Only the entry point and argument surface exist so far; driving a real
scheduler is the next step. Keeping the CLI thin means it cannot drift from the
library once that library lands.

Usage:
    ofp-schedule <file>...

Also invocable as `python -m ofplang.schedule`, and (once installed) as the
`schedule` subcommand of the umbrella `ofp` CLI.

Exit codes:
    0  scheduling succeeded
    2  usage / input error (bad arguments, missing input file)
    3  not implemented yet (placeholder while the scheduler is under construction)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ofp-schedule",
        description="Schedule ofplang v0 documents.",
    )
    parser.add_argument("paths", nargs="+", metavar="FILE", help="document(s) to schedule")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Pre-check inputs so a mistyped/missing path is a usage error (exit 2)
    # rather than surfacing later as an obscure failure.
    missing = [p for p in args.paths if not Path(p).is_file()]
    if missing:
        for p in missing:
            print(f"ofp-schedule: cannot open {p!r}: no such file", file=sys.stderr)
        return EXIT_USAGE

    print("ofp-schedule: scheduling is not implemented yet", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
