"""Schema validator for the execution document — plan or status
(SPECIFICATIONS.md §9.2). Shape only: it checks a single document on its own and
never reads the workflow or the environment. Cross-document checks (that a node /
arc / process exists, or that a spot is defined) are the execution layer's job
(§9.3).
"""

from __future__ import annotations

from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import Diagnostics, ValidationResult
from ofplang.schedule.core.identifiers import is_identifier, parse_qualified_spot
from ofplang.schedule.core.yamlnode import YMap, YScalar, YSeq, YNode
from ofplang.schedule.validation import _shape as shape
from ofplang.schedule.validation import errors

DOC_TOP = {"time", "now", "outcome", "objective", "interface", "activities", "meta"}
OUTCOMES = {"optimal", "feasible", "infeasible", "unknown"}
# `failed` / `cancelled` are terminal statuses (§6.2): a run stops on any failure,
# so they only ever appear in a final status, never fed back to the scheduler (a
# terminal status as a replan input is rejected, `terminal_status_not_replannable`).
STATUSES = {"pending", "running", "completed", "failed", "cancelled"}
OBJECTIVE_KEYS = {"kind", "value"}
TIME_KEYS = {"unit"}
PROCESSING_KEYS = {"kind", "status", "start", "end", "process", "mode", "node", "devices", "input_spots", "output_spots"}
# A transport carries an optional `seq` (its position in a multi-leg chain that
# serves one logical arc; absent on a single-leg transport). See §6.6.
TRANSPORT_KEYS = {"kind", "status", "start", "end", "from_spot", "to_spot", "transporter", "arc", "seq"}
# A relay (§6) is a transport junction: it belongs to a logical `arc` at a `seq`
# position, occupies one `spot`, and is instantaneous (end == start).
RELAY_KEYS = {"kind", "status", "start", "end", "arc", "seq", "spot"}
ACTIVITY_KINDS = {"processing", "transport", "relay"}
ARC_ENDPOINT_KEYS = {"node", "port"}


def validate_document(source) -> ValidationResult:
    """Validate the execution document at `source` (a file path)."""
    root = yamlnode.load_file(source)
    diags = Diagnostics()
    _check(root, diags)
    return ValidationResult(diags.items)


def _check(root: YNode | None, diags: Diagnostics) -> None:
    if not isinstance(root, YMap):
        if root is not None:
            diags.error(errors.WRONG_TYPE, "document must be a mapping", "", at=root)
        else:
            diags.error(errors.WRONG_TYPE, "document is empty", "")
        return

    shape.unknown_keys(root, DOC_TOP, "", diags)

    _check_time(root.get("time"), diags)
    shape.nonneg_int(root.get("now"), "now", diags)
    _check_outcome(root.get("outcome"), diags)
    _check_objective(root.get("objective"), diags)
    _check_interface(root.get("interface"), diags)

    if "activities" not in root:
        diags.error(errors.MISSING_ACTIVITIES, "activities is required", "activities", at=root)
    else:
        activities = shape.as_seq(root.get("activities"), "activities", diags)
        if activities is not None:
            for i, item in enumerate(activities.items):
                _check_activity(item, f"activities[{i}]", diags)


def _check_time(node: YNode | None, diags: Diagnostics) -> None:
    # `time` is an optional echo, but when present it must carry a well-formed
    # `unit`, checked exactly as the environment validator does (§5.1) so the same
    # field is treated the same in both documents.
    tmap = shape.as_map(node, "time", diags)
    if tmap is None:
        return
    shape.unknown_keys(tmap, TIME_KEYS, "time", diags)
    unit = tmap.get("unit")
    if unit is None and "unit" not in tmap:
        diags.error(errors.MISSING_REQUIRED_FIELD, "time.unit is required", "time.unit", at=tmap)
        return
    if not (isinstance(unit, YScalar) and unit.is_str and unit.text.strip()):
        diags.error(errors.EMPTY_TIME_UNIT, "time.unit must be a non-empty string", "time.unit", at=unit or tmap)


def _check_outcome(node: YNode | None, diags: Diagnostics) -> None:
    if node is None:
        return
    if not (isinstance(node, YScalar) and node.is_str and node.value in OUTCOMES):
        diags.error(errors.UNKNOWN_OUTCOME, "outcome is not a defined value", "outcome", at=node)


