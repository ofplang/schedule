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
    interface: dict | None = None,
) -> dict:
    """Build the execution-document dict for `solution`."""
    activities: list[dict] = []

    for p in solution.processing:
        if p.boundary is not None:
            # A synthetic boundary node (§6.8) is not a workflow activity; it is
            # never rendered. Its boundary arc (carrying an empty-path endpoint) is
            # emitted as an ordinary transport below.
            continue
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
        entry["start"] = t.start
        entry["end"] = t.end
        entry["from_spot"] = t.option.from_spot
        entry["to_spot"] = t.option.to_spot
        # A same-spot move (§5.4) is a physical no-op: no transporter carries it,
        # so the field is omitted (§6.4). The occupied devices still derive from
        # the spots, and the route (from == to) is unambiguous without it.
        if t.option.from_spot != t.option.to_spot:
            entry["transporter"] = t.option.transporter
        entry["arc"] = _arc(t.arc)
        # Chain position on a multi-leg move (§6.6); omit for a single-leg transport.
        if t.seq is not None:
            entry["seq"] = t.seq
        activities.append(entry)

    # A stable, readable order: by time, then processing before transport.
    activities.sort(key=lambda a: (a["start"], a["end"], a["kind"]))
    # Standard output normalization: elide relay + zero-distance re-transport pairs.
    activities = _fold_relayed_zero_distance(activities)

    doc: dict = {"time": {"unit": instance.time_unit}}
    if now is not None:
        doc["now"] = now
    # The interface boundary constraint (§6.8) round-trips: echo it verbatim so the
    # plan can be fed back as the next document.
    if interface:
        doc["interface"] = interface
    doc["outcome"] = solution.outcome
    doc["objective"] = {"kind": "makespan", "value": solution.makespan}
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


def _arc(arc) -> dict:
    """Render an Arc as the document's `{from, to}` provenance."""
    return {
        "from": {"node": list(arc.src.node), "port": arc.src.port},
        "to": {"node": list(arc.dst.node), "port": arc.dst.port},
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
    relay_at: dict[tuple, int] = {}
    for i, a in enumerate(activities):
        if a["kind"] == "relay":
            relay_at[(_arc_key(a["arc"]), a["seq"])] = i
    drop: set[int] = set()
    for i, a in enumerate(activities):
        if a["kind"] != "transport" or a["from_spot"] != a["to_spot"] or a["start"] != a["end"]:
            continue
        seq = a.get("seq")
        if seq is None:
            continue  # standalone same-spot hop: no relay to pair with, so kept
        j = relay_at.get((_arc_key(a["arc"]), seq - 1))
        if j is not None:
            drop.add(i)
            drop.add(j)
    return [a for i, a in enumerate(activities) if i not in drop]


def to_yaml(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
