"""Enable `python -m ofplang.schedule <file>...`.

Intent: mirror the console-script entry point so the CLI is reachable without an
installed script, which is convenient in dev checkouts and CI.
"""

from ofplang.schedule.cli import main

raise SystemExit(main())
