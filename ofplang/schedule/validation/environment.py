"""Schema validator for the execution environment definition (SPECIFICATIONS.md
§9.1). Shape only: it checks a single environment document on its own and never
reads the workflow. Cross-workflow and solvability checks are the execution
layer's job (§9.3).
"""

from __future__ import annotations

from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import Diagnostics, ValidationResult
from ofplang.schedule.core.identifiers import (
    is_identifier,
    parse_qualified_resource,
    parse_qualified_spot,
)
from ofplang.schedule.core.yamlnode import YMap, YNode, YScalar
from ofplang.schedule.validation import _objective, errors
from ofplang.schedule.validation import _shape as shape
from ofplang.schedule.validation.duplicates import check_duplicate_keys

# Allowed keys per structure (unknown keys are errors; §9.1, strict).
ENV_TOP = {"time", "devices", "transporters", "transports", "processes", "objective"}
REQUIRED_SECTIONS = ("time", "devices", "processes")
TIME_KEYS = {"unit"}
DEVICE_KEYS = {"id", "spots", "resources"}
TRANSPORTER_KEYS = {"id"}
TRANSPORT_KEYS = {"transporter", "from", "to", "duration"}
PROCESS_KEYS = {"modes"}
MODE_KEYS = {"id", "devices", "duration", "input_spots", "output_spots", "consumption"}
RESOURCE_KEYS = {"capacity"}
OBJECTIVE_KEYS = {"kind"}


def validate_environment(source) -> ValidationResult:
    """Validate the environment definition `source`: a path to a file, or an
    already-loaded document (a mapping), so a caller holding it in memory need not
    round-trip it through a file. An in-memory document has no source positions, so
    its diagnostics carry no `location` and locate by `path` alone."""
    return validate_environment_node(yamlnode.load_source(source))


def validate_environment_node(root: YNode | None) -> ValidationResult:
    """Validate an environment definition that is already wrapped (`yamlnode`).

    The entry point for a caller that needs the node tree for something else too --
    the loader that builds a model from the same document, the CLI that guesses the
    document kind -- so the file is parsed once instead of once per pass. `root` is
    None for an empty document, which is itself reported."""
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

    # Repeated keys anywhere in the document, before the schema itself: a repeat
    # is read last-wins, so the document says something other than it appears to
    # (§9, `duplicates`). Reported and then carried on with -- one document can
    # have several independent problems.
    check_duplicate_keys(root, diags)

    shape.unknown_keys(root, ENV_TOP, "", diags)
    for section in REQUIRED_SECTIONS:
        if section not in root:
            diags.error(errors.MISSING_REQUIRED_SECTION, f"{section} is required", section, at=root)

    _check_time(root.get("time"), diags)
    devices, resources = _check_devices(root.get("devices"), diags)
    transporters = _check_transporters(root.get("transporters"), diags)
    _check_cross_kind(devices, resources, transporters, root, diags)
    _check_transports(root.get("transports"), devices, transporters, diags)
    _check_processes(root.get("processes"), devices, resources, diags)
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
        diags.error(
            errors.EMPTY_TIME_UNIT,
            "time.unit must be a non-empty string",
            "time.unit",
            at=unit or tmap,
        )


def _check_devices(
    node: YNode | None, diags: Diagnostics
) -> tuple[dict[str, set[str]], dict[str, dict[str, int | None]]]:
    """Return ({device_id: {spot names}}, {device_id: {resource: capacity}}) for
    the well-formed devices found. The resources come back separately because the
    mode checks need capacities, not just names."""
    devices: dict[str, set[str]] = {}
    resources: dict[str, dict[str, int | None]] = {}
    seq = shape.as_seq(node, "devices", diags)
    if seq is None:
        return devices, resources
    if not seq.items:
        diags.error(errors.EMPTY_DEVICES, "devices must not be empty", "devices", at=seq)
        return devices, resources

    for i, item in enumerate(seq.items):
        base = f"devices[{i}]"
        dmap = shape.as_map(item, base, diags)
        if dmap is None:
            continue
        shape.unknown_keys(dmap, DEVICE_KEYS, base, diags)
        dev_id = _check_id(dmap, base, "device", errors.INVALID_IDENTIFIER, diags)
        spots = _check_spots(dmap.get("spots"), base, diags)
        stocks = _check_resources(dmap.get("resources"), base, diags)
        if dev_id is not None:
            if dev_id in devices:
                diags.error(
                    errors.DUPLICATE_DEVICE_ID,
                    f"duplicate device id {dev_id!r}",
                    shape.join(base, "id"),
                    at=dmap.get("id"),
                )
            else:
                devices[dev_id] = spots
                resources[dev_id] = stocks
    return devices, resources


