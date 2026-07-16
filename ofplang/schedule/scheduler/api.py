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
from pathlib import Path

import yaml

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
    document_path=None,
    status_path=None,
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
    random_seed: int | None = None,
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

    # Unified execution-document input (SPEC §6.1). `document_path` is primary;
    # `status_path` is a deprecated alias (removed in a later phase). Shape-validate
    # it once, then read `interface` (the boundary constraint, §6.8). There is no
    # separate initial-vs-replan path: an initial plan is a replan with empty
    # history and now = 0, so the same normalize + solve handles both. `had_now`
    # only drives whether the output echoes `now`.
    doc_path = document_path if document_path is not None else status_path
    interface = None
    had_now = False
    root = None
    if doc_path is not None:
        doc_result = validate_document(doc_path)
        diagnostics += doc_result.diagnostics
        if not doc_result.ok:
            return ScheduleReport(None, None, None, diagnostics)
        raw = yaml.safe_load(Path(doc_path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            interface = raw.get("interface")
            had_now = "now" in raw
        root = yamlnode.load_file(doc_path)

    # 3. Build the instance (boundary nodes/arcs from interface always re-created,
    # like relays) and normalize the document into the augmented instance +
    # fixation (empty history when there is no document). Reachability is checked
    # per pending leg after normalization (committed legs are facts).
    base, inst_diags = build_instance(workflow, env, interface=interface, check_reachability=False)
    diagnostics += inst_diags.items
    if base is None:
        return ScheduleReport(None, None, None, diagnostics)

    instance, fixation, norm_diags = normalize(base, root, env)
    diagnostics += norm_diags.items
    if instance is None or fixation is None:
        return ScheduleReport(None, None, None, diagnostics)

    reach = Diagnostics()
    report_unreachable(instance, set(fixation.arcs), reach)
    diagnostics += reach.items
    if _has_error(reach.items):
        return ScheduleReport(None, None, None, diagnostics)

    # 4. Solve, then 5. render the plan (only when feasible).
    solution = solve(
        instance,
        fixation=fixation,
        running_task_margin=running_task_margin,
        max_time_seconds=max_time_seconds,
        random_seed=random_seed,
    )
    if solution.outcome not in ("optimal", "feasible"):
        diagnostics.append(Diagnostic(errors.INFEASIBLE, "no feasible schedule found"))
        return ScheduleReport(solution.outcome, None, None, diagnostics)

    plan = render_plan(
        instance,
        solution,
        workflow=str(workflow_path),
        environment=str(environment_path),
        status=str(doc_path) if root is not None else None,
        now=fixation.now if had_now else None,
        placements=fixation.placements,
        interface=interface,
    )
    return ScheduleReport(solution.outcome, solution.makespan, plan, diagnostics)
