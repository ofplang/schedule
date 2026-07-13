"""Load an execution environment definition into the typed `Environment` model.

The document is first run through the existing schema validator
(`validate_environment`, §9.1); only a shape-valid document is turned into a
model. Because that pass has already guaranteed the structure, the build here
reads with a plain YAML load and does not re-check shapes.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ofplang.schedule.core.diagnostics import ValidationResult
from ofplang.schedule.scheduler.model import (
    Device,
    Environment,
    Mode,
    ProcessCapability,
)
from ofplang.schedule.validation import validate_environment


def load_environment(source) -> tuple[Environment | None, ValidationResult]:
    """Validate then load the environment at `source`.

    Returns `(environment, result)`. On any error the environment is None and the
    result carries the diagnostics; warnings alone still yield a model.
    """
    result = validate_environment(source)
    if not result.ok:
        return None, result
    data = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    return _build(data), result


def _build(data: dict) -> Environment:
    time_unit = data["time"]["unit"]

    # Devices own a set of local spot names.
    devices = {
        d["id"]: Device(d["id"], frozenset(d.get("spots", [])))
        for d in data["devices"]
    }

    # Transporters and the (transporter, from, to) -> duration table (both
    # optional; absent when the workflow has no Object-bearing arcs).
    transporters = tuple(t["id"] for t in data.get("transporters", []))
    transports: dict[tuple[str, str, str], int] = {}
    for entry in data.get("transports", []):
        transports[(entry["transporter"], entry["from"], entry["to"])] = entry["duration"]

    # Per-process capabilities; a mode without an explicit id is numbered by its
    # position (D21 / §5.5) so the plan can reference the selected mode stably.
    processes: dict[str, ProcessCapability] = {}
    for name, proc in data["processes"].items():
        modes = tuple(
            Mode(
                id=str(m.get("id", index)),
                devices=tuple(m.get("devices", [])),
                duration=m["duration"],
                input_spots=dict(m.get("input_spots", {})),
                output_spots=dict(m.get("output_spots", {})),
            )
            for index, m in enumerate(proc["modes"])
        )
        processes[name] = ProcessCapability(name, modes)

    objective_kind = data.get("objective", {}).get("kind", "makespan")

    return Environment(
        time_unit=time_unit,
        devices=devices,
        transporters=transporters,
        transports=transports,
        processes=processes,
        objective_kind=objective_kind,
    )