def _check_objective(node: YNode | None, diags: Diagnostics) -> None:
    omap = shape.as_map(node, "objective", diags)
    if omap is None:
        return
    shape.unknown_keys(omap, OBJECTIVE_KEYS, "objective", diags)
    kind = omap.get("kind")
    if kind is None:
        diags.error(errors.MISSING_REQUIRED_FIELD, "objective.kind is required", "objective.kind", at=omap)
    elif not (isinstance(kind, YScalar) and kind.is_str and kind.value == "makespan"):
        diags.error(errors.UNKNOWN_OBJECTIVE_KIND, "objective.kind must be makespan", "objective.kind", at=kind)
    shape.nonneg_int(omap.get("value"), "objective.value", diags)


def _check_interface(node: YNode | None, diags: Diagnostics) -> None:
    # Shape only (§6.8): `interface` is `{inputs?, outputs?}`, each a map of a port
    # identifier to a qualified spot. That a port is an Object-bearing boundary port
    # (and completeness / spot existence) is the execution layer's job (§9.3).
    imap = shape.as_map(node, "interface", diags)
    if imap is None:
        return
    shape.unknown_keys(imap, {"inputs", "outputs"}, "interface", diags)
    for side in ("inputs", "outputs"):
        smap = shape.as_map(imap.get(side), f"interface.{side}", diags)
        if smap is None:
            continue
        for entry in smap.entries:
            path = f"interface.{side}.{entry.key}"
            if not is_identifier(entry.key):
                diags.error(errors.INVALID_IDENTIFIER, f"invalid port name {entry.key!r}", path, at=entry.value or smap)
            _check_qualified_spot(entry.value, path, diags)


def _check_activity(node: YNode, base: str, diags: Diagnostics) -> None:
    amap = shape.as_map(node, base, diags)
    if amap is None:
        return

    # The kind selects the rest of the schema; if it is absent or unrecognised we
    # cannot validate the other fields, so we stop after that one diagnostic.
    kind_node = amap.get("kind")
    if kind_node is None:
        diags.error(errors.MISSING_REQUIRED_FIELD, "kind is required", shape.join(base, "kind"), at=amap)
        return
    if not (isinstance(kind_node, YScalar) and kind_node.is_str and kind_node.value in ACTIVITY_KINDS):
        diags.error(errors.UNKNOWN_ACTIVITY_KIND, "kind must be processing, transport, or relay", shape.join(base, "kind"), at=kind_node)
        return
    kind = kind_node.value

    allowed = {"processing": PROCESSING_KEYS, "transport": TRANSPORT_KEYS, "relay": RELAY_KEYS}[kind]
    shape.unknown_keys(amap, allowed, base, diags)
    _check_status(amap.get("status"), base, diags)
    _check_interval(amap, base, diags)

    if kind == "processing":
        _check_processing(amap, base, diags)
    elif kind == "transport":
        _check_transport(amap, base, diags)
    else:
        _check_relay(amap, base, diags)


def _check_status(node: YNode | None, base: str, diags: Diagnostics) -> None:
    if node is None:
        return
    if not (isinstance(node, YScalar) and node.is_str and node.value in STATUSES):
        diags.error(
            errors.UNKNOWN_STATUS,
            "status is not pending/running/completed/failed/cancelled",
            shape.join(base, "status"),
            at=node,
        )


def _check_interval(amap: YMap, base: str, diags: Diagnostics) -> None:
    start = shape.require(amap, "start", base, diags)
    end = shape.require(amap, "end", base, diags)
    shape.nonneg_int(start, shape.join(base, "start"), diags)
    shape.nonneg_int(end, shape.join(base, "end"), diags)
    # Ordering is only meaningful once both are integers.
    if isinstance(start, YScalar) and start.is_int and isinstance(end, YScalar) and end.is_int and end.value < start.value:
        diags.error(errors.END_BEFORE_START, "end is earlier than start", shape.join(base, "end"), at=end)


def _check_processing(amap: YMap, base: str, diags: Diagnostics) -> None:
    _require_str(amap, "process", base, diags)
    _require_str(amap, "mode", base, diags)
    node = shape.require(amap, "node", base, diags)
    _check_node_path(node, shape.join(base, "node"), diags)


def _check_node_path(node: YNode | None, path: str, diags: Diagnostics) -> None:
    seq = shape.as_seq(node, path, diags)
    if seq is None:
        return
    if not seq.items:
        diags.error(errors.EMPTY_NODE_PATH, "node path is empty", path, at=seq)
        return
    for i, element in enumerate(seq.items):
        if not (isinstance(element, YScalar) and element.is_str):
            diags.error(errors.WRONG_TYPE, "node-path element must be a string", f"{path}[{i}]", at=element)
        elif not is_identifier(element.value):
            diags.error(errors.INVALID_IDENTIFIER, f"invalid node id {element.value!r}", f"{path}[{i}]", at=element)


