"""Replanning normalization: turn a workflow instance + an execution status into
the augmented instance the solver runs, plus the fixation of the executed part.

A replan input reports what has happened (SPEC §7). Two things make it more than
a flat set of fixed activities:

- **Fixed parts are historical facts** (SPEC §9.3): a `completed` / `running`
  activity or transport is pinned from its *reported* assignment — a processing's
  echo (mode/spots/devices), a transport's route (from/to spot + transporter) —
  and is **not** re-validated against the current environment. Only pending work
  is resolved and optimised against the current env, so a device can be taken out
  of the env (its mode removed) between replans without invalidating the history
  that used it.

- **Re-routing needs relays** (SPEC §4.5 / §6.4.1). When a transport has already
  delivered (or committed to deliver) an Object to a spot but its destination
  processing is still pending — e.g. that device just became unavailable — the
  Object must be moved on from where it landed. The move is modelled as a chain
  of transport legs through **relays** (instantaneous junctions at the committed
  spots); the destination's mode stays free, and a re-transport leg carries the
  Object from the last committed spot to wherever the chosen mode needs it (a
  zero-distance hop if it stays put).

The chain is rebuilt from the **committed legs** alone (the started transports of
an arc, ordered by `seq`): a relay is derived at each committed leg's arrival
spot, and — when the destination is still pending — a pending re-transport leg is
appended from the last committed spot. Relays and the pending leg are regenerated
every solve, so `pending` / relay entries in the input are ignored; only the
committed legs are carried (matched by `arc` + `seq`). This reconstructs the first
re-route, a fed-back plan, a chain of re-routes, and a spot revisit uniformly.

Relays are ordinary `ActivityInstance`s (a single zero-duration, device-less,
single-spot mode), and legs are ordinary `ArcInstance`s, so the solver
(`cpsat.solve`) treats them exactly like any activity/transport — no relay logic
lives there. All relay awareness is here (construction) and in `plan` (render).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.core.identifiers import format_node_path
from ofplang.schedule.core.yamlnode import YMap, YNode, YScalar, YSeq
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    ArcInstance,
    Instance,
    RelayInfo,
    _transport_options,
)
from ofplang.schedule.scheduler.model import Arc, Endpoint, Mode, NodePath
from ofplang.schedule.scheduler.status import (
    ActivityFixation,
    ArcFixation,
    Fixation,
    _arc_key,
    _node_path,
    _placements,
    _status_of,
    _text,
    _times,
    _to_plain,
)
from ofplang.schedule.validation import errors

_STARTED = ("completed", "running")


@dataclass(frozen=True)
class _Leg:
    """A committed (started) transport leg read from the status input."""

    seq: int
    status: str
    start: int
    end: int
    from_spot: str
    to_spot: str
    transporter: str


def normalize(base: Instance, root: YNode | None, env) -> tuple[Instance | None, Fixation | None, Diagnostics]:
    """Build the augmented instance and fixation from `base` (the workflow
    instance, built with `check_reachability=False`) and the status `root`."""
    diags = Diagnostics()
    # `root` is the execution document, or None for an initial plan (no document).
    # An initial plan is the degenerate case of a replan with empty history and
    # now = 0 (SPEC §6.1), so the same machinery handles both. `now` is an ordinary
    # parameter ("schedule the remaining work at or after now"), independent of
    # history: it may be set with no started activities (re-optimise the future),
    # but started activities without a `now` are an error (they cannot be pinned
    # relative to an absent reference time).
    if root is not None and not isinstance(root, YMap):
        diags.error(errors.WRONG_TYPE, "execution document must be a mapping", "")
        return None, None, diags

    now_node = root.get("now") if isinstance(root, YMap) else None
    has_now = isinstance(now_node, YScalar) and now_node.is_int
    if not has_now and isinstance(root, YMap) and _has_started_activities(root):
        diags.error(errors.STATUS_MISSING_NOW, "a document with started activities must set now", "now", at=root)
        return None, None, diags
    now = now_node.value if has_now else 0

    node_index = {act.node: i for i, act in enumerate(base.activities)}
    arc_keys = {_arc_key_of(a.arc) for a in base.arcs}
    if isinstance(root, YMap):
        fixed_proc, legs_by_arc = _read_status(root, node_index, arc_keys, now, diags)
    else:
        fixed_proc, legs_by_arc = {}, {}
    if _has_error(diags):
        return None, None, diags

    # 1. Augmented activities: fixed processing frozen from its reported echo,
    #    pending processing kept with the environment's modes.
    activities: list[ActivityInstance] = []
    act_fix: dict[int, ActivityFixation] = {}
    for i, act in enumerate(base.activities):
        fp = fixed_proc.get(act.node)
        if fp is None:
            activities.append(act)  # pending: environment modes
            continue
        mode = _frozen_processing_mode(fp.entry, act.process, env, diags)
        if mode is None:
            continue
        activities.append(ActivityInstance(act.node, act.process, (mode,)))
        act_fix[i] = ActivityFixation(fp.status, fp.start, fp.end, 0)
    if _has_error(diags):
        return None, None, diags

    # 2. Per workflow arc, rebuild its transport chain from the committed legs,
    #    deriving relays and (when the destination is pending) a re-transport.
    arcs: list[ArcInstance] = []
    arc_fix: dict[int, ArcFixation] = {}
    for arc_inst in base.arcs:
        key = _arc_key_of(arc_inst.arc)
        _build_chain(
            arc_inst, legs_by_arc.get(key, []), fixed_proc, node_index,
            now, env, activities, act_fix, arcs, arc_fix, diags,
        )
    if _has_error(diags):
        return None, None, diags

    instance = Instance(env, base.time_unit, tuple(activities), tuple(arcs), base.precedence)
    placements = _placements(root) if isinstance(root, YMap) else []
    fixation = Fixation(now, act_fix, arc_fix, placements)
    return instance, fixation, diags


def _has_started_activities(root: YMap) -> bool:
    """Whether the document carries any `completed` / `running` activity — the
    history that requires a `now` to pin it against."""
    activities = root.get("activities")
    if not isinstance(activities, YSeq):
        return False
    return any(isinstance(item, YMap) and _status_of(item) in _STARTED for item in activities.items)


# --------------------------------------------------------------------------
# Reading the status input.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _FixedProc:
    status: str
    start: int
    end: int
    entry: YMap


def _read_status(root, node_index, arc_keys, now, diags):
    """Collect fixed processing (by node) and committed transport legs (by arc)
    from the status; relays and pending entries are ignored (regenerated)."""
    fixed_proc: dict[NodePath, _FixedProc] = {}
    legs_by_arc: dict[tuple, list[_Leg]] = defaultdict(list)

    activities_node = root.get("activities")
    items = activities_node.items if isinstance(activities_node, YSeq) else []
    seen_arc_seq: set[tuple] = set()
    for i, item in enumerate(items):
        if not isinstance(item, YMap):
            continue
        status = _status_of(item)
        if status not in _STARTED:
            continue  # pending / relay / status-less: regenerated from committed legs
        base = f"activities[{i}]"
        kind = _text(item.get("kind"))
        start, end = _times(item)
        if kind == "processing":
            path = _node_path(item.get("node"))
            if path not in node_index:
                diags.error(errors.STATUS_NODE_UNKNOWN, f"status references a processing node not in the workflow: {format_node_path(path)}", f"{base}.node", at=item.get("node") or item)
                continue
            if path in fixed_proc:
                diags.error(errors.STATUS_DUPLICATE, f"processing node {format_node_path(path)} is fixed more than once", base, at=item)
                continue
            _check_times(status, start, end, now, item, base, diags)
            fixed_proc[path] = _FixedProc(status, start, end, item)
        elif kind == "transport":
            key = _arc_key(item.get("arc"))
            if key is None:
                continue
            if key not in arc_keys:
                diags.error(errors.STATUS_ARC_UNKNOWN, "status references a transport arc not in the workflow", f"{base}.arc", at=item.get("arc") or item)
                continue
            seq = _seq_of(item)
            if (key, seq) in seen_arc_seq:
                diags.error(errors.STATUS_DUPLICATE, "transport leg is fixed more than once (same arc + seq)", base, at=item)
                continue
            seen_arc_seq.add((key, seq))
            _check_times(status, start, end, now, item, base, diags)
            legs_by_arc[key].append(_Leg(seq, status, start, end, _text(item.get("from_spot")), _text(item.get("to_spot")), _text(item.get("transporter"))))
    return fixed_proc, legs_by_arc


# --------------------------------------------------------------------------
# Building one arc's chain.
# --------------------------------------------------------------------------


def _build_chain(arc_inst, legs, fixed_proc, node_index, now, env, activities, act_fix, arcs, arc_fix, diags):
    logical = arc_inst.arc
    src_i, dst_i = arc_inst.src_activity, arc_inst.dst_activity
    dst_fixed = _node_of(activities, dst_i) in fixed_proc

    legs = sorted(legs, key=lambda leg: leg.seq)

    if not legs:
        # No committed leg: a single pending transport, resolved against the
        # (possibly frozen) endpoints. Reachability of pending legs is checked
        # by the caller after normalization.
        options = _transport_options(activities[src_i], logical.src.port, activities[dst_i], logical.dst.port, env)
        arcs.append(ArcInstance(logical, src_i, dst_i, tuple(options)))
        return

    # A committed leg means the source transport started, so the source processing
    # must be completed — unless the source is the input boundary node (SPEC §6.8),
    # which is the workflow's origin (the entry Object is present from time 0), not a
    # processing that runs.
    if activities[src_i].boundary is None and _node_of(activities, src_i) not in fixed_proc:
        diags.error(errors.BROKEN_TRANSPORT_CHAIN, f"a started transport leaves {format_node_path(logical.src.node)} but that activity is not completed", "", at=None)
        return

    prev_i = src_i
    prev_spot = None  # the spot the previous leg delivered to (None before leg 0)
    for k, leg in enumerate(legs):
        is_last = k == len(legs) - 1
        # Continuity: each leg departs from where the previous one arrived.
        if prev_spot is not None and leg.from_spot != prev_spot:
            diags.error(errors.BROKEN_TRANSPORT_CHAIN, f"transport leg seq {leg.seq} departs {leg.from_spot!r} but the previous leg arrived at {prev_spot!r}", "", at=None)
            return

        if is_last and dst_fixed:
            # The final committed leg delivers straight to the (fixed) successor.
            dst = dst_i
        else:
            # Derive a relay at this leg's arrival spot; it continues the chain.
            dst = _append_relay(activities, act_fix, logical, leg, now)

        option = _frozen_leg_option(leg)
        arc_index = len(arcs)
        arcs.append(ArcInstance(logical, prev_i, dst, (option,), seq=leg.seq))
        arc_fix[arc_index] = ArcFixation(leg.status, leg.start, leg.end, 0)
        prev_i, prev_spot = dst, leg.to_spot

    # After the committed legs: if the destination is still pending, add a
    # pending re-transport from the last committed spot to the successor.
    if not dst_fixed:
        options = _transport_options(activities[prev_i], "out", activities[dst_i], logical.dst.port, env)
        arcs.append(ArcInstance(logical, prev_i, dst_i, tuple(options), seq=legs[-1].seq + 2))


def _append_relay(activities, act_fix, logical, leg, now) -> int:
    """Append a relay activity at `leg`'s arrival spot and return its index. A
    relay whose delivering leg has completed is itself fixed (its arrival is a
    fact); one fed by a running leg is pending (the Object is still on its way)."""
    idx = len(activities)
    mode = Mode(id="relay", devices=(), duration=0, input_spots={"in": leg.to_spot}, output_spots={"out": leg.to_spot})
    activities.append(ActivityInstance((), "", (mode,), relay=RelayInfo(logical, leg.seq + 1, leg.to_spot)))
    if leg.status == "completed":
        act_fix[idx] = ActivityFixation("completed", leg.end, leg.end, 0)
    return idx


def _frozen_leg_option(leg: _Leg):
    from ofplang.schedule.scheduler.instance import TransportOption

    return TransportOption(0, 0, leg.transporter, leg.from_spot, leg.to_spot, max(0, leg.end - leg.start))


def _frozen_processing_mode(entry: YMap, process: str, env, diags) -> Mode | None:
    """A fixed processing activity's occupancy comes from its reported echo
    (input_spots / output_spots / devices), falling back to the environment's
    mode of the reported id, and erroring only if neither resolves (SPEC §9.3)."""
    mode_id = _text(entry.get("mode"))
    inp = _to_plain(entry.get("input_spots"))
    out = _to_plain(entry.get("output_spots"))
    devs = _to_plain(entry.get("devices"))
    if inp or out or devs:
        return Mode(mode_id, tuple(devs or []), 0, dict(inp or {}), dict(out or {}))
    capability = env.processes.get(process)
    if capability is not None:
        for mode in capability.modes:
            if mode.id == mode_id:
                return mode
    diags.error(errors.STATUS_MODE_UNKNOWN, f"cannot pin fixed activity: mode {mode_id!r} has no echo and process {process!r} does not offer it", "", at=entry)
    return None


# --------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------


def _arc_key_of(arc: Arc) -> tuple:
    return (arc.src.node, arc.src.port, arc.dst.node, arc.dst.port)


def _node_of(activities, i: int) -> NodePath:
    return activities[i].node


def _seq_of(item: YMap) -> int:
    node = item.get("seq")
    return node.value if isinstance(node, YScalar) and node.is_int else 0


def _check_times(status: str, start: int, end: int, now: int, item: YMap, base: str, diags) -> None:
    if status == "completed" and end > now:
        diags.error(errors.STATUS_TIME_INCONSISTENT, "completed activity ends after now", f"{base}.end", at=item.get("end") or item)
    elif status == "running" and start > now:
        diags.error(errors.STATUS_TIME_INCONSISTENT, "running activity starts after now", f"{base}.start", at=item.get("start") or item)


def _has_error(diags: Diagnostics) -> bool:
    return any(d.severity == "error" for d in diags.items)
