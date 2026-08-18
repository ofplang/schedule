"""Public entry point: workflow + environment (+ status) -> execution plan.

Orchestrates the pipeline (validate/load environment -> parse workflow -> build
instance -> solve -> render plan) and collects diagnostics from every stage into
one report. Given a `document_path` that sets `now`, the same pipeline replans: the
execution status is shape-validated, matched against the instance to build the fixation
(completed/running activities pinned, pending re-optimised at/after `now`), and
the fixed history plus `now` and the `interface` constraint are carried into the output.

Every input -- workflow, environment, execution document -- is accepted either as a
path or as an already-loaded document (a mapping), so an embedder that holds them in
memory (the rolling-horizon runner replanning each tick) does not round-trip them
through files. An in-memory document is read as it stands and never written to; the
`interface` echoed into the plan is copied, so the plan shares no structure with it.
A document read from a file is parsed once here: the same wrapped tree is what the
schema validator checks, what `interface` / `now` are read off, and what the
normalizer matches against the instance.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import ERROR, Diagnostic, Diagnostics
from ofplang.schedule.core.yamlnode import YMap
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance, report_unreachable
from ofplang.schedule.scheduler.normalize import normalize
from ofplang.schedule.scheduler.plan import render_plan
from ofplang.schedule.scheduler.workflow import parse_workflow
from ofplang.schedule.validation import errors
from ofplang.schedule.validation.document import validate_document_node


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


def _provenance(value, source: str | None) -> str:
    """What the plan's `meta` records for one input: the display name the caller
    gave, else the path it was read from -- or `<in-memory>` for a document that
    was handed over already loaded and so has no path to name."""
    if source is not None:
        return source
    return "<in-memory>" if isinstance(value, dict) else str(value)


def schedule(
    workflow_path,
    environment_path,
    *,
    document_path=None,
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
    random_seed: int | None = None,
    workflow_source: str | None = None,
    environment_source: str | None = None,
    document_source: str | None = None,
) -> ScheduleReport:
    """Each of `workflow_path`, `environment_path` and `document_path` is a path to
    a file or an already-loaded document (a mapping) -- e.g. an import-expanded
    workflow, or the status a runner just rendered from its own history.

    `workflow_source` / `environment_source` / `document_source` are optional
    display paths recorded as the plan's `meta` provenance: a caller that passes an
    in-memory document (so nothing is read from disk here) can still name the
    original file, instead of the plan showing `<in-memory>`.

    In-memory documents are read, never written to."""
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

    # Unified execution-document input (SPEC §6.1). Shape-validate it once, then read
    # `interface` (the boundary constraint, §6.8). There is no separate
    # initial-vs-replan path: an initial plan is a replan with empty history and
    # now = 0, so the same normalize + solve handles both. `had_now` only drives
    # whether the output echoes `now`.
    doc_path = document_path
    interface = None
    had_now = False
    root = None
    if doc_path is not None:
        root = yamlnode.load_source(doc_path)
        doc_result = validate_document_node(root)
        diagnostics += doc_result.diagnostics
        if not doc_result.ok:
            return ScheduleReport(None, None, None, diagnostics)
        # Read the two fields the pipeline needs off what was already parsed. From a
        # file, `to_plain` builds fresh objects, so the plan cannot end up sharing a
        # subtree with anything; an in-memory document is the caller's own, so its
        # `interface` is copied before being echoed into the plan (below).
        if isinstance(doc_path, dict):
            interface = copy.deepcopy(doc_path.get("interface"))
            had_now = "now" in doc_path
        elif isinstance(root, YMap):
            interface = yamlnode.to_plain(root.get("interface"))
            had_now = "now" in root

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
        workflow=_provenance(workflow_path, workflow_source),
        environment=_provenance(environment_path, environment_source),
        status=_provenance(doc_path, document_source) if root is not None else None,
        now=fixation.now if had_now else None,
        interface=interface,
    )
    return ScheduleReport(solution.outcome, solution.makespan, plan, diagnostics)
