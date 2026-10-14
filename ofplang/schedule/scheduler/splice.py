"""The plan being replaced, with what a decision changed spliced in (design.md D67).

A replan is handed the stamped plan it replaces. Where only durations moved, that plan's
order still covers the instance and is laid out again (`normalize._previous_plan`,
`greedy._replay_previous`). Two decisions change the shape it was made for, and those
are the ones a run meets most: a branch decided during the run on the arm not assumed,
and a loop going on one invocation more. Then the plan covers everything but the region
that changed -- and what that region needs was kept free for it (D66): the outcome
decided was planned locally, on the candidate the plan kept, and the next invocation of
a loop runs the modes of the last one, on the spots the loop kept. So the order is still
a schedule once that region's new work is put in it: in the modes the local plan or the
invocation before gave it, at the moment of the decision, before everything that came
after the region's window.

This module only reads: which regions changed (`changed_regions`), and the previous
plan with their new work in it (`previous_plan`). The modes of a decided branch's new
outcome come from its candidates, which only the caller can plan again (`api`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ofplang.schedule.core.yamlnode import YMap, YNode, YScalar, YSeq, to_plain
from ofplang.schedule.scheduler import normalize
from ofplang.schedule.scheduler.instance import Instance
from ofplang.schedule.scheduler.model import GATE_IN, GATE_OUT, Mode, NodePath
from ofplang.schedule.scheduler.status import (
    Fixation,
    PreviousPlan,
    PreviousStep,
    arc_key,
    job_of,
    node_path,
    scoped,
    status_of,
    text,
    times,
)


@dataclass(frozen=True)
class Change:
    """A region whose shape a decision changed since the plan being replaced: a branch
    decided on the arm that plan did not assume (`arm`), or a loop that went on past the
    invocation it was planned to end at (`loop`). `region` is in the instance's
    namespace; `job` indexes the jobs; `path` is the workflow's own node path."""

    region: NodePath
    kind: str
    job: int
    path: NodePath


def _entries(root: YNode | None) -> list:
    activities = root.get("activities") if isinstance(root, YMap) else None
    return list(activities.items) if isinstance(activities, YSeq) else []


def changed_regions(root: YNode | None, jobs, workflows) -> list[Change]:
    """The regions the plan being replaced marked as waiting on a decision (§6.14) whose
    decision has since changed what is expanded."""
    index = {job.id: j for j, job in enumerate(jobs)}
    found: list[Change] = []
    for item in _entries(root):
        if not isinstance(item, YMap) or text(item.get("kind")) != "decision":
            continue
        assumed = item.get("assumed")
        if assumed is None or not to_plain(assumed):
            continue
        job = job_of(item)
        j = index.get(job if job else None, index.get(job))
        if j is None:
            continue
        workflow = workflows[j]
        path = node_path(item.get("node"))
        region = scoped(job, path)
        length = item.get("length")
        if isinstance(length, YScalar) and length.is_int:
            # A traversal whose length the run has counted since (design.md D70).
            traversal = workflow.lengths.get(path)
            if traversal is not None and not traversal.assumed and (
                traversal.length != length.value
            ):
                found.append(Change(region, "length", j, path))
            continue
        iteration = item.get("iteration")
        if isinstance(iteration, YScalar) and iteration.is_int:
            loop = workflow.loops.get(path)
            if loop is not None and loop.count > iteration.value + 1:
                found.append(Change(region, "loop", j, path))
            continue
        gate = workflow.branch_gates.get(path)
        if gate is not None and not gate.assumed and gate.arm != text(item.get("arm")):
            found.append(Change(region, "arm", j, path))
    # One change inside another is part of it.
    return [c for c in found
            if not any(o is not c and c.region[: len(o.region)] == o.region for o in found)]


def _occupies(mode: Mode) -> tuple:
    return (
        tuple(mode.devices),
        tuple(sorted(mode.input_spots.items())),
        tuple(sorted(mode.output_spots.items())),
    )


def _spots(mode: Mode) -> set[str]:
    return set(mode.input_spots.values()) | set(mode.output_spots.values())


def _back_one(path: NodePath, known) -> NodePath | None:
    """The same node one invocation back -- the innermost index that can go down by one
    and still name a node `known`."""
    for k in range(len(path) - 1, -1, -1):
        v = path[k]
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            back = (*path[:k], v - 1, *path[k + 1:])
            if back in known:
                return back
    return None


