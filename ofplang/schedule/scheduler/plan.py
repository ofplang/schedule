"""Render a solved instance as an execution document (SPECIFICATIONS.md §6).

A plan is action-first: each activity's main fields say what is actually done,
with the workflow provenance (`node` / `arc`) carried alongside. On an initial
plan every activity is pending, so `status` and `now` are omitted. On a replan the
same document carries the full timeline: completed/running activities keep a
`status` (§6.2) and `now` is echoed, so the output re-optimises the future while
showing the fixed history — and round-trips as the next replan input. The
`interface` boundary constraint (§6.8) is echoed in both cases.
"""

from __future__ import annotations

from typing import Any

import yaml

from ofplang.schedule.core import objective as objective_stages
from ofplang.schedule.scheduler.cpsat import Solution
from ofplang.schedule.scheduler.instance import Instance
from ofplang.schedule.scheduler.model import JobSpec


def render_plan(
    instance: Instance,
    solution: Solution,
    *,
    # A string for one workflow, the list of them (in job order) for a joint plan.
    workflow: str | list[str] | None = None,
    environment: str | None = None,
    status: str | None = None,
    now: int | None = None,
    interface: dict | None = None,
    inventories: dict | None = None,
    occupied: list | None = None,
    ignore_resources: bool = False,
    jobs: tuple[JobSpec, ...] = (),
) -> dict:
    """Build the execution-document dict for `solution`.

    `inventories` is echoed either way -- it is the caller's own input, and dropping
    it would lose the round trip -- but with `ignore_resources` no activity carries a
    `consumption` echo, so a plan adds nothing a reader that predates resources
    cannot read (§4.7.3).

    `jobs` is the roster of a joint plan (SPEC §6.11), in job order, and giving it is
    what says every workflow node path in the instance is job-prefixed
    (`instance.prefix_instance`). Rendering splits that prefix back off into each
    activity's `job` field, so `node` stays the workflow-relative path it has always
    been. Empty -- the single-workflow case -- means node paths carry no prefix and no
    activity gets a `job`, which is what keeps such a plan byte-for-byte what it was."""
    job_ids = tuple(job.id for job in jobs)
    activities: list[dict] = []

    for p in solution.processing:
        if p.boundary is not None:
            # A synthetic boundary node (§6.8) is not a workflow activity; it is
            # never rendered. Its boundary arc (carrying an empty-path endpoint) is
            # emitted as an ordinary transport below.
            continue
        if p.relay is not None:
            # A relay junction (§6.4.1): identity is its arc + seq + spot, not a
            # workflow node. Its job is the one the arc belongs to.
            entry: dict[str, Any] = {"kind": "relay"}
            _set_job(entry, _arc_job(p.relay.arc, job_ids))
            if p.status is not None:
                entry["status"] = p.status
            entry.update(
                {
                    "start": p.start,
                    "end": p.end,
                    "seq": p.relay.seq,
                    "spot": p.relay.spot,
                    "arc": _arc(p.relay.arc, job_ids),
                }
            )
            activities.append(entry)
            continue

        entry = {"kind": "processing"}
        job, node = _split_job(p.node, job_ids)
        _set_job(entry, job)
        # A fixed activity keeps its status (§6.2); pending activities omit it.
        if p.status is not None:
            entry["status"] = p.status
        entry.update(
            {
                "start": p.start,
                "end": p.end,
                "process": p.process,
                "mode": p.mode.id,
                "node": node,
            }
        )
        # Derivable echo of the selected mode (§6.3); omit when empty.
        if p.mode.devices:
            entry["devices"] = list(p.mode.devices)
        # Written only where it is False, so a plan from an environment that never
        # says `device_access` is shaped exactly as it always was (§6.3). It is part
        # of the echo for the same reason `consumption` is: a started activity is
        # read back from the echo, never re-read against the current environment
        # (§7), and an activity that occupied nothing must not come back as one that
        # occupies its devices -- a running hold would then block them for the rest
        # of its run.
        if not p.mode.device_access:
            entry["device_access"] = False
        if p.mode.input_spots:
            entry["input_spots"] = dict(p.mode.input_spots)
        if p.mode.output_spots:
            entry["output_spots"] = dict(p.mode.output_spots)
        # Echoed where the mode consumes something (§6.3): a later replan may
        # withdraw this mode from the environment, and a fixed activity is not read
        # back against it, so the amount has to be here to be recoverable.
        #
        # Not with the model switched off (§4.7.3). The echo exists to make history
        # replayable and nothing replays it here, and it is the one thing that would
        # otherwise leave the plan shaped unlike one from an environment that never
        # declared a resource -- which is the whole point of switching off, since a
        # reader that predates resources rejects the key outright.
        if p.mode.consumption and not ignore_resources:
            entry["consumption"] = dict(p.mode.consumption)
        activities.append(entry)

    for t in solution.transport:
        entry = {"kind": "transport"}
        _set_job(entry, _arc_job(t.arc, job_ids))
        if t.status is not None:
            entry["status"] = t.status
        entry["start"] = t.start
        entry["end"] = t.end
        entry["from_spot"] = t.option.from_spot
        entry["to_spot"] = t.option.to_spot
        # A same-spot move (§5.4) is a physical no-op: no transporter carries it,
        # so the field is omitted (§6.4). The occupied devices still derive from
        # the spots, and the route (from == to) is unambiguous without it.
        if t.option.from_spot != t.option.to_spot:
            entry["transporter"] = t.option.transporter
        entry["arc"] = _arc(t.arc, job_ids)
        # Chain position on a multi-leg move (§6.6); omit for a single-leg transport.
        if t.seq is not None:
            entry["seq"] = t.seq
        activities.append(entry)

    # A stable, readable order: by time, then processing before transport.
    for r in solution.replenishment:
        # A refill has no workflow provenance -- the scheduler decided to run it --
        # so it carries an explicit `id` where the others carry a node or an arc
        # (§4.2, §6.9). `amounts` keys are bare: `device` already says whose stock.
        entry = {
            "kind": "replenishment",
            "id": r.id,
            "start": r.start,
            "end": r.end,
            "device": r.device,
            "replenisher": r.replenisher,
            "amounts": dict(r.amounts),
        }
        if r.status is not None:
            entry["status"] = r.status
        activities.append(entry)

    activities.sort(key=lambda a: (a["start"], a["end"], a["kind"]))
    # Standard output normalization: elide relay + zero-distance re-transport pairs.
    activities = _fold_relayed_zero_distance(activities)

    doc: dict = {"time": {"unit": instance.time_unit}}
    if now is not None:
        doc["now"] = now
    # The roster of jobs this plan covers (§6.11), before everything the jobs then
    # qualify. Present exactly on a joint plan: a single-workflow plan has no roster
    # and no `job` on any activity, which is what leaves it unchanged.
    if jobs:
        doc["jobs"] = [_job_entry(job) for job in jobs]
    # The interface boundary constraint (§6.8) round-trips: echo it verbatim so the
    # plan can be fed back as the next document.
    if interface:
        doc["interface"] = interface
    # `inventories` says what the run started with, so it does not change from one
    # replan to the next (§6.10). Echoed verbatim for the same reason `interface` is:
    # the plan has to be usable as the next document, and levels are replayed from
    # this rather than reported.
    if inventories:
        doc["inventories"] = inventories
    # What is sitting on a spot that this plan does not otherwise account for (§6.12).
    # Echoed for the reason `interface` is: it is the caller's own input, and it does
    # not stop being true because a plan was made.
    if occupied:
        doc["occupied"] = occupied
    doc["outcome"] = solution.outcome
    # `value` takes the shape of its `kind` (§6.1): a scalar for one stage, a list
    # for several. A plan is only rendered when a solve succeeded, so every stage
    # has a value.
    doc["objective"] = objective_stages.render(
        solution.objective_kind, solution.objective_values
    )
    doc["activities"] = activities
    meta = {}
    if workflow is not None:
        meta["workflow"] = workflow
    if environment is not None:
        meta["environment"] = environment
    if status is not None:
        meta["status"] = status
    if meta:
        doc["meta"] = meta
    return doc


