"""Routes that need no transporter (SPEC §4.6 / §5.4).

An environment may declare a move that no transporter performs -- a device
shifting material between its own spots, a chute, a conveyor. It is an ordinary
transport in every other respect: it takes time, it holds its source and
destination devices over `[a, b]`, and it is ordered against the activities at
either end. It simply enters no transporter's non-overlap set, so it does not
serialise against the moves an arm is making elsewhere.

The environment says so by writing `transporter: null`. The key is *required*, so
a forgotten transporter stays an error rather than quietly declaring a route that
occupies none -- that mistake being silent is the whole reason to demand the word.
"""

from __future__ import annotations

import copy

from ofplang.schedule import (
    JobInput,
    schedule,
    schedule_jobs,
    validate_document,
    validate_environment,
)
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.plan import to_yaml
from ofplang.schedule.scheduler.visualize import render_svg
from tests.schedutil import SIMPLE_WF, kinds, write

# `source` puts the sample on station_0.core, `target` reads it from
# station_0.side, and station_0 moves it between the two itself. No transporter is
# defined at all: the point is that none is needed.
INTERNAL_ENV = """
time: { unit: second }
devices:
  - { id: station_0, spots: [core, side] }
transports:
  - { transporter: null, from: station_0.core, to: station_0.side, duration: 1 }
processes:
  source:
    modes:
      - { devices: [station_0], duration: 2, output_spots: { source_out: station_0.core } }
  target:
    modes:
      - { devices: [station_0], duration: 2, input_spots: { target_in: station_0.side } }
"""

# Two machines, each able to hand off inside itself with nothing carrying the move.
TWO_INTERNAL_ENV = """
time: { unit: second }
devices:
  - { id: station_0, spots: [core, side] }
  - { id: station_1, spots: [core, side] }
transports:
  - { transporter: null, from: station_0.core, to: station_0.side, duration: 1 }
  - { transporter: null, from: station_1.core, to: station_1.side, duration: 1 }
processes:
  source:
    modes:
      - { devices: [station_0], duration: 2,
          output_spots: { source_out: station_0.core } }
      - { devices: [station_1], duration: 2,
          output_spots: { source_out: station_1.core } }
  target:
    modes:
      - { devices: [station_0], duration: 2,
          input_spots: { target_in: station_0.side } }
      - { devices: [station_1], duration: 2,
          input_spots: { target_in: station_1.side } }
"""

# The same move, but an arm could also make it -- slowly. Both routes are offered.
ARM_TOO_ENV = """
time: { unit: second }
devices:
  - { id: station_0, spots: [core, side] }
transporters: [ { id: arm } ]
transports:
  - { transporter: null, from: station_0.core, to: station_0.side, duration: 1 }
  - { transporter: arm, from: station_0.core, to: station_0.side, duration: 9 }
processes:
  source:
    modes:
      - { devices: [station_0], duration: 2, output_spots: { source_out: station_0.core } }
  target:
    modes:
      - { devices: [station_0], duration: 2, input_spots: { target_in: station_0.side } }
"""

# Source and target read the same spot: the hand-off is a physical no-op.
SAME_SPOT_ENV = """
time: { unit: second }
devices:
  - { id: station_0, spots: [core] }
processes:
  source:
    modes:
      - { devices: [station_0], duration: 2, output_spots: { source_out: station_0.core } }
  target:
    modes:
      - { devices: [station_0], duration: 2, input_spots: { target_in: station_0.core } }
"""


def test_environment_with_a_transporter_less_route_is_valid(tmp_path):
    result = validate_environment(write(tmp_path, "env.yaml", INTERNAL_ENV))
    assert result.ok, [(d.code, d.path) for d in result.errors]


def test_the_route_is_keyed_by_none(tmp_path):
    env, result = load_environment(write(tmp_path, "env.yaml", INTERNAL_ENV))
    assert result.ok, [(d.code, d.path) for d in result.errors]
    assert env.transports[(None, "station_0.core", "station_0.side")] == 1
    assert env.transporters == ()
    # The lookup is by the same None the table is keyed by.
    assert env.transport_duration(None, "station_0.core", "station_0.side") == 1
    assert env.transport_duration("nobody", "station_0.core", "station_0.side") is None