def previous_plan(
    root: YNode | None,
    instance: Instance,
    fixation: Fixation,
    changes: list[Change],
    decided: dict[NodePath, Mode],
) -> PreviousPlan | None:
    """The previous plan's pending order with each changed region's new work spliced in,
    or None where it cannot be.

    Everything outside the changed regions is read exactly as `normalize` reads it: all
    or nothing. Inside them, what the plan names is read the same way and the rest is
    new: an activity takes the mode `decided` gives it (a branch's outcome, from its
    candidate), the one its invocation before ran in (a loop going on), or -- inside a
    region still undecided -- that region's candidate's. New work outside every changed
    region is allowed only where it has an invocation before it (a map over what a loop
    that went on collected, D67 hole 8), and goes after everything the plan had.
    A new move takes a route that agrees with the modes at its ends, preferring the one
    its counterpart took.

    **Where the new work goes.** Not before the decision (`now`); after any Object a
    started activity left on one of its spots has gone, as the plan had it go; and after
    whatever it follows. Then every step is moved after whatever it follows: a loop's
    own gates, which now follow its new last invocation, go after that invocation."""
    if not changes or not isinstance(root, YMap):
        return None
    act_fix, arc_fix = fixation.activities, fixation.arcs
    regions = [c.region for c in changes]
    # A traversal's new invocations keep no spots of their own beforehand (design.md
    # D70): they go after everything the plan had, as new work outside every region
    # does -- unless the traversal is inside a region still keeping its spots, whose
    # window cannot close before them. There they go in its window, after the decision,
    # as a decided region's new work always has.
    windows = [tuple(res.region) for res in instance.reservations]
    # A fold that carries an Object holds it in its gate to the end of the region: what
    # follows waits for the gate, so the new invocations go where the Object already is,
    # before it, rather than after everything with the Object in the way.
    holding = {
        tuple(act.node[: len(act.node) - 2])
        for act in instance.activities
        if act.gate and len(act.node) >= 3 and act.node[-2] == GATE_OUT
        and any(GATE_IN in mode.input_spots for mode in act.modes)
    }
    counted = [
        c.region for c in changes
        if c.kind == "length" and tuple(c.region) not in holding
        and not any(len(w) < len(c.region) and tuple(c.region[: len(w)]) == w for w in windows)
    ]

    def inside(path: NodePath) -> bool:
        return any(tuple(path[: len(r)]) == tuple(r) for r in regions)

    def appended(path: NodePath) -> bool:
        return not inside(path) or any(tuple(path[: len(r)]) == tuple(r) for r in counted)

    processing: dict[NodePath, YMap] = {}
    transport: dict[tuple, YMap] = {}
    kept_by: dict[NodePath, set[str]] = {}
    for item in _entries(root):
        if not isinstance(item, YMap):
            continue
        kind = text(item.get("kind"))
        if kind == "reservation":
            spots = to_plain(item.get("spots"))
            kept_by[scoped(job_of(item), node_path(item.get("node")))] = (
                set(spots) if isinstance(spots, list) else set()
            )
            continue
        if status_of(item) != "pending":
            continue
        if kind in ("processing", "gate"):
            processing.setdefault(scoped(job_of(item), node_path(item.get("node"))), item)
        elif kind == "transport":
            key = arc_key(item.get("arc"), job_of(item))
            if key is not None:
                transport.setdefault((key, normalize._seq_of(item)), item)

    acts = instance.activities
    by_node = {a.node: i for i, a in enumerate(acts) if a.relay is None and a.boundary is None}
    delivering = {arc.dst_activity: r for r, arc in enumerate(instance.arcs)}
    chosen: dict[int, int] = {}
    old: list[PreviousStep] = []
    new: list[PreviousStep] = []
    late: set[tuple[str, int]] = set()  # new work outside every change: after everything
    implicit: dict[int, int] = {}
    fresh: list[int] = []

    for i, act in enumerate(acts):
        if i in act_fix:
            chosen[i] = act_fix[i].mode_index
            continue
        if act.relay is not None:
            fed_by = delivering.get(i)
            if fed_by is None or fed_by not in arc_fix:
                return None  # a planned chain: not read back (normalize)
            chosen[i] = 0
            implicit[i] = 0
            continue
        if act.boundary is not None:
            continue
        planned = processing.get(act.node)
        mode_index = None
        if planned is not None:
            read = normalize._gate_mode if act.gate else normalize._mode_named
            mode_index = read(act, planned)
        if planned is not None and mode_index is not None:
            chosen[i] = mode_index
            start, end = times(planned)
            old.append(PreviousStep("place", i, mode_index, start, end))
        elif inside(act.node) or _back_one(act.node, by_node) is not None:
            fresh.append(i)
        else:
            return None

    # The modes of the new work. A region still undecided inside a changed one runs on
    # one of its own candidates -- the first the changed region kept the spots of.
    pinned: dict[int, int] = {}
    for res in instance.reservations:
        if not res.candidates or not inside(res.region):
            continue
        outer = max((r for r in regions if tuple(res.region[: len(r)]) == tuple(r)), key=len)
        kept = kept_by.get(outer)
        pick = next(
            (pins for spots, pins in res.candidates if kept is None or spots <= kept),
            res.candidates[0][1],
        )
        pinned.update(dict(pick))
    for i in fresh:
        act = acts[i]
        mode_index = pinned.get(i)
        if mode_index is None and act.node in decided:
            want = decided[act.node]
            mode_index = next(
                (k for k, m in enumerate(act.modes)
                 if (act.gate and m.input_spots.get(GATE_IN) == want.input_spots.get(GATE_IN))
                 or (not act.gate and _occupies(m) == _occupies(want))),
                None,
            )
        if mode_index is None:
            back = _back_one(act.node, by_node)
            if back is not None and by_node[back] in chosen:
                want = acts[by_node[back]].modes[chosen[by_node[back]]]
                mode_index = next(
                    (k for k, m in enumerate(act.modes) if _occupies(m) == _occupies(want)), None
                )
        if mode_index is None and inside(act.node):
            # No invocation before it to follow -- the first of a traversal inside a
            # loop's next invocation, where the last one had none (design.md D70): a mode
            # on the spots the changed region kept, which is what it kept them for.
            outer = max((r for r in regions if tuple(act.node[: len(r)]) == tuple(r)), key=len)
            kept = kept_by.get(outer)
            if kept is not None:
                mode_index = next(
                    (k for k, m in enumerate(act.modes) if _spots(m) <= kept), None
                )
        if mode_index is None:
            return None
        chosen[i] = mode_index
        new.append(PreviousStep("place", i, mode_index, 0, 0))
        if appended(act.node):
            late.add(("place", i))

    # The moves, read or new. A new one into a final output that rests where the schedule
    # puts it is chosen last, once every other resting place is known: a finished product
    # holds its spot to the end, so it cannot rest where another one does (design.md D70).
    resting: list[int] = []
    for r, arc in enumerate(instance.arcs):
        if r in arc_fix:
            continue
        dst = acts[arc.dst_activity]
        if (
            dst.boundary is not None and dst.boundary.kind == "output" and len(dst.modes) > 1
            and arc.src_activity in fresh
        ):
            resting.append(r)
            continue
        if acts[arc.src_activity].relay is not None:
            option_index = normalize._hand_over(arc, chosen)
            if option_index is None:
                return None
            landing = arc_fix[delivering[arc.src_activity]].end
            old.append(PreviousStep("move", r, option_index, landing, landing))
        else:
            moved = transport.get((normalize._arc_key_of(arc.arc), arc.seq or 0))
            option_index = (
                normalize._option_taken(instance, arc, moved, chosen) if moved is not None else None
            )
            if moved is not None and option_index is not None:
                start, end = times(moved)
                old.append(PreviousStep("move", r, option_index, start, end))
            else:
                if not (inside(arc.arc.src.node) or inside(arc.arc.dst.node)
                        or arc.src_activity in fresh or arc.dst_activity in fresh):
                    return None
                option_index = _route(instance, r, chosen, by_node, transport)
                if option_index is None:
                    return None
                new.append(PreviousStep("move", r, option_index, 0, 0))
                if not (inside(arc.arc.src.node) or inside(arc.arc.dst.node)) or (
                    ("place", arc.src_activity) in late or ("place", arc.dst_activity) in late
                ):
                    late.add(("move", r))
        option = arc.options[option_index]
        for end_index, mode_index in (
            (arc.src_activity, option.src_mode_index),
            (arc.dst_activity, option.dst_mode_index),
        ):
            if acts[end_index].boundary is None or end_index in chosen:
                continue
            if implicit.setdefault(end_index, mode_index) != mode_index:
                return None
    taken = {
        spot
        for i, act in enumerate(acts)
        if act.boundary is not None and act.boundary.kind in ("output", "held")
        and (i in implicit or len(act.modes) == 1)
        for spot in _spots(act.modes[implicit.get(i, 0)])
    }
    for r in resting:
        arc = instance.arcs[r]
        preferred = _route(instance, r, chosen, by_node, transport)
        free = [
            k for k, option in enumerate(arc.options)
            if chosen.get(arc.src_activity, option.src_mode_index) == option.src_mode_index
            and option.to_spot not in taken
        ]
        if not free:
            return None
        option_index = preferred if preferred in free else free[0]
        option = arc.options[option_index]
        implicit[arc.dst_activity] = option.dst_mode_index
        taken.add(option.to_spot)
        new.append(PreviousStep("move", r, option_index, 0, 0))
        if ("place", arc.src_activity) in late or not inside(arc.arc.src.node):
            late.add(("move", r))
    aside = _out_of_the_way(instance, old, new, implicit, taken)
    if aside is None:
        return None
    return PreviousPlan(_ordered(instance, fixation, aside, new, late), implicit)


