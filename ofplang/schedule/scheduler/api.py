"""Public entry point: workflow + environment (+ status) -> execution plan.

Orchestrates the pipeline (validate/load environment -> parse workflow -> build
instance -> solve -> render plan) and collects diagnostics from every stage into
one report. Given a `status_path`, the same pipeline replans: the execution
status is shape-validated, matched against the instance to build the fixation
(completed/running activities pinned, pending re-optimised at/after `now`), and
the fixed history plus `now` and `placements` are carried into the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import ERROR, Diagnostic, Diagnostics
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance, report_unreachable
from ofplang.schedule.scheduler.normalize import normalize
from ofplang.schedule.scheduler.plan import render_plan
from ofplang.schedule.scheduler.workflow import parse_workflow
from ofplang.schedule.validation import errors
from ofplang.schedule.validation import validate_document


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


def schedule(
    workflow_path,
    environment_path,
    *,
    status_path=None,
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
) -> ScheduleReport:
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

    # 3. Instance + execution-layer checks (§9.3 subset). On an initial plan,
    # reachability is checked per workflow arc here. On a replan it is deferred:
    # committed transports may lack a direct current-env route (they re-route
    # through relays), so reachability is re-checked per pending leg after
    # normalization.
    if status_path is None:
        instance, inst_diags = build_instance(workflow, env)
        diagnostics += inst_diags.items
        if instance is None:
            return ScheduleReport(None, None, None, diagnostics)
        fixation = None
    else:
        base, inst_diags = build_instance(workflow, env, check_reachability=False)
        diagnostics += inst_diags.items
        if base is None:
            return ScheduleReport(None, None, None, diagnostics)

        # 4. Replan status: shape-validate, then normalize into the augmented
        # instance + fixation. Fixed parts are pinned as historical facts (not
        # re-validated against the env); a prior plan feeds back verbatim.
        status_result = validate_document(status_path)
        diagnostics += status_result.diagnostics
        if not status_result.ok:
            return ScheduleReport(None, None, None, diagnostics)
        instance, fixation, norm_diags = normalize(base, yamlnode.load_file(status_path), env)
        diagnostics += norm_diags.items
        if instance is None or fixation is None:
            return ScheduleReport(None, None, None, diagnostics)

        # Reachability of the pending legs only (committed legs are facts).
        reach = Diagnostics()
        report_unreachable(instance, set(fixation.arcs), reach)
        diagnostics += reach.items
        if _has_error(reach.items):
            return ScheduleReport(None, None, None, diagnostics)

    # 5. Solve, then 6. render the plan (only when feasible).
    solution = solve(
        instance,
        fixation=fixation,
        running_task_margin=running_task_margin,
        max_time_seconds=max_time_seconds,
    )
    if solution.outcome not in ("optimal", "feasible"):
        diagnostics.append(Diagnostic(errors.INFEASIBLE, "no feasible schedule found"))
        return ScheduleReport(solution.outcome, None, None, diagnostics)

    plan = render_plan(
        instance,
        solution,
        workflow=str(workflow_path),
        environment=str(environment_path),
        status=str(status_path) if status_path is not None else None,
        now=fixation.now if fixation is not None else None,
        placements=fixation.placements if fixation is not None else None,
    )
    return ScheduleReport(solution.outcome, solution.makespan, plan, diagnostics)
