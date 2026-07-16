"""Standard output normalization of zero-distance transports (SPEC §6.4 / §6.4.1).

Two related behaviours, both always on (not optional):

- A relay together with the zero-distance re-transport it feeds (an Object that
  stays where the previous real leg delivered it) is folded out of the output.
- A kept same-spot transport (`from_spot == to_spot`) carries no `transporter`,
  since no transporter performs a physical no-op.

The unit tests exercise the render-level fold directly; the rest go through the
public API to confirm the behaviour end to end and that it round-trips.
"""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule import schedule, validate_document
from ofplang.schedule.scheduler.plan import _fold_relayed_zero_distance, to_yaml

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

_ARC = {"from": {"node": ["A"], "port": "o"}, "to": {"node": ["B"], "port": "i"}}


def _leg(from_spot, to_spot, *, seq=None, start=0, end=0):
    e = {"kind": "transport", "start": start, "end": end, "from_spot": from_spot, "to_spot": to_spot, "arc": _ARC}
    if seq is not None:
        e["seq"] = seq
    return e


def _relay(spot, seq, *, at=0):
    return {"kind": "relay", "start": at, "end": at, "arc": _ARC, "seq": seq, "spot": spot}


# --- render-level fold -----------------------------------------------------


def test_fold_drops_stay_put_relay_and_its_zero_distance_leg():
    acts = [
        _leg("W.a", "X.c", seq=0, start=0, end=3),   # real leg, arrives X.c
        _relay("X.c", 1, at=3),                        # junction at X.c
        _leg("X.c", "X.c", seq=2, start=5, end=5),   # zero-distance re-transport
    ]
    folded = _fold_relayed_zero_distance(acts)
    assert folded == [acts[0]]  # only the real leg survives


def test_fold_keeps_a_relay_between_two_real_moves():
    acts = [
        _leg("W.a", "X.c", seq=0, start=0, end=3),
        _relay("X.c", 1, at=3),
        _leg("X.c", "Y.c", seq=2, start=5, end=9),   # real re-transport, not a no-op
    ]
    assert _fold_relayed_zero_distance(acts) == acts  # nothing folded


def test_fold_keeps_a_standalone_same_spot_transport():
    # No preceding relay (single-leg, so `seq` is absent): there is no committed
    # leg to reconstruct it from on a replan, so it must stay.
    acts = [_leg("X.c", "X.c", start=4, end=4)]
    assert _fold_relayed_zero_distance(acts) == acts


def test_fold_leaves_a_zero_distance_leg_without_a_matching_relay():
    # Defensive: a same-spot leg with a seq but no relay at seq-1 is not folded.
    acts = [_leg("X.c", "X.c", seq=2, start=5, end=5), _relay("X.c", 5, at=5)]
    assert _fold_relayed_zero_distance(acts) == acts


# --- document validator: transporter is optional only for a same-spot move --


def _doc(tmp_path, transport_line):
    text = f"""time: {{ unit: second }}
outcome: optimal
objective: {{ kind: makespan, value: 0 }}
activities:
- kind: transport
  start: 0
  end: 0
{transport_line}
  arc: {{ from: {{ node: [A], port: o }}, to: {{ node: [B], port: i }} }}
"""
    p = tmp_path / "doc.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_same_spot_transport_may_omit_transporter(tmp_path):
    doc = _doc(tmp_path, "  from_spot: d.c\n  to_spot: d.c")
    assert validate_document(doc).ok


def test_real_move_still_requires_transporter(tmp_path):
    doc = _doc(tmp_path, "  from_spot: d.c\n  to_spot: e.c")
    result = validate_document(doc)
    assert not result.ok
    assert any(d.code == "missing_required_field" for d in result.diagnostics)


# --- end to end ------------------------------------------------------------


def test_reformatter_keeps_same_spot_hops_without_a_transporter():
    # The reformatter routes two handoffs through its shared rf_link buffer, so
    # those transports are same-spot (single-leg): kept, but without a transporter.
    report = schedule(EXAMPLES / "reformatter.workflow.yaml", EXAMPLES / "reformatter.env.yaml")
    assert report.ok
    same_spot = [
        a for a in report.plan["activities"]
        if a["kind"] == "transport" and a["from_spot"] == a["to_spot"]
    ]
    assert len(same_spot) == 2
    assert all("transporter" not in a and "seq" not in a for a in same_spot)


_SAME_SPOT_ENV = """time: {unit: second}
devices:
  - { id: station_0, spots: [core] }
transporters: [ { id: transport } ]
transports: []
processes:
  source: { modes: [ { devices: [station_0], duration: 2, output_spots: { source_out: station_0.core } } ] }
  target: { modes: [ { devices: [station_0], duration: 2, input_spots: { target_in: station_0.core } } ] }
"""

# Source done; the same-spot hop to the target has not started. `now` = 2.
_SAME_SPOT_STATUS = """time: {unit: second}
now: 2
activities:
- { kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], devices: [station_0], output_spots: { source_out: station_0.core } }
"""


def test_same_spot_replan_keeps_hop_without_transporter_and_round_trips(tmp_path):
    env = tmp_path / "env.yaml"
    env.write_text(_SAME_SPOT_ENV, encoding="utf-8")
    status = tmp_path / "s.yaml"
    status.write_text(_SAME_SPOT_STATUS, encoding="utf-8")

    report = schedule(EXAMPLES / "simple.workflow.yaml", env, document_path=status)
    assert report.ok
    legs = [a for a in report.plan["activities"] if a["kind"] == "transport"]
    assert len(legs) == 1  # the stay-put relay + any extra leg are folded
    assert legs[0]["from_spot"] == legs[0]["to_spot"] == "station_0.core"
    assert "transporter" not in legs[0]  # a no-op carries no transporter

    fed = tmp_path / "fed.yaml"
    fed.write_text(to_yaml(report.plan), encoding="utf-8")
    assert validate_document(fed).ok  # the transporter-less hop validates on read-back
    second = schedule(EXAMPLES / "simple.workflow.yaml", env, document_path=fed)
    assert second.ok and second.makespan == report.makespan