def _check_resources(node: YNode | None, base: str, diags: Diagnostics) -> dict[str, int | None]:
    """Return {resource name: capacity} for one device's declared stocks (§5.2).

    A resource whose capacity is missing or malformed is still *declared*, and maps
    to None. Dropping it instead would make every mode that draws on it report
    `unknown_resource` as well -- a second diagnostic for a resource the document
    plainly declares, pointing at the wrong place.

    `resources` is an **open** map -- its keys are the user's resource names -- so an
    `x-` key here is an ordinary (badly named) entry, not an extension (§9.4). Each
    definition under it is closed, where `x-` is an extension point as usual. A
    repeated resource name is a repeated mapping key and is already reported as
    `duplicate_key` (§9); nothing extra is said about it here.
    """
    stocks: dict[str, int | None] = {}
    rmap = shape.as_map(node, shape.join(base, "resources"), diags)
    if rmap is None:
        return stocks
    for entry in rmap.entries:
        path = shape.join(base, f"resources.{entry.key}")
        if not is_identifier(entry.key):
            diags.error(
                errors.INVALID_IDENTIFIER,
                f"invalid resource name {entry.key!r}",
                path,
                at=entry.value or rmap,
            )
            continue
        stocks[entry.key] = None
        spec = shape.as_map(entry.value, path, diags)
        if spec is None:
            continue
        shape.unknown_keys(spec, RESOURCE_KEYS, path, diags)
        capacity = shape.require(spec, "capacity", path, diags)
        if capacity is None:
            continue
        cap_path = shape.join(path, "capacity")
        if not (isinstance(capacity, YScalar) and capacity.is_int):
            diags.error(errors.WRONG_TYPE, "capacity must be an integer", cap_path, at=capacity)
            continue
        if capacity.value <= 0:
            diags.error(
                errors.NONPOSITIVE_VALUE,
                "capacity must be positive",
                cap_path,
                at=capacity,
            )
            continue
        stocks[entry.key] = capacity.value
    return stocks


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
            diags.error(
                errors.INVALID_IDENTIFIER, f"invalid spot name {item.value!r}", path, at=item
            )
            continue
        if item.value in spots:
            diags.error(errors.DUPLICATE_SPOT_ID, f"duplicate spot {item.value!r}", path, at=item)
        else:
            spots.add(item.value)
    return spots


def _check_transporters(node: YNode | None, diags: Diagnostics) -> dict[str, tuple[str, YNode]]:
    """Return {transporter_id: (path, id node)} for the well-formed transporters
    found. The position is kept so the cross-kind check can point at the id.
    """
    transporters: dict[str, tuple[str, YNode]] = {}
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
        path = shape.join(base, "id")
        if tid in transporters:
            diags.error(
                errors.DUPLICATE_TRANSPORTER_ID,
                f"duplicate transporter id {tid!r}",
                path,
                at=tmap.get("id"),
            )
        else:
            id_node = tmap.get("id")
            transporters[tid] = (path, id_node if id_node is not None else tmap)
    return transporters


def _check_id(
    ymap: YMap, base: str, kind: str, invalid_code: str, diags: Diagnostics
) -> str | None:
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


