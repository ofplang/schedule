"""Collapsing a class of interchangeable transporters (SPEC §10.4, FORMULATION
CP-SAT notes).

Arms that can make the same moves in the same times are told apart by nothing the
schedule can see, so the model encodes **one** route between them and a cumulative
of the class's size, and which arm makes each move is decided after the solve.

What these tests have to pin is that the two encodings agree. The optimum must not
move, the plan must still name a real arm for every move, and no two moves may end
up on one arm -- the last being the thing a capacity resource does not check for
you. And it must not fire where the arms differ, or where a zero-duration route
makes a capacity resource weaker than a non-overlap.
"""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule import schedule
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import build_instance
from ofplang.schedule.scheduler.symmetry import (
    aggregatable_transporters,
    interchangeable_classes,
)
from ofplang.schedule.scheduler.workflow import parse_workflow
from tests.schedutil import write

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CODE = "interchangeable_resources"

# Two independent chains, each on devices of its own, so that **the arms are the
# only thing they share**. Four moves of ten; one arm serialises them all, two do
# not. Anything else in common would make the makespan say something other than
# what the arms did.
WORKFLOW = """
spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  make: { kind: atomic, outputs: { o: { type: Sample, phase: data } },
          objects: { create: [outputs.o] } }
  hold:
    kind: atomic
    behavior: [object_identity_map]
    inputs:  { i: { type: Sample, phase: data } }
    outputs: { o: { type: Sample, phase: data } }
  drop: { kind: atomic, inputs: { i: { type: Sample, phase: data } },
          objects: { consume: [inputs.i] } }
  main:
    kind: composite
    inputs: {}
    outputs: {}
    body:
      nodes:
        - { id: M1, process: make }
        - { id: M2, process: make }
        - { id: H1, process: hold, state: { i: { from: M1.o } } }
        - { id: H2, process: hold, state: { i: { from: M2.o } } }
        - { id: D1, process: drop, state: { i: { from: H1.o } } }
        - { id: D2, process: drop, state: { i: { from: H2.o } } }
      returns: {}
entry: main
"""

CHAINS = (1, 2)


def env_text(arms: int, *, duration: int = 10, speeds: tuple[int, ...] = ()) -> str:
    """A laboratory with `arms` arms, and one bench / shelf / bin per chain.

    `speeds` gives the arms different durations, which is how the negative cases
    make them tell apart."""
    per_arm = speeds or (duration,) * arms
    names = [f"arm_{k}" for k in range(1, arms + 1)]

    devices = "\n".join(
        f"  - {{ id: {kind}_{c}, spots: [only] }}"
        for c in CHAINS
        for kind in ("bench", "shelf", "bin")
    )
    transports = "\n".join(
        f"  - {{ transporter: {arm}, from: {frm}_{c}.only, to: {to}_{c}.only, "
        f"duration: {d} }}"
        for arm, d in zip(names, per_arm, strict=True)
        for c in CHAINS
        for frm, to in (("bench", "shelf"), ("shelf", "bin"))
    )

    def modes(kind: str, dur: int, side: str) -> str:
        port = "o" if side == "output_spots" else "i"
        return "\n".join(
            f"      - {{ devices: [{kind}_{c}], duration: {dur}, "
            f"{side}: {{ {port}: {kind}_{c}.only }} }}"
            for c in CHAINS
        )

    hold_modes = "\n".join(
        f"      - {{ devices: [shelf_{c}], duration: 1, "
        f"input_spots: {{ i: shelf_{c}.only }}, output_spots: {{ o: shelf_{c}.only }} }}"
        for c in CHAINS
    )
    arms_line = ", ".join(f"{{ id: {n} }}" for n in names)
    return (
        "time: { unit: second }\n"
        f"devices:\n{devices}\n"
        f"transporters: [ {arms_line} ]\n"
        f"transports:\n{transports}\n"
        "processes:\n"
        f"  make:\n    modes:\n{modes('bench', 1, 'output_spots')}\n"
        f"  hold:\n    modes:\n{hold_modes}\n"
        f"  drop:\n    modes:\n{modes('bin', 1, 'input_spots')}\n"
    )


