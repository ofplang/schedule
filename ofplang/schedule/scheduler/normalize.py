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
from dataclasses import dataclass, replace

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.core.identifiers import format_node_path, parse_qualified_resource
from ofplang.schedule.core.yamlnode import YMap, YNode, YScalar, YSeq, to_plain
from ofplang.schedule.scheduler.instance import (
    ActivityInstance,
    ArcInstance,
    Instance,
    RefillCandidate,
    RefillOption,
    RelayInfo,
    transport_options,
)
from ofplang.schedule.scheduler.model import Arc, Mode, NodePath
from ofplang.schedule.scheduler.status import (
    ActivityFixation,
    ArcFixation,
    Fixation,
    RefillFixation,
    arc_key,
    job_of,
    node_path,
    scoped,
    status_of,
    text,
    times,
)
from ofplang.schedule.validation import errors

_STARTED = ("completed", "running")
# Terminal statuses (§6.2): a run stops on any failure, so a document carrying one
# is a final status, not a replannable history. The scheduler rejects it rather
# than silently treating it as pending.
_TERMINAL = ("failed", "cancelled")


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


def normalize(
    base: Instance, root: YNode | None, env, *, ignore_resources: bool = False
) -> tuple[Instance | None, Fixation | None, Diagnostics]:
    """Build the augmented instance and fixation from `base` (the workflow
    instance, built with `check_reachability=False`) and the status `root`.

    `ignore_resources` switches the consumable model off (§4.7.3): no levels are
    derived, so nothing constrains the solve and nothing about `inventories` is
    checked."""
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
        diags.error(
            errors.STATUS_MISSING_NOW,
            "a document with started activities must set now",
            "now",
            at=root,
        )
        return None, None, diags

    # A terminal status (failed / cancelled) means the run has stopped; there is no
    # remaining work to plan, so such a document is not a valid replan input.
    if isinstance(root, YMap) and _has_terminal_status(root):
        diags.error(
            errors.TERMINAL_STATUS_NOT_REPLANNABLE,
            "a document with a failed / cancelled activity is terminal and cannot be replanned",
            "activities",
            at=root,
        )
        return None, None, diags
    now = now_node.value if isinstance(now_node, YScalar) and now_node.is_int else 0

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
            # `_frozen_processing_mode` only returns None after recording an error,
            # and the loop returns below; still, append a placeholder so `activities`
            # stays index-aligned with `base.activities` (and `base.precedence`)
            # rather than silently shifting every later index.
            activities.append(act)
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

    # 3. Started refills read back from the status, then the consumable levels at
    #    `now` replayed from what the run started with (§4.7.2), then the refills the
    #    solver may still run (§10).
    refills = _read_refills(root, now, diags, ignore_resources)
    if _has_error(diags):
        return None, None, diags

    levels = _derive_levels(instance, act_fix, refills, root, env, diags, ignore_resources)
    if _has_error(diags):
        return None, None, diags

    if levels:
        instance = replace(
            instance,
            replenishments=_refill_candidates(instance, act_fix, env, set(refills)),
        )

    fixation = Fixation(now, act_fix, arc_fix, levels, refills)
    return instance, fixation, diags


