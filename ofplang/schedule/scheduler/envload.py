"""Load an execution environment definition into the typed `Environment` model.

The document is first run through the existing schema validator (§9.1); only a
shape-valid document is turned into a model. Because that pass has already
guaranteed the structure, the build here does not re-check shapes.

A file is parsed exactly once: the wrapped tree goes to the validator (which needs
its positions) and the plain value the model is built from is derived from that same
tree (`yamlnode.to_plain`). `source` may also be an already-loaded environment
document (a mapping), which is read as it stands -- nothing parsed, nothing written
to a file. Either way the document is treated as read-only: the model copies every
collection it keeps, so it never aliases the caller's dict.
"""

from __future__ import annotations

from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import ValidationResult
from ofplang.schedule.scheduler.model import (
    Device,
    Environment,
    Mode,
    ProcessCapability,
)
from ofplang.schedule.validation.environment import validate_environment_node


def load_environment(source) -> tuple[Environment | None, ValidationResult]:
    """Validate then load the environment `source` (a path, or the document itself).

    Returns `(environment, result)`. On any error the environment is None and the
    result carries the diagnostics; warnings alone still yield a model.
    """
    root = yamlnode.load_source(source)
    result = validate_environment_node(root)
    if not result.ok:
        return None, result
    # An in-memory document is already the value; a file's value comes off the tree
    # just parsed, not from a second read. A shape-valid environment is a mapping --
    # the validator reports anything else and we returned above.
    data = source if isinstance(source, dict) else yamlnode.to_plain(root)
    assert isinstance(data, dict)
    return _build(data), result


def _build(data: dict) -> Environment:
    time_unit = data["time"]["unit"]

    # Devices own a set of local spot names and, optionally, consumable stocks.
    devices = {
        d["id"]: Device(
            d["id"],
            frozenset(d.get("spots", [])),
            {name: spec["capacity"] for name, spec in (d.get("resources") or {}).items()},
        )
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
                consumption=dict(m.get("consumption", {})),
                device_access=bool(m.get("device_access", True)),
            )
            for index, m in enumerate(proc["modes"])
        )
        processes[name] = ProcessCapability(name, modes)

    # Replenishers and the (replenisher, device) -> duration table (§5.6, §5.7),
    # both optional in exactly the way transporters and transports are.
    replenishers = tuple(r["id"] for r in data.get("replenishers", []))
    replenishments: dict[tuple[str, str], int] = {}
    for entry in data.get("replenishments", []):
        replenishments[(entry["replenisher"], entry["device"])] = entry["duration"]

    return Environment(
        time_unit=time_unit,
        devices=devices,
        transporters=transporters,
        transports=transports,
        processes=processes,
        replenishers=replenishers,
        replenishments=replenishments,
    )
