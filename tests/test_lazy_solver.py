"""Importing the package must not load the solver.

`ortools` costs about 0.8 s and 33 MB to import, and a caller that only wants
the constructed schedule (`greedy`, which knows nothing of CP-SAT) should not
pay it. That is a property of the *import graph*, and the only thing keeping it
true is that nobody adds a module-level `import cpsat` -- which is exactly the
kind of thing that goes back silently. So it is asserted.

**In a fresh interpreter, necessarily.** By the time pytest reaches this file
other test modules have long since imported `cpsat`, so asking about
`sys.modules` in this process would answer a different question.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _in_a_fresh_interpreter(code: str) -> list[str]:
    """Run `code` and return the ofplang/ortools modules it ended up loading."""
    probe = textwrap.dedent(code) + textwrap.dedent(
        """
        import sys
        print("\\n".join(sorted(
            name for name in sys.modules
            if name.startswith("ortools") or name.endswith("scheduler.cpsat")
        )))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    return [line for line in done.stdout.splitlines() if line]


def test_importing_the_package_does_not_load_the_solver():
    loaded = _in_a_fresh_interpreter("import ofplang.schedule")
    assert loaded == [], f"importing the package pulled in {loaded}"


def test_building_a_schedule_by_hand_does_not_load_the_solver():
    # The case this exists for: the greedy all the way to a schedule, with the
    # solver never touched.
    loaded = _in_a_fresh_interpreter(
        """
        from pathlib import Path
        from ofplang.schedule.scheduler.envload import load_environment
        from ofplang.schedule.scheduler.greedy import construct
        from ofplang.schedule.scheduler.instance import build_instance
        from ofplang.schedule.scheduler.workflow import parse_workflow

        examples = Path("examples")
        workflow, _ = parse_workflow(examples / "reformatter.workflow.yaml")
        environment, _ = load_environment(examples / "reformatter.env.yaml")
        instance, _ = build_instance(workflow, environment)
        assert construct(instance) is not None
        """
    )
    assert loaded == [], f"constructing a schedule pulled in {loaded}"


def test_solving_does_load_the_solver():
    # The control. An assertion that nothing loads ortools would also pass if
    # ortools had simply stopped being reachable, which is not the claim.
    loaded = _in_a_fresh_interpreter(
        """
        from pathlib import Path
        from ofplang.schedule import schedule

        examples = Path("examples")
        schedule(examples / "simple.workflow.yaml", examples / "simple.env.yaml")
        """
    )
    assert any(name.startswith("ortools") for name in loaded)