def _read_refills(
    root: YNode | None, now: int, diags: Diagnostics, ignore_resources: bool
) -> dict[str, RefillFixation]:
    """Started refills from the status, by `id` (§6.9).

    Read even with the model switched off: they are historical fact and the plan has
    to carry them back out (§4.7.3), which it cannot do if they were never read.
    Nothing is computed from them there.

    A `pending` refill is refused rather than ignored. Every other kind of pending
    entry is re-derived from the workflow, so ignoring it loses nothing; a refill has
    no workflow to be re-derived from, and how many to run is the scheduler's
    decision, not the caller's.
    """
    refills: dict[str, RefillFixation] = {}
    if not isinstance(root, YMap):
        return refills
    activities = root.get("activities")
    if not isinstance(activities, YSeq):
        return refills

    for index, item in enumerate(activities.items):
        if not isinstance(item, YMap) or text(item.get("kind")) != "replenishment":
            continue
        path = f"activities[{index}]"
        status = status_of(item)
        identifier = text(item.get("id"))
        if status == "pending":
            diags.error(
                errors.PENDING_REPLENISHMENT_IN_STATUS,
                "a pending replenishment is not carried over: how many to run is "
                "re-decided every solve",
                path,
                at=item,
            )
            continue
        if status not in ("completed", "running"):
            continue  # terminal statuses are refused elsewhere

        start, end = times(item)
        if status == "completed" and end > now:
            diags.error(
                errors.STATUS_TIME_INCONSISTENT,
                f"completed replenishment {identifier!r} ends after now",
                path,
                at=item,
            )
            continue
        if status == "running" and start > now:
            diags.error(
                errors.STATUS_TIME_INCONSISTENT,
                f"running replenishment {identifier!r} starts after now",
                path,
                at=item,
            )
            continue

        amounts = to_plain(item.get("amounts"))
        amounts = amounts if isinstance(amounts, dict) else {}
        refills[identifier] = RefillFixation(
            status=status,
            start=start,
            end=end,
            device=text(item.get("device")),
            replenisher=text(item.get("replenisher")),
            amounts={k: v for k, v in amounts.items() if isinstance(v, int)},
        )
    return refills


def _refill_candidates(
    instance: Instance,
    act_fix: dict[int, ActivityFixation],
    env,
    used_ids: set[str],
) -> tuple[RefillCandidate, ...]:
    """The refills the solver may run (FORMULATION §10).

    One per (pending consuming activity, device it might consume on) that some
    replenisher can reach. A fixed activity contributes none: its draw is already
    spent and folded into the levels at `now`.

    Ids avoid those the status already uses. They are unique within one document and
    no more (§6.9) -- only started refills are ever matched by id, and a started one
    keeps the id it was planned with, carried forward in the status.
    """
    candidates: list[RefillCandidate] = []
    counter = 0
    for index, activity in enumerate(instance.activities):
        if index in act_fix:
            continue
        devices: list[str] = []
        for mode in activity.modes:
            for qualified in mode.consumption:
                parsed = parse_qualified_resource(qualified)
                if parsed is not None and parsed[0] not in devices:
                    devices.append(parsed[0])
        for device in devices:
            options = tuple(
                RefillOption(replenisher, duration)
                for replenisher, duration in env.refills(device)
            )
            # No replenisher reaches it: a legitimate environment (§5.7), and simply
            # no candidate. The stock only falls.
            if not options:
                continue
            resources = tuple(sorted(env.devices[device].resources))
            while f"replenishment_{counter}" in used_ids:
                counter += 1
            identifier = f"replenishment_{counter}"
            counter += 1
            candidates.append(
                RefillCandidate(identifier, device, index, options, resources)
            )
    return tuple(candidates)


def _resource_model_declared(instance: Instance) -> bool:
    """Whether consumables would constrain this instance (§9.3).

    Some mode of some *invoked* process must declare `consumption`. Declaring
    `resources` on a device is deliberately not enough: a stock nothing draws on
    constrains nothing, and an environment should be free to describe what a device
    holds without obliging every document written against it to state a level.
    """
    return any(mode.consumption for act in instance.activities for mode in act.modes)