def _check_cross_kind(
    devices: dict[str, set[str]],
    resources: dict[str, dict[str, int | None]],
    transporters: dict[str, tuple[str, YNode]],
    root: YNode,
    diags: Diagnostics,
) -> None:
    """Check the ids that are used as more than one kind (§8.2).

    Two machines sharing an id is an **error**: a machine is taken out of service
    by id alone at execution time, so the two would be indistinguishable there.
    Every other coincidence is only a readability **warning** -- a spot is always
    referenced in its qualified form `<device>.<spot>` and a resource in
    `<device>.<resource>` or under a key naming its device, so neither is ever
    mistaken for a machine. Each colliding string yields exactly one diagnostic.
    """
    conflicts = set(devices) & set(transporters)
    for value in sorted(conflicts):
        path, node = transporters[value]
        diags.error(
            errors.MACHINE_ID_CONFLICT,
            f"id {value!r} names both a device and a transporter",
            path,
            at=node,
        )

    spot_names: set[str] = set()
    for names in devices.values():
        spot_names |= names
    resource_names: set[str] = set()
    for stocks in resources.values():
        resource_names |= set(stocks)
    kinds = {
        "device": set(devices.keys()),
        "transporter": set(transporters),
        "spot": spot_names,
        "resource": resource_names,
    }
    owner: dict[str, str] = {}
    coincident: set[str] = set()
    for kind, ids in kinds.items():
        for value in ids:
            if value in owner:
                coincident.add(value)
            else:
                owner[value] = kind
    for value in sorted(coincident - conflicts):
        diags.warning(
            errors.CROSS_KIND_ID_COINCIDENCE,
            f"id {value!r} is used across device/spot/resource/transporter",
            "",
            at=root,
        )


def _check_transports(
    node: YNode | None,
    devices: dict[str, set[str]],
    transporters: dict[str, tuple[str, YNode]],
    diags: Diagnostics,
) -> None:
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
                diags.error(
                    errors.WRONG_TYPE,
                    "transporter must be a string",
                    shape.join(base, "transporter"),
                    at=tr,
                )
            elif tr.value not in transporters:
                diags.error(
                    errors.UNKNOWN_TRANSPORTER,
                    f"unknown transporter {tr.value!r}",
                    shape.join(base, "transporter"),
                    at=tr,
                )

        _check_ref_spot(tmap, "from", base, devices, diags)
        _check_ref_spot(tmap, "to", base, devices, diags)
        shape.nonneg_int(
            shape.require(tmap, "duration", base, diags), shape.join(base, "duration"), diags
        )

        # Duplicate (transporter, from, to) triple — compared on raw scalar text.
        triple = tuple(_scalar(tmap.get(k)) for k in ("transporter", "from", "to"))
        if None not in triple:
            if triple in seen:
                diags.error(
                    errors.DUPLICATE_TRANSPORT_ENTRY, "duplicate transport entry", base, at=tmap
                )
            else:
                seen.add(triple)


def _check_ref_spot(
    tmap: YMap, key: str, base: str, devices: dict[str, set[str]], diags: Diagnostics
) -> None:
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


def _check_processes(
    node: YNode | None,
    devices: dict[str, set[str]],
    resources: dict[str, dict[str, int | None]],
    diags: Diagnostics,
) -> None:
    pmap = shape.as_map(node, "processes", diags)
    if pmap is None:
        return
    for entry in pmap.entries:
        base = f"processes.{entry.key}"
        proc = shape.as_map(entry.value, base, diags)
        if proc is None:
            continue
        shape.unknown_keys(proc, PROCESS_KEYS, base, diags)
        modes = shape.as_seq(
            shape.require(proc, "modes", base, diags), shape.join(base, "modes"), diags
        )
        if modes is None:
            continue
        if not modes.items:
            diags.error(
                errors.EMPTY_MODES, "process has no modes", shape.join(base, "modes"), at=modes
            )
            continue
        for j, mode in enumerate(modes.items):
            _check_mode(mode, f"{base}.modes[{j}]", devices, resources, diags)