def _instance(tmp_path, text: str):
    env_path = write(tmp_path, "env.yaml", text)
    wf_path = write(tmp_path, "wf.yaml", WORKFLOW)
    wf, _ = parse_workflow(wf_path)
    env, _ = load_environment(env_path)
    instance, _ = build_instance(wf, env)
    assert instance is not None
    return instance, wf_path, env_path


def _plan(tmp_path, text: str):
    _, wf_path, env_path = _instance(tmp_path, text)
    return schedule(wf_path, env_path)


def _moves(report):
    return [a for a in report.plan["activities"] if a["kind"] == "transport"]


# --- the decision --------------------------------------------------------


def test_identical_arms_are_collapsed(tmp_path):
    instance, _, _ = _instance(tmp_path, env_text(3))
    pools = aggregatable_transporters(instance, interchangeable_classes(instance))
    assert pools == {
        "arm_1": ("arm_1", 3),
        "arm_2": ("arm_1", 3),
        "arm_3": ("arm_1", 3),
    }


def test_arms_of_different_speed_are_not_collapsed(tmp_path):
    instance, _, _ = _instance(tmp_path, env_text(2, speeds=(10, 11)))
    assert aggregatable_transporters(instance, interchangeable_classes(instance)) == {}


def test_a_zero_duration_route_stops_the_class_being_collapsed(tmp_path):
    # §5.4 lets a transport take no time and still name an arm. A zero-length
    # interval is the one place a capacity resource is weaker than a non-overlap
    # (`NoOverlap` refuses a point strictly inside another interval; counting
    # concurrent demand does not), so such a class is left as it was -- still
    # reported, just not collapsed.
    #
    # 🔴 This is not a corner case. A **same-spot** route is zero-duration for
    # every transporter and for none alike (§5.4, `transport_duration`), so an
    # instance with any same-spot hand-off gets one such route per arm -- and that
    # is every real laboratory measured so far (`reformatter_2t` and the RNA-seq
    # case studies are all blocked by exactly this, on a single arc each). Whether
    # a no-op should occupy an arm at all is a separate question; see
    # dev-notes/report-model-size-and-presolve.md §17.
    instance, _, _ = _instance(tmp_path, env_text(2, duration=0))
    classes = interchangeable_classes(instance)
    assert [c.scope for c in classes if c.scope == "transporter"] == ["transporter"]
    assert aggregatable_transporters(instance, classes) == {}


# --- the model it produces -----------------------------------------------


def test_collapsing_shrinks_the_model_and_keeps_the_optimum(tmp_path):
    # The same laboratory with the arms told apart by a hair, so that the two
    # encodings can be compared on the same shape. `speeds=(4, 4)` is the same
    # laboratory as `(4, 5)` except that its arms are interchangeable.
    collapsed = _plan(tmp_path, env_text(2))
    assert collapsed.plan is not None, [d.code for d in collapsed.diagnostics]
    alone = _plan(tmp_path, env_text(1))
    # The laboratory offers twice the routes, and the model is given the same
    # number as the one-arm laboratory: one per (mode pair, spots) group.
    assert collapsed.stats.model.transport_options == 2 * alone.stats.model.transport_options
    assert (
        collapsed.stats.model.encoded_transport_options
        == alone.stats.model.encoded_transport_options
    )
    assert collapsed.stats.model.variables == alone.stats.model.variables
    # And two arms really do beat one on this shape, so the capacity is doing work
    # rather than being a bound nothing reaches.
    assert collapsed.makespan < alone.makespan


def test_every_move_names_a_real_arm(tmp_path):
    report = _plan(tmp_path, env_text(3))
    arms = {f"arm_{k}" for k in (1, 2, 3)}
    named = {move["transporter"] for move in _moves(report)}
    assert named
    assert named <= arms


