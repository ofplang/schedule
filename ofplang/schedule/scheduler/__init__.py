"""Scheduler pipeline for ofplang.schedule.

Turns a v0 workflow plus an execution environment into an execution plan
(SPECIFICATIONS.md §6) by building and solving the optimization model of
docs/FORMULATION.md. Covers a single workflow with mode selection, spot/device
occupancy, transport, and replanning from an execution status (fix
completed/running as historical facts, re-optimise the rest at/after `now`),
minimising makespan; device-local resources are not handled yet (decision D21). A
started transport that has committed an Object to a spot while its destination is
still pending is normalized into a relay + re-transport (SPEC §6.4.1), so the
destination can be re-routed; the solver treats relays as ordinary 0-duration
activities.

Pipeline: envload -> workflow -> instance -> (status -> normalize ->) cpsat ->
plan, orchestrated by `api.schedule`.
"""
