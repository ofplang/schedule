"""Render a solved instance as an execution document (SPECIFICATIONS.md §6).

A plan is action-first: each activity's main fields say what is actually done,
with the workflow provenance (`node` / `arc`) carried alongside. On an initial
plan every activity is pending, so `status`, `now`, and `placements` are omitted.
On a replan the same document carries the full timeline: completed/running
activities keep a `status` (§6.2), `now` is echoed, and `placements` are carried
through verbatim, so the output re-optimises the future while showing the fixed
history — and round-trips as the next replan input.
"""

from __future__ import annotations

import yaml

from ofplang.schedule.scheduler.cpsat import Solution
from ofplang.schedule.scheduler.instance import Instance


def render_plan(
    instance: Instance,
    solution: Solution,
    *,
    workflow: str | None = None,
    environment: str | None = None,
    status: str | None = None,
    now: int | None = None,
    placements: list | None = None,
) -> dict:
    """Build the execution-document dict for `solution`."""
    activities: list[dict] = []

    for p in solution.processing:
        if p.relay is not None:
            # A relay junction (§6.4.1): identity is its arc + seq + spot, not a
            # workflow node.
            entry = {"kind": "relay"}
            if p.status is not None:
                entry["status"] = p.status
            entry.update({"start": p.start, "end": p.end, "seq": p.relay.seq, "spot": p.relay.spot, "arc": _arc(p.relay.arc)})
            activities.append(entry)
            continue

        entry = {"kind": "processing"}
        # A fixed activity keeps its status (§6.2); pending activities omit it.
        if p.status is not None:
            entry["status"] = p.status
        entry.update(
            {
                "start": p.start,
                "end": p.end,
                "process": p.process,
                "mode": p.mode.id,
                "node": list(p.node),
            }
        )
        # Derivable echo of the selected mode (§6.3); omit when empty.
        if p.mode.devices:
            entry["devices"] = list(p.mode.devices)
        if p.mode.input_spots:
            entry["input_spots"] = dict(p.mode.input_spots)
        if p.mode.output_spots:
            entry["output_spots"] = dict(p.mode.output_spots)
        activities.append(entry)

    for t in solution.transport:
        entry = {"kind": "transport"}
        if t.status is not None:
            entry["status"] = t.status
        entry.update(
            {
                "start": t.start,
                "end": t.end,
                "from_spot": t.option.from_spot,
                "to_spot": t.option.to_spot,
                "transporter": t.option.transporter,
                "arc": _arc(t.arc),
            }
        )
        # Chain position on a multi-leg move (§6.6); omit for a single-leg transport.
        if t.seq is not None:
            entry["seq"] = t.seq
        activities.append(entry)

    # A stable, readable order: by time, then processing before transport.
    activities.sort(key=lambda a: (a["start"], a["end"], a["kind"]))

    doc: dict = {"time": {"unit": instance.time_unit}}
    if now is not None:
        doc["now"] = now
    doc["outcome"] = solution.outcome
    doc["objective"] = {"kind": "makespan", "value": solution.makespan}
    doc["activities"] = activities
    if placements:
        doc["placements"] = placements
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


def _arc(arc) -> dict:
    """Render an Arc as the document's `{from, to}` provenance."""
    return {
        "from": {"node": list(arc.src.node), "port": arc.src.port},
        "to": {"node": list(arc.dst.node), "port": arc.dst.port},
    }


def to_yaml(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
