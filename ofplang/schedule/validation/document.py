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

DOC_TOP = {"time", "now", "outcome", "objective", "activities", "placements", "meta"}
OUTCOMES = {"optimal", "feasible", "infeasible", "unknown"}
STATUSES = {"pending", "running", "completed"}
OBJECTIVE_KEYS = {"kind", "value"}
TIME_KEYS = {"unit"}
PROCESSING_KEYS = {"kind", "status", "start", "end", "process", "mode", "node", "devices", "input_spots", "output_spots"}
TRANSPORT_KEYS = {"kind", "status", "start", "end", "from_spot", "to_spot", "transporter", "arc"}
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
    _check_placements(root.get("placements"), diags)

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


def _check_placements(node: YNode | None, diags: Diagnostics) -> None:
    seq = shape.as_seq(node, "placements", diags)
    if seq is None:
        return
    for i, item in enumerate(seq.items):
        base = f"placements[{i}]"
        pmap = shape.as_map(item, base, diags)
        if pmap is None:
            continue
        shape.unknown_keys(pmap, {"object", "spot"}, base, diags)
        _check_placement_object(pmap.get("object"), shape.join(base, "object"), diags)
        spot = shape.require(pmap, "spot", base, diags)
        _check_qualified_spot(spot, shape.join(base, "spot"), diags)


def _check_placement_object(node: YNode | None, path: str, diags: Diagnostics) -> None:
    # The object must be exactly `{input}` (an entry input) or exactly
    # `{node, port}` (a produced Object); anything else is malformed.
    if not isinstance(node, YMap):
        diags.error(errors.MALFORMED_PLACEMENT, "placement object must be a mapping", path, at=node)
        return
    keys = set(node.keys())
    if keys not in ({"input"}, {"node", "port"}):
        diags.error(errors.MALFORMED_PLACEMENT, "object must be exactly input or node+port", path, at=node)


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
    if not (isinstance(kind_node, YScalar) and kind_node.is_str and kind_node.value in ("processing", "transport")):
        diags.error(errors.UNKNOWN_ACTIVITY_KIND, "kind must be processing or transport", shape.join(base, "kind"), at=kind_node)
        return
    kind = kind_node.value

    shape.unknown_keys(amap, PROCESSING_KEYS if kind == "processing" else TRANSPORT_KEYS, base, diags)
    _check_status(amap.get("status"), base, diags)
    _check_interval(amap, base, diags)

    if kind == "processing":
        _check_processing(amap, base, diags)
    else:
        _check_transport(amap, base, diags)


def _check_status(node: YNode | None, base: str, diags: Diagnostics) -> None:
    if node is None:
        return
    if not (isinstance(node, YScalar) and node.is_str and node.value in STATUSES):
        diags.error(errors.UNKNOWN_STATUS, "status is not pending/running/completed", shape.join(base, "status"), at=node)


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
    _check_qualified_spot(shape.require(amap, "from_spot", base, diags), shape.join(base, "from_spot"), diags)
    _check_qualified_spot(shape.require(amap, "to_spot", base, diags), shape.join(base, "to_spot"), diags)
    _require_str(amap, "transporter", base, diags)
    arc = shape.require(amap, "arc", base, diags)
    if arc is not None:
        _check_arc(arc, shape.join(base, "arc"), diags)


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
    if not isinstance(path_node, YSeq) or not path_node.items:
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


def _require_str(amap: YMap, key: str, base: str, diags: Diagnostics) -> None:
    node = shape.require(amap, key, base, diags)
    if node is not None and not (isinstance(node, YScalar) and node.is_str):
        diags.error(errors.WRONG_TYPE, f"{key} must be a string", shape.join(base, key), at=node)
