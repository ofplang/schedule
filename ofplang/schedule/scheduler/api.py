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
from dataclasses import dataclass, field, replace

from ofplang.schedule.core import objective as objective_stages
from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import ERROR, Diagnostic, Diagnostics
from ofplang.schedule.core.yamlnode import YMap
from ofplang.schedule.scheduler.cpsat import solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import (
    build_instance,
    merge_instances,
    prefix_instance,
    report_unreachable,
)
from ofplang.schedule.scheduler.model import Workflow
from ofplang.schedule.scheduler.normalize import normalize
from ofplang.schedule.scheduler.plan import render_plan
from ofplang.schedule.scheduler.plancheck import check_plan_inventories
from ofplang.schedule.scheduler.stats import SolveStats
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
    # What the solve cost (stats.py). Set on every path that reached the solver --
    # including an infeasible instance and a plan withheld by a defect -- and None
    # where the pipeline stopped before solving, since there is then nothing to
    # measure. It describes the *solve*, never the schedule, which is why none of it
    # goes into the plan document: that stays portable v0 (SPEC §6).
    stats: SolveStats | None = None

    @property
    def ok(self) -> bool:
        return self.plan is not None and self.outcome in ("optimal", "feasible")


def _has_error(diagnostics) -> bool:
    return any(d.severity == ERROR for d in diagnostics)


def _objective_of(declared) -> tuple[str, ...]:
    """The stages to minimise: the execution document's declaration, else the
    default (§4.8, §6.1).

    One declaration site. The objective says how *this run* is to be optimised, so
    it belongs with the run's other planning inputs rather than with the description
    of the lab -- the same argument that put `interface` and `inventories` in the
    document. The environment was read here too until 0.2.1, first as the only site
    and then as a deprecated fallback; it is now refused there
    (`objective_in_environment`), so nothing reaches this function from the lab.

    A `kind` that names no stage list falls back to the default rather than being
    reported: the document validator has already refused it (`unknown_objective_kind`)
    and the pipeline stopped, so the only way to arrive here with one is an unvalidated
    call, where guessing the default beats raising.
    """
    if declared is None:
        return objective_stages.DEFAULT
    return objective_stages.normalize(declared) or objective_stages.DEFAULT