def test_no_two_moves_share_an_arm_at_the_same_time(tmp_path):
    # The thing a capacity resource does not check for you. Four moves, three arms.
    report = _plan(tmp_path, env_text(3))
    by_arm: dict[str, list[tuple[int, int]]] = {}
    for move in _moves(report):
        by_arm.setdefault(move["transporter"], []).append((move["start"], move["end"]))
    for arm, spans in by_arm.items():
        spans.sort()
        for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
            assert end <= start, f"{arm} holds two moves at once: {spans}"


def test_collapsing_does_not_change_what_the_solver_is_asked_for(tmp_path):
    # Two arms, and a third that is slower: the class is {arm_1, arm_2} and arm_3
    # keeps its own non-overlap. The optimum must be the one the whole laboratory
    # allows, not the one the class allows.
    mixed = _plan(tmp_path, env_text(3, speeds=(10, 10, 1)))
    assert mixed.plan is not None, [d.code for d in mixed.diagnostics]
    # The fast arm is the one worth using, so the plan has to be free to use it.
    assert "arm_3" in {move["transporter"] for move in _moves(mixed)}


# --- what it is reported as ----------------------------------------------


def test_a_collapsed_class_is_not_reported_as_a_cost(tmp_path):
    # The diagnostic says what the model is paying for. A collapsed class is not
    # paying, so it says nothing -- the detector still finds it.
    _, wf_path, env_path = _instance(tmp_path, env_text(3))
    report = schedule(wf_path, env_path)
    assert [d.code for d in report.diagnostics if d.code == CODE] == []
    instance, _, _ = _instance(tmp_path, env_text(3))
    assert any(c.scope == "transporter" for c in interchangeable_classes(instance))


def test_a_class_that_cannot_be_collapsed_is_still_reported(tmp_path):
    report = _plan(tmp_path, env_text(2, duration=0))
    found = [d for d in report.diagnostics if d.code == CODE]
    assert any("transporter" in d.message for d in found)


def test_the_two_arms_example_is_untouched():
    # Deliberately asymmetric arms: no class, so nothing is collapsed and the plan
    # is what it always was.
    wf, _ = parse_workflow(EXAMPLES / "two_arms.workflow.yaml")
    env, _ = load_environment(EXAMPLES / "two_arms.env.yaml")
    instance, _ = build_instance(wf, env)
    assert instance is not None
    assert aggregatable_transporters(instance, interchangeable_classes(instance)) == {}


# --- history ------------------------------------------------------------


def test_an_arm_holding_a_committed_move_is_not_in_a_class(tmp_path):
    # A replan where arm_2 has already made a move. It is no longer interchangeable
    # with the idle arms -- a fact cannot be relabelled -- so the class is gone and
    # nothing is collapsed.
    text = env_text(2)
    instance, wf_path, env_path = _instance(tmp_path, text)
    assert aggregatable_transporters(instance, interchangeable_classes(instance))

    status = write(
        tmp_path,
        "status.yaml",
        "time: { unit: second }\n"
        "now: 12\n"
        "activities:\n"
        "- { kind: processing, status: completed, start: 0, end: 1, process: make,\n"
        "    mode: '0', node: [M1], output_spots: { o: bench_1.only } }\n"
        "- kind: transport\n"
        "  status: completed\n"
        "  start: 1\n"
        "  end: 11\n"
        "  from_spot: bench_1.only\n"
        "  to_spot: shelf_1.only\n"
        "  transporter: arm_2\n"
        "  arc: { from: { node: [M1], port: o }, to: { node: [H1], port: i } }\n",
    )
    report = schedule(wf_path, env_path, document_path=status)
    assert report.plan is not None, [d.code for d in report.diagnostics]
    # The committed move still says arm_2, and it was not reassigned.
    committed = [m for m in _moves(report) if m.get("status") == "completed"]
    assert [m["transporter"] for m in committed] == ["arm_2"]
    # With arm_2 pinned only arm_1 is left, so there is no class of two to collapse.
    assert [d.code for d in report.diagnostics if d.code == CODE] == []
