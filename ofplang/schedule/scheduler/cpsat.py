"""Build and solve the CP-SAT model for an instance (docs/FORMULATION.md).

Mode selection, spot/device occupancy, and transport are expressed with optional
intervals whose presence is the mode/route selector, exactly as the FORMULATION
CP-SAT notes describe. The objective is a lexicographic stage list (§4.8) of which
only `makespan` is realisable so far, so what is actually minimised is c_max.

Occupancy bookkeeping mirrors FORMULATION §6/§7: a processing activity holds its
mode's spots and devices over its run interval; a transport holds the source spot
over [e_src, b], the destination spot over [a, s_dst], and the source device,
destination device, and transporter over its body interval [a, b]. NoOverlap is
applied per spot, per device, and per transporter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ortools.sat.python import cp_model

from ofplang.schedule.core import objective as objective_stages
from ofplang.schedule.core.identifiers import parse_qualified_resource, parse_qualified_spot
from ofplang.schedule.scheduler.instance import (
    BoundaryInfo,
    Instance,
    RelayInfo,
    TransportOption,
)
from ofplang.schedule.scheduler.model import Arc, Mode, NodePath
from ofplang.schedule.scheduler.status import Fixation


@dataclass(frozen=True)
class ProcessingResult:
    activity: int
    node: NodePath
    process: str
    mode: Mode
    start: int
    end: int
    # On a replan, the reported status of a fixed activity; None when pending.
    status: str | None = None
    # Set (opaquely, by the solver) when this activity is a relay junction; drives
    # rendering (`kind: relay`). None for a normal processing activity.
    relay: RelayInfo | None = None
    # Set when this activity is a synthetic boundary node (§6.8); rendering skips it.
    boundary: BoundaryInfo | None = None


@dataclass(frozen=True)
class TransportResult:
    arc: Arc
    option: TransportOption
    start: int
    end: int
    status: str | None = None
    # A leg's chain position (§6.6); None for a single-leg transport.
    seq: int | None = None


@dataclass(frozen=True)
class RefillResult:
    """A refill in the plan (§6.9). `amounts` is what it adds, keyed by bare
    resource name -- `device` already says whose stock it is."""

    id: str
    device: str
    replenisher: str
    amounts: dict[str, int]
    start: int
    end: int
    status: str | None = None


@dataclass(frozen=True)
class Solution:
    outcome: str  # optimal | feasible | infeasible | unknown
    makespan: int | None
    processing: tuple[ProcessingResult, ...]
    transport: tuple[TransportResult, ...]
    replenishment: tuple[RefillResult, ...] = ()
    # The objective actually minimised, and what each of its stages reached. These
    # are the *effective* stages (`core.objective.effective`): a stage this
    # instance cannot tell two schedules apart by is dropped, so a plan from an
    # environment without resources reports the bare makespan it always did.
    # Defaulted so the infeasible return below stays a four-argument call.
    objective_kind: tuple[str, ...] = (objective_stages.MAKESPAN,)
    objective_values: tuple[int, ...] = ()


_STATUS = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
}


def solve(
    instance: Instance,
    *,
    fixation: Fixation | None = None,
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
    random_seed: int | None = None,
    objective: tuple[str, ...] | None = None,
) -> Solution:
    """Build and solve the model. With a `fixation` (a replan), completed/running
    activities are pinned to their reported times, mode, and route, pending ones
    are held to start at or after `now`, and a running activity's end is clamped
    up to `now + running_task_margin` so an overrunning task is never fixed to a
    finish in the past (FORMULATION §9).

    `objective` is the stage list to minimise (§4.8). The caller reads it off the
    execution document, because where a declaration lives is a question about the
    *inputs*, not about the model; falling back here to the default only keeps a
    bare `solve(instance)` meaningful.

    By default the solve is non-deterministic: CP-SAT runs a multi-worker
    portfolio that races on wall-clock time, so a fresh run may return a different
    optimal schedule (the makespan is unique, but which of the equally-optimal
    schedules comes back is not). Passing `random_seed` makes the solve
    reproducible — it fixes the seed *and* pins the search to a single worker,
    since a fixed seed alone does not defeat the inter-worker race. This is meant
    for tests that assert on a specific plan; it forgoes parallelism, and note
    that reproducibility only holds when the solve runs to completion (a solve
    truncated by `max_time_seconds` still depends on wall-clock timing)."""
    model = cp_model.CpModel()
    now = fixation.now if fixation is not None else 0
    horizon = _horizon(instance, fixation, running_task_margin)

    # Resource occupancy: interval lists to feed NoOverlap, keyed by qualified
    # spot, by device, and by transporter.
    spot_iv: dict[str, list] = {}
    device_iv: dict[str, list] = {}
    transporter_iv: dict[str, list] = {}

    def add(mapping: dict[str, list], key: str, interval) -> None:
        mapping.setdefault(key, []).append(interval)

    # Makespan variable, created up front so the output boundary node's interval
    # can end exactly at it (§8 / FORMULATION §3-bis).
    c_max = model.NewIntVar(0, horizon, "c_max")

    # --- processing activities (including the synthetic boundary nodes) ---
    starts, ends, mode_lits = [], [], []
    # ends that define the makespan (the output node's own end IS c_max, so it is excluded)
    make_ends: list = []
    for i, act in enumerate(instance.activities):
        s = model.NewIntVar(0, horizon, f"s{i}")
        e = model.NewIntVar(0, horizon, f"e{i}")
        fx = fixation.activities.get(i) if fixation is not None else None
        boundary = act.boundary
        lits = []
        for m, mode in enumerate(act.modes):
            present = model.NewBoolVar(f"x{i}_{m}")
            lits.append(present)
            # The output boundary node holds its spots until the makespan, so its
            # size is free (end pinned to c_max below). For a pending activity the
            # optional interval ties e = s + duration when this mode is chosen; for
            # a fixed activity the size is free (times pinned below) so an
            # overrunning running activity can hold its resources past its nominal
            # duration.
            if boundary is not None and boundary.kind == "output":
                size = model.NewIntVar(0, horizon, f"bsz{i}_{m}")
            else:
                size = mode.duration if fx is None else model.NewIntVar(0, horizon, f"psz{i}_{m}")
            iv = model.NewOptionalIntervalVar(s, size, e, present, f"pi{i}_{m}")
            for spot in set(mode.input_spots.values()) | set(mode.output_spots.values()):
                add(spot_iv, spot, iv)
            for device in mode.devices:
                add(device_iv, device, iv)
        model.AddExactlyOne(lits)
        if boundary is not None:
            # Boundary nodes are re-created every solve and are not fixation-managed
            # (§9): the input node sits at time 0 (a given origin, exempt from the
            # pending s >= now rule), the output node's end is the makespan.
            if boundary.kind == "input":
                model.Add(s == 0)
                model.Add(e == 0)
            else:
                model.Add(e == c_max)
        elif fx is not None:
            # Completed/running: pin mode and times (running end clamped up to
            # now + margin). The pinned mode's interval then occupies its spots
            # and devices over the actual [start, end].
            model.Add(lits[fx.mode_index] == 1)
            model.Add(s == fx.start)
            model.Add(e == _fixed_end(fx.status, fx.end, now, running_task_margin))
        elif fixation is not None:
            # Pending during a replan: cannot start before now.
            model.Add(s >= now)
        starts.append(s)
        ends.append(e)
        # The input node (end 0) is harmless in the makespan max; the output node's
        # end equals c_max, so feeding it back would be circular — exclude it.
        if boundary is None or boundary.kind != "output":
            make_ends.append(e)
        mode_lits.append(lits)

    # --- transport activities (one per arc) ---
    arc_starts, arc_ends, arc_opt_lits = [], [], []
    for r, arc in enumerate(instance.arcs):
        a = model.NewIntVar(0, horizon, f"a{r}")
        b = model.NewIntVar(0, horizon, f"b{r}")
        _s_src, e_src = starts[arc.src_activity], ends[arc.src_activity]
        s_dst = starts[arc.dst_activity]
        fr = fixation.arcs.get(r) if fixation is not None else None

        lits = []
        for k, opt in enumerate(arc.options):
            present = model.NewBoolVar(f"q{r}_{k}")
            lits.append(present)
            # Route selection must agree with the endpoint modes (§4).
            model.AddImplication(present, mode_lits[arc.src_activity][opt.src_mode_index])
            model.AddImplication(present, mode_lits[arc.dst_activity][opt.dst_mode_index])
            # Transport body [a, b]; occupies source device, destination device,
            # and the transporter. The size is the option's duration for a pending
            # transport, free for a fixed one (times pinned below).
            body_size = opt.duration if fr is None else model.NewIntVar(0, horizon, f"tbsz{r}_{k}")
            body = model.NewOptionalIntervalVar(a, body_size, b, present, f"tb{r}_{k}")
            # from_spot/to_spot are validated qualified spots, so parsing succeeds.
            src_parsed = parse_qualified_spot(opt.from_spot)
            dst_parsed = parse_qualified_spot(opt.to_spot)
            assert src_parsed is not None and dst_parsed is not None
            src_device, dst_device = src_parsed[0], dst_parsed[0]
            add(device_iv, src_device, body)
            # A move between two spots of the *same* device occupies that device
            # once, not twice. Registering the same interval twice puts it in the
            # device's non-overlap set against itself, which no positive duration
            # can satisfy -- so every such environment came out infeasible however
            # simple it was. (A same-*spot* no-op escaped that only because a
            # zero-length interval overlaps nothing.)
            if dst_device != src_device:
                add(device_iv, dst_device, body)
            # A same-spot no-op route carries no transporter (opt.transporter is
            # None), so it occupies no transporter resource.
            if opt.transporter is not None:
                add(transporter_iv, opt.transporter, body)
            # Source spot held [e_src, b]; destination spot held [a, s_dst].
            src_size = model.NewIntVar(0, horizon, f"ss{r}_{k}")
            add(
                spot_iv,
                opt.from_spot,
                model.NewOptionalIntervalVar(e_src, src_size, b, present, f"si{r}_{k}"),
            )
            dst_size = model.NewIntVar(0, horizon, f"ds{r}_{k}")
            add(
                spot_iv,
                opt.to_spot,
                model.NewOptionalIntervalVar(a, dst_size, s_dst, present, f"di{r}_{k}"),
            )
        model.AddExactlyOne(lits)
        if fr is not None:
            # Completed/running transport: pin route and times (running end
            # clamped up to now + margin).
            model.Add(lits[fr.option_index] == 1)
            model.Add(a == fr.start)
            model.Add(b == _fixed_end(fr.status, fr.end, now, running_task_margin))
        elif fixation is not None:
            # Pending during a replan: cannot start before now, even if the
            # source finished earlier.
            model.Add(a >= now)

        # Ordering (§3): transport after source ends, before destination starts.
        model.Add(a >= e_src)
        model.Add(s_dst >= b)
        # A boundary-output delivery has its successor (the output node) pinned to
        # the makespan, so the delivery must be counted in it (§8); otherwise a
        # delivery later than every real end could not fit before c_max.
        dst_boundary = instance.activities[arc.dst_activity].boundary
        if dst_boundary is not None and dst_boundary.kind == "output":
            make_ends.append(b)
        arc_starts.append(a)
        arc_ends.append(b)
        arc_opt_lits.append(lits)

    # --- precedence (covers Pure Data dependencies too) ---
    for si, di in instance.precedence:
        model.Add(starts[di] >= ends[si])

    # --- refills (FORMULATION §10) and the stocks they feed (§11) ---
    # Before the non-overlap below, not after: a refill holds the device it fills and
    # the replenisher that fills it (§4.7.1), and it registers those the same way every
    # other activity does -- by appending to `device_iv`. `AddNoOverlap` takes the list
    # it is given at the moment it is called, so anything appended afterwards is
    # unconstrained. Adding the refills first is what makes them occupy anything at all.
    refills = _add_refills(
        model, instance, fixation, mode_lits, horizon, now, device_iv, running_task_margin
    )
    _add_resources(model, instance, fixation, mode_lits, starts, refills)

    # --- resource non-overlap ---
    for intervals in spot_iv.values():
        model.AddNoOverlap(intervals)
    for intervals in device_iv.values():
        model.AddNoOverlap(intervals)
    for intervals in transporter_iv.values():
        model.AddNoOverlap(intervals)

    # --- objective ---
    # c_max is the max over real activity ends and boundary-output deliveries
    # (make_ends); the output node's own end equals c_max and is not fed back.
    if make_ends:
        model.AddMaxEquality(c_max, make_ends)
    else:
        model.Add(c_max == 0)

    # `replenishment_count` can only tell two schedules apart where a refill is
    # possible at all; `effective` drops it otherwise, which is what keeps a
    # resource-free plan reporting the bare makespan it always did.
    stages = objective_stages.effective(
        objective or objective_stages.DEFAULT,
        replenishment_possible=bool(instance.replenishments),
    )
    n_refills = sum(vars_.present for vars_ in refills.values())

    # Lexicographic stages as a single weighted objective (FORMULATION "Objective").
    # Exact because the count is bounded by the number of candidates, so one unit of
    # the earlier stage outweighs every attainable value of the later one -- and it
    # keeps the solve to one pass, on a model whose size is already what bounds solve
    # time (dev-notes/report-solver-scalability.md).
    bound = len(instance.replenishments)
    terms = {
        objective_stages.MAKESPAN: (c_max, horizon),
        objective_stages.REPLENISHMENT_COUNT: (n_refills, bound),
    }
    expression = 0
    weight = 1
    for stage in reversed(stages):
        value, stage_bound = terms[stage]
        expression = expression + weight * value
        weight *= stage_bound + 1
    model.Minimize(expression)

    solver = cp_model.CpSolver()
    if max_time_seconds is not None:
        solver.parameters.max_time_in_seconds = max_time_seconds
    if random_seed is not None:
        # Reproducible mode: a fixed seed only determines a single worker's search,
        # so also pin to one worker — otherwise the portfolio's inter-worker race
        # still varies which optimal schedule is returned.
        solver.parameters.random_seed = random_seed
        solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    outcome = _STATUS.get(status, "unknown")

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Solution(outcome, None, (), (), objective_kind=stages)

    act_fix = fixation.activities if fixation is not None else {}
    arc_fix = fixation.arcs if fixation is not None else {}
    processing = tuple(
        ProcessingResult(
            activity=i,
            node=act.node,
            process=act.process,
            mode=act.modes[_selected(solver, mode_lits[i])],
            start=solver.Value(starts[i]),
            end=solver.Value(ends[i]),
            status=act_fix[i].status if i in act_fix else None,
            relay=act.relay,
            boundary=act.boundary,
        )
        for i, act in enumerate(instance.activities)
    )
    transport = tuple(
        TransportResult(
            arc=arc.arc,
            option=arc.options[_selected(solver, arc_opt_lits[r])],
            start=solver.Value(arc_starts[r]),
            end=solver.Value(arc_ends[r]),
            status=arc_fix[r].status if r in arc_fix else None,
            seq=arc.seq,
        )
        for r, arc in enumerate(instance.arcs)
    )
    replenishment = _refill_results(
        solver, instance, fixation, refills, processing, running_task_margin
    )
    values = {
        objective_stages.MAKESPAN: solver.Value(c_max),
        # Counted from what survives normalisation, not from the solver's own count:
        # topping up an earlier refill can leave a later one adding nothing, and such
        # a refill is dropped rather than reported.
        objective_stages.REPLENISHMENT_COUNT: sum(
            1 for r in replenishment if r.status is None
        ),
    }
    return Solution(
        outcome,
        solver.Value(c_max),
        processing,
        transport,
        replenishment,
        objective_kind=stages,
        objective_values=tuple(values[stage] for stage in stages),
    )


def _refill_results(
    solver,
    instance: Instance,
    fixation: Fixation | None,
    refills,
    processing,
    running_task_margin: int = 0,
) -> tuple[RefillResult, ...]:
    """The refills of a solved model, with their amounts normalised to a fill.

    §4.7.1 says a planned refill fills each resource to `capacity`, but the reservoir
    offers no level *variable* to write that against, so the amounts are left free in
    the model and settled here. With times and selections known, replaying a stock's
    events gives each refill exactly the room it has.

    Where a refill's end meets a draw's start, it fills to the level **that instant
    leaves behind**. The refill goes in first, as §4.7 says, but the level checked
    against `capacity` is the one after the draw as well: the figure in between is
    not a state anything observes, and it is not what the reservoir this is settling
    -- or `plancheck`, which replays the finished plan -- is written against either.
    So "the level immediately before" is not a thing to fill against where the two
    meet. A refill landing on a stock that is momentarily full has to add the room
    the simultaneous draw makes; read the other way it appears to have no room, is
    dropped as adding nothing, and the plan fails its own inventory check.

    Sound and free: raising an amount only raises later levels, so the lower bound
    slackens while the upper is met with equality. Times and selections are
    untouched, so this is the same optimum, reported determinately instead of
    arbitrarily among the many amount assignments the constraints admit.

    A refill left adding nothing -- an earlier one having already filled the stock --
    is dropped rather than reported: it would hold two machines and change no level.

    Started refills keep their **amounts**: those are what was reported, not what a
    fill would have been (§6.9) -- a refill that only half filled a bottle is history,
    not a figure to correct. A *running* one's `end` is another matter, and is reported
    as the clamped one the model was built on (`now + margin` at the earliest, §9). The
    two differ because the amounts are a fact the reporter knows and the end is one it
    does not: `running` means the actual end is still unknown, so the plan reports the
    same estimate it scheduled around. Reporting the raw one instead would put a
    document out saying a machine was free at a time the schedule assumed it busy.
    """
    fixed = fixation.replenishments if fixation is not None else {}
    now = fixation.now if fixation is not None else 0
    results = [
        RefillResult(
            identifier,
            fix.device,
            fix.replenisher,
            dict(fix.amounts),
            fix.start,
            _fixed_end(fix.status, fix.end, now, running_task_margin),
            fix.status,
        )
        for identifier, fix in sorted(fixed.items())
    ]

    selected = [c for c in instance.replenishments if solver.Value(refills[c.id].present)]
    if not selected:
        return tuple(results)

    # Events per stock, in the order they occur. At equal times a refill lands before
    # a draw (§4.7), so it feeds work starting exactly when it ends.
    events: dict[tuple[str, str], list[tuple[int, int, str | None]]] = {}
    for p in processing:
        if p.status is not None:  # already spent, and inside the levels at `now`
            continue
        for qualified, amount in p.mode.consumption.items():
            parsed = parse_qualified_resource(qualified)
            if parsed is not None:
                events.setdefault(parsed, []).append((p.start, -amount, None))
    for fix in fixed.values():
        if fix.status != "running":
            continue
        for resource, amount in fix.amounts.items():
            events.setdefault((fix.device, resource), []).append((fix.end, amount, None))
    for candidate in selected:
        end = solver.Value(refills[candidate.id].end)
        for resource in candidate.resources:
            events.setdefault((candidate.device, resource), []).append((end, 0, candidate.id))

    amounts: dict[str, dict[str, int]] = {c.id: {} for c in selected}
    for key, entries in events.items():
        level = (fixation.levels.get(key, 0) if fixation is not None else 0)
        capacity = _capacity_of(instance, key[0], key[1])
        # Group by instant. The refill still goes in before the draw (§4.7); what
        # the grouping decides is *where the level is read* -- after the whole
        # instant, not between the two changes. `plancheck` replays a finished plan
        # the same way ("applies every change at one time point before looking at
        # the level"), and the reservoir this is settling amounts for is written
        # against the same point. Filling to capacity before the simultaneous draw
        # is subtracted instead makes a refill landing on a full stock look as if
        # there were no room for it, and the fill it was chosen to provide is then
        # rounded away to nothing.
        at_time: dict[int, list[tuple[int, str | None]]] = {}
        for time, change, refill_id in entries:
            at_time.setdefault(time, []).append((change, refill_id))
        for time in sorted(at_time):
            group = at_time[time]
            draws = sum(change for change, rid in group if rid is None)
            # A device is exclusive and a refill holds it, so at most one refill of
            # a given stock can land at any one instant; the loop is written for a
            # list only so that a second one would take what the first left rather
            # than double-count.
            for _change, refill_id in group:
                if refill_id is None:
                    continue
                # Room to fill: what it takes to leave this instant at `capacity`,
                # never more than the stock can hold in one visit (the model bounds
                # each amount by `capacity`, so a larger figure here would be one
                # the solver never proved).
                added = min(capacity - level - draws, capacity)
                if added > 0:
                    amounts[refill_id][key[1]] = added
                    level += added
            level += draws

    for candidate in selected:
        if not amounts[candidate.id]:
            continue  # adds nothing after the fill above; not worth two machines
        vars_ = refills[candidate.id]
        chosen = next(
            (o.replenisher for o in candidate.options
             if solver.Value(vars_.end) - solver.Value(vars_.start) == o.duration),
            candidate.options[0].replenisher,
        )
        results.append(
            RefillResult(
                candidate.id,
                candidate.device,
                chosen,
                amounts[candidate.id],
                solver.Value(vars_.start),
                solver.Value(vars_.end),
            )
        )
    return tuple(results)


@dataclass(frozen=True)
class _RefillVars:
    """The solver variables of one refill candidate (FORMULATION §10)."""

    # Solver objects, left unannotated like every other CP-SAT handle in this
    # module: ortools ships no type information (pyproject skips it for mypy).
    present: Any  # BoolVar: this candidate runs at all
    start: Any
    end: Any
    amounts: dict[str, Any]  # resource -> IntVar, how much it adds


def _add_refills(
    model,
    instance: Instance,
    fixation: Fixation | None,
    mode_lits,
    horizon: int,
    now: int,
    device_iv: dict[str, list],
    running_task_margin: int = 0,
) -> dict[str, _RefillVars]:
    """Build each refill candidate: whether it runs, when, on which replenisher, and
    how much it adds (FORMULATION §10).

    A candidate is *optional* -- that is the one structural difference from a
    transport's route, which must be served by exactly one option.

    It occupies two machines (§4.7.1). The device it refills is held whichever
    replenisher does the work, so that is one interval whose presence is the
    candidate; the replenisher is held only by the one chosen, so those are an
    interval each. The device's interval needs a Literal for its presence, and so
    does the reservoir event, which is why the candidate carries a real BoolVar
    rather than just the sum of its per-replenisher literals.
    """
    refills: dict[str, _RefillVars] = {}
    fixed_refills = fixation.replenishments if fixation is not None else {}

    for candidate in instance.replenishments:
        present = model.NewBoolVar(f"refill_{candidate.id}")
        start = model.NewIntVar(now, horizon, f"refill_start_{candidate.id}")
        end = model.NewIntVar(now, horizon, f"refill_end_{candidate.id}")

        option_lits = []
        for option in candidate.options:
            chosen = model.NewBoolVar(f"refill_{candidate.id}_{option.replenisher}")
            option_lits.append(chosen)
            model.Add(end == start + option.duration).OnlyEnforceIf(chosen)
            device_iv.setdefault(option.replenisher, []).append(
                model.NewOptionalIntervalVar(
                    start, option.duration, end, chosen,
                    f"riv_{candidate.id}_{option.replenisher}",
                )
            )
        model.Add(sum(option_lits) == present)

        # The refilled device is held for the whole visit, whoever performs it. The
        # size is a variable because which replenisher was chosen decides it, and an
        # interval's size must be a variable or a constant, not an expression.
        size = model.NewIntVar(0, horizon, f"rdur_{candidate.id}")
        model.Add(size == end - start)
        device_iv.setdefault(candidate.device, []).append(
            model.NewOptionalIntervalVar(start, size, end, present, f"rdev_{candidate.id}")
        )

        # Only where the activity this candidate was generated from actually runs on
        # the device. If it runs elsewhere it draws nothing there, and every other
        # activity that does draw carries its own candidate -- so this prunes without
        # losing a schedule.
        gate = [
            mode_lits[candidate.origin][m]
            for m, mode in enumerate(instance.activities[candidate.origin].modes)
            if candidate.device in mode.devices
        ]
        model.Add(present <= sum(gate) if gate else present == 0)

        amounts: dict[str, Any] = {}
        for resource in candidate.resources:
            capacity = _capacity_of(instance, candidate.device, resource)
            amount = model.NewIntVar(0, capacity, f"refill_{candidate.id}_{resource}")
            model.Add(amount <= capacity * present)
            amounts[resource] = amount
        # A selected refill must add something; a refill that adds nothing would hold
        # two machines for no reason.
        if amounts:
            model.Add(sum(amounts.values()) >= present)

        refills[candidate.id] = _RefillVars(present, start, end, amounts)

    # A running refill still holds its two machines while it finishes -- and, like any
    # running activity, until at least `now + margin`. Pinning it to the end that was
    # reported would let the next user of the device start the instant the report says
    # it finished, which is exactly what a running report cannot promise: the reporter
    # does not know the actual end yet, only the plan's estimate (FORMULATION §9).
    for identifier, fix in fixed_refills.items():
        if fix.status != "running":
            continue
        end = _fixed_end(fix.status, fix.end, now, running_task_margin)
        interval = model.NewIntervalVar(
            fix.start, max(end - fix.start, 0), end, f"rfix_{identifier}"
        )
        device_iv.setdefault(fix.device, []).append(interval)
        if fix.replenisher:
            device_iv.setdefault(fix.replenisher, []).append(interval)

    return refills


def _add_resources(
    model,
    instance: Instance,
    fixation: Fixation | None,
    mode_lits,
    starts,
    refills: dict[str, _RefillVars],
) -> None:
    """Constrain each stock by what is left and what may still be added.

    FORMULATION §11 states the rule as a level held within `[0, capacity]` at every
    event, which is a reservoir. **A stock nothing can refill does not need one.**
    Its level only ever falls, so the whole trajectory is bounded by its end:
    "level >= 0 at every draw" is exactly "everything still to be drawn fits in what
    is left at `now`" -- one linear inequality, no event times, no ordering. That is
    not a special case worth skipping: an environment with stocks and no replenisher
    is a supported configuration (§5.6), the one where an operator tops up outside
    the schedule, and it should not pay for machinery it cannot use.

    So the reservoir is built only for the stocks a refill can actually reach.

    Fixed activities contribute nothing either way: their draw is already spent and
    folded into the levels at `now` (§4.7.2).
    """
    levels = fixation.levels if fixation is not None else {}
    if not levels:
        return
    fixed = fixation.activities if fixation is not None else {}
    fixed_refills = fixation.replenishments if fixation is not None else {}

    # Draws still to come, per stock: the amount and the literal that selects it.
    draws: dict[tuple[str, str], list[tuple[int, Any, Any]]] = {}
    for i, act in enumerate(instance.activities):
        if i in fixed:
            continue
        for m, mode in enumerate(act.modes):
            for qualified, amount in mode.consumption.items():
                parsed = parse_qualified_resource(qualified)
                if parsed is None:  # pragma: no cover - the env validator rejects it
                    continue
                draws.setdefault(parsed, []).append((amount, mode_lits[i][m], starts[i]))

    # Which stocks a refill can reach: candidates the solver may choose, plus the
    # running ones whose increase is already fixed.
    refillable: set[tuple[str, str]] = set()
    for candidate in instance.replenishments:
        for resource in candidate.resources:
            refillable.add((candidate.device, resource))
    for fix in fixed_refills.values():
        if fix.status == "running":
            for resource in fix.amounts:
                refillable.add((fix.device, resource))

    for key in sorted(set(draws) | refillable):
        device, resource = key
        capacity = _capacity_of(instance, device, resource)
        if key not in refillable:
            # Monotone: one inequality is exactly equivalent to the reservoir.
            terms = [amount * lit for amount, lit, _ in draws.get(key, ())]
            if terms:
                model.Add(sum(terms) <= levels.get(key, 0))
            continue

        times: list = []
        changes: list = []
        actives: list = []

        # The starting level enters as a fixed change at time 0: the reservoir's own
        # level always begins at 0, and its documentation names this as the way to
        # state an initial state.
        times.append(0)
        changes.append(levels.get(key, 0))
        actives.append(1)

        for amount, lit, start in draws.get(key, ()):
            times.append(start)
            changes.append(-amount)
            actives.append(lit)

        for candidate in instance.replenishments:
            if candidate.device != device or resource not in candidate.resources:
                continue
            vars_ = refills[candidate.id]
            times.append(vars_.end)
            changes.append(vars_.amounts[resource])
            actives.append(vars_.present)

        for fix in fixed_refills.values():
            # A completed refill is already inside `levels`; a running one has not
            # landed and is a fixed increase at its (fixed) end.
            if fix.status != "running" or fix.device != device:
                continue
            amount = fix.amounts.get(resource, 0)
            if amount:
                times.append(fix.end)
                changes.append(amount)
                actives.append(1)

        model.AddReservoirConstraintWithActive(times, changes, actives, 0, capacity)


def _capacity_of(instance: Instance, device: str, resource: str) -> int:
    entry = instance.env.devices.get(device)
    return (entry.resources.get(resource, 0) if entry is not None else 0) or 0


def _fixed_end(status: str, reported_end: int, now: int, margin: int) -> int:
    """The pinned end of a fixed activity. A completed activity keeps its actual
    end; a running one is clamped up to now + margin so an overrun is never fixed
    to a finish in the past (FORMULATION §9: e_i = max(ê_i, now + m))."""
    if status == "running":
        return max(reported_end, now + margin)
    return reported_end


def _selected(solver: cp_model.CpSolver, lits) -> int:
    """Index of the one true presence literal in a selection group."""
    for i, lit in enumerate(lits):
        if solver.Value(lit) == 1:
            return i
    return 0  # pragma: no cover - AddExactlyOne guarantees one true literal


def _horizon(instance: Instance, fixation: Fixation | None, margin: int) -> int:
    """A safe upper bound on any end time: the longest each activity/transport
    could take, summed (a fully serial schedule). On a replan the fixed part
    may already sit past that bound, so also clear `now`, every reported end,
    and the running-clamp margin."""
    total = 0
    for act in instance.activities:
        total += max((m.duration for m in act.modes), default=0)
    for arc in instance.arcs:
        total += max((o.duration for o in arc.options), default=0)
    # Refills are work too, and a bound that left them out would cut off schedules
    # that only fit once a stock is topped up -- turning feasible instances
    # infeasible. Adding the longest each could take keeps this the fully-serial
    # bound it already was, now serial over the refills as well.
    for candidate in instance.replenishments:
        total += max((o.duration for o in candidate.options), default=0)
    if fixation is not None:
        fixed_ends = (
            [f.end for f in fixation.activities.values()]
            + [f.end for f in fixation.arcs.values()]
            + [f.end for f in fixation.replenishments.values()]
        )
        total += fixation.now + max(fixed_ends, default=0) + margin
    return total + 1
