"""Schema validator for the execution environment definition (SPECIFICATIONS.md
§9.1). Shape only: it checks a single environment document on its own and never
reads the workflow. Cross-workflow and solvability checks are the execution
layer's job (§9.3).
"""

from __future__ import annotations

from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import Diagnostics, ValidationResult
from ofplang.schedule.core.identifiers import is_identifier, parse_qualified_spot
from ofplang.schedule.core.yamlnode import YMap, YScalar, YNode
from ofplang.schedule.validation import _shape as shape
from ofplang.schedule.validation import errors

# Allowed keys per structure (unknown keys are errors; §9.1, strict).
ENV_TOP = {"time", "devices", "transporters", "transports", "processes", "objective"}
REQUIRED_SECTIONS = ("time", "devices", "processes")
TIME_KEYS = {"unit"}
DEVICE_KEYS = {"id", "spots"}
TRANSPORTER_KEYS = {"id"}
TRANSPORT_KEYS = {"transporter", "from", "to", "duration"}
PROCESS_KEYS = {"modes"}
MODE_KEYS = {"id", "devices", "duration", "input_spots", "output_spots"}
OBJECTIVE_KEYS = {"kind"}


def validate_environment(source) -> ValidationResult:
    """Validate the environment definition at `source` (a file path)."""
    root = yamlnode.load_file(source)
    diags = Diagnostics()
    _check(root, diags)
    return ValidationResult(diags.items)


def _check(root: YNode | None, diags: Diagnostics) -> None:
    if not isinstance(root, YMap):
        # A non-mapping (or empty) document cannot carry any section.
        if root is not None:
            diags.error(errors.WRONG_TYPE, "environment must be a mapping", "", at=root)
        else:
            diags.error(errors.WRONG_TYPE, "environment document is empty", "")
        return

    shape.unknown_keys(root, ENV_TOP, "", diags)
    for section in REQUIRED_SECTIONS:
        if section not in root:
            diags.error(errors.MISSING_REQUIRED_SECTION, f"{section} is required", section, at=root)

    _check_time(root.get("time"), diags)
    devices = _check_devices(root.get("devices"), diags)
    transporters = _check_transporters(root.get("transporters"), diags)
    _check_cross_kind(devices, transporters, root, diags)
    _check_transports(root.get("transports"), devices, transporters, diags)
    _check_processes(root.get("processes"), devices, diags)
    _check_objective(root.get("objective"), diags)


def _check_time(node: YNode | None, diags: Diagnostics) -> None:
    tmap = shape.as_map(node, "time", diags)
    if tmap is None:
        return
    shape.unknown_keys(tmap, TIME_KEYS, "time", diags)
    unit = tmap.get("unit")
    if unit is None and "unit" not in tmap:
        diags.error(errors.MISSING_REQUIRED_FIELD, "time.unit is required", "time.unit", at=tmap)
        return
    # A non-string or empty/whitespace unit is reported with the dedicated code.
    if not (isinstance(unit, YScalar) and unit.is_str and unit.text.strip()):
        diags.error(errors.EMPTY_TIME_UNIT, "time.unit must be a non-empty string", "time.unit", at=unit or tmap)


def _check_devices(node: YNode | None, diags: Diagnostics) -> dict[str, set[str]]:
    """Return {device_id: {spot names}} for the well-formed devices found."""
    devices: dict[str, set[str]] = {}
    seq = shape.as_seq(node, "devices", diags)
    if seq is None:
        return devices
    if not seq.items:
        diags.error(errors.EMPTY_DEVICES, "devices must not be empty", "devices", at=seq)
        return devices

    for i, item in enumerate(seq.items):
        base = f"devices[{i}]"
        dmap = shape.as_map(item, base, diags)
        if dmap is None:
            continue
        shape.unknown_keys(dmap, DEVICE_KEYS, base, diags)
        dev_id = _check_id(dmap, base, "device", errors.INVALID_IDENTIFIER, diags)
        spots = _check_spots(dmap.get("spots"), base, diags)
        if dev_id is not None:
            if dev_id in devices:
                diags.error(errors.DUPLICATE_DEVICE_ID, f"duplicate device id {dev_id!r}", shape.join(base, "id"), at=dmap.get("id"))
            else:
                devices[dev_id] = spots
    return devices