def _job_entry(job: JobSpec) -> dict:
    """One roster entry (§6.11). `release` is written only where it is not 0 and
    `bound` / `fingerprint` only where they are set, so a roster says no more than it
    has to -- and a job that arrived at time 0 with no promise yet renders as the bare
    `{id: ...}` it did before those fields existed."""
    entry: dict = {"id": job.id}
    if job.release:
        entry["release"] = job.release
    if job.bound is not None:
        entry["bound"] = job.bound
    if job.fingerprint is not None:
        entry["fingerprint"] = job.fingerprint
    # Echoed verbatim, for the reason the top-level one is (§6.8): it is the caller's
    # own planning input, and a plan that dropped it could not be the next one's.
    if job.interface:
        entry["interface"] = job.interface
    return entry


def _split_job(path, jobs: tuple[str, ...]) -> tuple[str | None, list]:
    """Split a job-prefixed node path into its job id and the workflow-relative path
    the document carries (SPEC §6.11).

    Two paths are left alone: any path at all when `jobs` is empty (a single-workflow
    plan prefixes nothing), and the **empty** path, which is the interface side of a
    boundary arc (§6.8) and belongs to no job.
    """
    if not jobs or not path:
        return None, list(path)
    job = path[0]
    # The prefix was put there by `instance.prefix_instance` from this same job list.
    # Anything else means the two have drifted, and silently shipping a mangled `node`
    # would be far worse than stopping: a replan matches activities by that path.
    assert job in jobs, f"node path {list(path)!r} does not start with a known job id"
    return job, list(path[1:])


