"""What a run says when the solver merely ran out of time.

`infeasible` is defined as a proof -- the solver showed there is no schedule
(SPEC §10.4) -- and `jobs_not_plannable_together` is a claim about every job in
the roster. A timeout establishes neither, so neither may be reported, and the
per-job probe that produces the second must not run at all: it is a solve per
job at the full budget, which on a joint plan that timed out was measured at 6.2
times the budget the caller asked for.

The solve is stubbed rather than provoked. A timeout reproduced by giving a real
instance too little time is a race, and what is under test here is a contract,
not a duration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ofplang.schedule.scheduler.api as api_mod
from ofplang.schedule import JobInput, schedule, schedule_jobs
from ofplang.schedule.scheduler.result import Solution

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def answers(monkeypatch):
    """Make every solve return `outcome`, and count the calls."""

    def install(outcome: str) -> list[int]:
        calls = [0]

        def stub(instance, **kwargs):
            calls[0] += 1
            return Solution(outcome, None, (), (), stats=None)

        monkeypatch.setattr(api_mod, "solve", stub)
        return calls

    return install


def _workflow(name: str) -> dict:
    import yaml

    return yaml.safe_load((EXAMPLES / f"{name}.workflow.yaml").read_text(encoding="utf-8"))


def _environment(name: str) -> dict:
    import yaml

    return yaml.safe_load((EXAMPLES / f"{name}.env.yaml").read_text(encoding="utf-8"))


def _codes(report) -> list[str]:
    return [d.code for d in report.diagnostics]


def test_a_timeout_does_not_claim_the_instance_is_infeasible(answers):
    calls = answers("unknown")
    report = schedule(_workflow("simple"), _environment("simple"), max_time_seconds=1)
    assert report.outcome == "unknown"
    assert "infeasible" not in _codes(report)
    assert calls[0] == 1  # and nothing was solved a second time to say so


def test_a_proof_is_still_reported_as_one(answers):
    answers("infeasible")
    report = schedule(_workflow("simple"), _environment("simple"), max_time_seconds=1)
    assert report.outcome == "infeasible"
    assert "infeasible" in _codes(report)


def test_a_timed_out_joint_plan_probes_nothing(answers):
    # One solve, not one per job: the probe exists to name the job that cannot be
    # planned, and after a timeout there is nothing to name.
    calls = answers("unknown")
    jobs = [JobInput(id=f"job{k}", workflow=_workflow("simple")) for k in range(1, 6)]
    report = schedule_jobs(jobs, _environment("simple"), max_time_seconds=1)
    assert report.outcome == "unknown"
    assert _codes(report) == []
    assert calls[0] == 1


def test_a_joint_plan_proved_impossible_is_still_explained(answers):
    # Every probe comes back with the same proof, so no single job accounts for it
    # -- which is a thing worth saying, and is said.
    answers("infeasible")
    jobs = [JobInput(id=f"job{k}", workflow=_workflow("simple")) for k in range(1, 4)]
    report = schedule_jobs(jobs, _environment("simple"), max_time_seconds=1)
    assert "infeasible" in _codes(report)
    assert "jobs_not_plannable_together" in _codes(report)


def test_one_silent_probe_withdraws_the_claim_about_every_job(monkeypatch):
    # The main solve proves it, then one probe runs out of time. "No single job
    # accounts for this" is a statement about all of them, and one probe that
    # said nothing is enough to leave it unsaid.
    outcomes = iter(["infeasible", "infeasible", "unknown", "infeasible", "infeasible"])

    def stub(instance, **kwargs):
        return Solution(next(outcomes, "infeasible"), None, (), (), stats=None)

    monkeypatch.setattr(api_mod, "solve", stub)
    jobs = [JobInput(id=f"job{k}", workflow=_workflow("simple")) for k in range(1, 4)]
    report = schedule_jobs(jobs, _environment("simple"), max_time_seconds=1)
    assert "infeasible" in _codes(report)
    assert "jobs_not_plannable_together" not in _codes(report)
