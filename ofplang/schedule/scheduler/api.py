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
from ofplang.schedule.core.diagnostics import ERROR, WARNING, Diagnostic, Diagnostics
from ofplang.schedule.core.yamlnode import YMap
from ofplang.schedule.scheduler.cpsat import Solution, solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import (
    build_instance,
    merge_instances,
    prefix_instance,
    report_unreachable,
)
from ofplang.schedule.scheduler.model import JobSpec, Workflow
from ofplang.schedule.scheduler.normalize import normalize
from ofplang.schedule.scheduler.plan import render_plan
from ofplang.schedule.scheduler.plancheck import check_plan_inventories
from ofplang.schedule.scheduler.stats import SolveStats
from ofplang.schedule.scheduler.workflow import fingerprint, parse_workflow
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


_SOLVED = ("optimal", "feasible")


def _has_error(diagnostics) -> bool:
    return any(d.severity == ERROR for d in diagnostics)


def _objective_of(declared, job_count: int = 0) -> tuple[str, ...]:
    """The stages to minimise: the execution document's declaration, else the
    default (§4.8, §6.1).

    One declaration site. The objective says how *this run* is to be optimised, so
    it belongs with the run's other planning inputs rather than with the description
    of the lab -- the same argument that put `interface` and `inventories` in the
    document. The environment was read here too until 0.2.1, first as the only site
    and then as a deprecated fallback; it is now refused there
    (`objective_in_environment`), so nothing reaches this function from the lab.

    The default depends on the job count (§4.8): with several jobs there is something
    for `completion_time_sum` to trade off, and with one there is not. Only the default
    does -- a stated `kind` is honoured as written, whatever the count.

    A `kind` that names no stage list falls back to the default rather than being
    reported: the document validator has already refused it (`unknown_objective_kind`)
    and the pipeline stopped, so the only way to arrive here with one is an unvalidated
    call, where guessing the default beats raising.
    """
    if declared is None:
        return objective_stages.default(job_count)
    return objective_stages.normalize(declared) or objective_stages.default(job_count)