def _set_job(entry: dict, job: str | None) -> None:
    """Record which job an activity belongs to, right after `kind` so it reads as
    part of the activity's identity. Omitted entirely on a single-workflow plan
    (`job` is None there), which is what keeps such a plan unchanged.

    A replenishment never gets one: the scheduler decided to run it, and in a joint
    plan a single refill commonly serves several jobs (§6.9, §6.11)."""
    if job is not None:
        entry["job"] = job


def _arc_job(arc, jobs: tuple[str, ...]) -> str | None:
    """The job an arc belongs to. A boundary arc has one empty-path endpoint (§6.8),
    so the job is read off whichever end names a real node."""
    return _split_job(arc.src.node, jobs)[0] or _split_job(arc.dst.node, jobs)[0]


def _arc(arc, jobs: tuple[str, ...] = ()) -> dict:
    """Render an Arc as the document's `{from, to}` provenance, with the job prefix
    split off both endpoints (it is carried once, by the activity's `job`)."""
    return {
        "from": {"node": _split_job(arc.src.node, jobs)[1], "port": arc.src.port},
        "to": {"node": _split_job(arc.dst.node, jobs)[1], "port": arc.dst.port},
    }


def _arc_key(arc: dict) -> tuple:
    """The identity of a rendered `{from, to}` arc, for pairing legs and relays."""
    f, t = arc["from"], arc["to"]
    return (tuple(f["node"]), f["port"], tuple(t["node"]), t["port"])


def _fold_relayed_zero_distance(activities: list[dict]) -> list[dict]:
    """Drop each relay together with the zero-distance transport leg it feeds.

    When a real leg delivers an Object to a spot and the destination then consumes
    at that *same* spot, the departing leg is a physical no-op (`from_spot ==
    to_spot`, `start == end`) sitting behind a relay (§4.5): the Object never
    moves. The relay + no-op leg carry no information — the real leg already
    delivers where the destination reads — so both are elided (SPEC §6.4.1 / §7).
    The plan stays valid with the same makespan, and it round-trips: a replan
    regenerates the relay and re-transport from the surviving committed leg.

    A single-leg same-spot transport (no preceding relay, so `seq` is absent) is
    kept: there is no committed leg to reconstruct it from on a replan, so eliding
    it would not round-trip. It carries no `transporter` (rendered above), which is
    what marks it as a no-op in the output.
    """
    # `job` is part of the pairing key, not decoration: the rendered `arc` has its job
    # prefix split off (§6.11), so two jobs running the same workflow render the same
    # arc, and without the job a leg could pair with the other job's relay.
    relay_at: dict[tuple, int] = {}
    for i, a in enumerate(activities):
        if a["kind"] == "relay":
            relay_at[(a.get("job"), _arc_key(a["arc"]), a["seq"])] = i
    drop: set[int] = set()
    for i, a in enumerate(activities):
        if a["kind"] != "transport" or a["from_spot"] != a["to_spot"] or a["start"] != a["end"]:
            continue
        seq = a.get("seq")
        if seq is None:
            continue  # standalone same-spot hop: no relay to pair with, so kept
        j = relay_at.get((a.get("job"), _arc_key(a["arc"]), seq - 1))
        if j is not None:
            drop.add(i)
            drop.add(j)
    return [a for i, a in enumerate(activities) if i not in drop]


def to_yaml(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