def _derive_levels(
    instance: Instance,
    act_fix: dict[int, ActivityFixation],
    refills: dict[str, RefillFixation],
    root: YNode | None,
    env,
    diags: Diagnostics,
    ignore_resources: bool = False,
) -> dict[tuple[str, str], int]:
    """The level of each `(device, resource)` at `now`.

    Levels are replayed, never reported (§4.7.2): the document says what the run
    started with and the history says what has been drawn since. Every started
    processing activity has already taken its consumption -- it is taken at the
    start, and the activity has started -- so the sum over the fixed activities is
    subtracted from the initial levels. A refill puts stock back the other way: a
    completed one has landed and is added here, while a running one has not and
    reaches the solver as a fixed increase at its end instead (§4.7.2).

    With `ignore_resources` the model is switched off (§4.7.3) and this returns no
    levels, which is what makes the solver's constraint vanish. Switching off is
    total: the checks below do not run either, so a mistake in `inventories` goes
    unreported -- the same bargain as any feature accepted and not applied (§2).
    Off is always a relaxation, so no schedule is lost by it.
    """
    declared = _resource_model_declared(instance)
    if ignore_resources:
        if declared:
            diags.warning(
                errors.RESOURCES_IGNORED,
                "the resource model is switched off, so consumption, the starting "
                "levels and the checks over them are not applied",
                "",
            )
        return {}
    if not declared:
        return {}

    inventories = root.get("inventories") if isinstance(root, YMap) else None
    if inventories is None:
        diags.error(
            errors.MISSING_INVENTORIES,
            "the environment's modes consume resources, so the document must say "
            "what the run started with (inventories.levels); an empty `levels` "
            "means every stock starts empty",
            "inventories",
            at=root,
        )
        return {}

    levels = _initial_levels(inventories, env, diags)
    if _has_error(diags):
        return levels

    for index, fixation in act_fix.items():
        mode = instance.activities[index].modes[fixation.mode_index]
        for qualified, amount in mode.consumption.items():
            parsed = parse_qualified_resource(qualified)
            if parsed is None:
                continue
            levels[parsed] = levels.get(parsed, 0) - amount

    # A completed refill has already raised the level; a running one has not landed
    # and reaches the solver as a fixed increase at its end instead (§4.7.2).
    for refill in refills.values():
        if refill.status != "completed":
            continue
        for resource, amount in refill.amounts.items():
            levels[(refill.device, resource)] = levels.get((refill.device, resource), 0) + amount

    for (device, resource), level in sorted(levels.items()):
        capacity = env.devices[device].resources.get(resource) if device in env.devices else None
        if level < 0 or (capacity is not None and level > capacity):
            diags.error(
                errors.STATUS_INVENTORY_INCONSISTENT,
                f"replaying the history leaves {device}.{resource} at {level}, "
                f"outside [0, {capacity}]",
                f"inventories.levels.{device}.{resource}",
                at=root,
            )
    return levels


def _initial_levels(node: YNode, env, diags: Diagnostics) -> dict[tuple[str, str], int]:
    """`inventories.levels` resolved against the environment (§9.3).

    A resource the environment declares but the document does not name starts at 0,
    so the map is filled from the environment first and then overwritten. Naming
    something the environment does not declare is an error -- device and resource
    *declarations* survive a re-route (only modes and routes are withdrawn, §7), so
    this stays strict across replans exactly as the `interface` spot checks do.
    """
    levels = {
        (device_id, resource): 0
        for device_id, device in env.devices.items()
        for resource in device.resources
    }
    initial = node.get("levels") if isinstance(node, YMap) else None
    if not isinstance(initial, YMap):
        return levels

    for entry in initial.entries:
        device_id = entry.key
        path = f"inventories.levels.{device_id}"
        device = env.devices.get(device_id)
        if device is None:
            diags.error(errors.UNKNOWN_DEVICE, f"unknown device {device_id!r}", path, at=entry)
            continue
        stocks = entry.value
        if not isinstance(stocks, YMap):
            continue
        for level_entry in stocks.entries:
            resource = level_entry.key
            level_path = f"{path}.{resource}"
            capacity = device.resources.get(resource)
            if capacity is None:
                diags.error(
                    errors.UNKNOWN_RESOURCE,
                    f"device {device_id!r} declares no resource {resource!r}",
                    level_path,
                    at=level_entry,
                )
                continue
            value = level_entry.value
            if not (isinstance(value, YScalar) and value.is_int):
                continue
            if value.value > capacity:
                diags.error(
                    errors.INVENTORY_EXCEEDS_CAPACITY,
                    f"{level_path} is {value.value}, above the capacity {capacity}",
                    level_path,
                    at=value,
                )
                continue
            levels[(device_id, resource)] = value.value
    return levels


