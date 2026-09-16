"""Interchangeable resource classes of one instance (SPECIFICATIONS.md §10.4).

**This reports and does nothing else.** No constraint is added, no model is
changed, no plan differs because of anything here. It exists because the size of
the model is what bounds solve time (dev-notes/report-solver-scalability.md), and
the largest single source of size is a resource the instance offers *several
interchangeable ways* of using: an environment whose loader has W spots gives
every source and sink W modes and every loader-side arc W options, so the model
grows with W² while every one of those choices leads to the same makespan.
Measured on the 64-job benchmark instance: 8,384 modes, of which 8,064 are that
kind of choice -- and the solver never leaves presolve.

Knowing *where* that happens is the prerequisite for doing anything about it, and
it is not readable off the environment: what matters is which processes this
workflow actually instantiated and which routes survive for its arcs. So the
question is asked of the built `Instance`, and the answer is about this instance
rather than about the laboratory. A device two spots of which only this workflow
never tells apart is interchangeable here and possibly not in the next run, which
is the right answer to give and the reason the environment cannot be asked.

## What is being claimed

A set of resources is **interchangeable for this instance** when relabelling its
members maps the instance onto itself -- every activity keeps the same set of
modes and every arc the same set of routes. Then no schedule is lost by choosing
differently among them, which is exactly why the choice costs the search without
buying anything.

Three scopes, because they are separate shapes and a laboratory can carry all
three at once:

- **spot** -- spots of one device (a plate hotel's slots, a loader's bays);
- **device** -- whole devices, carried with their spots (a pool of thermal
  cyclers, two readers of the same model);
- **transporter** -- arms that can make the same moves in the same times.

Measured on the case studies, the transporter scope is the one that finds the
most: the standard RNA-seq laboratory has four identical arms, and **5,285 of its
5,360 route options exist only to choose between them**. The capacity-annotated
growth-curve laboratory carries four spot classes at once (a fridge of eight, an
incubator of eight, and two two-spot devices), which together account for every
mode and route that lab has over the single-spot version of itself.

## How it is decided

A cheap signature proposes candidate groups, then each group is **verified** by
applying the transposition to the instance and checking it comes back unchanged.
The transpositions `(m0 mi)` generate the symmetric group on the group's members,
so verifying those settles the whole class.

🔴 **A class is only ever claimed after verification.** The signature is a
necessary condition and nothing more, so a signature that groups too eagerly
costs a split, never a false claim. That matters even for a diagnostic: this is
the report a later slice would act on, and a false claim there is a legitimate
schedule silently thrown away.

Anything the document has pinned to a *particular* resource is left out before
grouping: a spot carrying reported history, and -- through the verification
itself, since such a node carries a single spot-fixing mode -- a boundary
binding (§6.8) or an `occupied` hold (§6.12). Those resources really are
distinguished by what is on them, however alike the hardware is. So a class
shrinks as a run accumulates history, which is the right way round: the initial
plan is the slow one.

## Why it is indexed

This runs on every plan, including a replan a laboratory is waiting on, so it
must not cost anything. Both halves are therefore O(model) once rather than
O(model) per resource: every signature is accumulated in a single pass, and a
verification looks only where the substitution acts -- the modes and routes that
name one of the two members being swapped. Everything else is literally
unchanged by the substitution and needs no checking.

Measured: a replan (32 modes) costs **0.5 ms**, a case-study laboratory
(`r1_env_c`, 58 modes) **1.4 ms**, and the worst instance in the benchmark
(`s3_w64_s3`: 8,384 modes, 8,320 routes, 64 spots in one class) **290 ms** --
against 1.9 seconds for the same detector written without the index.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ofplang.schedule.core.diagnostics import Diagnostics
from ofplang.schedule.scheduler.instance import Instance
from ofplang.schedule.scheduler.model import Mode
from ofplang.schedule.scheduler.status import Fixation
from ofplang.schedule.validation import errors

# A substitution acts on qualified spots, on device ids, and on transporter ids.
# Three functions rather than one, because a spot swap must not touch device ids
# while a device swap must move both.
Subst = tuple[
    Callable[[str], str], Callable[[str], str], Callable[[str | None], str | None]
]

SPOT = "spot"
DEVICE = "device"
TRANSPORTER = "transporter"


@dataclass(frozen=True)
class InterchangeableClass:
    """One verified class: what kind of resource, which members, and what the
    instance is paying for the choice between them.

    `modes` / `options` count the modes and routes that would collapse into one if
    the class were treated as a single resource of capacity `len(members)`. They
    are what makes a finding worth reporting -- a class of two spots that no
    activity chooses between is interchangeable in exactly the same sense and says
    nothing about solve time."""

    scope: str  # SPOT | DEVICE | TRANSPORTER
    members: tuple[str, ...]
    modes: int
    options: int


def _identity(x):
    return x


_IDENTITY: Subst = (_identity, _identity, _identity)


def _device_of(qualified_spot: str) -> str:
    return qualified_spot.split(".", 1)[0]


def _sortable(transporter: str | None) -> str:
    """A transporter name for ordering. A transporter-less route (§5.4) carries
    `None`, which cannot be sorted beside a name; the empty string can, and no
    transporter is named that."""
    return transporter or ""


# --------------------------------------------------------------------------
# One pass over the instance: where every resource is named, and what names it.
# --------------------------------------------------------------------------


@dataclass
class _Index:
    """Where each resource is named, and the per-resource signature.

    Built in one pass. `modes_at` / `arcs_at` are what let a verification look
    only where the substitution acts; `signature` is the necessary condition that
    proposes candidate groups; `identity_keys` is each activity's own mode set,
    which every verification looks its substituted modes up in."""

    # resource name -> the (activity, mode) pairs that name it
    modes_at: dict[str, set[tuple[int, int]]] = field(default_factory=dict)
    # resource name -> the arcs whose options name it
    arcs_at: dict[str, set[int]] = field(default_factory=dict)
    # activity -> the arcs it is an endpoint of
    arcs_of_activity: dict[int, set[int]] = field(default_factory=dict)
    # resource name -> its signature, per scope
    signature: dict[str, list] = field(default_factory=dict)
    identity_keys: list[dict[tuple, int]] = field(default_factory=list)
    # arc -> its own option keys, so a verification can ask whether a route it
    # moved landed on one the arc already had without rebuilding the whole set
    option_keys: list[frozenset[tuple]] = field(default_factory=list)


def _mode_key(mode: Mode, subst: Subst) -> tuple:
    """Everything a mode *does*, with a substitution applied.

    `id` is excluded on purpose: a mode's name is not part of what it does, and
    interchangeable twins are exactly the modes that differ only in their name and
    in which member of the class they name."""
    sub_spot, sub_device, _ = subst
    return (
        mode.duration,
        tuple(sub_device(d) for d in mode.devices),
        mode.device_access,
        tuple(sorted((p, sub_spot(s)) for p, s in mode.input_spots.items())),
        tuple(sorted((p, sub_spot(s)) for p, s in mode.output_spots.items())),
        # A consumption key is a qualified `device.resource`, so a device swap
        # moves it too.
        tuple(
            sorted(
                (sub_device(_device_of(k)) + "." + k.split(".", 1)[1], amount)
                for k, amount in mode.consumption.items()
            )
        ),
    )


def _option_key(option, src_perm: dict[int, int], dst_perm: dict[int, int], subst: Subst) -> tuple:
    """A route, with the substitution applied -- including the endpoint modes it
    names, which have moved with it. A mode absent from a permutation was not
    moved, so it stands for itself."""
    sub_spot, _, sub_transporter = subst
    return (
        src_perm.get(option.src_mode_index, option.src_mode_index),
        dst_perm.get(option.dst_mode_index, option.dst_mode_index),
        sub_transporter(option.transporter),
        sub_spot(option.from_spot),
        sub_spot(option.to_spot),
        option.duration,
    )


def _build_index(instance: Instance, scope: str) -> _Index:
    """Index the instance for one scope, in a single pass over it."""
    index = _Index()
    index.identity_keys = [
        {_mode_key(mode, _IDENTITY): m for m, mode in enumerate(act.modes)}
        for act in instance.activities
    ]

    def at_mode(name: str, activity: int, mode: int) -> None:
        index.modes_at.setdefault(name, set()).add((activity, mode))

    def at_arc(name: str, arc: int) -> None:
        index.arcs_at.setdefault(name, set()).add(arc)

    def sig(name: str) -> list:
        return index.signature.setdefault(name, [])

    for i, act in enumerate(instance.activities):
        for m, mode in enumerate(act.modes):
            if scope == SPOT:
                for spot in set(mode.input_spots.values()) | set(mode.output_spots.values()):
                    at_mode(spot, i, m)
                    # What an activity does with a spot: a spot used differently by
                    # any activity is not the same resource as its neighbour.
                    sig(spot).append(
                        ("mode", i, mode.duration, tuple(mode.devices), mode.device_access)
                    )
            elif scope == DEVICE:
                for device in mode.devices:
                    at_mode(device, i, m)
                    sig(device).append(
                        (
                            "mode",
                            i,
                            mode.duration,
                            mode.device_access,
                            len(mode.devices),
                            tuple(sorted(mode.input_spots)),
                            tuple(sorted(mode.output_spots)),
                        )
                    )
                for key in mode.consumption:
                    at_mode(_device_of(key), i, m)

    index.option_keys = [
        frozenset(_option_key(o, {}, {}, _IDENTITY) for o in arc.options)
        for arc in instance.arcs
    ]

    for r, arc in enumerate(instance.arcs):
        index.arcs_of_activity.setdefault(arc.src_activity, set()).add(r)
        index.arcs_of_activity.setdefault(arc.dst_activity, set()).add(r)
        for option in arc.options:
            duration = option.duration
            transporter = _sortable(option.transporter)
            if scope == SPOT:
                at_arc(option.from_spot, r)
                at_arc(option.to_spot, r)
                sig(option.from_spot).append(("route", r, "from", transporter, duration))
                sig(option.to_spot).append(("route", r, "to", transporter, duration))
            elif scope == DEVICE:
                frm, to = _device_of(option.from_spot), _device_of(option.to_spot)
                at_arc(frm, r)
                at_arc(to, r)
                # A move that stays inside the device is marked as such rather than
                # naming it, so a pool's internal moves do not tell its members
                # apart.
                sig(frm).append(("route", r, "from", transporter, duration, frm == to))
                sig(to).append(("route", r, "to", transporter, duration, frm == to))
            elif option.transporter is not None:
                at_arc(option.transporter, r)
                sig(option.transporter).append(
                    ("route", r, option.from_spot, option.to_spot, duration)
                )

    if scope == DEVICE:
        # A device is also what it is made of and what can refill it.
        for name, entry in instance.env.devices.items():
            sig(name).append(("spots", tuple(sorted(entry.spots))))
            sig(name).append(("resources", tuple(sorted(entry.resources.items()))))
        for candidate in instance.replenishments:
            sig(candidate.device).append(
                (
                    "refill",
                    tuple(sorted(candidate.resources)),
                    tuple(sorted((o.replenisher, o.duration) for o in candidate.options)),
                )
            )
    return index


# --------------------------------------------------------------------------
# Verification: is relabelling these two an automorphism of the instance?
# --------------------------------------------------------------------------


def _is_automorphism(
    instance: Instance, index: _Index, subst: Subst, a: str, b: str, scope: str
) -> bool:
    """Does swapping `a` and `b` map this instance onto itself?

    Only what names `a` or `b` can move. Every other mode and route is *literally*
    unchanged by the substitution, so it maps to itself and needs no check -- which
    is what makes this affordable on the instances where the answer matters."""
    touched = index.modes_at.get(a, frozenset()) | index.modes_at.get(b, frozenset())
    perms: dict[int, dict[int, int]] = {}
    for activity, mode in touched:
        act = instance.activities[activity]
        original = index.identity_keys[activity]
        if len(original) != len(act.modes):
            # Two modes of one activity that do the same thing. Nothing about that
            # is wrong, but it leaves the index permutation undetermined, and this
            # is a diagnostic: it declines to guess rather than claim a class.
            return False
        target = original.get(_mode_key(act.modes[mode], subst))
        if target is None:
            return False  # the twin this mode would need does not exist
        perms.setdefault(activity, {})[mode] = target

    # An arc has to be re-read when a route of its own names a member, or when
    # either endpoint's modes moved (its options name those modes by index).
    arcs: set[int] = set(index.arcs_at.get(a, frozenset()) | index.arcs_at.get(b, frozenset()))
    for activity in perms:
        arcs |= index.arcs_of_activity.get(activity, frozenset())
    # A route the substitution leaves alone maps to itself and is trivially still
    # one of the arc's own, so only the moved ones are looked at. Checking that
    # each of those lands on a route the arc already had is enough to conclude the
    # whole set maps onto itself: the substitution is an involution on routes, so a
    # map into the set is a bijection of it.
    sub_spot, _, sub_transporter = subst
    for r in arcs:
        arc = instance.arcs[r]
        src_perm = perms.get(arc.src_activity, {})
        dst_perm = perms.get(arc.dst_activity, {})
        before = index.option_keys[r]
        for option in arc.options:
            if (
                sub_spot(option.from_spot) == option.from_spot
                and sub_spot(option.to_spot) == option.to_spot
                and sub_transporter(option.transporter) == option.transporter
                and option.src_mode_index not in src_perm
                and option.dst_mode_index not in dst_perm
            ):
                continue
            if _option_key(option, src_perm, dst_perm, subst) not in before:
                return False

    if scope == DEVICE:
        # A refill names a device and the resources it tops up, so a device swap
        # has to map the candidate set onto itself as well. Cheap enough to
        # compare whole: an instance has few refill candidates.
        _, sub_device, _ = subst

        def refills(sub):
            return {
                (
                    sub(c.device),
                    tuple(sorted(c.resources)),
                    tuple(sorted((o.replenisher, o.duration) for o in c.options)),
                )
                for c in instance.replenishments
            }

        if refills(_identity) != refills(sub_device):
            return False
    return True


def _spot_swap(a: str, b: str) -> Subst:
    def spot(q: str) -> str:
        return b if q == a else (a if q == b else q)

    return spot, _identity, _identity


def _device_swap(a: str, b: str) -> Subst:
    def spot(q: str) -> str:
        device, _, name = q.partition(".")
        if device == a:
            return f"{b}.{name}"
        if device == b:
            return f"{a}.{name}"
        return q

    def device(d: str) -> str:
        return b if d == a else (a if d == b else d)

    return spot, device, _identity


def _transporter_swap(a: str, b: str) -> Subst:
    def transporter(t: str | None) -> str | None:
        return b if t == a else (a if t == b else t)

    return _identity, _identity, transporter


# --------------------------------------------------------------------------
# What the document has pinned, and what a class costs
# --------------------------------------------------------------------------


def _pinned(instance: Instance, fixation: Fixation | None) -> tuple[frozenset[str], frozenset[str]]:
    """The spots and devices reported history has pinned to a particular one.

    A completed or running activity ran in one mode on one machine; that is a fact
    about the world and not a relabelling anybody is free to make."""
    if fixation is None:
        return frozenset(), frozenset()
    spots: set[str] = set()
    devices: set[str] = set()
    for i, fix in fixation.activities.items():
        mode = instance.activities[i].modes[fix.mode_index]
        spots.update(mode.input_spots.values())
        spots.update(mode.output_spots.values())
        devices.update(mode.devices)
    for r, arc_fix in fixation.arcs.items():
        option = instance.arcs[r].options[arc_fix.option_index]
        spots.update((option.from_spot, option.to_spot))
        devices.update((_device_of(option.from_spot), _device_of(option.to_spot)))
    return frozenset(spots), frozenset(devices)


def _cost(instance: Instance, index: _Index, members: frozenset[str]) -> tuple[int, int]:
    """How many modes and routes the choice between these members costs: per
    activity and per arc, every alternative it offers but one.

    Read off the index, so this is proportional to what the class touches rather
    than to the whole instance."""
    per_activity: dict[int, int] = {}
    for member in members:
        for activity, _mode in index.modes_at.get(member, frozenset()):
            per_activity[activity] = per_activity.get(activity, 0) + 1
    modes = sum(count - 1 for count in per_activity.values() if count > 1)

    per_arc: dict[int, int] = {}
    for r in {r for m in members for r in index.arcs_at.get(m, frozenset())}:
        count = 0
        for option in instance.arcs[r].options:
            if (
                option.from_spot in members
                or option.to_spot in members
                or _device_of(option.from_spot) in members
                or _device_of(option.to_spot) in members
                or option.transporter in members
            ):
                count += 1
        per_arc[r] = count
    options = sum(count - 1 for count in per_arc.values() if count > 1)
    return modes, options


# --------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------


def _classes_of_scope(
    instance: Instance,
    scope: str,
    candidates: Iterable[Iterable[str]],
    swap: Callable[[str, str], Subst],
) -> list[InterchangeableClass]:
    """Verify each proposed group of one scope, and cost what survives.

    `candidates` is grouped by the caller because the grouping is scope-shaped:
    spots never group across devices, while devices and transporters group across
    the whole instance."""
    index = _build_index(instance, scope)
    found: list[InterchangeableClass] = []
    for group in candidates:
        by_signature: dict[tuple, list[str]] = {}
        for name in sorted(group):
            signature = tuple(sorted(index.signature.get(name, [])))
            by_signature.setdefault(signature, []).append(name)
        for proposed in by_signature.values():
            if len(proposed) < 2:
                continue
            # `(m0 mi)` generate the symmetric group on what survives, so checking
            # each candidate against the first settles the class.
            head, rest = proposed[0], proposed[1:]
            members = [head] + [
                other
                for other in rest
                if _is_automorphism(instance, index, swap(head, other), head, other, scope)
            ]
            if len(members) < 2:
                continue
            modes, options = _cost(instance, index, frozenset(members))
            found.append(InterchangeableClass(scope, tuple(members), modes, options))
    return found


def interchangeable_classes(
    instance: Instance, fixation: Fixation | None = None
) -> tuple[InterchangeableClass, ...]:
    """Every verified interchangeable class of this instance, in scope order.

    Empty is the ordinary answer and not a failure: an instance whose modes are all
    distinguishable -- a different duration, a route that reaches only one of them
    -- has no class, and neither has one that offers no choice at all."""
    pinned_spots, pinned_devices = _pinned(instance, fixation)

    # Spots group within one device only: a spot of another device is a different
    # resource, and the grouping never crosses that line.
    spot_groups = [
        [
            f"{device}.{name}"
            for name in sorted(entry.spots)
            if f"{device}.{name}" not in pinned_spots
        ]
        for device, entry in sorted(instance.env.devices.items())
    ]
    # Devices are swapped with their spots, so only devices whose spots are named
    # alike can be swapped this way. A general bijection between two devices' spot
    # names would be a wider claim than "the same machine twice", and this reports
    # the narrow one.
    device_groups: dict[tuple[str, ...], list[str]] = {}
    for device in sorted(instance.env.devices):
        if device in pinned_devices:
            continue
        shape = tuple(sorted(instance.env.devices[device].spots))
        device_groups.setdefault(shape, []).append(device)
    # A transporter has no attributes of its own, so what tells two apart is only
    # which moves they can make and how long they take.
    transporter_group = [sorted(instance.env.transporters)]

    return tuple(
        _classes_of_scope(instance, SPOT, spot_groups, _spot_swap)
        + _classes_of_scope(instance, DEVICE, device_groups.values(), _device_swap)
        + _classes_of_scope(instance, TRANSPORTER, transporter_group, _transporter_swap)
    )


def report_interchangeable(
    instance: Instance, fixation: Fixation | None, diags: Diagnostics
) -> None:
    """Emit `interchangeable_resources` for every class the choice within costs
    the model something (§10.4).

    A class that costs nothing is not reported: two spots nothing chooses between
    are interchangeable in the same sense and say nothing about solve time, and a
    diagnostic nobody can act on is noise. There is no threshold beyond that --
    what the choice costs is in the message, and how much is too much is the
    reader's to decide."""
    for found in interchangeable_classes(instance, fixation):
        if found.modes == 0 and found.options == 0:
            continue
        diags.warning(
            errors.INTERCHANGEABLE_RESOURCES,
            f"{len(found.members)} interchangeable {found.scope}s "
            f"({', '.join(found.members)}): the instance offers "
            f"{found.modes} modes and {found.options} routes that only choose "
            "between them, and every choice leads to the same schedule",
        )