def _check_mode(
    node: YNode,
    base: str,
    devices: dict[str, set[str]],
    resources: dict[str, dict[str, int | None]],
    diags: Diagnostics,
) -> None:
    mmap = shape.as_map(node, base, diags)
    if mmap is None:
        return
    shape.unknown_keys(mmap, MODE_KEYS, base, diags)

    # Optional mode id: an identifier when present.
    idn = mmap.get("id")
    if idn is not None:
        if not (isinstance(idn, YScalar) and idn.is_str):
            diags.error(
                errors.WRONG_TYPE, "mode id must be a string", shape.join(base, "id"), at=idn
            )
        elif not is_identifier(idn.value):
            diags.error(
                errors.INVALID_IDENTIFIER,
                f"invalid mode id {idn.value!r}",
                shape.join(base, "id"),
                at=idn,
            )

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

    # Required integer duration. A mode that occupies a device must take positive
    # time (a real operation is never instantaneous). A device-less Pure-Data-only
    # mode (§5.5) holds nothing physical, so a zero duration is coherent -- like a
    # relay or a same-spot transport (§5.4) -- and is allowed. A negative duration
    # is always invalid.
    dur = shape.require(mmap, "duration", base, diags)
    if dur is not None:
        if not (isinstance(dur, YScalar) and dur.is_int):
            diags.error(
                errors.WRONG_TYPE,
                "duration must be an integer",
                shape.join(base, "duration"),
                at=dur,
            )
        else:
            has_devices = bool(mode_devices)
            if dur.value < 0 or (dur.value == 0 and has_devices):
                diags.error(
                    errors.NONPOSITIVE_DURATION,
                    (
                        "processing duration must be positive "
                        "(a device-less pure-data mode may be zero)"
                    ),
                    shape.join(base, "duration"),
                    at=dur,
                )

    _check_mode_spots(
        mmap.get("input_spots"),
        shape.join(base, "input_spots"),
        devices,
        mode_devices,
        errors.INPUT_SPOTS_SHARE_SPOT,
        diags,
    )
    _check_mode_spots(
        mmap.get("output_spots"),
        shape.join(base, "output_spots"),
        devices,
        mode_devices,
        errors.OUTPUT_SPOTS_SHARE_SPOT,
        diags,
    )
    _check_mode_consumption(
        mmap.get("consumption"),
        shape.join(base, "consumption"),
        devices,
        resources,
        mode_devices,
        diags,
    )


def _check_mode_consumption(
    node: YNode | None,
    path: str,
    devices: dict[str, set[str]],
    resources: dict[str, dict[str, int | None]],
    mode_devices: set[str] | None,
    diags: Diagnostics,
) -> None:
    """Check a mode's `consumption` (§5.5): qualified resource -> positive amount.

    Keys are qualified for the same reason spots are -- a mode may name several
    devices, so a bare resource name would not say whose stock is drawn on. The map
    is open (its keys are the user's resources), so `x-` in it is an ordinary entry
    (§9.4) and no closed-key check applies.

    A mode may not draw more of a resource than its device can ever hold: it would
    describe work that cannot run however the schedule is arranged, and everything
    the solver could report about it would be an unexplained infeasibility. Both
    sides are in this document, so it is settled here rather than left to the
    execution layer (§9.1).
    """
    cmap = shape.as_map(node, path, diags)
    if cmap is None:
        return
    for entry in cmap.entries:
        key_path = shape.join(path, entry.key)
        parsed = parse_qualified_resource(entry.key)
        if parsed is None:
            diags.error(
                errors.MALFORMED_QUALIFIED_RESOURCE,
                f"{entry.key!r} is not a qualified resource <device>.<resource>",
                key_path,
                at=entry.value or cmap,
            )
            continue
        device, resource = parsed
        amount = entry.value
        if not (isinstance(amount, YScalar) and amount.is_int):
            diags.error(errors.WRONG_TYPE, "consumption must be an integer", key_path, at=amount)
            continue
        if amount.value <= 0:
            # A resource a mode does not draw on is left out, not written as `0`.
            diags.error(
                errors.NONPOSITIVE_VALUE, "consumption must be positive", key_path, at=amount
            )
            continue
        if device not in devices:
            diags.error(
                errors.UNKNOWN_DEVICE, f"unknown device {device!r}", key_path, at=amount
            )
            continue
        if mode_devices is not None and device not in mode_devices:
            diags.error(
                errors.RESOURCE_DEVICE_NOT_IN_MODE,
                f"device {device!r} is not one of this mode's devices",
                key_path,
                at=amount,
            )
            continue
        stocks = resources.get(device, {})
        if resource not in stocks:
            diags.error(
                errors.UNKNOWN_RESOURCE,
                f"device {device!r} declares no resource {resource!r}",
                key_path,
                at=amount,
            )
            continue
        capacity = stocks[resource]
        # None means the resource is declared but its capacity did not parse, which
        # is already reported where it is written. Nothing to compare against.
        if capacity is not None and amount.value > capacity:
            diags.error(
                errors.CONSUMPTION_EXCEEDS_CAPACITY,
                f"consumes {amount.value} of {entry.key!r}, which holds at most {capacity}",
                key_path,
                at=amount,
            )


