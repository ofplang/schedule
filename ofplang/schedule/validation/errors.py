"""Stable error codes for the schema validators (SPECIFICATIONS.md §10).

Codes are shared across ofplang.schedule's validators and are a separate catalog
from ofplang.validate's. Referencing them as constants keeps the validators and
the conformance suite from drifting on spelling.
"""

# §10.1 Shared
UNKNOWN_KEY = "unknown_key"
MISSING_REQUIRED_FIELD = "missing_required_field"
WRONG_TYPE = "wrong_type"
INVALID_IDENTIFIER = "invalid_identifier"
MALFORMED_QUALIFIED_SPOT = "malformed_qualified_spot"
UNKNOWN_OBJECTIVE_KIND = "unknown_objective_kind"
NEGATIVE_VALUE = "negative_value"

# §10.2 Environment definition
MISSING_REQUIRED_SECTION = "missing_required_section"
EMPTY_DEVICES = "empty_devices"
EMPTY_MODES = "empty_modes"
DUPLICATE_DEVICE_ID = "duplicate_device_id"
DUPLICATE_TRANSPORTER_ID = "duplicate_transporter_id"
DUPLICATE_SPOT_ID = "duplicate_spot_id"
CROSS_KIND_ID_COINCIDENCE = "cross_kind_id_coincidence"
NONPOSITIVE_DURATION = "nonpositive_duration"
EMPTY_TIME_UNIT = "empty_time_unit"
UNKNOWN_TRANSPORTER = "unknown_transporter"
UNKNOWN_DEVICE = "unknown_device"
UNKNOWN_SPOT = "unknown_spot"
DUPLICATE_TRANSPORT_ENTRY = "duplicate_transport_entry"
INPUT_SPOTS_SHARE_SPOT = "input_spots_share_spot"
OUTPUT_SPOTS_SHARE_SPOT = "output_spots_share_spot"
SPOT_DEVICE_NOT_IN_MODE = "spot_device_not_in_mode"

# §10.3 Execution document
MISSING_ACTIVITIES = "missing_activities"
UNKNOWN_ACTIVITY_KIND = "unknown_activity_kind"
UNKNOWN_STATUS = "unknown_status"
UNKNOWN_OUTCOME = "unknown_outcome"
END_BEFORE_START = "end_before_start"
EMPTY_NODE_PATH = "empty_node_path"
MALFORMED_ARC = "malformed_arc"
# A `relay` activity (a transport junction, §6) has a non-zero duration; a relay
# is instantaneous, so its `end` must equal its `start`.
RELAY_NONZERO_DURATION = "relay_nonzero_duration"

# Execution layer (§9.3) and scheduling. These are produced by the scheduler
# (not the schema validators) while reading the workflow and building/solving the
# instance. They are error-severity like the rest.
UNSUPPORTED_FEATURE = "unsupported_feature"
NO_ENTRY_PROCESS = "no_entry_process"
PROCESS_NOT_DEFINED = "process_not_defined"
# A composite is (transitively) defined in terms of itself; v0 forbids recursion,
# so the expander cannot flatten it. Caught defensively because the scheduler does
# not run ofplang.validate (which would reject it as recursive_process_dependency).
RECURSIVE_COMPOSITE = "recursive_composite"
NO_CAPABILITY = "no_capability"
# A mode's `input_spots` / `output_spots` names a port the process does not have.
UNKNOWN_PROCESS_PORT = "unknown_process_port"
# A port is mapped on the wrong side (an output port under `input_spots`, or an
# input port under `output_spots`).
WRONG_PORT_DIRECTION = "wrong_port_direction"
# A Pure Data (non-Object-bearing) port is mapped to a spot; only Object-bearing
# ports occupy spots (§5.5).
PURE_DATA_PORT_MAPPED = "pure_data_port_mapped"
# A mode does not map every Object-bearing port of its process (§9.3 coverage).
MODE_PORTS_INCOMPLETE = "mode_ports_incomplete"
ARC_UNREACHABLE = "arc_unreachable"
INFEASIBLE = "infeasible"

# Interface / boundary conditions (§6.8, §9.3). An `interface` binding pins a
# workflow boundary port (entry input / final output) to a spot.
# The bound name is not an Object-bearing boundary port on that side — not a main
# port at all, on the wrong side, or an Object-bearing entry input with no consumer
# (a pass-through, out of scope).
INTERFACE_UNKNOWN_PORT = "interface_unknown_port"
# The bound name is a Pure Data boundary port (occupies no spot).
INTERFACE_PURE_DATA_PORT = "interface_pure_data_port"
# Two `interface.inputs` bind the same spot (an entry spot holds one Object).
INTERFACE_DUPLICATE_SPOT = "interface_duplicate_spot"
# An Object-bearing entry input has no `interface` binding (only where interface is
# required; optional in the current phase).
INTERFACE_INPUT_MISSING = "interface_input_missing"