def _out_of_the_way(
    instance: Instance,
    old: list[PreviousStep],
    new: list[PreviousStep],
    implicit: dict[int, int],
    taken: set[str],
) -> list[PreviousStep] | None:
    """`old` with every finished product the new work would find in its way moved aside
    (design.md D70, H1): a final output that rests where the schedule puts it holds that
    spot to the end, and the plan being replaced may have left one on a spot the new work
    needs -- a cup left in the hand that makes the next one. Its move has not happened
    (only pending moves are in `old`), so it goes to a spot nothing in the plan uses
    instead. None where there is none."""
    acts = instance.activities

    def spots_of(step: PreviousStep) -> set[str]:
        if step.kind == "place":
            return _spots(acts[step.index].modes[step.choice])
        option = instance.arcs[step.index].options[step.choice]
        return {option.from_spot, option.to_spot}

    wanted: set[str] = set()
    for step in new:
        wanted |= spots_of(step)
    used: set[str] = set(taken) | wanted
    for step in old:
        used |= spots_of(step)
    moved: list[PreviousStep] = []
    for step in old:
        arc = instance.arcs[step.index] if step.kind == "move" else None
        dst = acts[arc.dst_activity] if arc is not None else None
        if (
            arc is None or dst is None or dst.boundary is None
            or dst.boundary.kind != "output" or len(dst.modes) < 2
            or arc.options[step.choice].to_spot not in wanted
        ):
            moved.append(step)
            continue
        source_mode = arc.options[step.choice].src_mode_index
        aside = next(
            (k for k, option in enumerate(arc.options)
             if option.src_mode_index == source_mode and option.to_spot not in used),
            None,
        )
        if aside is None:
            return None
        option = arc.options[aside]
        used.add(option.to_spot)
        implicit[arc.dst_activity] = option.dst_mode_index
        moved.append(replace(step, choice=aside))
    return moved


