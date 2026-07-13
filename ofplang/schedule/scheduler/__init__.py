"""Scheduler pipeline for ofplang.schedule.

Turns a v0 workflow plus an execution environment into an execution plan
(SPECIFICATIONS.md §6) by building and solving the optimization model of
docs/FORMULATION.md. Covers a single workflow with mode selection, spot/device
occupancy, transport, and replanning from an execution status (fix
completed/running, re-optimise the rest at/after `now`), minimising makespan;
device-local resources are not handled yet (decision D21). Replan input is
assumed normalized (FORMULATION §9): a started transport must not feed a pending
processing activity.

Pipeline: envload -> workflow -> instance -> (status ->) cpsat -> plan,
orchestrated by `api.schedule`.
"""
