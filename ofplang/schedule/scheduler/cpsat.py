"""Build and solve the CP-SAT model for an instance (docs/FORMULATION.md).

Mode selection, spot/device occupancy, and transport are expressed with optional
intervals whose presence is the mode/route selector, exactly as the FORMULATION
CP-SAT notes describe. Only makespan is minimised.

Occupancy bookkeeping mirrors FORMULATION §6/§7: a processing activity holds its
mode's spots and devices over its run interval; a transport holds the source spot
over [e_src, b], the destination spot over [a, s_dst], and the source device,
destination device, and transporter over its body interval [a, b]. NoOverlap is
applied per spot, per device, and per transporter.
"""

from __future__ import annotations

from dataclasses import dataclass

from ortools.sat.python import cp_model

from ofplang.schedule.core.identifiers import parse_qualified_spot
from ofplang.schedule.scheduler.instance import ArcInstance, BoundaryInfo, Instance, RelayInfo, TransportOption
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
class Solution:
    outcome: str  # optimal | feasible | infeasible | unknown
    makespan: int | None
    processing: tuple[ProcessingResult, ...]
    transport: tuple[TransportResult, ...]


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
) -> Solution:
    """Build and solve the model. With a `fixation` (a replan), completed/running
    activities are pinned to their reported times, mode, and route, pending ones
    are held to start at or after `now`, and a running activity's end is clamped
    up to `now + running_task_margin` so an overrunning task is never fixed to a
    finish in the past (FORMULATION §9).

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
    make_ends: list = []  # ends that define the makespan (the output node's own end IS c_max, so it is excluded)
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
        s_src, e_src = starts[arc.src_activity], ends[arc.src_activity]
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
            src_device = parse_qualified_spot(opt.from_spot)[0]
            dst_device = parse_qualified_spot(opt.to_spot)[0]
            add(device_iv, src_device, body)
            add(device_iv, dst_device, body)
            add(transporter_iv, opt.transporter, body)
            # Source spot held [e_src, b]; destination spot held [a, s_dst].
            src_size = model.NewIntVar(0, horizon, f"ss{r}_{k}")
            add(spot_iv, opt.from_spot, model.NewOptionalIntervalVar(e_src, src_size, b, present, f"si{r}_{k}"))
            dst_size = model.NewIntVar(0, horizon, f"ds{r}_{k}")
            add(spot_iv, opt.to_spot, model.NewOptionalIntervalVar(a, dst_size, s_dst, present, f"di{r}_{k}"))
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

    # --- resource non-overlap ---
    for intervals in spot_iv.values():
        model.AddNoOverlap(intervals)
    for intervals in device_iv.values():
        model.AddNoOverlap(intervals)
    for intervals in transporter_iv.values():
        model.AddNoOverlap(intervals)

    # --- objective: minimise makespan ---
    # c_max is the max over real activity ends and boundary-output deliveries
    # (make_ends); the output node's own end equals c_max and is not fed back.
    if make_ends:
        model.AddMaxEquality(c_max, make_ends)
    else:
        model.Add(c_max == 0)
    model.Minimize(c_max)

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
        return Solution(outcome, None, (), ())

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
    return Solution(outcome, solver.Value(c_max), processing, transport)


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
    could take, summed (a fully serial schedule). On a replan the fixed part may
    already sit past that bound, so also clear `now`, every reported end, and the
    running-clamp margin."""
    total = 0
    for act in instance.activities:
        total += max((m.duration for m in act.modes), default=0)
    for arc in instance.arcs:
        total += max((o.duration for o in arc.options), default=0)
    if fixation is not None:
        fixed_ends = [f.end for f in fixation.activities.values()] + [f.end for f in fixation.arcs.values()]
        total += fixation.now + max(fixed_ends, default=0) + margin
    return total + 1