def _route(instance: Instance, r: int, chosen: dict[int, int], by_node, transport) -> int | None:
    """A route for new move `r` agreeing with the modes chosen at its ends: the one its
    counterpart an invocation back took where there is one, else the first."""
    arc = instance.arcs[r]
    fits = [
        k for k, option in enumerate(arc.options)
        if chosen.get(arc.src_activity, option.src_mode_index) == option.src_mode_index
        and chosen.get(arc.dst_activity, option.dst_mode_index) == option.dst_mode_index
    ]
    if not fits:
        return None
    src, dst = tuple(arc.arc.src.node), tuple(arc.arc.dst.node)
    for key, item in transport.items():
        (src_node, src_port, _si, dst_node, dst_port, _di), _seq = key
        if src_port != arc.arc.src.port or dst_port != arc.arc.dst.port:
            continue
        ends = (tuple(src_node), tuple(dst_node))
        if ends == (src, dst) or ends[0] not in _shifts(src) or ends[1] not in _shifts(dst):
            continue
        taken = (text(item.get("from_spot")), text(item.get("to_spot")))
        for k in fits:
            option = arc.options[k]
            if (option.from_spot, option.to_spot) == taken:
                return k
    return fits[0]


def _shifts(path: NodePath) -> set[NodePath]:
    """`path` itself and every path one invocation back from it."""
    found = {tuple(path)}
    for k, v in enumerate(path):
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            found.add((*path[:k], v - 1, *path[k + 1:]))
    return found