def _has_started_activities(root: YMap) -> bool:
    """Whether the document carries any `completed` / `running` activity — the
    history that requires a `now` to pin it against."""
    activities = root.get("activities")
    if not isinstance(activities, YSeq):
        return False
    return any(isinstance(item, YMap) and status_of(item) in _STARTED for item in activities.items)


def _has_terminal_status(root: YMap) -> bool:
    """Whether the document carries any `failed` / `cancelled` activity — a terminal
    status that marks the run as stopped and so cannot be a replan input."""
    activities = root.get("activities")
    if not isinstance(activities, YSeq):
        return False
    return any(
        isinstance(item, YMap) and status_of(item) in _TERMINAL for item in activities.items
    )


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
    from the status; relays and pending entries are ignored (regenerated).

    Both keys are read back into the instance's namespace with the activity's `job`
    restored (`status.scoped`, SPEC §6.11), because that is the namespace
    `node_index` and `arc_keys` are in -- they come from the merged instance, whose
    paths are job-prefixed. Doing so is also what keeps two jobs of the *same*
    workflow apart here: their activities render identical `node` paths, so without
    the job the second would read as a duplicate of the first and its history would
    be dropped."""
    fixed_proc: dict[NodePath, _FixedProc] = {}
    legs_by_arc: dict[tuple, list[_Leg]] = defaultdict(list)

    activities_node = root.get("activities")
    items = activities_node.items if isinstance(activities_node, YSeq) else []
    seen_arc_seq: set[tuple] = set()
    for i, item in enumerate(items):
        if not isinstance(item, YMap):
            continue
        status = status_of(item)
        if status not in _STARTED:
            continue  # pending / relay / status-less: regenerated from committed legs
        base = f"activities[{i}]"
        kind = text(item.get("kind"))
        job = job_of(item)
        start, end = times(item)
        if kind == "processing":
            path = scoped(job, node_path(item.get("node")))
            subject = _subject(job, node_path(item.get("node")))
            if path not in node_index:
                diags.error(
                    errors.STATUS_NODE_UNKNOWN,
                    f"status references a processing node not in the workflow: {subject}",
                    f"{base}.node",
                    at=item.get("node") or item,
                )
                continue
            if path in fixed_proc:
                diags.error(
                    errors.STATUS_DUPLICATE,
                    f"processing node {subject} is fixed more than once",
                    base,
                    at=item,
                )
                continue
            _check_times(status, start, end, now, item, base, diags)
            fixed_proc[path] = _FixedProc(status, start, end, item)
        elif kind == "transport":
            key = arc_key(item.get("arc"), job)
            if key is None:
                continue
            if key not in arc_keys:
                diags.error(
                    errors.STATUS_ARC_UNKNOWN,
                    "status references a transport arc not in the workflow"
                    + (f" (job {job!r})" if job else ""),
                    f"{base}.arc",
                    at=item.get("arc") or item,
                )
                continue
            seq = _seq_of(item)
            # `key` already carries the job, so "same arc + seq" means the same leg of
            # the same job -- two jobs' matching legs are distinct here.
            if (key, seq) in seen_arc_seq:
                diags.error(
                    errors.STATUS_DUPLICATE,
                    "transport leg is fixed more than once (same arc + seq)"
                    + (f" in job {job!r}" if job else ""),
                    base,
                    at=item,
                )
                continue
            seen_arc_seq.add((key, seq))
            _check_times(status, start, end, now, item, base, diags)
            legs_by_arc[key].append(
                _Leg(
                    seq,
                    status,
                    start,
                    end,
                    text(item.get("from_spot")),
                    text(item.get("to_spot")),
                    text(item.get("transporter")),
                )
            )
    return fixed_proc, legs_by_arc


# --------------------------------------------------------------------------
# Building one arc's chain.
# --------------------------------------------------------------------------


def _build_chain(
    arc_inst, legs, fixed_proc, node_index, now, env, activities, act_fix, arcs, arc_fix, diags
):
    logical = arc_inst.arc
    src_i, dst_i = arc_inst.src_activity, arc_inst.dst_activity
    dst_fixed = _node_of(activities, dst_i) in fixed_proc

    legs = sorted(legs, key=lambda leg: leg.seq)

    if not legs:
        # No committed leg: a single pending transport, resolved against the
        # (possibly frozen) endpoints. Reachability of pending legs is checked
        # by the caller after normalization.
        options = transport_options(
            activities[src_i], logical.src.port, activities[dst_i], logical.dst.port, env
        )
        arcs.append(ArcInstance(logical, src_i, dst_i, tuple(options)))
        return

    # A committed leg means the source transport started, so the source processing
    # must be completed — unless the source is the input boundary node (SPEC §6.8),
    # which is the workflow's origin (the entry Object is present from time 0), not a
    # processing that runs.
    if activities[src_i].boundary is None and _node_of(activities, src_i) not in fixed_proc:
        diags.error(
            errors.BROKEN_TRANSPORT_CHAIN,
            f"a started transport leaves {format_node_path(logical.src.node)} "
            f"but that activity is not completed",
            "",
            at=None,
        )
        return

    # Cross-activity temporal consistency of the committed legs (review #3): a leg
    # cannot end before it starts, and the first leg cannot depart before its source
    # activity completed. Either is a self-contradictory status; report it here so it
    # surfaces as a diagnostic rather than as a bare INFEASIBLE from the pinned model.
    src_fix = act_fix.get(src_i)
    for k, leg in enumerate(legs):
        if leg.end < leg.start:
            diags.error(
                errors.STATUS_TIME_INCONSISTENT,
                f"transport leg seq {leg.seq} ends before it starts",
                "",
                at=None,
            )
            return
        if k == 0 and src_fix is not None and leg.start < src_fix.end:
            diags.error(
                errors.STATUS_TIME_INCONSISTENT,
                f"transport leg seq {leg.seq} departs before its source activity ends",
                "",
                at=None,
            )
            return

    prev_i = src_i
    prev_spot = None  # the spot the previous leg delivered to (None before leg 0)
    for k, leg in enumerate(legs):
        is_last = k == len(legs) - 1
        # Continuity: each leg departs from where the previous one arrived.
        if prev_spot is not None and leg.from_spot != prev_spot:
            diags.error(
                errors.BROKEN_TRANSPORT_CHAIN,
                f"transport leg seq {leg.seq} departs {leg.from_spot!r} "
                f"but the previous leg arrived at {prev_spot!r}",
                "",
                at=None,
            )
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
        options = transport_options(
            activities[prev_i], "out", activities[dst_i], logical.dst.port, env
        )
        arcs.append(ArcInstance(logical, prev_i, dst_i, tuple(options), seq=legs[-1].seq + 2))


def _append_relay(activities, act_fix, logical, leg, now) -> int:
    """Append a relay activity at `leg`'s arrival spot and return its index. A
    relay whose delivering leg has completed is itself fixed (its arrival is a
    fact); one fed by a running leg is pending (the Object is still on its way)."""
    idx = len(activities)
    mode = Mode(
        id="relay",
        devices=(),
        duration=0,
        input_spots={"in": leg.to_spot},
        output_spots={"out": leg.to_spot},
    )
    activities.append(
        ActivityInstance((), "", (mode,), relay=RelayInfo(logical, leg.seq + 1, leg.to_spot))
    )
    if leg.status == "completed":
        act_fix[idx] = ActivityFixation("completed", leg.end, leg.end, 0)
    return idx


def _frozen_leg_option(leg: _Leg):
    from ofplang.schedule.scheduler.instance import TransportOption

    return TransportOption(
        0, 0, leg.transporter, leg.from_spot, leg.to_spot, max(0, leg.end - leg.start)
    )


def _frozen_processing_mode(entry: YMap, process: str, env, diags) -> Mode | None:
    """A fixed processing activity's occupancy comes from its reported echo
    (input_spots / output_spots / devices), falling back to the environment's
    mode of the reported id, and erroring only if neither resolves (SPEC §9.3)."""
    mode_id = text(entry.get("mode"))
    # The echo is schema-valid by this point (§9.2), so these are a mapping / mapping /
    # sequence; narrowed rather than assumed because `to_plain` returns a plain value of
    # whatever shape the document had.
    inp = to_plain(entry.get("input_spots"))
    out = to_plain(entry.get("output_spots"))
    devs = to_plain(entry.get("devices"))
    cons = to_plain(entry.get("consumption"))
    # Absent reads as the default, exactly as in the environment (§6.3). Without it
    # a running non-accessing activity would be pinned as one that occupies its
    # devices, blocking them for the rest of its run.
    access = to_plain(entry.get("device_access"))
    # `consumption` is part of the echo for the same reason the spots are: a replan
    # may withdraw the very mode this activity used -- that is how a re-route is
    # triggered -- and a fixed activity is never re-read against the current
    # environment (§7), so what it consumed has to travel with it (§6.3). It also
    # counts towards "there is an echo here": a hand-written status may state the
    # consumption and leave the spots out.
    if inp or out or devs or cons or access is not None:
        return Mode(
            mode_id,
            tuple(devs) if isinstance(devs, list) else (),
            0,
            dict(inp) if isinstance(inp, dict) else {},
            dict(out) if isinstance(out, dict) else {},
            consumption=dict(cons) if isinstance(cons, dict) else {},
            device_access=True if access is None else bool(access),
        )
    capability = env.processes.get(process)
    if capability is not None:
        for mode in capability.modes:
            if mode.id == mode_id:
                return mode
    diags.error(
        errors.STATUS_MODE_UNKNOWN,
        f"cannot pin fixed activity: mode {mode_id!r} has no echo and "
        f"process {process!r} does not offer it",
        "",
        at=entry,
    )
    return None


# --------------------------------------------------------------------------
# Small helpers.
# --------------------------------------------------------------------------


def _subject(job: str, path: NodePath) -> str:
    """How a status entry's processing activity is named in a diagnostic: its
    workflow-relative node path, and the job it belongs to where there is one.

    The scoped path would read as `job1/Assay` and invite the reader to look for a
    node called `job1`; the job is not part of the workflow, so it is said separately.
    """
    where = format_node_path(path)
    return f"{where} (job {job!r})" if job else where


def _arc_key_of(arc: Arc) -> tuple:
    return (arc.src.node, arc.src.port, arc.dst.node, arc.dst.port)


def _node_of(activities, i: int) -> NodePath:
    return activities[i].node


def _seq_of(item: YMap) -> int:
    node = item.get("seq")
    return node.value if isinstance(node, YScalar) and node.is_int else 0


def _check_times(status: str, start: int, end: int, now: int, item: YMap, base: str, diags) -> None:
    if status == "completed" and end > now:
        diags.error(
            errors.STATUS_TIME_INCONSISTENT,
            "completed activity ends after now",
            f"{base}.end",
            at=item.get("end") or item,
        )
    elif status == "running" and start > now:
        diags.error(
            errors.STATUS_TIME_INCONSISTENT,
            "running activity starts after now",
            f"{base}.start",
            at=item.get("start") or item,
        )


def _has_error(diags: Diagnostics) -> bool:
    return any(d.severity == "error" for d in diags.items)
