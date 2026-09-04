"""Schema validator for the execution document — plan or status
(SPECIFICATIONS.md §9.2). Shape only: it checks a single document on its own and
never reads the workflow or the environment. Cross-document checks (that a node /
arc / process exists, or that a spot is defined) are the execution layer's job
(§9.3).
"""

from __future__ import annotations

from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import Diagnostics, ValidationResult
from ofplang.schedule.core.identifiers import (
    is_identifier,
    parse_qualified_resource,
    parse_qualified_spot,
)
from ofplang.schedule.core.yamlnode import YMap, YNode, YScalar, YSeq
from ofplang.schedule.validation import _objective, errors
from ofplang.schedule.validation import _shape as shape
from ofplang.schedule.validation.duplicates import check_duplicate_keys

DOC_TOP = {
    "time",
    "now",
    "jobs",
    "occupied",
    "outcome",
    "objective",
    "interface",
    "inventories",
    "activities",
    "meta",
}
# One entry of the `jobs` roster (§6.11): who the job is (`id`, `fingerprint`), what
# constrains it (`release`, `bound`), and where its boundary material sits
# (`interface`) -- the same section a single-workflow document carries at the top
# level, one per job, because it binds one workflow's ports.
JOB_KEYS = {"id", "release", "bound", "fingerprint", "interface"}
# One entry of `occupied` (§6.12): a spot something is sitting on, since when, and
# optionally which job left it there.
OCCUPIED_KEYS = {"spot", "since", "job"}
INVENTORIES_KEYS = {"levels"}
OUTCOMES = {"optimal", "feasible", "infeasible", "unknown"}
# `failed` / `cancelled` are terminal statuses (§6.2): a run stops on any failure,
# so they only ever appear in a final status, never fed back to the scheduler (a
# terminal status as a replan input is rejected, `terminal_status_not_replannable`).
STATUSES = {"pending", "running", "completed", "failed", "cancelled"}
OBJECTIVE_KEYS = {"kind", "value"}
TIME_KEYS = {"unit"}
# `job` scopes an activity's workflow provenance to one of several workflows planned
# together (§6.11). Optional and absent from every single-workflow plan, which is why
# it is not part of the identity of a document that never had one: a plan with one
# workflow is exactly the document it always was.
PROCESSING_KEYS = {
    "kind",
    "job",
    "status",
    "start",
    "end",
    "process",
    "mode",
    "node",
    "devices",
    "input_spots",
    "output_spots",
    "consumption",
    "device_access",
}
# A transport carries an optional `seq` (its position in a multi-leg chain that
# serves one logical arc; absent on a single-leg transport). See §6.6.
TRANSPORT_KEYS = {
    "kind",
    "job",
    "status",
    "start",
    "end",
    "from_spot",
    "to_spot",
    "transporter",
    "arc",
    "seq",
}
# A relay (§6) is a transport junction: it belongs to a logical `arc` at a `seq`
# position, occupies one `spot`, and is instantaneous (end == start).
RELAY_KEYS = {"kind", "job", "status", "start", "end", "arc", "seq", "spot"}
# A replenishment (§6.9) refills one device's stocks. It is the one kind with no
# workflow provenance -- no `node`, no `arc` -- so it carries an explicit `id`, and
# no `job` either: the scheduler decided to run it, and in a joint plan one refill
# commonly serves several jobs (§6.11).
REPLENISHMENT_KEYS = {"kind", "status", "start", "end", "id", "device", "replenisher", "amounts"}
ACTIVITY_KINDS = {"processing", "transport", "relay", "replenishment"}
ARC_ENDPOINT_KEYS = {"node", "port"}


def validate_document(source) -> ValidationResult:
    """Validate the execution document `source`: a path to a file, or an
    already-loaded document (a mapping), so a caller holding it in memory need not
    round-trip it through a file. An in-memory document has no source positions, so
    its diagnostics carry no `location` and locate by `path` alone."""
    return validate_document_node(yamlnode.load_source(source))