def test_plans_and_reports_a_null_transporter(tmp_path):
    report = schedule(SIMPLE_WF, write(tmp_path, "env.yaml", INTERNAL_ENV))
    assert report.outcome == "optimal"
    # 2 + 1 + 2, fully serial: the move holds station_0 exactly as any other
    # transport holds its endpoint devices (§4.5).
    assert report.makespan == 5

    (t,) = kinds(report.plan, "transport")
    assert (t["from_spot"], t["to_spot"]) == ("station_0.core", "station_0.side")
    # Written, and written as null. Omitting it would be indistinguishable from a
    # document that forgot to say which transporter carried the move.
    assert "transporter" in t and t["transporter"] is None


def _ran(plan: dict, now: int) -> dict:
    """`plan` as the status of a run that got as far as `now`: everything that had
    finished by then is fixed history, and the rest is still to come."""
    doc = copy.deepcopy(plan)
    doc["now"] = now
    for activity in doc["activities"]:
        if activity["end"] <= now:
            activity["status"] = "completed"
    return doc


def test_a_null_transporter_survives_being_read_back(tmp_path):
    """🔴 Null is a meaning, not a missing value, and it has to come back the same.

    Reading a document flattens a string field that is not a string to the empty string,
    which is right for a spot or a mode id and wrong here: `transporter: null` says
    **nothing carried this move** (§5.4), and flattened, the leg goes on to occupy a
    transporter named by the empty string -- so two such moves, fixed history in some
    later replan, contend for a machine that does not exist.

    Invisible until a caller fed a plan straight back as the next document, which is
    what a rolling run does: the move was planned null, reported null, read as `""`, and
    reported `""` from the second plan on.
    """
    env = write(tmp_path, "env.yaml", INTERNAL_ENV)
    first = schedule(SIMPLE_WF, env)
    assert first.ok

    status = write(tmp_path, "s.yaml", to_yaml(_ran(first.plan, 3)))
    second = schedule(SIMPLE_WF, env, document_path=status)
    assert second.ok, [d.code for d in second.diagnostics]
    (t,) = kinds(second.plan, "transport")
    assert "transporter" in t and t["transporter"] is None
    assert second.makespan == first.makespan


def test_two_fixed_transporter_less_moves_do_not_queue_behind_each_other(tmp_path):
    """The consequence of the above, measured. Flattened to `""`, every transporter-less
    leg in a document's history occupies one phantom machine, so two that ran at the same
    time are an overlap on it -- and the replan they are handed to is infeasible for a
    reason nothing in the laboratory can explain."""
    env = write(tmp_path, "env.yaml", TWO_INTERNAL_ENV)
    plan = schedule_jobs(
        [JobInput("job1", SIMPLE_WF), JobInput("job2", SIMPLE_WF)], env, random_seed=0
    )
    assert plan.ok, [d.code for d in plan.diagnostics]
    moves = kinds(plan.plan, "transport")
    assert len(moves) == 2
    # Each on its own machine, so they overlap -- which is the whole point.
    assert min(m["end"] for m in moves) > max(m["start"] for m in moves)

    status = write(tmp_path, "s.yaml", to_yaml(_ran(plan.plan, 3)))
    again = schedule_jobs(
        [JobInput("job1", SIMPLE_WF), JobInput("job2", SIMPLE_WF)], env,
        document_path=status, random_seed=0,
    )
    assert again.ok, [d.code for d in again.diagnostics]
    assert again.makespan == plan.makespan


def test_the_rendered_plan_is_a_valid_document(tmp_path):
    report = schedule(SIMPLE_WF, write(tmp_path, "env.yaml", INTERNAL_ENV))
    out = write(tmp_path, "plan.yaml", to_yaml(report.plan))
    result = validate_document(out)
    assert result.ok, [(d.code, d.path) for d in result.errors]


def test_it_is_offered_alongside_the_transporters_that_can_also_serve(tmp_path):
    """Both routes are enumerated for the same spot pair, and the plan picks."""
    report = schedule(SIMPLE_WF, write(tmp_path, "env.yaml", ARM_TOO_ENV))
    assert report.outcome == "optimal"
    assert report.makespan == 5  # the 1-second route, not the arm's 9
    (t,) = kinds(report.plan, "transport")
    assert t["transporter"] is None


def test_a_same_spot_move_still_omits_the_key(tmp_path):
    """The no-op hand-off is unchanged: it omits `transporter` entirely (§6.4).

    Two spellings of "nothing carries this" would be one too many, so the split is
    by what the reader can derive: a same-spot move is visibly a no-op, while a
    real move needs the field to say it.
    """
    report = schedule(SIMPLE_WF, write(tmp_path, "env.yaml", SAME_SPOT_ENV))
    assert report.outcome == "optimal"
    (t,) = kinds(report.plan, "transport")
    assert t["from_spot"] == t["to_spot"]
    assert "transporter" not in t


