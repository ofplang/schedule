"""A self-check on the plan this scheduler is about to hand out (SPEC §4.7.2).

The consumable model lives in the solver: a stock is a reservoir whose level must
stay in `[0, capacity]` (FORMULATION §11). The plan is then *rendered* from the
solved model, and the refill amounts are not read off the solver but derived --
§4.7.1 says a planned refill fills to capacity, and the reservoir offers no level
variable to write that against. So the document that leaves here is not literally
the thing the solver proved; it is a second computation over the same answer.

Two computations that must agree, and once agreed on nothing but an argument. This
module checks instead of arguing: replay the rendered plan's own activities against
the levels the run started from, and refuse to hand out a plan whose stocks go
outside `[0, capacity]`.

It is not a validator of anyone's input. Every input has already been checked --
the environment by its schema, the document's `inventories` against the
environment, the reported history by `status_inventory_inconsistent`. Reaching a
finding here means the solver's answer and the rendered document disagree, which is
a defect in this package. It is worth the cost anyway: the failure it guards is
silent. A plan that under-fills a stock schedules cleanly, reports `optimal`, and
runs dry in a real lab hours later.

**Completions before starts.** A refill that ends exactly when a draw starts is the
normal way a schedule packs work -- a device is released at one op's end and taken
at the next op's start, at the same instant. At such an instant the completion is
applied first and the level is checked, then the start (SPEC §4.7). Both levels are
real and both are checked: the one the refill leaves behind must fit in the device
(`<= capacity`), and the one the draw leaves behind must not be negative. This is
the order the solver was built under -- its reservoir sees completions at `2t` and
starts at `2t + 1` -- so this replay agrees with it by construction rather than by
argument.
"""

from __future__ import annotations

from ofplang.schedule.core.identifiers import parse_qualified_resource


def _declared_levels(env, inventories: dict | None) -> dict[tuple[str, str], int]:
    """Every stock the environment declares, at the level the run started with.

    Filled from the environment first so a stock the document does not name starts
    at 0 -- the same rule `_initial_levels` applies when the document is read.
    """
    levels = {
        (device_id, resource): 0
        for device_id, device in env.devices.items()
        for resource in device.resources
    }
    stated = (inventories or {}).get("levels") or {}
    if not isinstance(stated, dict):
        return levels
    for device_id, stock in stated.items():
        if not isinstance(stock, dict):
            continue
        for resource, level in stock.items():
            if isinstance(level, int) and not isinstance(level, bool):
                levels[(device_id, resource)] = level
    return levels


# Ordering key for one level change: the time it happens at, then whether it is a
# completion (0) or a start (1). Completions go first at a shared instant (§4.7),
# and the level is checked between them -- the same separation the solver's
# reservoir gets from mapping completions to `2t` and starts to `2t + 1`.
COMPLETION, START = 0, 1


def _events(activities: list[dict]) -> dict[tuple[int, int], dict[tuple[str, str], int]]:
    """Every level change the plan contains, summed per instant-phase and per stock.

    A processing draws its `consumption` echo at its `start` -- consumption is taken
    when the activity starts (§4.7.2). A refill adds its `amounts` at its `end` --
    stock that has not landed cannot be drawn on. Nothing else touches a stock.
    """
    events: dict[tuple[int, int], dict[tuple[str, str], int]] = {}

    def change(time, phase, key, delta):
        at = (int(time), phase)
        events.setdefault(at, {})
        events[at][key] = events[at].get(key, 0) + delta

    for activity in activities:
        kind = activity.get("kind")
        if kind == "processing":
            for qualified, amount in (activity.get("consumption") or {}).items():
                parsed = parse_qualified_resource(qualified)
                if parsed is not None and isinstance(amount, int):
                    change(activity.get("start", 0), START, parsed, -amount)
        elif kind == "replenishment":
            device = activity.get("device")
            for resource, amount in (activity.get("amounts") or {}).items():
                if device is not None and isinstance(amount, int):
                    change(activity.get("end", 0), COMPLETION, (device, resource), amount)
    return events


def check_plan_inventories(plan: dict, env, inventories: dict | None) -> list[str]:
    """Every way the rendered `plan` drives a stock outside `[0, capacity]`.

    Returns one message per offending stock (the first moment it goes wrong, so a
    single mistake does not read as a dozen), or an empty list when the plan is
    coherent. An empty list is also the answer when nothing consumes: with no
    `consumption` echo anywhere there is no level to get wrong, which covers a
    resource-free environment and `--ignore-resources` alike (§4.7.3).
    """
    activities = plan.get("activities") or []
    events = _events(activities)
    if not events:
        return []

    levels = _declared_levels(env, inventories)
    reported: dict[tuple[str, str], str] = {}
    for at in sorted(events):
        time, phase = at
        for key, delta in events[at].items():
            levels[key] = levels.get(key, 0) + delta
        for key in sorted(events[at]):
            if key in reported:
                continue
            device, resource = key
            entry = env.devices.get(device)
            capacity = entry.resources.get(resource) if entry is not None else None
            level = levels.get(key, 0)
            if level < 0 or (capacity is not None and level > capacity):
                what = "a refill" if phase == COMPLETION else "a draw"
                reported[key] = (
                    f"the plan leaves {device}.{resource} at {level} after {what} "
                    f"at time {time}, outside [0, {capacity}]"
                )
    return [reported[key] for key in sorted(reported)]