def validate_document_node(root: YNode | None) -> ValidationResult:
    """Validate an execution document that is already wrapped (`yamlnode`).

    The entry point for a caller that needs the node tree for something else too --
    the loader that builds a model from the same document, the CLI that guesses the
    document kind -- so the file is parsed once instead of once per pass. `root` is
    None for an empty document, which is itself reported."""
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

    # Repeated keys anywhere in the document, before the schema itself: a repeat
    # is read last-wins, so the document says something other than it appears to
    # (§9, `duplicates`). Reported and then carried on with -- one document can
    # have several independent problems.
    check_duplicate_keys(root, diags)

    shape.unknown_keys(root, DOC_TOP, "", diags)

    _check_time(root.get("time"), diags)
    shape.nonneg_int(root.get("now"), "now", diags)
    _check_outcome(root.get("outcome"), diags)
    _check_objective(root.get("objective"), diags)
    _check_interface(root.get("interface"), diags)
    _check_inventories(root.get("inventories"), diags)
    # The roster comes first because every activity is checked against it: a `job`
    # naming no roster entry is a document that describes work belonging to a job it
    # never introduces. `None` (no roster at all) and an empty set are different --
    # the first says this is a single-workflow document, where a `job` is simply out
    # of place, and both are reported the same way.
    job_ids = _check_jobs(root, diags)
    _check_occupied(root, job_ids, diags)

    if "activities" not in root:
        diags.error(errors.MISSING_ACTIVITIES, "activities is required", "activities", at=root)
    else:
        activities = shape.as_seq(root.get("activities"), "activities", diags)
        if activities is not None:
            for i, item in enumerate(activities.items):
                _check_activity(item, f"activities[{i}]", job_ids, diags)
            _check_activity_ids(activities, diags)


def _check_jobs(root: YMap, diags: Diagnostics) -> set[str] | None:
    """The `jobs` roster (§6.11): the workflows this document's activities came from.

    Returns the ids it names, or **None** where the document has no roster at all --
    a single-workflow document, which is every document written before joint planning
    existed. The two are kept apart because they say different things to an activity
    carrying a `job`, even though both make one an error.

    Order is meaningful and preserved: it is the order the jobs were given, and it is
    where a priority order will live. Nothing reads it yet.
    """
    if "jobs" not in root:
        return None
    seq = shape.as_seq(root.get("jobs"), "jobs", diags)
    if seq is None:
        return set()

    ids: set[str] = set()
    for i, item in enumerate(seq.items):
        base = f"jobs[{i}]"
        jmap = shape.as_map(item, base, diags)
        if jmap is None:
            continue
        shape.unknown_keys(jmap, JOB_KEYS, base, diags)
        # `release` is the earliest start (§6.11) and `bound` the completion time the
        # job was promised; both are times, so both are checked as `now` is.
        shape.nonneg_int(jmap.get("release"), shape.join(base, "release"), diags)
        shape.nonneg_int(jmap.get("bound"), shape.join(base, "bound"), diags)
        if "interface" in jmap:
            _check_interface(jmap.get("interface"), diags, shape.join(base, "interface"))
        # `fingerprint` says which workflow the job runs. Its content is the
        # scheduler's own (`workflow.fingerprint`), so the schema asks only that it be
        # a string -- a validator that re-derived the digest would need the workflow,
        # which is execution-layer (§9.3).
        fp = jmap.get("fingerprint")
        if fp is not None and not (isinstance(fp, YScalar) and fp.is_str):
            diags.error(
                errors.WRONG_TYPE,
                "job fingerprint must be a string",
                shape.join(base, "fingerprint"),
                at=fp,
            )
        node = shape.require(jmap, "id", base, diags)
        if node is None:
            continue
        path = shape.join(base, "id")
        if not (isinstance(node, YScalar) and node.is_str):
            diags.error(errors.WRONG_TYPE, "job id must be a string", path, at=node)
        elif not is_identifier(node.value):
            diags.error(
                errors.INVALID_IDENTIFIER, f"invalid job id {node.value!r}", path, at=node
            )
        elif node.value in ids:
            diags.error(
                errors.DUPLICATE_JOB_ID, f"duplicate job id {node.value!r}", path, at=node
            )
        else:
            ids.add(node.value)
    return ids


def _check_occupied(root: YMap, job_ids: set[str] | None, diags: Diagnostics) -> None:
    """`occupied` (§6.12): spots held by something the plan does not otherwise account
    for -- material a stopped job left behind.

    The scheduler models occupancy through activity intervals, and a completed
    activity's interval has ended, so a spot that still physically holds something is
    free as far as the model can tell. This section is how a document says otherwise.
    `spot` and `since` are required: without the time there is no interval to hold, and
    "occupied from the beginning" is a different claim from "occupied since the failure".
    `job` is optional traceability -- which job left it -- and must name a roster entry
    where there is one.
    """
    if "occupied" not in root:
        return
    seq = shape.as_seq(root.get("occupied"), "occupied", diags)
    if seq is None:
        return
    for i, item in enumerate(seq.items):
        base = f"occupied[{i}]"
        omap = shape.as_map(item, base, diags)
        if omap is None:
            continue
        shape.unknown_keys(omap, OCCUPIED_KEYS, base, diags)
        spot = shape.require(omap, "spot", base, diags)
        if spot is not None:
            _check_qualified_spot(spot, shape.join(base, "spot"), diags)
        since = omap.get("since")
        if since is None and "since" not in omap:
            diags.error(
                errors.MISSING_REQUIRED_FIELD,
                "since is required: it is when the spot became occupied",
                shape.join(base, "since"),
                at=omap,
            )
        else:
            shape.nonneg_int(since, shape.join(base, "since"), diags)
        _check_job(omap.get("job"), base, "occupied", job_ids, diags)