def test_the_chart_draws_the_move_on_its_own_devices(tmp_path):
    """With no transporter lane to carry it, the endpoints are the move itself.

    Ghosting them would leave the move drawn nowhere at all.
    """
    report = schedule(SIMPLE_WF, write(tmp_path, "env.yaml", INTERNAL_ENV))
    # The "auto" theme names roles as CSS classes, which is what makes the two
    # kinds of bar tellable apart in the markup at all.
    svg = render_svg(report.plan, view="device", theme="auto")
    # The class must not be *used* by a bar; the stylesheet still defines it.
    assert 'class="bar xfer-ghost"' not in svg
    assert 'class="bar xfer"' in svg
    # And the bar it draws carries the move's label, so it is readable on the lane
    # rather than being an unexplained block (the ghost bars carry none).
    assert 'class="barlabel xfer"' in svg


# --- what the schema validators make of the key -----------------------------

_ENV_HEAD = """
time: { unit: second }
devices:
  - { id: station_0, spots: [core, side] }
processes:
  noop:
    modes:
      - { devices: [station_0], duration: 1, input_spots: { i: station_0.core } }
transports:
"""

_NO_KEY = "  - { from: station_0.core, to: station_0.side, duration: 1 }"

_NOT_A_STRING = "  - { transporter: 7, from: station_0.core, to: station_0.side, duration: 1 }"

_TWO_NULL_ROUTES = """
  - { transporter: null, from: station_0.core, to: station_0.side, duration: 1 }
  - { transporter: null, from: station_0.core, to: station_0.side, duration: 2 }
"""

_NULL_AND_CARRIED = """
  - { transporter: null, from: station_0.core, to: station_0.side, duration: 1 }
  - { transporter: arm, from: station_0.core, to: station_0.side, duration: 9 }
"""


def _env_check(tmp_path, transports: str):
    return validate_environment(write(tmp_path, "env.yaml", _ENV_HEAD + transports))


def test_env_a_missing_transporter_key_is_still_an_error(tmp_path):
    """The point of writing null: forgetting the key cannot mean the same thing.

    Were absence itself to say "needs none", a typo would declare a route that
    occupies no transporter, and the arm it meant to name would stay free in every
    plan made from this environment -- silently, and in the lab.
    """
    result = _env_check(tmp_path, _NO_KEY)
    assert [d.code for d in result.errors] == ["missing_required_field"]


def test_env_a_non_string_transporter_is_still_an_error(tmp_path):
    result = _env_check(tmp_path, _NOT_A_STRING)
    assert [d.code for d in result.errors] == ["wrong_type"]


def test_env_two_identical_transporter_less_routes_are_a_duplicate(tmp_path):
    """A null transporter is a value in the duplicate key like any other.

    Read as "absent" it would drop out of the check, and the pair would go
    unreported -- which is the one case the check exists for.
    """
    result = _env_check(tmp_path, _TWO_NULL_ROUTES)
    assert [d.code for d in result.errors] == ["duplicate_transport_entry"]


def test_env_a_named_transporter_is_still_resolved(tmp_path):
    """Admitting null must not stop the named case being checked."""
    result = _env_check(tmp_path, _NULL_AND_CARRIED)
    assert [d.code for d in result.errors] == ["unknown_transporter"]  # `arm` is undeclared


_DOC_NULL = """
time: { unit: second }
activities:
- kind: transport
  start: 0
  end: 1
  from_spot: station_0.core
  to_spot: station_0.side
  transporter: null
  arc:
    from: { node: [A], port: o }
    to: { node: [B], port: i }
"""

_DOC_NO_KEY = """
time: { unit: second }
activities:
- kind: transport
  start: 0
  end: 1
  from_spot: station_0.core
  to_spot: station_0.side
  arc:
    from: { node: [A], port: o }
    to: { node: [B], port: i }
"""


def test_doc_a_null_transporter_is_accepted_on_a_real_move(tmp_path):
    result = validate_document(write(tmp_path, "d.yaml", _DOC_NULL))
    assert result.ok, [(d.code, d.path) for d in result.errors]


def test_doc_a_missing_transporter_is_still_an_error_on_a_real_move(tmp_path):
    result = validate_document(write(tmp_path, "d.yaml", _DOC_NO_KEY))
    assert [d.code for d in result.errors] == ["missing_required_field"]
