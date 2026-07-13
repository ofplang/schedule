"""Scheduler pipeline for ofplang.schedule.

Turns a v0 workflow plus an execution environment into an execution plan
(SPECIFICATIONS.md §6) by building and solving the optimization model of
docs/FORMULATION.md. The initial slice covers a single workflow with mode
selection, spot/device occupancy, and transport, minimising makespan; there is
no replanning or device-local resource handling yet (decision D21).

Pipeline: envload -> workflow -> instance -> cpsat -> plan, orchestrated by
`api.schedule`.
"""
