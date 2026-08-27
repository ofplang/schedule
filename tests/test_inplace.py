"""A purely in-place workflow (all hand-offs at one spot) with no transporters.

An Object produced and consumed at the *same* spot is a same-spot hand-off
(§5.4): a physical no-op that no transporter carries (§6.4). Such a workflow --
with no transporters defined at all -- must still schedule: the same-spot arc
gets a synthesized transporter-less zero-duration route rather than being
rejected as arc_unreachable (review #2).
"""

from __future__ import annotations

from ofplang.schedule import schedule as run_schedule

_WORKFLOW = """\
spec_version: "0.0"
types:
  Widget: { domain: object }
processes:
  make:
    kind: atomic
    inputs: {}
    outputs:
      w: { type: Widget, phase: data }
    objects:
      create: [outputs.w]
  use:
    kind: atomic
    inputs:
      w: { type: Widget, phase: data }
    outputs: {}
    objects:
      consume: [inputs.w]
  main:
    kind: composite
    inputs: {}
    outputs: {}
    body:
      nodes:
        - id: Make
          process: make
        - id: Use
          process: use
          state:
            w: { from: Make.w }
      returns: {}
entry: main
"""

_ENV = """\
time: { unit: second }
devices: [ { id: dev, spots: [slot] } ]
transporters: []
transports: []
processes:
  make: { modes: [ { devices: [dev], duration: 1, output_spots: { w: dev.slot } } ] }
  use: { modes: [ { devices: [dev], duration: 1, input_spots: { w: dev.slot } } ] }
"""


def test_in_place_workflow_with_no_transporters_schedules(tmp_path):
    wf = tmp_path / "inplace.workflow.yaml"
    env = tmp_path / "inplace.env.yaml"
    wf.write_text(_WORKFLOW, encoding="utf-8")
    env.write_text(_ENV, encoding="utf-8")
    report = run_schedule(str(wf), str(env))
    assert report.ok, [d.code for d in report.diagnostics]
    # make (1) then use (1), the same-spot hand-off between them free.
    assert report.makespan == 2
    # The same-spot delivery is a no-op leg: the plan omits its transporter (§6.4).
    transports = [a for a in report.plan["activities"] if a.get("kind") == "transport"]
    assert transports and all("transporter" not in a for a in transports)
