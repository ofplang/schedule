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
from ofplang.schedule.validation import _shape as shape
from ofplang.schedule.validation import errors
from ofplang.schedule.validation.duplicates import check_duplicate_keys

# Allowed keys per structure (unknown keys are errors; §9.1, strict).
ENV_TOP = {
    "time",
    "devices",
    "transporters",
    "transports",
    "replenishers",
    "replenishments",
    "processes",
}
# Keys this format used to define and no longer reads. They are excluded from the
# unknown-key check and reported on their own, because "this moved" and "this means
# nothing here" are different things to tell an author, and only the first can say
# where the section went.
ENV_RETIRED = {"objective"}
REQUIRED_SECTIONS = ("time", "devices", "processes")
TIME_KEYS = {"unit"}
DEVICE_KEYS = {"id", "spots", "resources"}
TRANSPORTER_KEYS = {"id"}
TRANSPORT_KEYS = {"transporter", "from", "to", "duration"}
REPLENISHER_KEYS = {"id"}
REPLENISHMENT_KEYS = {"replenisher", "device", "duration"}
PROCESS_KEYS = {"modes"}
MODE_KEYS = {
    "id",
    "devices",
    "device_access",
    "duration",
    "input_spots",
    "output_spots",
    "consumption",
}
RESOURCE_KEYS = {"capacity"}


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

    shape.unknown_keys(root, ENV_TOP | ENV_RETIRED, "", diags)
    for section in REQUIRED_SECTIONS:
        if section not in root:
            diags.error(errors.MISSING_REQUIRED_SECTION, f"{section} is required", section, at=root)

    _check_time(root.get("time"), diags)
    devices, resources = _check_devices(root.get("devices"), diags)
    transporters = _check_transporters(root.get("transporters"), diags)
    replenishers = _check_replenishers(root.get("replenishers"), diags)
    _check_cross_kind(devices, resources, transporters, replenishers, root, diags)
    _check_transports(root.get("transports"), devices, transporters, diags)
    _check_replenishments(root.get("replenishments"), devices, resources, replenishers, diags)
    _check_processes(root.get("processes"), devices, resources, diags)
    _check_retired(root, diags)


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
    return _check_machines(node, "transporters", "transporter", errors.DUPLICATE_TRANSPORTER_ID,
                           TRANSPORTER_KEYS, diags)


def _check_replenishers(node: YNode | None, diags: Diagnostics) -> dict[str, tuple[str, YNode]]:
    return _check_machines(node, "replenishers", "replenisher", errors.DUPLICATE_REPLENISHER_ID,
                           REPLENISHER_KEYS, diags)


def _check_machines(
    node: YNode | None,
    section: str,
    kind: str,
    duplicate_code: str,
    allowed: set[str],
    diags: Diagnostics,
) -> dict[str, tuple[str, YNode]]:
    """Return {machine_id: (path, id node)} for the well-formed entries of one
    machine list. The position is kept so the cross-kind check can point at the id.

    Transporters and replenishers are the same shape because they are the same kind
    of thing (§8.2): an individually exclusive machine, named by an id, taken out of
    service by that id alone. Only the duplicate code differs, so that a diagnostic
    still names the section the reader is looking at.
    """
    machines: dict[str, tuple[str, YNode]] = {}
    seq = shape.as_seq(node, section, diags)
    if seq is None:
        return machines
    for i, item in enumerate(seq.items):
        base = f"{section}[{i}]"
        mmap = shape.as_map(item, base, diags)
        if mmap is None:
            continue
        shape.unknown_keys(mmap, allowed, base, diags)
        machine_id = _check_id(mmap, base, kind, errors.INVALID_IDENTIFIER, diags)
        if machine_id is None:
            continue
        path = shape.join(base, "id")
        if machine_id in machines:
            diags.error(
                duplicate_code,
                f"duplicate {kind} id {machine_id!r}",
                path,
                at=mmap.get("id"),
            )
        else:
            id_node = mmap.get("id")
            machines[machine_id] = (path, id_node if id_node is not None else mmap)
    return machines


