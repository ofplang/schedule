"""Tests for loading the execution environment into the typed model."""

from __future__ import annotations

from pathlib import Path

from ofplang.schedule.scheduler.envload import load_environment

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_load_simple_env():
    env, result = load_environment(EXAMPLES / "simple.env.yaml")
    assert result.ok
    assert env is not None
    assert env.time_unit == "second"

    assert set(env.devices) == {"station_0", "station_1"}
    assert env.devices["station_0"].spots == frozenset({"core"})
    assert env.transporters == ("transport",)
    assert env.transport_duration("transport", "station_0.core", "station_1.core") == 1
    # A same-spot move is free; an unknown move is unreachable.
    assert env.transport_duration("transport", "station_0.core", "station_0.core") == 0
    assert env.transport_duration("transport", "station_1.core", "station_0.core") is None

    source = env.processes["source"]
    assert len(source.modes) == 1
    mode = source.modes[0]
    assert mode.id == "0"  # no explicit id -> positional
    assert mode.devices == ("station_0",)
    assert mode.duration == 2
    assert mode.output_spots == {"source_out": "station_0.core"}
    assert mode.input_spots == {}


def test_load_reformatter_env():
    env, result = load_environment(EXAMPLES / "reformatter.env.yaml")
    assert result.ok
    assert env is not None
    assert set(env.devices) == {
        "prep",
        "reformatter",
        "motoman",
        "biomek2000_a3",
        "biomek2000_a4",
    }
    assert len(env.transports) == 10
    assert set(env.processes) == {
        "preparation",
        "reformatter_12",
        "motoman_7",
        "biomek_a3_24",
        "biomek_a4_16",
        "reformatter_20",
        "biomek_a3_19",
        "reformatter_3",
    }
    # reformatter_20 shares the rf_link buffer between an input and an output.
    mode = env.processes["reformatter_20"].modes[0]
    assert mode.duration == 20
    assert mode.input_spots["rf20_in_rf12"] == "reformatter.rf_link"
    assert mode.output_spots["rf20_out_rf3"] == "reformatter.rf_link"


def test_invalid_environment_returns_no_model():
    # devices missing -> schema-invalid -> no model, diagnostics present.
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write("time:\n  unit: second\nprocesses: {}\n")
        path = f.name
    env, result = load_environment(path)
    assert env is None
    assert not result.ok