# Replanning (§9.3): produced while matching an execution status against the
# workflow/instance and building the fixation for the solver. A status names a
# processing activity by its `node` path and a transport by its `arc`, sets a
# `status` (completed / running) and times on started activities, and is assumed
# already normalized (a started transport never feeds a pending processing).
# A status is missing its replan reference time `now`.
STATUS_MISSING_NOW = "status_missing_now"
# A status entry's `node` does not match any processing activity in the workflow.
STATUS_NODE_UNKNOWN = "status_node_unknown"
# A status entry's `arc` does not match any Object-bearing arc in the workflow.
STATUS_ARC_UNKNOWN = "status_arc_unknown"
# A fixed processing activity cannot be pinned: its `mode` has no echo
# (input/output spots, devices) and the current environment does not offer it.
STATUS_MODE_UNKNOWN = "status_mode_unknown"
# A started activity's reported times contradict `now` (a completed activity
# ends after `now`, or a running activity starts after `now`).
STATUS_TIME_INCONSISTENT = "status_time_inconsistent"
# Two status entries fix the same activity (same node) or the same transport leg
# (same arc + seq).
STATUS_DUPLICATE = "status_duplicate"
# A committed transport chain in a replan input is inconsistent: a started
# transport leg whose source processing is not completed, a leg whose from_spot
# does not continue the previous leg's arrival spot, or similar.
BROKEN_TRANSPORT_CHAIN = "broken_transport_chain"
# A replan input carries a terminal status (`failed` / `cancelled`). Such a status
# is a valid execution-document shape, but a run that has failed is not replannable
# (v0 stops the whole run on any failure), so it cannot be fed to the scheduler.
TERMINAL_STATUS_NOT_REPLANNABLE = "terminal_status_not_replannable"

# The `cross_kind_id_coincidence` code is the only warning; everything else is an
# error. The runner and CLI use this to check severity.
WARNING_CODES = frozenset({CROSS_KIND_ID_COINCIDENCE})

ERROR_CODES = frozenset(
    {
        UNKNOWN_KEY,
        MISSING_REQUIRED_FIELD,
        WRONG_TYPE,
        INVALID_IDENTIFIER,
        MALFORMED_QUALIFIED_SPOT,
        UNKNOWN_OBJECTIVE_KIND,
        NEGATIVE_VALUE,
        MISSING_REQUIRED_SECTION,
        EMPTY_DEVICES,
        EMPTY_MODES,
        DUPLICATE_DEVICE_ID,
        DUPLICATE_TRANSPORTER_ID,
        DUPLICATE_SPOT_ID,
        CROSS_KIND_ID_COINCIDENCE,
        NONPOSITIVE_DURATION,
        EMPTY_TIME_UNIT,
        UNKNOWN_TRANSPORTER,
        UNKNOWN_DEVICE,
        UNKNOWN_SPOT,
        DUPLICATE_TRANSPORT_ENTRY,
        INPUT_SPOTS_SHARE_SPOT,
        OUTPUT_SPOTS_SHARE_SPOT,
        SPOT_DEVICE_NOT_IN_MODE,
        MISSING_ACTIVITIES,
        UNKNOWN_ACTIVITY_KIND,
        UNKNOWN_STATUS,
        UNKNOWN_OUTCOME,
        END_BEFORE_START,
        EMPTY_NODE_PATH,
        MALFORMED_ARC,
        RELAY_NONZERO_DURATION,
        UNSUPPORTED_FEATURE,
        NO_ENTRY_PROCESS,
        PROCESS_NOT_DEFINED,
        RECURSIVE_COMPOSITE,
        NO_CAPABILITY,
        UNKNOWN_PROCESS_PORT,
        WRONG_PORT_DIRECTION,
        PURE_DATA_PORT_MAPPED,
        MODE_PORTS_INCOMPLETE,
        ARC_UNREACHABLE,
        INFEASIBLE,
        INTERFACE_UNKNOWN_PORT,
        INTERFACE_PURE_DATA_PORT,
        INTERFACE_DUPLICATE_SPOT,
        INTERFACE_INPUT_MISSING,
        STATUS_MISSING_NOW,
        STATUS_NODE_UNKNOWN,
        STATUS_ARC_UNKNOWN,
        STATUS_MODE_UNKNOWN,
        STATUS_TIME_INCONSISTENT,
        STATUS_DUPLICATE,
        BROKEN_TRANSPORT_CHAIN,
        TERMINAL_STATUS_NOT_REPLANNABLE,
    }
)

# Every declared code (errors and warnings). The conformance runner rejects any
# expected code not present here.
ALL_CODES = ERROR_CODES | WARNING_CODES
