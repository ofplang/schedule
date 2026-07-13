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
from ofplang.schedule.scheduler.instance import ArcInstance, Instance, TransportOption
from ofplang.schedule.scheduler.model import Arc, Mode, NodePath


@dataclass(frozen=True)
class ProcessingResult:
    activity: int
    node: NodePath
    process: str
    mode: Mode
    start: int
    end: int


@dataclass(frozen=True)
class TransportResult:
    arc: Arc
    option: TransportOption
    start: int
    end: int


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


def solve(instance: Instance, *, max_time_seconds: float | None = None) -> Solution:
    model = cp_model.CpModel()
    horizon = _horizon(instance)

    # Resource occupancy: interval lists to feed NoOverlap, keyed by qualified
    # spot, by device, and by transporter.
    spot_iv: dict[str, list] = {}
    device_iv: dict[str, list] = {}
    transporter_iv: dict[str, list] = {}

    def add(mapping: dict[str, list], key: str, interval) -> None:
        mapping.setdefault(key, []).append(interval)

    # --- processing activities ---
    starts, ends, mode_lits = [], [], []
    for i, act in enumerate(instance.activities):
        s = model.NewIntVar(0, horizon, f"s{i}")
        e = model.NewIntVar(0, horizon, f"e{i}")
        lits = []
        for m, mode in enumerate(act.modes):
            present = model.NewBoolVar(f"x{i}_{m}")
            lits.append(present)
            # Optional interval ties e = s + duration when this mode is chosen.
            iv = model.NewOptionalIntervalVar(s, mode.duration, e, present, f"pi{i}_{m}")
            for spot in set(mode.input_spots.values()) | set(mode.output_spots.values()):
                add(spot_iv, spot, iv)
            for device in mode.devices:
                add(device_iv, device, iv)
        model.AddExactlyOne(lits)
        starts.append(s)
        ends.append(e)
        mode_lits.append(lits)

    # --- transport activities (one per arc) ---
    arc_starts, arc_ends, arc_opt_lits = [], [], []
    for r, arc in enumerate(instance.arcs):
        a = model.NewIntVar(0, horizon, f"a{r}")
        b = model.NewIntVar(0, horizon, f"b{r}")
        s_src, e_src = starts[arc.src_activity], ends[arc.src_activity]
        s_dst = starts[arc.dst_activity]

        lits = []
        for k, opt in enumerate(arc.options):
            present = model.NewBoolVar(f"q{r}_{k}")
            lits.append(present)
            # Route selection must agree with the endpoint modes (§4).
            model.AddImplication(present, mode_lits[arc.src_activity][opt.src_mode_index])
            model.AddImplication(present, mode_lits[arc.dst_activity][opt.dst_mode_index])
            # Transport body [a, b] with the option's duration; occupies source
            # device, destination device, and the transporter.
            body = model.NewOptionalIntervalVar(a, opt.duration, b, present, f"tb{r}_{k}")
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

        # Ordering (§3): transport after source ends, before destination starts.
        model.Add(a >= e_src)
        model.Add(s_dst >= b)
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
    c_max = model.NewIntVar(0, horizon, "c_max")
    if ends:
        model.AddMaxEquality(c_max, ends)
    else:
        model.Add(c_max == 0)
    model.Minimize(c_max)

    solver = cp_model.CpSolver()
    if max_time_seconds is not None:
        solver.parameters.max_time_in_seconds = max_time_seconds
    status = solver.Solve(model)
    outcome = _STATUS.get(status, "unknown")

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Solution(outcome, None, (), ())

    processing = tuple(
        ProcessingResult(
            activity=i,
            node=act.node,
            process=act.process,
            mode=act.modes[_selected(solver, mode_lits[i])],
            start=solver.Value(starts[i]),
            end=solver.Value(ends[i]),
        )
        for i, act in enumerate(instance.activities)
    )
    transport = tuple(
        TransportResult(
            arc=arc.arc,
            option=arc.options[_selected(solver, arc_opt_lits[r])],
            start=solver.Value(arc_starts[r]),
            end=solver.Value(arc_ends[r]),
        )
        for r, arc in enumerate(instance.arcs)
    )
    return Solution(outcome, solver.Value(c_max), processing, transport)


def _selected(solver: cp_model.CpSolver, lits) -> int:
    """Index of the one true presence literal in a selection group."""
    for i, lit in enumerate(lits):
        if solver.Value(lit) == 1:
            return i
    return 0  # pragma: no cover - AddExactlyOne guarantees one true literal


def _horizon(instance: Instance) -> int:
    """A safe upper bound on any end time: the longest each activity/transport
    could take, summed (a fully serial schedule)."""
    total = 0
    for act in instance.activities:
        total += max((m.duration for m in act.modes), default=0)
    for arc in instance.arcs:
        total += max((o.duration for o in arc.options), default=0)
    return total + 1