def _check_transport(amap: YMap, base: str, diags: Diagnostics) -> None:
    from_spot = shape.require(amap, "from_spot", base, diags)
    to_spot = shape.require(amap, "to_spot", base, diags)
    _check_qualified_spot(from_spot, shape.join(base, "from_spot"), diags)
    _check_qualified_spot(to_spot, shape.join(base, "to_spot"), diags)
    # `transporter` is required for a real move, but a same-spot move (§5.4) is a
    # physical no-op that no transporter carries, so it may be omitted (§6.4). When
    # present it must still be a string.
    if _same_spot(from_spot, to_spot):
        if "transporter" in amap:
            _require_str(amap, "transporter", base, diags)
    else:
        _require_str(amap, "transporter", base, diags)
    arc = shape.require(amap, "arc", base, diags)
    if arc is not None:
        _check_arc(arc, shape.join(base, "arc"), diags)
    # Optional chain position (§6.6); a single-leg transport omits it.
    shape.nonneg_int(amap.get("seq"), shape.join(base, "seq"), diags)


def _check_relay(amap: YMap, base: str, diags: Diagnostics) -> None:
    # A relay is a transport junction (§6): it serves a logical `arc` at a `seq`
    # position and occupies one `spot`. It is instantaneous — `end` must equal
    # `start` (its interval was already range-checked by `_check_interval`).
    arc = shape.require(amap, "arc", base, diags)
    if arc is not None:
        _check_arc(arc, shape.join(base, "arc"), diags)
    _check_qualified_spot(shape.require(amap, "spot", base, diags), shape.join(base, "spot"), diags)
    shape.nonneg_int(shape.require(amap, "seq", base, diags), shape.join(base, "seq"), diags)

    start, end = amap.get("start"), amap.get("end")
    if (
        isinstance(start, YScalar) and start.is_int
        and isinstance(end, YScalar) and end.is_int
        and end.value != start.value
    ):
        diags.error(errors.RELAY_NONZERO_DURATION, "a relay is instantaneous (end must equal start)", shape.join(base, "end"), at=end)


def _check_arc(node: YNode, path: str, diags: Diagnostics) -> None:
    # An arc is `{from, to}`, each endpoint a `{node, port}`; any structural
    # deviation is reported as a single malformed_arc.
    ok = isinstance(node, YMap) and _endpoint_ok(node.get("from")) and _endpoint_ok(node.get("to"))
    if not ok:
        diags.error(errors.MALFORMED_ARC, "malformed arc (need from/to each with node and port)", path, at=node)


def _endpoint_ok(node: YNode | None) -> bool:
    if not isinstance(node, YMap):
        return False
    if any(entry.key not in ARC_ENDPOINT_KEYS for entry in node.entries):
        return False
    path_node = node.get("node")
    port = node.get("port")
    # An empty node path denotes the workflow interface (a boundary arc endpoint,
    # §6.4/§6.8); a non-empty path names an atomic node. Both are well-formed here.
    if not isinstance(path_node, YSeq):
        return False
    if not all(isinstance(x, YScalar) and is_identifier(x.value) for x in path_node.items):
        return False
    return isinstance(port, YScalar) and port.is_str and bool(port.value)


def _check_qualified_spot(node: YNode | None, path: str, diags: Diagnostics) -> None:
    # Shape only: a well-formed `<device>.<spot>` string. Whether the device/spot
    # exist is an execution-layer concern (no environment here).
    if node is None:
        return
    if not (isinstance(node, YScalar) and node.is_str):
        diags.error(errors.WRONG_TYPE, "expected a qualified spot string", path, at=node)
        return
    if parse_qualified_spot(node.value) is None:
        diags.error(errors.MALFORMED_QUALIFIED_SPOT, f"malformed spot {node.value!r}", path, at=node)


def _same_spot(from_spot: YNode | None, to_spot: YNode | None) -> bool:
    """Whether both spots are the same well-formed qualified spot string."""
    return (
        isinstance(from_spot, YScalar) and from_spot.is_str
        and isinstance(to_spot, YScalar) and to_spot.is_str
        and from_spot.value == to_spot.value
    )


def _require_str(amap: YMap, key: str, base: str, diags: Diagnostics) -> None:
    node = shape.require(amap, key, base, diags)
    if node is not None and not (isinstance(node, YScalar) and node.is_str):
        diags.error(errors.WRONG_TYPE, f"{key} must be a string", shape.join(base, key), at=node)