def _check_replenishments(
    node: YNode | None,
    devices: dict[str, set[str]],
    resources: dict[str, dict[str, int | None]],
    replenishers: dict[str, tuple[str, YNode]],
    diags: Diagnostics,
) -> None:
    """The replenishment capability table (§5.7), keyed by `(replenisher, device)`.

    Absence from the table is how the environment says a replenisher cannot refill a
    device -- reachability by presence, exactly as `transports` does it (§5.4). The
    difference from a transport is that a refill is never *required*, so nothing here
    can be unreachable: a device no entry names simply has stocks that only fall.
    """
    seq = shape.as_seq(node, "replenishments", diags)
    if seq is None:
        return
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(seq.items):
        base = f"replenishments[{i}]"
        emap = shape.as_map(item, base, diags)
        if emap is None:
            continue
        shape.unknown_keys(emap, REPLENISHMENT_KEYS, base, diags)

        replenisher = _require_name(emap, "replenisher", base, diags)
        if replenisher is not None and replenisher not in replenishers:
            diags.error(
                errors.UNKNOWN_REPLENISHER,
                f"unknown replenisher {replenisher!r}",
                shape.join(base, "replenisher"),
                at=emap.get("replenisher"),
            )
            replenisher = None

        device = _require_name(emap, "device", base, diags)
        if device is not None and device not in devices:
            diags.error(
                errors.UNKNOWN_DEVICE,
                f"unknown device {device!r}",
                shape.join(base, "device"),
                at=emap.get("device"),
            )
            device = None
        elif device is not None and not resources.get(device):
            # Nothing to refill: the visit would hold two machines and change no level.
            diags.error(
                errors.DEVICE_WITHOUT_RESOURCES,
                f"device {device!r} declares no resources to refill",
                shape.join(base, "device"),
                at=emap.get("device"),
            )

        duration = shape.require(emap, "duration", base, diags)
        if duration is not None:
            path = shape.join(base, "duration")
            if not (isinstance(duration, YScalar) and duration.is_int):
                diags.error(errors.WRONG_TYPE, "duration must be an integer", path, at=duration)
            elif duration.value <= 0:
                # A refill is real work, the same rule a device-occupying processing
                # mode follows (§5.5).
                diags.error(
                    errors.NONPOSITIVE_DURATION,
                    "replenishment duration must be positive",
                    path,
                    at=duration,
                )

        if replenisher is None or device is None:
            continue
        if (replenisher, device) in seen:
            diags.error(
                errors.DUPLICATE_REPLENISHMENT_ENTRY,
                f"duplicate replenishment entry for ({replenisher!r}, {device!r})",
                base,
                at=emap,
            )
        seen.add((replenisher, device))