def _check_spots(node: YNode | None, base: str, diags: Diagnostics) -> set[str]:
    spots: set[str] = set()
    seq = shape.as_seq(node, shape.join(base, "spots"), diags)
    if seq is None:
        return spots
    for j, item in enumerate(seq.items):
        path = shape.join(base, f"spots[{j}]")
        if not (isinstance(item, YScalar) and item.is_str):
            diags.error(errors.WRONG_TYPE, "spot name must be a string", path, at=item)
            continue
        if not is_identifier(item.value):
            diags.error(errors.INVALID_IDENTIFIER, f"invalid spot name {item.value!r}", path, at=item)
            continue
        if item.value in spots:
            diags.error(errors.DUPLICATE_SPOT_ID, f"duplicate spot {item.value!r}", path, at=item)
        else:
            spots.add(item.value)
    return spots


def _check_transporters(node: YNode | None, diags: Diagnostics) -> set[str]:
    transporters: set[str] = set()
    seq = shape.as_seq(node, "transporters", diags)
    if seq is None:
        return transporters
    for i, item in enumerate(seq.items):
        base = f"transporters[{i}]"
        tmap = shape.as_map(item, base, diags)
        if tmap is None:
            continue
        shape.unknown_keys(tmap, TRANSPORTER_KEYS, base, diags)
        tid = _check_id(tmap, base, "transporter", errors.INVALID_IDENTIFIER, diags)
        if tid is None:
            continue
        if tid in transporters:
            diags.error(errors.DUPLICATE_TRANSPORTER_ID, f"duplicate transporter id {tid!r}", shape.join(base, "id"), at=tmap.get("id"))
        else:
            transporters.add(tid)
    return transporters


def _check_id(ymap: YMap, base: str, kind: str, invalid_code: str, diags: Diagnostics) -> str | None:
    """Require a string identifier `id`; return it when valid, else None."""
    node = shape.require(ymap, "id", base, diags)
    if node is None:
        return None
    path = shape.join(base, "id")
    if not (isinstance(node, YScalar) and node.is_str):
        diags.error(errors.WRONG_TYPE, f"{kind} id must be a string", path, at=node)
        return None
    if not is_identifier(node.value):
        diags.error(invalid_code, f"invalid {kind} id {node.value!r}", path, at=node)
        return None
    return node.value


def _check_cross_kind(devices: dict[str, set[str]], transporters: set[str], root: YNode, diags: Diagnostics) -> None:
    """Warn (never error) when one string is used as more than one kind of id."""
    spot_names: set[str] = set()
    for names in devices.values():
        spot_names |= names
    kinds = {"device": set(devices.keys()), "transporter": transporters, "spot": spot_names}
    owner: dict[str, str] = {}
    coincident: set[str] = set()
    for kind, ids in kinds.items():
        for value in ids:
            if value in owner:
                coincident.add(value)
            else:
                owner[value] = kind
    for value in sorted(coincident):
        diags.warning(errors.CROSS_KIND_ID_COINCIDENCE, f"id {value!r} is used across device/spot/transporter", "", at=root)


