"""Moving one arc's Object in more than one leg (SPEC §4.5 / §6.4.1).

An arc whose endpoints are further apart than a single move is carried through
**relays**: a device the transporter reaches at one position only, a plate that has
to cross a hand-off station. How many legs are allowed is a setting, and it is
**1 by default** -- which is exactly what the scheduler has always planned, so
raising it is the only way to see any of this.

Two rules shape what is offered:

* only routes of the **fewest possible moves**, per endpoint spot pair. A pair that
  can be served directly is never sent round by way of somewhere else, whatever it
  would cost.
* a chain's length is the model's shape, so it cannot depend on which mode the
  solver picks. Where one mode pair needs two moves and another needs one, the
  shorter is served by the real move plus a same-spot no-op -- which §6.4.1 folds
  out of the output, leaving the single leg it always was.
"""

from __future__ import annotations

from ofplang.schedule import schedule
from tests.schedutil import SIMPLE_WF, kinds, write

# `source` may put the sample on either of the filler's two spots. From `a` the
# reader is one move away; from `b` it is two, by way of the hotel. The `a -> hotel`
# entry is the trap: it would let `a` reach the reader in 1 + 5 rather than 10, which
# is cheaper and forbidden -- `a` is already one move from the reader.
MIXED_ENV = """
time: { unit: second }
devices:
  - { id: filler, spots: [a, b] }
  - { id: hotel,  spots: [slot] }
  - { id: reader, spots: [stage] }
transporters: [ { id: arm } ]
transports:
  - { transporter: arm, from: filler.a,   to: reader.stage, duration: 10 }
  - { transporter: arm, from: filler.b,   to: hotel.slot,   duration: 5 }
  - { transporter: arm, from: hotel.slot, to: reader.stage, duration: 5 }
  - { transporter: arm, from: filler.a,   to: hotel.slot,   duration: 1 }
processes:
  source:
    modes:
      - { id: at_a, devices: [filler], duration: 2, output_spots: { source_out: filler.a } }
      - { id: at_b, devices: [filler], duration: 2, output_spots: { source_out: filler.b } }
  target:
    modes:
      - { id: read, devices: [reader], duration: 2, input_spots: { target_in: reader.stage } }
"""

# The door of a machine the arm can reach, and a block it cannot: the plate is put on
# the door and the machine shifts it inwards itself (a route with no transporter).
DOOR_ENV = """
time: { unit: second }
devices:
  - { id: bench, spots: [slot] }
  - { id: X,     spots: [door, block] }
transporters: [ { id: arm } ]
transports:
  - { transporter: arm,  from: bench.slot, to: X.door,  duration: 3 }
  - { transporter: null, from: X.door,     to: X.block, duration: 5 }
processes:
  source:
    modes:
      - { devices: [bench], duration: 2, output_spots: { source_out: bench.slot } }
  target:
    modes:
      - { devices: [X], duration: 2, input_spots: { target_in: X.block } }
"""


def _plan(tmp_path, env: str, legs: int = 1, name: str = "env.yaml"):
    return schedule(SIMPLE_WF, write(tmp_path, name, env), max_transport_legs=legs, random_seed=0)


# -- the default is one leg, which is what it always was ----------------------


def test_one_leg_is_the_default_and_refuses_what_it_always_refused(tmp_path):
    """The door's block is two moves away, so by default the arc has no route."""
    report = _plan(tmp_path, DOOR_ENV)
    assert not report.ok
    assert [d.code for d in report.diagnostics] == ["arc_unreachable"]


def test_one_leg_offers_only_the_pair_that_is_one_move_apart(tmp_path):
    report = _plan(tmp_path, MIXED_ENV)
    assert report.outcome == "optimal"
    assert report.makespan == 14  # 2 + 10 + 2, by way of `a`
    (leg,) = kinds(report.plan, "transport")
    assert (leg["from_spot"], leg["to_spot"]) == ("filler.a", "reader.stage")
    assert not kinds(report.plan, "relay")
    assert "seq" not in leg


# -- two legs: what one leg could not reach ------------------------------------


def test_two_legs_reach_a_spot_the_transporter_cannot(tmp_path):
    """The arm delivers to the door; the machine takes it in from there itself."""
    report = _plan(tmp_path, DOOR_ENV, legs=2)
    assert report.outcome == "optimal"
    assert report.makespan == 12  # 2 + 3 + 5 + 2, serial on the one machine

    first, second = kinds(report.plan, "transport")
    (relay,) = kinds(report.plan, "relay")
    assert (first["from_spot"], first["to_spot"]) == ("bench.slot", "X.door")
    assert (second["from_spot"], second["to_spot"]) == ("X.door", "X.block")
    assert relay["spot"] == "X.door"
    # A per-arc chain ordinal, legs on the even positions and the junction between
    # them (§6.6) -- the same numbering a re-routed move has always used.
    assert (first["seq"], relay["seq"], second["seq"]) == (0, 1, 2)
    # The second leg is the machine's own move, which no transporter carries (§5.4).
    assert first["transporter"] == "arm"
    assert second["transporter"] is None
    # Ordered through the junction, which is instantaneous.
    assert first["end"] <= relay["start"] == relay["end"] <= second["start"]