def _check_activity_ids(activities: YSeq, diags: Diagnostics) -> None:
    """An activity `id` identifies one activity within one document (§6.9).

    Only replenishments carry one, and it is the only thing that identifies them --
    every other kind is matched by the workflow element it serves -- so a repeat
    would silently merge two refills on a replan.
    """
    seen: set[str] = set()
    for i, item in enumerate(activities.items):
        if not isinstance(item, YMap):
            continue
        node = item.get("id")
        if not (isinstance(node, YScalar) and node.is_str):
            continue
        if node.value in seen:
            diags.error(
                errors.DUPLICATE_ACTIVITY_ID,
                f"duplicate activity id {node.value!r}",
                f"activities[{i}].id",
                at=node,
            )
        seen.add(node.value)


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
        diags.error(
            errors.EMPTY_TIME_UNIT,
            "time.unit must be a non-empty string",
            "time.unit",
            at=unit or tmap,
        )


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
    stages = None
    if kind is None:
        diags.error(
            errors.MISSING_REQUIRED_FIELD, "objective.kind is required", "objective.kind", at=omap
        )
    else:
        stages = _objective.check_kind(kind, diags)
    _objective.check_value(omap, stages, diags)


def _check_inventories(node: YNode | None, diags: Diagnostics) -> None:
    """Shape only (§6.10): `inventories` is `{initial}`, a map of device id to a map
    of resource name to a non-negative level.

    That the devices and resources exist, that no level is above its capacity, and
    that the section is *required* at all need the environment, so they are the
    execution layer's job (§9.3). Both maps here are open -- device ids and resource
    names are the user's -- so `x-` in either is an ordinary entry (§9.4).
    """
    imap = shape.as_map(node, "inventories", diags)
    if imap is None:
        return
    shape.unknown_keys(imap, INVENTORIES_KEYS, "inventories", diags)
    initial = shape.require(imap, "levels", "inventories", diags)
    dmap = shape.as_map(initial, "inventories.levels", diags)
    if dmap is None:
        return
    for entry in dmap.entries:
        path = shape.join("inventories.levels", entry.key)
        if not is_identifier(entry.key):
            diags.error(
                errors.INVALID_IDENTIFIER,
                f"invalid device id {entry.key!r}",
                path,
                at=entry.value or dmap,
            )
            continue
        levels = shape.as_map(entry.value, path, diags)
        if levels is None:
            continue
        for level in levels.entries:
            level_path = shape.join(path, level.key)
            if not is_identifier(level.key):
                diags.error(
                    errors.INVALID_IDENTIFIER,
                    f"invalid resource name {level.key!r}",
                    level_path,
                    at=level.value or levels,
                )
                continue
            shape.nonneg_int(level.value, level_path, diags)


def _check_interface(node: YNode | None, diags: Diagnostics, base: str = "interface") -> None:
    # Shape only (§6.8): `interface` is `{inputs?, outputs?}`, each a map of a port
    # identifier to a qualified spot. That a port is an Object-bearing boundary port
    # (and completeness / spot existence) is the execution layer's job (§9.3).
    #
    # `base` is where it is being reported from: the document's own `interface` for a
    # single workflow, or a roster entry's for one job of a joint plan (§6.11). The
    # shape is the same either way, and saying so once is what keeps the two from
    # drifting -- a binding a joint plan accepted and a single-workflow one refused
    # would be a difference with no reason behind it.
    imap = shape.as_map(node, base, diags)
    if imap is None:
        return
    shape.unknown_keys(imap, {"inputs", "outputs"}, base, diags)
    for side in ("inputs", "outputs"):
        smap = shape.as_map(imap.get(side), f"{base}.{side}", diags)
        if smap is None:
            continue
        for entry in smap.entries:
            path = f"{base}.{side}.{entry.key}"
            if not is_identifier(entry.key):
                diags.error(
                    errors.INVALID_IDENTIFIER,
                    f"invalid port name {entry.key!r}",
                    path,
                    at=entry.value or smap,
                )
            _check_qualified_spot(entry.value, path, diags)