def _roster_ids(roster) -> set[str]:
    """The job ids a document's `jobs` names (§6.11). The document has already been
    shape-validated, so every entry is a mapping with a string `id`; anything else is
    simply not counted rather than raising here."""
    return {
        entry["id"]
        for entry in roster
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def _attribute(items, job, jobs) -> list[Diagnostic]:
    """Name the job a per-workflow diagnostic came from.

    Only in a joint plan, and only in the message: the code and the source position
    stay exactly what the single-workflow pipeline produces, so nothing that matches
    on them has to know about jobs. Without this, two jobs running the same workflow
    report the same finding twice with no way to tell which is which.
    """
    if len(jobs) < 2:
        return list(items)
    return [replace(d, message=f"job {job.id!r}: {d.message}") for d in items]


def _provenance(value, source: str | None) -> str:
    """What the plan's `meta` records for one input: the display name the caller
    gave, else the path it was read from -- or `<in-memory>` for a document that
    was handed over already loaded and so has no path to name."""
    if source is not None:
        return source
    return "<in-memory>" if isinstance(value, dict) else str(value)


@dataclass(frozen=True)
class JobInput:
    """One workflow to plan as part of a joint plan (SPEC §6.11).

    `id` names the job in the plan: every activity of this workflow carries it as
    its `job`, and it is what tells two jobs' activities apart when both run the
    same workflow (so it must be unique within one call, and non-empty). `workflow`
    is a path or an already-loaded document, exactly as `schedule` accepts, and
    `source` is the optional display path recorded as provenance.
    """

    id: str
    workflow: object
    source: str | None = None


def schedule(
    workflow_path,
    environment_path,
    *,
    document_path=None,
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
    random_seed: int | None = None,
    ignore_resources: bool = False,
    collect_solutions: bool = False,
    workflow_source: str | None = None,
    environment_source: str | None = None,
    document_source: str | None = None,
) -> ScheduleReport:
    """Plan one workflow. See `schedule_jobs` for several at once.

    Each of `workflow_path`, `environment_path` and `document_path` is a path to
    a file or an already-loaded document (a mapping) -- e.g. an import-expanded
    workflow, or the status a runner just rendered from its own history.

    `workflow_source` / `environment_source` / `document_source` are optional
    display paths recorded as the plan's `meta` provenance: a caller that passes an
    in-memory document (so nothing is read from disk here) can still name the
    original file, instead of the plan showing `<in-memory>`.

    `ignore_resources` switches the consumable model off (§4.7.3): the environment's
    resource declarations are still shape-checked, but nothing is applied and the
    plan is shaped as it would be from an environment that never declared one. It is
    a relaxation, so it never turns a solvable instance unsolvable. This is how a
    resource-bearing environment can drive a consumer that does not know about
    resources -- though such a caller has to pass it, so driving `ofplang.run` this
    way is not possible until run does.

    `collect_solutions` records every improving solution the search found, into
    `report.stats.phases[-1].history`, which is what an anytime measurement (how good
    was the schedule at time t?) reads. Off by default: a solution callback runs
    inside the search and can perturb the timings it is there to measure, so only a
    caller that wants the curve pays for it. The rest of `report.stats` -- timings,
    bound, model size -- costs nothing and is always there.

    In-memory documents are read, never written to."""
    # The single-workflow call is the one-job case with an empty id, which is what
    # leaves node paths unprefixed and the plan free of any `job` field -- so a plan
    # for one workflow is exactly what it was before joint planning existed.
    return _run(
        [JobInput("", workflow_path, workflow_source)],
        environment_path,
        document_path=document_path,
        running_task_margin=running_task_margin,
        max_time_seconds=max_time_seconds,
        random_seed=random_seed,
        ignore_resources=ignore_resources,
        collect_solutions=collect_solutions,
        environment_source=environment_source,
        document_source=document_source,
    )


def schedule_jobs(
    jobs,
    environment_path,
    *,
    document_path=None,
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
    random_seed: int | None = None,
    ignore_resources: bool = False,
    collect_solutions: bool = False,
    environment_source: str | None = None,
    document_source: str | None = None,
) -> ScheduleReport:
    """Plan several workflows together, against one environment (SPEC §6.11).

    `jobs` is a sequence of `JobInput`. The jobs share everything the environment
    describes -- devices, spots, transporters -- and share the consumable stocks the
    execution document's `inventories` starts them at, because a stock belongs to a
    device rather than to a workflow. That sharing is the reason to plan jointly:
    the jobs compete for machines, and a refill that neither workflow needs on its
    own is planned once for both.

    Every other argument means what it does for `schedule`. What a joint plan does
    *not* have yet is anything that distinguishes the jobs from one another: there
    are no priorities, no release times and no per-job objective, so the makespan
    minimised is the one over all of them (design.md D38 stages this deliberately).
    Per-job `interface` is likewise not there yet, so a joint plan cannot use the
    document's single boundary constraint (`multi_job_interface`); workflows whose
    entry inputs are Object-bearing therefore cannot be planned jointly yet.
    """
    jobs = list(jobs)
    if not jobs:
        raise ValueError("schedule_jobs needs at least one job")
    ids = [job.id for job in jobs]
    if not all(ids):
        raise ValueError("every job needs a non-empty id (it names the job in the plan)")
    if len(set(ids)) != len(ids):
        raise ValueError(f"job ids must be unique within one plan: {ids}")
    return _run(
        jobs,
        environment_path,
        document_path=document_path,
        running_task_margin=running_task_margin,
        max_time_seconds=max_time_seconds,
        random_seed=random_seed,
        ignore_resources=ignore_resources,
        collect_solutions=collect_solutions,
        environment_source=environment_source,
        document_source=document_source,
    )


def _run(
    jobs,
    environment_path,
    *,
    document_path=None,
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
    random_seed: int | None = None,
    ignore_resources: bool = False,
    collect_solutions: bool = False,
    environment_source: str | None = None,
    document_source: str | None = None,
) -> ScheduleReport:
    """The pipeline both entry points run. One job with an empty id is the
    single-workflow case; several named jobs are a joint plan (§6.11)."""
    diagnostics: list[Diagnostic] = []

    # 1. Environment: schema-validate, then load into the model. One environment
    # serves every job -- that is what makes them compete for the same machines.
    env, env_result = load_environment(environment_path)
    diagnostics += env_result.diagnostics
    if env is None:
        return ScheduleReport(None, None, None, diagnostics)

    # 2. Workflows: our own minimal parse (D17), one per job. Every job is parsed
    # before any is rejected, so a caller with two broken workflows hears about both.
    workflows: list[Workflow] = []
    for job in jobs:
        workflow, wf_diags = parse_workflow(job.workflow)
        diagnostics += _attribute(wf_diags.items, job, jobs)
        if workflow is not None and not _has_error(wf_diags.items):
            workflows.append(workflow)
    if len(workflows) != len(jobs):
        return ScheduleReport(None, None, None, diagnostics)

    # Unified execution-document input (SPEC §6.1). Shape-validate it once, then read
    # `interface` (the boundary constraint, §6.8). There is no separate
    # initial-vs-replan path: an initial plan is a replan with empty history and
    # now = 0, so the same normalize + solve handles both. `had_now` only drives
    # whether the output echoes `now`.
    doc_path = document_path
    interface = None
    inventories = None
    declared_objective = None
    roster = None
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
            inventories = copy.deepcopy(doc_path.get("inventories"))
            declared_objective = (doc_path.get("objective") or {}).get("kind")
            roster = copy.deepcopy(doc_path.get("jobs"))
            had_now = "now" in doc_path
        elif isinstance(root, YMap):
            interface = yamlnode.to_plain(root.get("interface"))
            inventories = yamlnode.to_plain(root.get("inventories"))
            stated = yamlnode.to_plain(root.get("objective"))
            declared_objective = stated.get("kind") if isinstance(stated, dict) else None
            roster = yamlnode.to_plain(root.get("jobs")) if "jobs" in root else None
            had_now = "now" in root

    # A document that names its jobs (§6.11) has to name *these* jobs. It is a
    # replanning input describing work done by particular workflows, and matching that
    # history against a different set would pin it onto activities that never ran it.
    # Compared as a set: the roster's order is the record of how the jobs were given,
    # and re-stating them in another order is not a different plan.
    if roster is not None:
        stated_ids = _roster_ids(roster)
        # The single-workflow call has no job identity at all, so it matches only an
        # empty roster -- never one that names jobs. An empty roster and no roster say
        # the same thing there and are both accepted.
        given_ids = {job.id for job in jobs if job.id}
        if stated_ids != given_ids:
            given = f"{sorted(given_ids)} were given" if given_ids else "one unnamed workflow"
            diagnostics.append(
                Diagnostic(
                    errors.JOB_ROSTER_MISMATCH,
                    f"the document plans jobs {sorted(stated_ids)}, but {given}",
                    "jobs",
                )
            )
            return ScheduleReport(None, None, None, diagnostics)

    # The document's `interface` binds one workflow's boundary material to spots, so
    # it says nothing about which job each binding belongs to. Rather than guess (and
    # have several jobs' boundary nodes silently claim the same spot at time 0), a
    # joint plan refuses it. Per-job interface is the next stage; until then, a
    # workflow with Object-bearing entry inputs cannot be part of a joint plan --
    # `build_instance` rejects it with the usual `interface_input_missing`.
    if len(jobs) > 1 and interface:
        diagnostics.append(
            Diagnostic(
                errors.MULTI_JOB_INTERFACE,
                "interface binds one workflow's boundary and cannot be shared by "
                f"{len(jobs)} jobs",
                "interface",
            )
        )
        return ScheduleReport(None, None, None, diagnostics)

    # 3. Build one instance per job (boundary nodes/arcs from interface always
    # re-created, like relays), prefix each job's node paths with its id so the two
    # cannot collide, and merge. Everything downstream sees a single instance and
    # never learns that jobs exist -- which is what lets one refill candidate serve
    # activities from several jobs (`merge_instances`).
    bases = []
    for job, workflow in zip(jobs, workflows, strict=True):
        base, inst_diags = build_instance(
            workflow, env, interface=interface, check_reachability=False
        )
        diagnostics += _attribute(inst_diags.items, job, jobs)
        if base is not None:
            bases.append(prefix_instance(base, (job.id,) if job.id else ()))
    # Every job is built before any rejection, for the same reason every one is
    # parsed first: a caller whose environment is missing two capabilities should
    # hear about both, not be sent round the loop once per job.
    if len(bases) != len(jobs):
        return ScheduleReport(None, None, None, diagnostics)
    base = merge_instances(bases)

    instance, fixation, norm_diags = normalize(
        base, root, env, ignore_resources=ignore_resources
    )
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
        objective=_objective_of(declared_objective),
        collect_solutions=collect_solutions,
    )
    if solution.outcome not in ("optimal", "feasible"):
        diagnostics.append(Diagnostic(errors.INFEASIBLE, "no feasible schedule found"))
        return ScheduleReport(solution.outcome, None, None, diagnostics, solution.stats)

    # One job's provenance is the string it always was; a joint plan's is the list of
    # its workflows, in job order, so `meta` still names everything the plan came from.
    provenance = [_provenance(job.workflow, job.source) for job in jobs]
    plan = render_plan(
        instance,
        solution,
        workflow=provenance[0] if len(jobs) == 1 else provenance,
        environment=_provenance(environment_path, environment_source),
        status=_provenance(doc_path, document_source) if root is not None else None,
        now=fixation.now if had_now else None,
        interface=interface,
        inventories=inventories,
        ignore_resources=ignore_resources,
        jobs=tuple(job.id for job in jobs if job.id),
    )

    # 6. Check what is about to be handed out. The refill amounts in the document are
    # derived from the solved model rather than read off it (§4.7.1), so the document
    # is a second computation over the same answer -- and two computations that must
    # agree are worth checking rather than arguing about. A finding is a defect here,
    # not bad input, but it is reported instead of shipped: a plan that under-fills a
    # stock schedules cleanly, says `optimal`, and runs dry in a real lab.
    for message in check_plan_inventories(plan, env, inventories):
        diagnostics.append(Diagnostic(errors.PLAN_INVENTORY_INCONSISTENT, message, "activities"))
    if _has_error(diagnostics):
        return ScheduleReport(solution.outcome, None, None, diagnostics, solution.stats)

    return ScheduleReport(
        solution.outcome, solution.makespan, plan, diagnostics, solution.stats
    )