def _ordered(
    instance: Instance,
    fixation: Fixation,
    old: list[PreviousStep],
    new: list[PreviousStep],
    late: set[tuple[str, int]],
) -> tuple[PreviousStep, ...]:
    """Old and new steps in one order (design.md D67 §3.3).

    The order is the previous plan's, which is what the replay lays out: the old steps as
    it ran them, and each new one inserted -- after every old step before the decision's
    moment, after the move that takes away any Object a started activity left on one of
    its spots, and after the old steps it follows; one outside every changed region
    after everything. Then all of them are put in an order where each comes after what
    it follows, the earliest-inserted first: a loop's own gates, which now follow its new
    last invocation, go after it, and nothing else moves.

    A step's times stay the previous plan's (a new one takes those of the step it was
    inserted after): the replay reads only the order, but what keeps windows in their
    place reads the times against the history's."""
    import heapq

    now = fixation.now
    acts = instance.activities
    old_sorted = sorted(old, key=lambda s: (s.start, s.end, 0 if s.kind == "move" else 1, s.index))
    position = {(s.kind, s.index): float(k) for k, s in enumerate(old_sorted)}
    # Where an Object history left goes away -- through any gate it waits in on that spot
    # (`plancheck.history_holds`).
    from ofplang.schedule.scheduler.plancheck import history_holds

    holds = history_holds(instance, fixation)
    leaving: dict[str, float] = {}
    for k, step in enumerate(old_sorted):
        if step.kind != "move":
            continue
        option = instance.arcs[step.index].options[step.choice]
        if option.from_spot != option.to_spot and ("rs", step.index, option.from_spot) in holds:
            leaving[option.from_spot] = max(leaving.get(option.from_spot, -1.0), float(k))
    started = max((float(k) for k, s in enumerate(old_sorted) if s.start < now), default=-1.0)
    end_of_all = float(len(old_sorted))

    def spots_of(step: PreviousStep) -> set[str]:
        if step.kind == "place":
            return _spots(acts[step.index].modes[step.choice])
        option = instance.arcs[step.index].options[step.choice]
        return {option.from_spot, option.to_spot}

    before: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for a, b in instance.precedence:
        before.setdefault(("place", b), []).append(("place", a))
    for r, arc in enumerate(instance.arcs):
        before.setdefault(("place", arc.dst_activity), []).append(("move", r))
        before.setdefault(("move", r), []).append(("place", arc.src_activity))

    for step in new:
        key = (step.kind, step.index)
        if key in late:
            position[key] = end_of_all + 0.5
            continue
        at = max([started] + [leaving[s] for s in spots_of(step) if s in leaving])
        at = max([at] + [position[p] for p in before.get(key, ()) if p in position and p not in
                         {(n.kind, n.index) for n in new}])
        position[key] = at + 0.5

    # Each after what it follows, earliest position first.
    keys = list(position)
    waiting = {key: [p for p in before.get(key, ()) if p in position] for key in keys}
    followers: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for key, ps in waiting.items():
        for p in ps:
            followers.setdefault(p, []).append(key)
    count = {key: len(ps) for key, ps in waiting.items()}
    ready = [(position[key], key) for key in keys if count[key] == 0]
    heapq.heapify(ready)
    order: list[tuple[str, int]] = []
    while ready:
        _at, key = heapq.heappop(ready)
        order.append(key)
        for f in followers.get(key, ()):
            count[f] -= 1
            if count[f] == 0:
                heapq.heappush(ready, (position[f], f))
    if len(order) != len(keys):
        placed = set(order)
        order += [key for key in sorted(keys, key=lambda key: position[key]) if key not in placed]

    by_key = {(s.kind, s.index): s for s in (*old, *new)}
    fresh = {(s.kind, s.index) for s in new}
    steps: list[PreviousStep] = []
    last_start = float(now)
    for name in order:
        step = by_key[name]
        if name in fresh:
            step = replace(step, start=last_start, end=last_start)
        else:
            last_start = float(step.start)
        steps.append(step)
    return tuple(steps)
