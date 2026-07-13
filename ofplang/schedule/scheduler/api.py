"""Public entry point: workflow + environment -> execution plan.

Orchestrates the pipeline (validate/load environment -> parse workflow -> build
instance -> solve -> render plan) and collects diagnostics from every stage into
one report. This is the initial-plan slice: no replanning status is consumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ofplang.schedule.core.diagnostics import ERROR, Diagnostic
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.plan import render_plan
from ofplang.schedule.scheduler.workflow import parse_workflow
from ofplang.schedule.validation import errors


@dataclass(frozen=True)
class ScheduleReport:
    """Outcome of a scheduling run. `plan` is the execution document (§6) when a
    schedule was produced; `diagnostics` carries every stage's findings."""

    outcome: str | None
    makespan: int | None
    plan: dict | None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.plan is not None and self.outcome in ("optimal", "feasible")


def _has_error(diagnostics) -> bool:
    return any(d.severity == ERROR for d in diagnostics)


def schedule(workflow_path, environment_path, *, max_time_seconds: float | None = None) -> ScheduleReport:
    diagnostics: list[Diagnostic] = []

    # 1. Environment: schema-validate, then load into the model.
    env, env_result = load_environment(environment_path)
    diagnostics += env_result.diagnostics
    if env is None:
        return ScheduleReport(None, None, None, diagnostics)

    # 2. Workflow: our own minimal parse (D17).
    workflow, wf_diags = parse_workflow(workflow_path)
    diagnostics += wf_diags.items
    if workflow is None or _has_error(wf_diags.items):
        return ScheduleReport(None, None, None, diagnostics)

    # 3. Instance + execution-layer checks (§9.3 subset).
    instance, inst_diags = build_instance(workflow, env)
    diagnostics += inst_diags.items
    if instance is None:
        return ScheduleReport(None, None, None, diagnostics)

    # 4. Solve, then 5. render the plan (only when feasible).
    solution = solve(instance, max_time_seconds=max_time_seconds)
    if solution.outcome not in ("optimal", "feasible"):
        diagnostics.append(Diagnostic(errors.INFEASIBLE, "no feasible schedule found"))
        return ScheduleReport(solution.outcome, None, None, diagnostics)

    plan = render_plan(
        instance,
        solution,
        workflow=str(workflow_path),
        environment=str(environment_path),
    )
    return ScheduleReport(solution.outcome, solution.makespan, plan, diagnostics)