def _check_activity(
    node: YNode, base: str, job_ids: set[str] | None, diags: Diagnostics
) -> None:
    amap = shape.as_map(node, base, diags)
    if amap is None:
        return

    # The kind selects the rest of the schema; if it is absent or unrecognised we
    # cannot validate the other fields, so we stop after that one diagnostic.
    kind_node = amap.get("kind")
    if kind_node is None:
        diags.error(
            errors.MISSING_REQUIRED_FIELD, "kind is required", shape.join(base, "kind"), at=amap
        )
        return
    if not (
        isinstance(kind_node, YScalar) and kind_node.is_str and kind_node.value in ACTIVITY_KINDS
    ):
        diags.error(
            errors.UNKNOWN_ACTIVITY_KIND,
            "kind must be processing, transport, or relay",
            shape.join(base, "kind"),
            at=kind_node,
        )
        return
    kind = kind_node.value

    allowed = {
        "processing": PROCESSING_KEYS,
        "transport": TRANSPORT_KEYS,
        "relay": RELAY_KEYS,
        "replenishment": REPLENISHMENT_KEYS,
    }[kind]
    shape.unknown_keys(amap, allowed, base, diags)
    _check_job(amap.get("job"), base, kind, job_ids, diags)
    _check_status(amap.get("status"), base, diags)
    _check_interval(amap, base, diags)

    if kind == "processing":
        _check_processing(amap, base, diags)
    elif kind == "transport":
        _check_transport(amap, base, diags)
    elif kind == "replenishment":
        _check_replenishment(amap, base, diags)
    else:
        _check_relay(amap, base, diags)


def _check_job(
    node: YNode | None, base: str, kind: str, job_ids: set[str] | None, diags: Diagnostics
) -> None:
    """`job` (§6.11): which of the jointly planned workflows this activity came from.

    An identifier, because it prefixes the activity's provenance the way a machine id
    names a machine, and it must name an entry of the document's `jobs` roster: an
    activity belonging to a job the document never introduces has provenance that
    cannot be resolved.

    Required exactly where the roster is. A document that lists jobs and then leaves
    an activity unattributed is half-converted -- there is no "the" job to fall back
    on. Two things are exempt: a `replenishment`, which belongs to no job because one
    refill may serve several (§6.9), and an `occupied` entry (§6.12), where naming the
    job that left the material is traceability rather than provenance -- nobody may
    know, and the occupancy is real either way.
    """
    if node is None:
        if job_ids is not None and kind not in ("replenishment", "occupied"):
            diags.error(
                errors.MISSING_REQUIRED_FIELD,
                "job is required where the document lists jobs",
                shape.join(base, "job"),
            )
        return
    path = shape.join(base, "job")
    if not (isinstance(node, YScalar) and node.is_str):
        diags.error(errors.WRONG_TYPE, "job must be a string", path, at=node)
    elif not is_identifier(node.value):
        diags.error(errors.INVALID_IDENTIFIER, f"invalid job {node.value!r}", path, at=node)
    elif job_ids is None:
        diags.error(
            errors.UNKNOWN_JOB,
            f"job {node.value!r} but the document lists no jobs",
            path,
            at=node,
        )
    elif node.value not in job_ids:
        diags.error(
            errors.UNKNOWN_JOB, f"job {node.value!r} is not in jobs", path, at=node
        )


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
    if (
        isinstance(start, YScalar)
        and start.is_int
        and isinstance(end, YScalar)
        and end.is_int
        and end.value < start.value
    ):
        diags.error(
            errors.END_BEFORE_START, "end is earlier than start", shape.join(base, "end"), at=end
        )


def _check_processing(amap: YMap, base: str, diags: Diagnostics) -> None:
    _require_str(amap, "process", base, diags)
    _require_str(amap, "mode", base, diags)
    node = shape.require(amap, "node", base, diags)
    _check_node_path(node, shape.join(base, "node"), diags)
    _check_consumption(amap.get("consumption"), shape.join(base, "consumption"), diags)
    # The `device_access` echo (§6.3). Shape only: whether it agrees with the
    # environment is not this validator's business (nothing here reads the
    # environment), and a fixed activity is deliberately read from its echo rather
    # than from the environment anyway (§7).
    access = amap.get("device_access")
    if access is not None and not (isinstance(access, YScalar) and access.is_bool):
        diags.error(
            errors.WRONG_TYPE,
            "device_access must be a boolean",
            shape.join(base, "device_access"),
            at=access,
        )