def _check_transports(node: YNode | None, devices: dict[str, set[str]], transporters: set[str], diags: Diagnostics) -> None:
    seq = shape.as_seq(node, "transports", diags)
    if seq is None:
        return
    seen: set[tuple] = set()
    for i, item in enumerate(seq.items):
        base = f"transports[{i}]"
        tmap = shape.as_map(item, base, diags)
        if tmap is None:
            continue
        shape.unknown_keys(tmap, TRANSPORT_KEYS, base, diags)

        tr = shape.require(tmap, "transporter", base, diags)
        if tr is not None:
            if not (isinstance(tr, YScalar) and tr.is_str):
                diags.error(errors.WRONG_TYPE, "transporter must be a string", shape.join(base, "transporter"), at=tr)
            elif tr.value not in transporters:
                diags.error(errors.UNKNOWN_TRANSPORTER, f"unknown transporter {tr.value!r}", shape.join(base, "transporter"), at=tr)

        _check_ref_spot(tmap, "from", base, devices, diags)
        _check_ref_spot(tmap, "to", base, devices, diags)
        shape.nonneg_int(shape.require(tmap, "duration", base, diags), shape.join(base, "duration"), diags)

        # Duplicate (transporter, from, to) triple — compared on raw scalar text.
        triple = tuple(_scalar(tmap.get(k)) for k in ("transporter", "from", "to"))
        if None not in triple:
            if triple in seen:
                diags.error(errors.DUPLICATE_TRANSPORT_ENTRY, "duplicate transport entry", base, at=tmap)
            else:
                seen.add(triple)


def _check_ref_spot(tmap: YMap, key: str, base: str, devices: dict[str, set[str]], diags: Diagnostics) -> None:
    """Validate a required qualified-spot reference (`from`/`to`) against the
    defined devices/spots, short-circuiting so one bad ref yields one code."""
    node = tmap.get(key)
    path = shape.join(base, key)
    if node is None:
        diags.error(errors.MISSING_REQUIRED_FIELD, f"missing {key!r}", path, at=tmap)
        return
    if not (isinstance(node, YScalar) and node.is_str):
        diags.error(errors.WRONG_TYPE, f"{key} must be a qualified spot string", path, at=node)
        return
    _resolve_spot(node, path, devices, None, diags)


def _check_processes(node: YNode | None, devices: dict[str, set[str]], diags: Diagnostics) -> None:
    pmap = shape.as_map(node, "processes", diags)
    if pmap is None:
        return
    for entry in pmap.entries:
        base = f"processes.{entry.key}"
        proc = shape.as_map(entry.value, base, diags)
        if proc is None:
            continue
        shape.unknown_keys(proc, PROCESS_KEYS, base, diags)
        modes = shape.as_seq(shape.require(proc, "modes", base, diags), shape.join(base, "modes"), diags)
        if modes is None:
            continue
        if not modes.items:
            diags.error(errors.EMPTY_MODES, "process has no modes", shape.join(base, "modes"), at=modes)
            continue
        for j, mode in enumerate(modes.items):
            _check_mode(mode, f"{base}.modes[{j}]", devices, diags)


def _check_mode(node: YNode, base: str, devices: dict[str, set[str]], diags: Diagnostics) -> None:
    mmap = shape.as_map(node, base, diags)
    if mmap is None:
        return
    shape.unknown_keys(mmap, MODE_KEYS, base, diags)

    # Optional mode id: an identifier when present.
    idn = mmap.get("id")
    if idn is not None:
        if not (isinstance(idn, YScalar) and idn.is_str):
            diags.error(errors.WRONG_TYPE, "mode id must be a string", shape.join(base, "id"), at=idn)
        elif not is_identifier(idn.value):
            diags.error(errors.INVALID_IDENTIFIER, f"invalid mode id {idn.value!r}", shape.join(base, "id"), at=idn)

    # Optional devices list -> the set this mode occupies (for spot_device checks).
    # Each entry must be a defined device (§5.5); an undefined one is caught here
    # even when no spot references it (a device the mode merely occupies).
    mode_devices: set[str] | None = None
    dev_node = mmap.get("devices")
    if dev_node is not None:
        dseq = shape.as_seq(dev_node, shape.join(base, "devices"), diags)
        if dseq is not None:
            mode_devices = set()
            for k, dv in enumerate(dseq.items):
                path = shape.join(base, f"devices[{k}]")
                if not (isinstance(dv, YScalar) and dv.is_str):
                    diags.error(errors.WRONG_TYPE, "device id must be a string", path, at=dv)
                    continue
                mode_devices.add(dv.value)
                if dv.value not in devices:
                    diags.error(errors.UNKNOWN_DEVICE, f"unknown device {dv.value!r}", path, at=dv)

    # Required positive-integer duration.
    dur = shape.require(mmap, "duration", base, diags)
    if dur is not None:
        if not (isinstance(dur, YScalar) and dur.is_int):
            diags.error(errors.WRONG_TYPE, "duration must be an integer", shape.join(base, "duration"), at=dur)
        elif dur.value <= 0:
            diags.error(errors.NONPOSITIVE_DURATION, "processing duration must be positive", shape.join(base, "duration"), at=dur)

    _check_mode_spots(mmap.get("input_spots"), shape.join(base, "input_spots"), devices, mode_devices, errors.INPUT_SPOTS_SHARE_SPOT, diags)
    _check_mode_spots(mmap.get("output_spots"), shape.join(base, "output_spots"), devices, mode_devices, errors.OUTPUT_SPOTS_SHARE_SPOT, diags)


