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

from ofplang.schedule import schedule, validate_document, validate_environment
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