def test_two_legs_never_send_a_direct_pair_round(tmp_path):
    """`a -> hotel -> stage` costs 6 and `a -> stage` costs 10, and 10 is what is used.

    Cheaper is not the question: `a` is one move from the reader, so a two-move route
    from `a` is not among the routes that exist for it. Offered by spot alone -- the
    hotel being a candidate for `b`, and one move from `a` -- it would be, and the
    solver would take it.
    """
    report = _plan(tmp_path, MIXED_ENV, legs=2)
    assert report.outcome == "optimal"
    assert report.makespan == 14  # not 10: the 1 + 5 detour is not offered at all
    spots = [(a["from_spot"], a["to_spot"]) for a in kinds(report.plan, "transport")]
    assert ("filler.a", "hotel.slot") not in spots


def test_a_pair_that_is_one_move_apart_still_renders_as_one_leg(tmp_path):
    """Inside a two-leg chain, the direct pair is the real move plus a no-op.

    The fold (§6.4.1) takes that pair out, and what is left is the single leg the
    same plan has always shown -- `seq` included, which is to say absent.
    """
    # `b` made slow, so the direct route from `a` wins and is the one taken.
    slow = MIXED_ENV.replace("to: hotel.slot,   duration: 5", "to: hotel.slot,   duration: 20")
    one = _plan(tmp_path, slow, legs=1, name="one.yaml")
    two = _plan(tmp_path, slow, legs=2, name="two.yaml")
    assert one.outcome == two.outcome == "optimal"
    assert one.makespan == two.makespan == 14
    # Same plan, same document: no relay, no `seq`, nothing to say a chain was built.
    assert not kinds(two.plan, "relay")
    (leg,) = kinds(two.plan, "transport")
    assert "seq" not in leg
    assert one.plan["activities"] == two.plan["activities"]


def test_beyond_the_cap_is_still_unreachable(tmp_path):
    """Two moves offered, three needed: refused, and by the code it always was."""
    env = """
time: { unit: second }
devices:
  - { id: s0, spots: [core] }
  - { id: s1, spots: [core] }
  - { id: s2, spots: [core] }
  - { id: s3, spots: [core] }
transporters: [ { id: arm } ]
transports:
  - { transporter: arm, from: s0.core, to: s1.core, duration: 1 }
  - { transporter: arm, from: s1.core, to: s2.core, duration: 1 }
  - { transporter: arm, from: s2.core, to: s3.core, duration: 1 }
processes:
  source:
    modes: [ { devices: [s0], duration: 2, output_spots: { source_out: s0.core } } ]
  target:
    modes: [ { devices: [s3], duration: 2, input_spots: { target_in: s3.core } } ]
"""
    assert [d.code for d in _plan(tmp_path, env, legs=2).diagnostics] == ["arc_unreachable"]
    # Three is enough, and the chain is three legs and two junctions.
    report = _plan(tmp_path, env, legs=3, name="three.yaml")
    assert report.outcome == "optimal"
    assert len(kinds(report.plan, "transport")) == 3
    assert [r["seq"] for r in kinds(report.plan, "relay")] == [1, 3]


def test_a_cap_below_one_is_a_usage_error():
    """A move takes at least one transport activity."""
    from ofplang.schedule.cli import main

    assert main(["schedule", str(SIMPLE_WF), "--env", str(SIMPLE_WF),
                 "--max-transport-legs", "0"]) == 2


# -- what a chain looks like read back on a replan ------------------------------


def test_a_planned_chain_replans_from_its_first_leg(tmp_path):
    """The document round-trips: the committed leg rebuilds the junction and the rest.

    A chain planned this solve is history by the next one, and it is read back the
    way a re-routed move always has been -- matched by its arc and `seq` (§6.6), the
    relay derived from where the leg arrived, the remainder planned from there.
    """
    env = write(tmp_path, "env.yaml", DOOR_ENV)
    first = schedule(SIMPLE_WF, env, max_transport_legs=2, random_seed=0)
    assert first.outcome == "optimal"

    # The arm has delivered to the door; the machine has not taken it in yet.
    document = {"now": 5, "activities": []}
    for a in first.plan["activities"]:
        if a["kind"] == "relay":
            continue  # regenerated from the committed leg
        if a["end"] <= 5:
            document["activities"].append({**a, "status": "completed"})
    again = schedule(SIMPLE_WF, env, document_path=document,
                     max_transport_legs=2, random_seed=0)
    assert again.outcome == "optimal", [d.code for d in again.diagnostics]

    legs = kinds(again.plan, "transport")
    (relay,) = kinds(again.plan, "relay")
    assert [leg["seq"] for leg in legs] == [0, 2]
    assert legs[0]["status"] == "completed"  # history keeps the position it was given
    assert legs[1].get("status") is None
    assert relay["spot"] == "X.door" and relay["seq"] == 1