def _check_mode_spots(node: YNode | None, path: str, devices: dict[str, set[str]], mode_devices: set[str] | None, share_code: str, diags: Diagnostics) -> None:
    smap = shape.as_map(node, path, diags)
    if smap is None:
        return
    values: list[str] = []
    for entry in smap.entries:
        spot_node = entry.value
        port_path = shape.join(path, entry.key)
        if not (isinstance(spot_node, YScalar) and spot_node.is_str):
            diags.error(errors.WRONG_TYPE, "spot must be a qualified spot string", port_path, at=spot_node)
            continue
        values.append(spot_node.value)
        _resolve_spot(spot_node, port_path, devices, mode_devices, diags)
    # Two ports of one mode sharing a spot (checked among the well-formed strings).
    if len(values) != len(set(values)):
        diags.error(share_code, "two ports in the mode use the same spot", path, at=smap)


def _resolve_spot(node: YScalar, path: str, devices: dict[str, set[str]], mode_devices: set[str] | None, diags: Diagnostics) -> None:
    """Resolve one qualified spot against defined devices/spots and (optionally)
    the mode's own devices, emitting exactly the first applicable code."""
    parsed = parse_qualified_spot(node.value)
    if parsed is None:
        diags.error(errors.MALFORMED_QUALIFIED_SPOT, f"malformed spot {node.value!r}", path, at=node)
        return
    device, spot = parsed
    if device not in devices:
        diags.error(errors.UNKNOWN_DEVICE, f"unknown device {device!r}", path, at=node)
        return
    if spot not in devices[device]:
        diags.error(errors.UNKNOWN_SPOT, f"unknown spot {spot!r} on {device!r}", path, at=node)
        return
    if mode_devices is not None and device not in mode_devices:
        diags.error(errors.SPOT_DEVICE_NOT_IN_MODE, f"spot's device {device!r} is not one of the mode's devices", path, at=node)


def _check_objective(node: YNode | None, diags: Diagnostics) -> None:
    omap = shape.as_map(node, "objective", diags)
    if omap is None:
        return
    shape.unknown_keys(omap, OBJECTIVE_KEYS, "objective", diags)
    kind = omap.get("kind")
    if kind is None:
        diags.error(errors.MISSING_REQUIRED_FIELD, "objective.kind is required", "objective.kind", at=omap)
        return
    if not (isinstance(kind, YScalar) and kind.is_str and kind.value == "makespan"):
        diags.error(errors.UNKNOWN_OBJECTIVE_KIND, "objective.kind must be makespan", "objective.kind", at=kind)


def _scalar(node: YNode | None):
    """The Python value of a scalar node, or None for anything else."""
    return node.value if isinstance(node, YScalar) else None