def _check_mode_spots(
    node: YNode | None,
    path: str,
    devices: dict[str, set[str]],
    mode_devices: set[str] | None,
    share_code: str,
    diags: Diagnostics,
) -> None:
    smap = shape.as_map(node, path, diags)
    if smap is None:
        return
    values: list[str] = []
    for entry in smap.entries:
        spot_node = entry.value
        port_path = shape.join(path, entry.key)
        if not (isinstance(spot_node, YScalar) and spot_node.is_str):
            diags.error(
                errors.WRONG_TYPE,
                "spot must be a qualified spot string",
                port_path,
                at=spot_node,
            )
            continue
        values.append(spot_node.value)
        _resolve_spot(spot_node, port_path, devices, mode_devices, diags)
    # Two ports of one mode sharing a spot (checked among the well-formed strings).
    if len(values) != len(set(values)):
        diags.error(share_code, "two ports in the mode use the same spot", path, at=smap)


def _resolve_spot(
    node: YScalar,
    path: str,
    devices: dict[str, set[str]],
    mode_devices: set[str] | None,
    diags: Diagnostics,
) -> None:
    """Resolve one qualified spot against defined devices/spots and (optionally)
    the mode's own devices, emitting exactly the first applicable code."""
    parsed = parse_qualified_spot(node.value)
    if parsed is None:
        diags.error(
            errors.MALFORMED_QUALIFIED_SPOT, f"malformed spot {node.value!r}", path, at=node
        )
        return
    device, spot = parsed
    if device not in devices:
        diags.error(errors.UNKNOWN_DEVICE, f"unknown device {device!r}", path, at=node)
        return
    if spot not in devices[device]:
        diags.error(errors.UNKNOWN_SPOT, f"unknown spot {spot!r} on {device!r}", path, at=node)
        return
    if mode_devices is not None and device not in mode_devices:
        diags.error(
            errors.SPOT_DEVICE_NOT_IN_MODE,
            f"spot's device {device!r} is not one of the mode's devices",
            path,
            at=node,
        )


def _check_objective(node: YNode | None, diags: Diagnostics) -> None:
    omap = shape.as_map(node, "objective", diags)
    if omap is None:
        return
    shape.unknown_keys(omap, OBJECTIVE_KEYS, "objective", diags)
    kind = omap.get("kind")
    if kind is None:
        diags.error(
            errors.MISSING_REQUIRED_FIELD,
            "objective.kind is required",
            "objective.kind",
            at=omap,
        )
        return
    _objective.check_kind(kind, diags)


def _scalar(node: YNode | None):
    """The Python value of a scalar node, or None for anything else."""
    return node.value if isinstance(node, YScalar) else None