def _check_consumption(node: YNode | None, path: str, diags: Diagnostics) -> None:
    """A processing activity's `consumption` echo (§6.3): qualified resource ->
    positive amount, the same shape a mode declares (§5.5).

    Shape only. That the device and resource exist needs the environment, which this
    validator never reads (§9.2); the execution layer resolves them. The map is open
    (its keys are the user's resources), so `x-` in it is an ordinary entry (§9.4).
    """
    cmap = shape.as_map(node, path, diags)
    if cmap is None:
        return
    for entry in cmap.entries:
        key_path = shape.join(path, entry.key)
        if parse_qualified_resource(entry.key) is None:
            diags.error(
                errors.MALFORMED_QUALIFIED_RESOURCE,
                f"{entry.key!r} is not a qualified resource <device>.<resource>",
                key_path,
                at=entry.value or cmap,
            )
            continue
        amount = entry.value
        if not (isinstance(amount, YScalar) and amount.is_int):
            diags.error(errors.WRONG_TYPE, "consumption must be an integer", key_path, at=amount)
        elif amount.value <= 0:
            diags.error(
                errors.NONPOSITIVE_VALUE, "consumption must be positive", key_path, at=amount
            )


def _check_node_path(node: YNode | None, path: str, diags: Diagnostics) -> None:
    seq = shape.as_seq(node, path, diags)
    if seq is None:
        return
    if not seq.items:
        diags.error(errors.EMPTY_NODE_PATH, "node path is empty", path, at=seq)
        return
    for i, element in enumerate(seq.items):
        if not (isinstance(element, YScalar) and element.is_str):
            diags.error(
                errors.WRONG_TYPE, "node-path element must be a string", f"{path}[{i}]", at=element
            )
        elif not is_identifier(element.value):
            diags.error(
                errors.INVALID_IDENTIFIER,
                f"invalid node id {element.value!r}",
                f"{path}[{i}]",
                at=element,
            )


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


def _check_replenishment(amap: YMap, base: str, diags: Diagnostics) -> None:
    """A replenishment activity (§6.9): `id`, `device`, `replenisher`, `amounts`.

    Shape only. That the device and replenisher exist, and that the amounts name
    resources it holds, needs the environment and is the execution layer's job
    (§9.3). `amounts` keys are **bare** resource names -- `device` already says
    whose stock it is -- unlike a mode's `consumption`, which must qualify because a
    mode may name several devices.
    """
    for key in ("id", "device", "replenisher"):
        node = shape.require(amap, key, base, diags)
        if node is None:
            continue
        path = shape.join(base, key)
        if not (isinstance(node, YScalar) and node.is_str):
            diags.error(errors.WRONG_TYPE, f"{key} must be a string", path, at=node)
        elif not is_identifier(node.value):
            diags.error(errors.INVALID_IDENTIFIER, f"invalid {key} {node.value!r}", path, at=node)

    amounts = shape.require(amap, "amounts", base, diags)
    path = shape.join(base, "amounts")
    amap_amounts = shape.as_map(amounts, path, diags)
    if amap_amounts is None:
        return
    if not amap_amounts.entries:
        # A refill that adds nothing would hold two machines for no reason.
        diags.error(errors.EMPTY_AMOUNTS, "amounts must not be empty", path, at=amap_amounts)
        return
    for entry in amap_amounts.entries:
        entry_path = shape.join(path, entry.key)
        if not is_identifier(entry.key):
            diags.error(
                errors.INVALID_IDENTIFIER,
                f"invalid resource name {entry.key!r}",
                entry_path,
                at=entry.value or amap_amounts,
            )
            continue
        value = entry.value
        if not (isinstance(value, YScalar) and value.is_int):
            diags.error(errors.WRONG_TYPE, "amount must be an integer", entry_path, at=value)
        elif value.value <= 0:
            diags.error(
                errors.NONPOSITIVE_VALUE, "amount must be positive", entry_path, at=value
            )


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
        diags.error(
            errors.RELAY_NONZERO_DURATION,
            "a relay is instantaneous (end must equal start)",
            shape.join(base, "end"),
            at=end,
        )


def _check_arc(node: YNode, path: str, diags: Diagnostics) -> None:
    # An arc is `{from, to}`, each endpoint a `{node, port}`; any structural
    # deviation is reported as a single malformed_arc.
    ok = isinstance(node, YMap) and _endpoint_ok(node.get("from")) and _endpoint_ok(node.get("to"))
    if not ok:
        diags.error(
            errors.MALFORMED_ARC,
            "malformed arc (need from/to each with node and port)",
            path,
            at=node,
        )


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
        diags.error(
            errors.MALFORMED_QUALIFIED_SPOT, f"malformed spot {node.value!r}", path, at=node
        )


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