def _roster_entries(roster) -> dict[str, dict]:
    """A document's `jobs` roster (§6.11), by job id. The document has already been
    shape-validated, so every entry is a mapping with a string `id`; anything else is
    simply skipped rather than raising here."""
    return {
        entry["id"]: entry
        for entry in roster
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def _job_specs(jobs, workflows, roster: dict[str, dict] | None, now: int) -> list[JobSpec]:
    """Resolve each job's planning parameters (§6.11) from the roster it appears in.

    A job the roster does not name is **arriving now**, so it starts with no promise
    (`bound = None`, assigned by this solve) and, unless the roster says otherwise,
    cannot begin before `now` -- it did not exist earlier, and a schedule that started
    it in the past would be describing work nobody could have done.

    The fingerprint is always the one just computed from the workflow handed over, not
    the one the roster carries; the two are compared separately (`_check_fingerprints`)
    so a mismatch is reported rather than silently overwritten.
    """
    specs = []
    for job, workflow in zip(jobs, workflows, strict=True):
        entry = (roster or {}).get(job.id) or {}
        arriving = roster is not None and job.id not in roster
        release = entry.get("release")
        if not isinstance(release, int):
            release = now if arriving else 0
        bound = entry.get("bound")
        specs.append(
            JobSpec(
                id=job.id,
                release=release,
                bound=bound if isinstance(bound, int) else None,
                fingerprint=fingerprint(workflow),
                interface=copy.deepcopy(entry.get("interface")),
            )
        )
    return specs


def _check_fingerprints(specs, roster: dict[str, dict]) -> list[Diagnostic]:
    """Each job must be running the workflow its roster entry was planned for (§6.11).

    Nothing else ties an id to a workflow: two jobs handed over in the other order have
    ids that match as a set, so the roster check passes and each job would be matched
    against the other's history. An entry with no recorded fingerprint is left alone --
    it was written before this was recorded, and refusing it would strand documents
    that are otherwise perfectly replannable.
    """
    out = []
    for spec in specs:
        stated = (roster.get(spec.id) or {}).get("fingerprint")
        if isinstance(stated, str) and stated != spec.fingerprint:
            out.append(
                Diagnostic(
                    errors.JOB_WORKFLOW_MISMATCH,
                    f"job {spec.id!r} was planned for a different workflow "
                    f"(roster says {stated}, this one is {spec.fingerprint})",
                    "jobs",
                )
            )
    return out


def _boundary_spots(spec: JobSpec, side: str) -> dict[str, str]:
    """One job's `interface` bindings on one side, as spot -> port name."""
    bindings = (spec.interface or {}).get(side) or {}
    return {
        spot: port
        for port, spot in bindings.items()
        if isinstance(port, str) and isinstance(spot, str)
    }


def _check_boundary_spots(specs: tuple[JobSpec, ...]) -> list[Diagnostic]:
    """What two jobs may and may not share at the boundary (SPEC §6.8, §6.11).

    **Outputs cannot be shared.** A delivered Object holds its spot until the run is
    over, so two jobs delivering to one spot always overlap there -- the instance is
    infeasible however the schedule is arranged, and saying so beats "no feasible
    schedule found".

    **Inputs can be**, and often should: entry material holds its spot only from the
    job's release until the move that collects it, so a second job released after the
    first one's material has left uses the same place legitimately -- one loading bay,
    two runs. Whether the times work out is the solver's to decide, so this only warns.

    🔴 Both rules are true *of one plan*. A job that leaves the plan takes its material
    with it (design.md "ジョブの退出"), and then even an output spot is free for the
    next job. That is why both live in this one function: relaxing them later is
    editing one place.
    """
    out: list[Diagnostic] = []
    for side, code, severity in (
        ("outputs", errors.INTERFACE_DUPLICATE_SPOT, ERROR),
        ("inputs", errors.INTERFACE_SHARED_INPUT_SPOT, WARNING),
    ):
        owner: dict[str, tuple[str, str]] = {}
        for spec in specs:
            for spot, port in sorted(_boundary_spots(spec, side).items()):
                if spot in owner:
                    first_job, first_port = owner[spot]
                    detail = (
                        "two jobs cannot deliver to one spot: a delivered Object holds "
                        "it until the run is over"
                        if side == "outputs"
                        else "their releases must leave the first job's material time "
                        "to be collected before the second's arrives"
                    )
                    out.append(
                        Diagnostic(
                            code,
                            f"job {first_job!r} ({first_port}) and job {spec.id!r} "
                            f"({port}) both bind {spot!r} -- {detail}",
                            "jobs",
                            severity=severity,
                        )
                    )
                else:
                    owner[spot] = (spec.id, port)
    return out


def _promised(specs: tuple[JobSpec, ...], solution: Solution) -> tuple[JobSpec, ...]:
    """The roster with every job's promise settled (§6.11).

    A job that arrived with no bound gets the completion this solve achieved for it.
    A job that already had one keeps it: bounds do not ratchet. Tightening them each
    time the search happened to place a job early would turn ordinary variation in
    how long things take into a relaxation on the very next replan, and B_j would stop
    meaning "what this job was promised when it arrived" (design.md D38).
    """
    return tuple(
        spec
        if spec.bound is not None
        else replace(spec, bound=solution.job_completions.get(spec.id))
        for spec in specs
    )


def _solve_within_bounds(
    instance, specs: tuple[JobSpec, ...], solve_kwargs: dict
) -> tuple[Solution, tuple[JobSpec, ...], list[Diagnostic]]:
    """Solve subject to every job's promised bound, relaxing as little as possible if
    they cannot all be kept.

    Normally this is **one** solve: the promises hold, and the jobs that arrived
    without one are given the completion it found. The rest of this runs only when
    reality has moved -- work took longer than planned, a machine went out of service --
    and some promise can no longer be met.

    Then it takes three steps. First, drop every bound and solve once: if that is still
    unschedulable the promises were never the problem, and reporting a relaxation would
    send the reader to the wrong place. Otherwise walk the roster **in order**, keeping
    each promise that still admits a schedule given the ones already kept, and dropping
    the first that does not -- so a job is relaxed only when no schedule keeps it, and
    an earlier job is never relaxed to spare a later one. The dropped job is then
    re-promised from what the final solve achieves, like any job without a bound.

    (Within a batch of jobs submitted together, which are peers, the roster's order is
    the tie-break. That is a choice among equals, not a violation of anything.)
    """
    def attempt(trial: tuple[JobSpec, ...]) -> Solution:
        return solve(instance, jobs=trial, **solve_kwargs)

    solution = attempt(specs)
    if solution.outcome in _SOLVED:
        return solution, _promised(specs, solution), []

    unbounded = tuple(replace(spec, bound=None) for spec in specs)
    if not any(spec.bound is not None for spec in specs):
        return solution, specs, []
    probe = attempt(unbounded)
    if probe.outcome not in _SOLVED:
        # Not the promises. Hand back the original attempt, whose stats describe the
        # instance the caller actually asked about.
        return solution, specs, []

    kept: list[JobSpec] = []
    relaxed: list[JobSpec] = []
    for i, spec in enumerate(specs):
        trial = (*kept, spec, *unbounded[i + 1 :])
        if spec.bound is None or attempt(trial).outcome in _SOLVED:
            kept.append(spec)
        else:
            kept.append(replace(spec, bound=None))
            relaxed.append(spec)

    final = attempt(tuple(kept))
    settled = _promised(tuple(kept), final) if final.outcome in _SOLVED else tuple(kept)
    diagnostics = [
        Diagnostic(
            errors.JOB_BOUND_RELAXED,
            f"job {spec.id!r} could no longer finish by {spec.bound}, the completion it "
            "was promised; its bound was re-derived",
            "jobs",
            severity=WARNING,
        )
        for spec in relaxed
    ]
    return final, settled, diagnostics


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
    now_value = 0
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
            stated_now = doc_path.get("now")
            now_value = stated_now if isinstance(stated_now, int) else 0
        elif isinstance(root, YMap):
            interface = yamlnode.to_plain(root.get("interface"))
            inventories = yamlnode.to_plain(root.get("inventories"))
            stated = yamlnode.to_plain(root.get("objective"))
            declared_objective = stated.get("kind") if isinstance(stated, dict) else None
            roster = yamlnode.to_plain(root.get("jobs")) if "jobs" in root else None
            had_now = "now" in root
            stated_now = yamlnode.to_plain(root.get("now"))
            now_value = stated_now if isinstance(stated_now, int) else 0

    # A document that names its jobs (§6.11) has to name *these* jobs. It is a
    # replanning input describing work done by particular workflows, and matching that
    # history against a different set would pin it onto activities that never ran it.
    # Compared as sets: the roster's order is the record of how the jobs were given,
    # and re-stating them in another order is not a different plan.
    entries = _roster_entries(roster) if roster is not None else None
    if entries is not None:
        # The single-workflow call has no job identity at all, so it matches only an
        # empty roster -- never one that names jobs. An empty roster and no roster say
        # the same thing there and are both accepted.
        #
        # A *superset* is the arrival of new jobs (§6.11): the roster names the jobs
        # already being planned, and anything beyond it is joining them now. Missing
        # the other way stays an error -- a job the document has history for cannot
        # simply be dropped, or that history would have nowhere to land.
        given_ids = {job.id for job in jobs if job.id}
        if not set(entries) <= given_ids:
            missing = sorted(set(entries) - given_ids)
            given = f"{sorted(given_ids)} were given" if given_ids else "one unnamed workflow"
            diagnostics.append(
                Diagnostic(
                    errors.JOB_ROSTER_MISMATCH,
                    f"the document plans jobs {sorted(entries)}, but {given}"
                    f" -- {missing} would be dropped",
                    "jobs",
                )
            )
            return ScheduleReport(None, None, None, diagnostics)

    # Each job's planning parameters, and the check that it is running the workflow
    # its entry was planned for. The order of these two matters: the specs carry the
    # fingerprint just computed, so comparing them against the roster is what turns a
    # swap into a diagnostic rather than a silent overwrite.
    specs = _job_specs(jobs, workflows, entries, now_value)
    if entries is not None:
        mismatched = _check_fingerprints(specs, entries)
        if mismatched:
            diagnostics += mismatched
            return ScheduleReport(None, None, None, diagnostics)

    # `interface` binds *one* workflow's boundary ports, so in a joint plan it belongs
    # to a job rather than to the document. The test is whether *this call* names jobs,
    # not whether the input document happens to list them: an initial joint plan is
    # given a document with no roster yet, and sharing one boundary across its jobs is
    # the very ambiguity being refused. One rule, so there is never a question of which
    # applies -- named jobs mean per-job, an unnamed single workflow means top-level.
    if any(job.id for job in jobs) and interface:
        diagnostics.append(
            Diagnostic(
                errors.MULTI_JOB_INTERFACE,
                "interface binds one workflow's boundary ports, so a document that "
                "lists jobs carries it per job (jobs[].interface), not at the top level",
                "interface",
            )
        )
        return ScheduleReport(None, None, None, diagnostics)

    diagnostics += _check_boundary_spots(tuple(specs))
    if _has_error(diagnostics):
        return ScheduleReport(None, None, None, diagnostics)

    # 3. Build one instance per job (boundary nodes/arcs from interface always
    # re-created, like relays), prefix each job's node paths with its id so the two
    # cannot collide, and merge. Everything downstream sees a single instance and
    # never learns that jobs exist -- which is what lets one refill candidate serve
    # activities from several jobs (`merge_instances`).
    bases = []
    for job, workflow, spec in zip(jobs, workflows, specs, strict=True):
        base, inst_diags = build_instance(
            # A named job brings its own boundary (`spec.interface`); the unnamed
            # single-workflow call has the document's, which is refused above wherever
            # jobs are named, so exactly one of the two is ever set.
            workflow, env, interface=spec.interface or interface, check_reachability=False
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

    # 4. Solve, then 5. render the plan (only when feasible). One pass unless a
    # promised bound can no longer be kept (`_solve_within_bounds`).
    named = tuple(specs) if any(job.id for job in jobs) else ()
    solution, settled, relax_diags = _solve_within_bounds(
        instance,
        named,
        {
            "fixation": fixation,
            "running_task_margin": running_task_margin,
            "max_time_seconds": max_time_seconds,
            "random_seed": random_seed,
            "objective": _objective_of(declared_objective, len(named)),
            "collect_solutions": collect_solutions,
        },
    )
    diagnostics += relax_diags
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
        jobs=settled,
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
