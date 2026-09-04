"""What a solve cost, reported next to what it decided (`ScheduleReport.stats`).

These are measurement plumbing, and the properties that matter are the ones a
benchmark would silently get wrong answers from if they broke:

- the record exists on *every* path that reached the solver, an unschedulable
  instance included -- how long it takes to say "infeasible" is a measurement;
- the objective numbers a solve reports mid-search decode back to the same values
  the finished plan states, since the solver only ever sees one weighted number;
- the improvement history is only there when it was asked for, and improves.

Nothing here belongs in the plan document, so there is also a test that says so.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ofplang.schedule import schedule
from ofplang.schedule.core import objective as stages

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _simple(**kwargs):
    return schedule(EXAMPLES / "simple.workflow.yaml", EXAMPLES / "simple.env.yaml", **kwargs)


def _consumable(env=None, document=None, **kwargs):
    return schedule(
        EXAMPLES / "consumable.workflow.yaml",
        env if env is not None else EXAMPLES / "consumable.env.yaml",
        document_path=document if document is not None else EXAMPLES / "consumable.document.yaml",
        **kwargs,
    )


def _env_no_refills():
    """The consumable environment with nothing that can refill the reader: the
    reader starts empty (the example's document), so the assays can never run."""
    env = yaml.safe_load((EXAMPLES / "consumable.env.yaml").read_text(encoding="utf-8"))
    env = copy.deepcopy(env)
    env.pop("replenishers", None)
    env.pop("replenishments", None)
    return env


# --- the record is there, and says what ran ------------------------------


def test_a_solved_schedule_reports_what_the_solve_cost():
    report = _simple()
    assert report.plan is not None
    stats = report.stats
    assert stats is not None
    # One phase today, named for the only thing that runs (design.md D37 adds more).
    assert [p.name for p in stats.phases] == ["cpsat"]
    assert stats.final is not None
    assert stats.final.outcome == report.outcome == "optimal"
    assert stats.wall_time >= 0.0
    assert stats.deterministic_time > 0.0
    # The model is not empty, and the instance counts describe `simple`: a source
    # and a target joined by one transport.
    assert stats.model.variables > 0 and stats.model.constraints > 0
    assert stats.model.activities == 2
    assert stats.model.arcs == 1
    assert stats.model.replenishments == 0
    # The horizon is the fully-serial bound, so it is an upper bound on the answer.
    assert stats.model.horizon >= report.makespan


def test_an_unschedulable_instance_still_reports_the_solve():
    # A stock nothing can refill, starting empty: the assays can never run. The
    # point is not the outcome (test_resources covers that) but that the timings
    # survive it -- "how long until infeasible" is a benchmark number.
    report = _consumable(env=_env_no_refills())
    assert report.plan is None
    assert report.outcome == "infeasible"
    assert report.stats is not None
    assert report.stats.final is not None
    assert report.stats.final.outcome == "infeasible"
    assert report.stats.final.objective_value is None
    assert report.stats.final.objective_values is None
    # Timings, not a lower bound on them: presolve settles this one before the
    # search starts, so both clocks legitimately read zero. What must survive is
    # the model description -- the size a benchmark attributes the failure to.
    assert report.stats.deterministic_time >= 0.0
    assert report.stats.wall_time >= 0.0
    assert report.stats.model.variables > 0
    assert report.stats.model.replenishments == 0


def test_nothing_is_reported_when_no_solve_ran():
    # A workflow that fails the front door never reaches the solver, so there is
    # nothing to measure. (`simple.env.yaml` has no `heater` process.)
    report = schedule(EXAMPLES / "reformatter.workflow.yaml", EXAMPLES / "simple.env.yaml")
    assert report.plan is None
    assert report.stats is None


# --- the numbers agree with the plan -------------------------------------


def test_the_reported_objective_decodes_to_the_plan_objective():
    # The solver minimises one weighted number; the plan states the stages. A
    # benchmark reading the solve mid-flight has only the former, so the two must
    # be the same answer written twice.
    report = _consumable()
    assert report.plan is not None
    stats = report.stats
    assert stats is not None and stats.final is not None
    assert stats.objective_kind == ("makespan", "replenishment_count")
    assert stats.final.objective_values is not None
    assert stats.final.objective_values[0] == report.makespan
    assert list(stats.final.objective_values) == list(report.plan["objective"]["value"])


def test_a_proven_optimum_bounds_the_first_stage_at_its_own_value():
    # `optimal` means the bound met the incumbent, and the first stage is the one
    # the weighted encoding lets us read a bound for.
    report = _simple()
    stats = report.stats
    assert stats is not None and stats.final is not None
    assert stats.final.outcome == "optimal"
    assert stats.final.first_stage_bound == report.makespan


def test_the_weight_encoding_round_trips():
    kind = ("makespan", "replenishment_count")
    weights = stages.weights(kind, {"makespan": 100, "replenishment_count": 3})
    # Mixed radix: one refill costs more than every reachable makespan.
    assert weights == (4, 1)
    encoded = 37 * weights[0] + 2 * weights[1]
    assert stages.decode(weights, encoded) == (37, 2)
    # A bound on the whole expression bounds the leading stage and nothing else.
    assert stages.first_stage_bound(weights, float(encoded)) == 37
    # A fractional bound is an integer bound rounded up, then divided down.
    assert stages.first_stage_bound(weights, encoded - 0.5) == 37
    # Reversing the order reverses which stage is worth more.
    reversed_ = stages.weights(kind[::-1], {"makespan": 100, "replenishment_count": 3})
    assert reversed_ == (101, 1)


# --- the improvement history is opt-in -----------------------------------


def test_no_history_unless_it_was_asked_for():
    stats = _simple().stats
    assert stats is not None and stats.final is not None
    # None, not (): "not recorded" is a different claim from "there were none".
    assert stats.final.history is None


def test_the_history_records_improving_solutions():
    report = _consumable(collect_solutions=True)
    assert report.plan is not None
    stats = report.stats
    assert stats is not None and stats.final is not None
    history = stats.final.history
    assert history is not None and len(history) >= 1
    # Each entry improves on the one before: CP-SAT only calls back on a better
    # solution, and the search is a minimisation.
    values = [e.objective_value for e in history]
    assert values == sorted(values, reverse=True)
    assert len(set(values)) == len(values)
    # Time runs forward and the last incumbent is the one that was kept.
    assert all(a.wall_time <= b.wall_time for a, b in zip(history, history[1:], strict=False))
    assert history[-1].objective_value == stats.final.objective_value
    assert history[-1].objective_values == stats.final.objective_values


def test_collecting_solutions_does_not_change_the_answer():
    # The callback observes; it must not steer. Same seed, same optimum.
    plain = _consumable(random_seed=0)
    watched = _consumable(random_seed=0, collect_solutions=True)
    assert watched.makespan == plain.makespan
    assert watched.plan["objective"] == plain.plan["objective"]


# --- and none of it leaks into the document ------------------------------


def test_the_plan_document_is_untouched_by_measurement():
    # A plan is a portable v0 document (§6). Stats describe the solve, not the
    # schedule, so a plan solved with measurement on must be the same document.
    plain = _consumable(random_seed=0).plan
    watched = _consumable(random_seed=0, collect_solutions=True).plan
    assert yaml.safe_dump(watched) == yaml.safe_dump(plain)
