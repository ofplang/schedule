"""Device-less Pure-Data-only processes (SPECIFICATIONS.md §5.5).

A Pure-Data-only process occupies no device and no spot; it exists only to take
time and to impose ordering through its Pure Data arcs (a `bind` binding is a
precedence edge, not a transport). Its mode may therefore have a **zero**
duration -- an instantaneous step, like a relay or a same-spot transport (§5.4).

These drive `schedule()` end to end: an Object process `measure` produces a Pure
Data `reading` that a device-less `compute` consumes via `bind`, so `compute`
must be scheduled strictly after `measure` even though nothing is transported to
it and it holds no resource.
"""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule import schedule, validate_document
from ofplang.schedule.scheduler.plan import to_yaml

# measure (Object process) -> reading (Pure Data) --bind--> compute (device-less).
WORKFLOW = """
spec_version: "0.0"
types:
  Plate:   { domain: object }
  Reading: { domain: data }
  Score:   { domain: data }
processes:
  measure:
    kind: atomic
    inputs:  { plate:   { type: Plate,   phase: data } }
    outputs: { reading: { type: Reading, phase: data } }
    objects: { consume: [inputs.plate] }
  compute:
    kind: atomic
    inputs:  { reading: { type: Reading, phase: data } }
    outputs: { score:   { type: Score,   phase: data } }
  main:
    kind: composite
    inputs:  { sample: { type: Plate, phase: data } }
    outputs: {}
    body:
      nodes:
        - id: Measure
          process: measure
          state: { plate: { from: inputs.sample } }
        - id: Compute
          process: compute
          bind:  { reading: { from: Measure.reading } }
      returns: {}
entry: main
"""

# `compute` is device-less with a zero duration -- a genuine Pure-Data-only mode.
ENV = """
time: { unit: second }
devices:
  - id: loader
    spots: [stage]
  - id: reader_0
    spots: [stage]
transporters:
  - id: arm
transports:
  - { transporter: arm, from: loader.stage, to: reader_0.stage, duration: 2 }
processes:
  measure:
    modes:
      - { devices: [reader_0], duration: 10, input_spots: { plate: reader_0.stage } }
  compute:
    modes:
      - { id: mean_v1, duration: 0 }
objective: { kind: makespan }
"""

DOCUMENT = "interface:\n  inputs: { sample: loader.stage }\nactivities: []\n"


def _write(tmp_path):
    wf = tmp_path / "wf.yaml"
    wf.write_text(WORKFLOW, encoding="utf-8")
    ev = tmp_path / "env.yaml"
    ev.write_text(ENV, encoding="utf-8")
    doc = tmp_path / "doc.yaml"
    doc.write_text(DOCUMENT, encoding="utf-8")
    return wf, ev, doc


def test_device_less_pure_data_is_scheduled_after_its_producer(tmp_path):
    wf, ev, doc = _write(tmp_path)
    report = schedule(wf, ev, document_path=doc)
    assert report.ok and report.outcome == "optimal", [d.code for d in report.diagnostics]

    # The deterministic timeline: boundary transport [0,2], measure [2,12], and the
    # device-less compute instantaneously at [12,12] -- right after measure ends.
    (measure,) = [a for a in report.plan["activities"] if a.get("process") == "measure"]
    (compute,) = [a for a in report.plan["activities"] if a.get("process") == "compute"]
    assert (measure["start"], measure["end"]) == (2, 12)
    assert compute["start"] == compute["end"] == 12  # zero-duration, instantaneous
    assert compute["start"] >= measure["end"]  # the Pure Data precedence is honored
    assert report.makespan == 12

    # A device-less activity occupies nothing physical: no devices, no spots.
    assert "devices" not in compute
    assert "input_spots" not in compute and "output_spots" not in compute
    assert compute["mode"] == "mean_v1" and compute["node"] == ["Compute"]


def test_device_less_pure_data_plan_round_trips(tmp_path):
    # The rendered plan -- including the zero-duration processing activity -- must be
    # a valid execution document (end == start is accepted for a processing activity).
    wf, ev, doc = _write(tmp_path)
    report = schedule(wf, ev, document_path=doc)
    assert report.ok
    out = tmp_path / "plan.yaml"
    out.write_text(to_yaml(report.plan), encoding="utf-8")
    result = validate_document(out)
    assert result.ok, [(d.code, d.path) for d in result.errors]