def _require_name(ymap: YMap, key: str, base: str, diags: Diagnostics) -> str | None:
    """Require a string field; return its value when it is one, else None."""
    node = shape.require(ymap, key, base, diags)
    if node is None:
        return None
    if not (isinstance(node, YScalar) and node.is_str):
        diags.error(errors.WRONG_TYPE, f"{key} must be a string", shape.join(base, key), at=node)
        return None
    return node.value


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
    replenishers: dict[str, tuple[str, YNode]],
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
    # Every pair of machine kinds, so a device/replenisher or transporter/replenisher
    # clash is caught as squarely as the device/transporter one. Reported against the
    # second kind's position, which is where the reader can act.
    conflicts: set[str] = set()
    machine_kinds = (("device", None), ("transporter", transporters), ("replenisher", replenishers))
    seen_machines: dict[str, str] = dict.fromkeys(devices, "device")
    for kind, table in machine_kinds:
        if table is None:
            continue
        for value in sorted(table):
            if value in seen_machines:
                path, node = table[value]
                diags.error(
                    errors.MACHINE_ID_CONFLICT,
                    f"id {value!r} names both a {seen_machines[value]} and a {kind}",
                    path,
                    at=node,
                )
                conflicts.add(value)
            else:
                seen_machines[value] = kind

    spot_names: set[str] = set()
    for names in devices.values():
        spot_names |= names
    resource_names: set[str] = set()
    for stocks in resources.values():
        resource_names |= set(stocks)
    kinds = {
        "device": set(devices.keys()),
        "transporter": set(transporters),
        "replenisher": set(replenishers),
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
            f"id {value!r} is used across device/spot/resource/transporter/replenisher",
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

    # Optional `device_access` (§5.5 / §4.4.2): whether running this mode holds the
    # devices it names. It only means something for a mode that names devices, and a
    # non-accessing mode must hold a spot (one that holds neither device nor spot is
    # the Pure-Data-only mode, written by omitting `devices`) and must not consume
    # (§4.4.2: a stock's events are ordered by the device exclusion its draws sit
    # under). Each is reported once, from the declaration that is wrong.
    access_node = mmap.get("device_access")
    if access_node is not None:
        if not (isinstance(access_node, YScalar) and access_node.is_bool):
            diags.error(
                errors.WRONG_TYPE,
                "device_access must be a boolean",
                shape.join(base, "device_access"),
                at=access_node,
            )
        else:
            if not mode_devices:
                # One error per mistake: a mode with no devices is malformed more
                # fundamentally than by what it holds, so the rules below -- which
                # ask what a *non-accessing* mode must look like -- are not run.
                diags.error(
                    errors.DEVICE_ACCESS_WITHOUT_DEVICES,
                    "device_access needs a mode that names devices",
                    shape.join(base, "device_access"),
                    at=access_node,
                )
            elif access_node.value is False:
                if not _binds_a_spot(mmap):
                    diags.error(
                        errors.DEVICE_ACCESS_WITHOUT_SPOTS,
                        "a non-accessing mode must bind at least one spot",
                        shape.join(base, "device_access"),
                        at=access_node,
                    )
                if mmap.get("consumption") is not None:
                    diags.error(
                        errors.CONSUMPTION_WITHOUT_DEVICE_ACCESS,
                        "a non-accessing mode may not consume resources",
                        shape.join(base, "consumption"),
                        at=mmap.get("consumption"),
                    )

    # Required integer duration. A mode that occupies a device must take positive
    # time (a real operation is never instantaneous), and so must a non-accessing
    # mode, which names devices too -- material resting somewhere for no time is not
    # a step (§5.5). A device-less Pure-Data-only mode (§5.5) holds nothing physical,
    # so a zero duration is coherent -- like a relay or a same-spot transport (§5.4)
    # -- and is allowed. A negative duration is always invalid.
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


def _binds_a_spot(mmap: YMap) -> bool:
    """Whether a mode declares any Object-bearing port -> spot binding. Read off the
    raw maps rather than `_check_mode_spots`, which reports on the entries it finds
    and returns nothing: what matters here is only that something was declared."""
    for key in ("input_spots", "output_spots"):
        node = mmap.get(key)
        if isinstance(node, YMap) and node.entries:
            return True
    return False


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


def _check_retired(root: YMap, diags: Diagnostics) -> None:
    """Report a section this format used to define (§5, ENV_RETIRED).

    `objective` was read here until 0.2.1, where the document's declaration was
    honoured first and this one warned. It says how *this run* is to be optimised,
    which is a property of the run rather than of the lab -- the same argument that
    put `interface` and `inventories` in the execution document -- so it is read
    there and nowhere else now. Its shape is not checked: there is no point telling
    an author their stage list is malformed in a section that is not read at all.
    """
    node = root.get("objective")
    if node is None:
        return
    diags.error(
        errors.OBJECTIVE_IN_ENVIRONMENT,
        "the objective belongs to the execution document, which says how this run "
        "is to be optimised; it is no longer read from the environment",
        "objective",
        at=node,
    )


def _scalar(node: YNode | None):
    """The Python value of a scalar node, or None for anything else."""
    return node.value if isinstance(node, YScalar) else None
