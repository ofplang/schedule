"""Shared helpers for the scheduling integration tests.

These tests drive `schedule()` end to end on **valid** inputs and assert the
optimal makespan (CP-SAT's optimum is a unique value, so it is a stable golden
anchor) plus the key structural choices. Small hand-built environments keep the
optimum hand-verifiable.
"""

from __future__ import annotations

from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SIMPLE_WF = EXAMPLES / "simple.workflow.yaml"  # SampleSource(source) -> SampleTarget(target)


def write(directory, name: str, text: str) -> Path:
    p = Path(directory) / name
    p.write_text(text, encoding="utf-8")
    return p


def kinds(plan: dict, kind: str) -> list[dict]:
    return [a for a in plan["activities"] if a["kind"] == kind]


def st_env(
    *,
    devices,
    transports,
    source_dur: int = 2,
    target_dur: int = 2,
    source_dev: str = "station_0",
    source_spot: str = "station_0.core",
    target_modes=(("station_1", "station_1.core"),),
) -> str:
    """An environment for the `simple` workflow (source -> target). `devices` is a
    list of (id, [spots]); `transports` a list of (from, to, duration);
    `target_modes` the target's modes as (device, input-spot) or
    (device, input-spot, duration) tuples (duration defaults to `target_dur`)."""
    devs = "\n".join(f"  - {{ id: {d}, spots: [{', '.join(s)}] }}" for d, s in devices)
    trs = "\n".join(f"  - {{ transporter: transport, from: {f}, to: {t}, duration: {d} }}" for f, t, d in transports)
    tmodes = ", ".join(
        f"{{ devices: [{m[0]}], duration: {m[2] if len(m) > 2 else target_dur}, input_spots: {{ target_in: {m[1]} }} }}"
        for m in target_modes
    )
    return f"""time: {{ unit: second }}
devices:
{devs}
transporters: [ {{ id: transport }} ]
transports:
{trs}
processes:
  source: {{ modes: [ {{ devices: [{source_dev}], duration: {source_dur}, output_spots: {{ source_out: {source_spot} }} }} ] }}
  target: {{ modes: [ {tmodes} ] }}
"""


# A committed status for the `simple` workflow: source done, one transport leg
# done delivering to `to_spot`, target still pending. `now` and the leg's arrival
# spot are the knobs the replan/reroute tests turn.
def committed_source_and_leg(now: int, to_spot: str = "station_1.core", leg_end: int = 3) -> str:
    return f"""time: {{ unit: second }}
now: {now}
activities:
- {{ kind: processing, status: completed, start: 0, end: 2, process: source, mode: '0', node: [SampleSource], output_spots: {{ source_out: station_0.core }} }}
- kind: transport
  status: completed
  start: 2
  end: {leg_end}
  seq: 0
  from_spot: station_0.core
  to_spot: {to_spot}
  transporter: transport
  arc: {{ from: {{ node: [SampleSource], port: source_out }}, to: {{ node: [SampleTarget], port: target_in }} }}
"""


# --- a two-input workflow (S1, S2 -> merge) for multi-input replan tests ------

MULTI_INPUT_WF = """spec_version: "0.0"
types:
  Sample: { domain: object }
processes:
  source:  { kind: atomic, outputs: { o: { type: Sample, phase: data } }, objects: { create: [outputs.o] } }
  source2: { kind: atomic, outputs: { o: { type: Sample, phase: data } }, objects: { create: [outputs.o] } }
  merge:
    kind: atomic
    inputs:
      i1: { type: Sample, phase: data }
      i2: { type: Sample, phase: data }
    objects: { consume: [inputs.i1, inputs.i2] }
  main:
    kind: composite
    inputs: {}
    outputs: {}
    body:
      nodes:
        - { id: S1, process: source }
        - { id: S2, process: source2 }
        - id: M
          process: merge
          state:
            i1: { from: S1.o }
            i2: { from: S2.o }
      returns: {}
entry: main
"""
