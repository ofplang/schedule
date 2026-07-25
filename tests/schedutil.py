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
    trs = "\n".join(
        f"  - {{ transporter: transport, from: {f}, to: {t}, duration: {d} }}"
        for f, t, d in transports
    )
    tmodes = ", ".join(
        f"{{ devices: [{m[0]}], duration: {m[2] if len(m) > 2 else target_dur}, "
        f"input_spots: {{ target_in: {m[1]} }} }}"
        for m in target_modes
    )
    return (
        f"time: {{ unit: second }}\n"
        f"devices:\n"
        f"{devs}\n"
        f"transporters: [ {{ id: transport }} ]\n"
        f"transports:\n"
        f"{trs}\n"
        f"processes:\n"
        f"  source: {{ modes: [ {{ devices: [{source_dev}], duration: {source_dur}, "
        f"output_spots: {{ source_out: {source_spot} }} }} ] }}\n"
        f"  target: {{ modes: [ {tmodes} ] }}\n"
    )


# A committed status for the `simple` workflow: source done, one transport leg
# done delivering to `to_spot`, target still pending. `now` and the leg's arrival
# spot are the knobs the replan/reroute tests turn.
def committed_source_and_leg(now: int, to_spot: str = "station_1.core", leg_end: int = 3) -> str:
    return (
        f"time: {{ unit: second }}\n"
        f"now: {now}\n"
        f"activities:\n"
        f"- {{ kind: processing, status: completed, start: 0, end: 2, process: source, "
        f"mode: '0', node: [SampleSource], output_spots: {{ source_out: station_0.core }} }}\n"
        f"- kind: transport\n"
        f"  status: completed\n"
        f"  start: 2\n"
        f"  end: {leg_end}\n"
        f"  seq: 0\n"
        f"  from_spot: station_0.core\n"
        f"  to_spot: {to_spot}\n"
        f"  transporter: transport\n"
        f"  arc: {{ from: {{ node: [SampleSource], port: source_out }}, "
        f"to: {{ node: [SampleTarget], port: target_in }} }}\n"
    )


# --- a two-input workflow (S1, S2 -> merge) for multi-input replan tests ------

MULTI_INPUT_WF = (
    "spec_version: \"0.0\"\n"
    "types:\n"
    "  Sample: { domain: object }\n"
    "processes:\n"
    "  source:  { kind: atomic, outputs: { o: { type: Sample, phase: data } }, "
    "objects: { create: [outputs.o] } }\n"
    "  source2: { kind: atomic, outputs: { o: { type: Sample, phase: data } }, "
    "objects: { create: [outputs.o] } }\n"
    "  merge:\n"
    "    kind: atomic\n"
    "    inputs:\n"
    "      i1: { type: Sample, phase: data }\n"
    "      i2: { type: Sample, phase: data }\n"
    "    objects: { consume: [inputs.i1, inputs.i2] }\n"
    "  main:\n"
    "    kind: composite\n"
    "    inputs: {}\n"
    "    outputs: {}\n"
    "    body:\n"
    "      nodes:\n"
    "        - { id: S1, process: source }\n"
    "        - { id: S2, process: source2 }\n"
    "        - id: M\n"
    "          process: merge\n"
    "          state:\n"
    "            i1: { from: S1.o }\n"
    "            i2: { from: S2.o }\n"
    "      returns: {}\n"
    "entry: main\n"
)
