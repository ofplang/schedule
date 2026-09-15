"""Public entry point: workflow + environment (+ status) -> execution plan.

Orchestrates the pipeline (validate/load environment -> parse workflow -> build
instance -> solve -> render plan) and collects diagnostics from every stage into
one report. Given a `document_path` that sets `now`, the same pipeline replans: the
execution status is shape-validated, matched against the instance to build the fixation
(completed/running activities pinned, pending re-optimised at/after `now`), and
the fixed history plus `now` and the `interface` constraint are carried into the output.

Every input -- workflow, environment, execution document -- is accepted either as a
path or as an already-loaded document (a mapping), so an embedder that holds them in
memory (the rolling-horizon runner replanning each tick) does not round-trip them
through files. An in-memory document is read as it stands and never written to; the
`interface` echoed into the plan is copied, so the plan shares no structure with it.
A document read from a file is parsed once here: the same wrapped tree is what the
schema validator checks, what `interface` / `now` are read off, and what the
normalizer matches against the instance.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace

from ofplang.schedule.core import objective as objective_stages
from ofplang.schedule.core import yamlnode
from ofplang.schedule.core.diagnostics import ERROR, WARNING, Diagnostic, Diagnostics
from ofplang.schedule.core.yamlnode import YMap
from ofplang.schedule.scheduler.cpsat import Solution, solve
from ofplang.schedule.scheduler.envload import load_environment
from ofplang.schedule.scheduler.instance import (
    build_instance,
    job_membership,
    merge_instances,
    prefix_instance,
    report_unreachable,
)
from ofplang.schedule.scheduler.model import JobSpec, Workflow
from ofplang.schedule.scheduler.normalize import normalize
from ofplang.schedule.scheduler.plan import render_plan
from ofplang.schedule.scheduler.plancheck import check_plan_inventories
from ofplang.schedule.scheduler.stats import SolveStats
from ofplang.schedule.scheduler.status import ActivityFixation, ArcFixation, Fixation
from ofplang.schedule.scheduler.workflow import fingerprint, parse_workflow
from ofplang.schedule.validation import errors
from ofplang.schedule.validation.document import validate_document_node


@dataclass(frozen=True)
class ScheduleReport:
    """Outcome of a scheduling run. `plan` is the execution document (§6) when a
    schedule was produced; `diagnostics` carries every stage's findings."""

    outcome: str | None
    makespan: int | None
    plan: dict | None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # What the solve cost (stats.py). Set on every path that reached the solver --
    # including an infeasible instance and a plan withheld by a defect -- and None
    # where the pipeline stopped before solving, since there is then nothing to
    # measure. It describes the *solve*, never the schedule, which is why none of it
    # goes into the plan document: that stays portable v0 (SPEC §6).
    stats: SolveStats | None = None

    @property
    def ok(self) -> bool:
        return self.plan is not None and self.outcome in ("optimal", "feasible")


_SOLVED = ("optimal", "feasible")


def _has_error(diagnostics) -> bool:
    return any(d.severity == ERROR for d in diagnostics)


def _objective_of(declared, job_count: int = 0) -> tuple[str, ...]:
    """The stages to minimise: the execution document's declaration, else the
    default (§4.8, §6.1).

    One declaration site. The objective says how *this run* is to be optimised, so
    it belongs with the run's other planning inputs rather than with the description
    of the lab -- the same argument that put `interface` and `inventories` in the
    document. The environment was read here too until 0.2.1, first as the only site
    and then as a deprecated fallback; it is now refused there
    (`objective_in_environment`), so nothing reaches this function from the lab.

    The default depends on the job count (§4.8): with several jobs there is something
    for `completion_time_sum` to trade off, and with one there is not. Only the default
    does -- a stated `kind` is honoured as written, whatever the count.

    A `kind` that names no stage list falls back to the default rather than being
    reported: the document validator has already refused it (`unknown_objective_kind`)
    and the pipeline stopped, so the only way to arrive here with one is an unvalidated
    call, where guessing the default beats raising.
    """
    if declared is None:
        return objective_stages.default(job_count)
    return objective_stages.normalize(declared) or objective_stages.default(job_count)


# The statuses a job may leave the plan with (§6.11): work that has run its course,
# whether it got there or not. `running` and `pending` are the two that are still
# somebody's expectation -- a running activity is on a machine now, and `occupied`
# speaks of spots and cannot hold a device.
_FINISHED = ("completed", "cancelled", "failed")


def _roster_entries(roster) -> dict[str, dict]:
    """A document's `jobs` roster (§6.11), by job id. The document has already been
    shape-validated, so every entry is a mapping with a string `id`; anything else is
    simply skipped rather than raising here."""
    return {
        entry["id"]: entry
        for entry in roster
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def _document_activities(doc_path, root) -> list[dict]:
    """The document's activities as plain dicts, from whichever form it arrived in."""
    if isinstance(doc_path, dict):
        listed = doc_path.get("activities")
    elif isinstance(root, yamlnode.YMap):
        listed = yamlnode.to_plain(root.get("activities"))
    else:
        return []
    return [a for a in listed if isinstance(a, dict)] if isinstance(listed, list) else []


def _activity_spots(activity: dict) -> set[str]:
    """Every spot an activity touches: a processing's input and output spots, a
    transport's two ends. What it could be holding, in other words. A replenishment
    touches none — a stock is not material (§4.7)."""
    if activity.get("kind") == "replenishment":
        return set()
    spots = {s for s in (activity.get("input_spots") or {}).values() if isinstance(s, str)}
    spots |= {s for s in (activity.get("output_spots") or {}).values() if isinstance(s, str)}
    spots |= {activity[k] for k in ("from_spot", "to_spot") if isinstance(activity.get(k), str)}
    return spots


def _job_of_activity(activity: dict) -> str | None:
    """Which job an activity belongs to, or None for one that belongs to none — a
    replenishment, which this scheduler decided to run and which commonly serves
    several (§6.9)."""
    if activity.get("kind") == "replenishment":
        return None
    job = activity.get("job")
    return job if isinstance(job, str) else ""


def _trajectory(activities: list[dict], entry_spots: set[str]) -> tuple[set[str], set[str]]:
    """Which spots one job's own history has left holding something, and which spots it
    touched at all — replayed from what its **completed** activities did.

    The effects are the ones the specification gives, applied only by an activity that
    completed:

    - a processing holds its output spots and releases an input spot that is not also
      an output — an in-place port keeps its spot occupied across the operation (§5.5);
    - a transport releases its source and holds its destination, unless the two are the
      same spot, which is a no-op the material sits through (§5.4);
    - a replenishment touches no spot (§4.7).

    **`failed` and `cancelled` need no case here**, which is much of the reason to write
    it this way. A failed activity applied nothing, so the spot keeps whatever the last
    completed one left — and the failure's own spots are claimed separately and
    unconditionally by the caller. Cancelled work never ran, so its material is wherever
    the last completed activity put it, which is what this replay says: the rule that a
    cancelled hand-off leaves the material upstream falls out rather than being written
    down a second time.
    """
    held = set(entry_spots)
    seen = set(held)
    ordered = sorted(
        (a for a in activities if a.get("status") == "completed"),
        key=lambda a: (a.get("end", 0), a.get("start", 0)),
    )
    for activity in ordered:
        seen |= _activity_spots(activity)
        if activity.get("kind") == "transport":
            source, destination = activity.get("from_spot"), activity.get("to_spot")
            if isinstance(source, str) and isinstance(destination, str) and source != destination:
                held.discard(source)
            if isinstance(destination, str):
                held.add(destination)
            continue
        outputs = {s for s in (activity.get("output_spots") or {}).values() if isinstance(s, str)}
        inputs = {s for s in (activity.get("input_spots") or {}).values() if isinstance(s, str)}
        held -= inputs - outputs
        held |= outputs
    return held, seen


def derived_holds(document: dict) -> list[dict]:
    """What this execution document says is held that it does not state (§6.12).

    The answer to "which spots may nothing be planned onto, beyond the ones the
    document names?" -- the holds a stopped job's own history implies, worked out
    exactly as a solve works them out and reported the way `occupied` entries are.

    🔴 **Published because the question has one answer and one place to give it.**
    A runner driving a rolling run has to ask it -- a job arriving onto the spot a
    stopped job's plate is sitting on cannot be admitted -- and a runner that worked
    it out for itself would be a second implementation of a rule that already lives
    here, differing from it in ways that show up only as an unplannable document.
    Deriving it is this package's; asking is anybody's.

    The document is read as it stands, with no validation: it is either one this
    package produced or one about to be handed back to it, and a malformed one simply
    describes fewer holds. Nothing is derived for a job the document does not say has
    stopped, and a spot a running activity is using is not among them -- that activity
    accounts for it perfectly well.
    """
    activities = [a for a in (document.get("activities") or []) if isinstance(a, dict)]
    roster = document.get("jobs")
    entries = _roster_entries(roster) if isinstance(roster, list) else {}
    now = document.get("now")
    now = now if isinstance(now, int) else 0
    held = _holds_of(activities, entries, _stopped_jobs(activities), now)
    return [{"spot": spot, "since": since} for spot, since in sorted(held.items())]


def _stopped_jobs(activities: list[dict]) -> set[str]:
    """The jobs a terminal status has stopped (§6.2, §6.11): a `failed` or `cancelled`
    activity stops the job it belongs to, and only that job."""
    return {
        job
        for a in activities
        if a.get("status") in ("failed", "cancelled")
        and (job := _job_of_activity(a)) is not None
    }


def _holds_of(activities: list[dict], entries: dict | None, job_ids, now: int) -> dict[str, int]:
    """What each of `job_ids` is still holding, and since when (§6.12).

    🔴 **Derived here rather than declared.** A stopped job's material is invisible to
    the model — the activity that put it somewhere has ended, so its interval no longer
    covers the spot — and the plan will send other work onto it. The caller used to be
    the one to work this out and say so in `occupied`; but every input to the working
    out is in this document (the job's completed activities and their spots, the spots
    its failed activity touched, which jobs a terminal status stopped, what is running,
    and the roster's `interface` and `release`), so **the document answers the question
    it poses**, and a caller cannot get it wrong by getting it different.

    Three rules decide what a job is holding, and all three are needed:

    1. **Ownership.** A spot is this job's only if this job touched it *last*. Two jobs
       of one workflow use the same bench slot one after the other, so "every spot this
       job ever touched" would claim the plate the next job has just made.
    2. **Trajectory.** A job can empty a spot it owns — it collects its own entry
       material, or carries a plate away. So ownership is settled by what this job's own
       history did to that spot (`_trajectory`).
    3. **The failure's claim**, unconditionally. A failed activity applies no material
       effect, so what it was carrying is at one of the spots it touched and nothing
       says which; a failed transport therefore claims **both** ends. Over-claiming
       costs a slower plan; under-claiming puts a plate where a plate already is.

    A spot a **running** activity is holding is not residue: §6.12 is for what the plan
    does not otherwise account for, and a running activity accounts for its spots
    perfectly well. It becomes residue the moment that activity comes off, which is why
    this is recomputed every solve rather than settled once.

    `since` is the truthful moment — the end of the last of the job's finished
    activities to touch the spot, or the release at which unmoved entry material
    appeared — capped at `now`. It changes no plan (a held node holds from
    `max(since, now)` either way, §6.12); it is what the record says when a withdrawal
    writes one of these out.

    Asked of the jobs a terminal status has **stopped**, to keep the plan off what they
    left behind, and of a job that is **leaving** the plan, to decide what has to be
    written down before its history goes with it. The question is the same either way,
    so the answer is computed once here and read for both.
    """
    job_ids = sorted(set(job_ids))
    if not job_ids:
        return {}

    # The last activity to **run** decided what is on each spot now. Cancelled work is
    # not among them: it never ran, so it touched nothing -- and it is pinned to a
    # zero-length interval at `now`, which would otherwise make it the last toucher of
    # every spot its mode names and hand it ownership of material it never saw.
    owners: dict[str, tuple[int, str | None]] = {}
    running: set[str] = set()
    for activity in activities:
        if activity.get("status") not in ("completed", "running", "failed"):
            continue
        owner = _job_of_activity(activity)
        end = activity.get("end", 0)
        if activity.get("status") == "running":
            running |= _activity_spots(activity)
        for spot in _activity_spots(activity):
            previous = owners.get(spot)
            if previous is None or end >= previous[0]:
                owners[spot] = (end, owner)

    out: dict[str, int] = {}
    for job_id in job_ids:
        entry = (entries or {}).get(job_id) or {}
        stated_release = entry.get("release")
        release = stated_release if isinstance(stated_release, int) else 0
        # Entry material is *there*, given, from the job's release (§6.8); until the
        # move that collects it, no activity has touched it and only the interface says
        # where it is.
        placed = release <= now
        interface_inputs = {
            spot
            for spot in ((entry.get("interface") or {}).get("inputs") or {}).values()
            if isinstance(spot, str)
        }
        entry_spots = interface_inputs if placed else set()

        mine = [a for a in activities if _job_of_activity(a) == job_id]
        held, seen = _trajectory(mine, entry_spots)
        candidates = {spot for spot, (_end, owner) in owners.items() if owner == job_id}
        candidates |= entry_spots
        claim: set[str] = set()
        for activity in mine:
            if activity.get("status") == "failed":
                claim |= _activity_spots(activity)
        residue = ((candidates & held) | (candidates - seen) | claim) - running

        for spot in residue:
            # Dated by what finished there, so cancelled work -- pinned at `now` and
            # never run -- does not date a spot it never touched.
            ends = [
                a.get("end", 0)
                for a in mine
                if a.get("status") in ("completed", "failed") and spot in _activity_spots(a)
            ]
            if ends:
                since = min(max(ends), now)
            elif spot in interface_inputs:
                since = release
            else:
                since = now
            # One spot, one hold: where two stopped jobs both claim it (a failed
            # transport claims both ends, and the other end may be another job's), the
            # earlier moment is the one that happened.
            out[spot] = min(out[spot], since) if spot in out else since
    return out


def _frozen_note(frozen: list[dict]) -> str:
    """The frozen spots, for the withdrawal's report. One or many reads the same."""
    spots = ", ".join(entry["spot"] for entry in frozen)
    if len(frozen) == 1:
        return (
            f"; {spots} stays occupied (§6.12): leaving collects a bound output spot "
            f"holding the product it promised, and this is not one -- so nothing has "
            f"said what is there is gone"
        )
    return (
        f"; {spots} stay occupied (§6.12): leaving collects a bound output spot "
        f"holding the product it promised, and these are not -- so nothing has said "
        f"what is there is gone"
    )


def _delivered_product(mine: list[dict], spot: str) -> bool:
    """Whether what a leaving job holds at `spot` is the product its binding promised.

    The evidence is the delivery itself (§6.8): a boundary output arc is carried by an
    ordinary transport, so the product reached its spot exactly when a completed move
    of this job arrived there — and it is still what is there only if nothing of this
    job's touched the spot afterwards. Ties go to *not* delivered: over-claiming a hold
    costs a slower plan, under-claiming puts a plate where a plate already is.
    """
    touched = [
        a
        for a in mine
        if a.get("status") in ("completed", "failed") and spot in _activity_spots(a)
    ]
    if not touched:
        return False
    last_end = max(a.get("end", 0) for a in touched)
    return all(
        a.get("status") == "completed"
        and a.get("kind") == "transport"
        and a.get("to_spot") == spot
        and ((a.get("arc") or {}).get("to") or {}).get("node") == []
        for a in touched
        if a.get("end", 0) == last_end
    )


def _frozen_holds(
    withdraw, entries, activities, occupied, now: int, derived: set[str] | None = None
) -> list[dict]:
    """What a withdrawing job is still holding that nobody will be able to say once it
    has gone (D44, §6.12).

    🔴 **This is the one moment an occupancy is written down**, and the rule is: an
    entry may be written only for something that will **not** be derivable after the
    write. Everything a job holds is derivable while the job is in the document
    (`_holds_of` reads it from the history) — and stops being so the instant the job
    leaves and takes that history with it. So it is derived here, before the model is
    built, and written into the plan's `occupied` for the next document to carry.

    **What the caller bound is left out — where it holds what the binding promised.**
    They named the spot and asked for the product to end up there, so leaving is a
    statement about a place they chose and a thing they know is on it. The delivery
    having arrived, and arrived *last*, is what says so (`_delivered_product`): a
    delivery that failed on the way, or material another part of the workflow came to
    rest there afterwards, is not what they asked for, and freeing the spot on its
    account would hand the next job a plate.

    Anything else is written down. The schedule picked where an unbound output came to
    rest (§6.8); a plate a failure left mid-workflow was never anyone's choice; and
    **entry material is written too, though the caller bound its spot** — a job that
    never started still has all of it sitting there, which a job that ran would have
    had collected as its first move, and from outside nobody can tell those apart.
    Whether it is still there, or went on its way and the bay has since been used by
    someone else, is exactly what the history says and what is about to leave with it.
    Freeing a spot with something on it is measurable rather than hypothetical
    (design.md D44).

    `since` is the truthful moment, not the moment the claim is made: the end of the
    last of the job's own activities to touch the spot. It changes no plan — §6.12
    holds an entry from `max(since, now)` either way — but the stated `since` is
    echoed unchanged into the plan, so **it is the only record of when the material
    actually got there**, and the history that would have said so is about to go.

    🔴 `derived` is what the jobs that are **staying** still hold, and nothing in it is
    written. Two stopped jobs can claim one spot — a failed transport claims both its
    ends, and the other end may be where another job's history left something — so a
    spot the leaver holds may be one the stayer goes on deriving. Writing it would put
    the same hold in the document twice from the next replan on, which
    `occupied_already_derived` refuses; and because the entry rides in the echo, that
    would not be one bad call but every call after it.
    """
    held = {
        str(entry.get("spot"))
        for entry in (occupied or [])
        if isinstance(entry, dict) and entry.get("spot") is not None
    }
    held |= derived or set()
    out: list[dict] = []
    for job_id in sorted(withdraw):
        entry = (entries or {}).get(job_id) or {}
        # What the caller **bound** is the half they have just said they collected:
        # they named the spot and asked for the product to end up there. Only where
        # the product really is there, though -- a binding whose delivery failed, or
        # whose spot something else came to rest on, promises nothing about what a
        # leaving caller has in their hands.
        mine = [a for a in activities if _job_of_activity(a) == job_id]
        bound = {
            spot
            for spot in ((entry.get("interface") or {}).get("outputs") or {}).values()
            if isinstance(spot, str) and _delivered_product(mine, spot)
        }
        for spot, since in sorted(_holds_of(activities, entries, [job_id], now).items()):
            if spot in bound or spot in held:
                # A spot is named once (§6.12): a second held node on it would contend
                # with the first and report only `infeasible`.
                continue
            held.add(spot)
            out.append({"spot": spot, "since": since})
    return out


def _check_withdrawals(withdraw, entries, jobs, activities) -> list[Diagnostic]:
    """What must be true before a job may leave the plan (design.md D42).

    Withdrawing is the physical act of collecting what a job left in the laboratory,
    said out loud: the roster is the set of jobs something of which is still there,
    and an entry is removed when nothing is. So the refusals here are the ways the
    document itself says something *is* still there, plus the two ways the call
    contradicts itself.

    🔴 None of them can check the thing that matters -- whether the material was
    really collected. That is a fact about the room, not the document, which is why
    withdrawal is asked for explicitly rather than inferred from a job going quiet.
    What these do is refuse the cases where the document already knows better.

    An `occupied` entry (§6.12) is not one of them, though it once was. Withdrawing
    was refused while an entry named the leaving job, on the reasoning that the
    document was saying something of it is still here. But the residue of a job that
    stops is declared *because* the job is there and moves into `occupied` *because*
    it leaves, so requiring the entry to go first ran the same material backwards --
    and the section no longer names a job to refuse on.
    """
    out: list[Diagnostic] = []
    known = set(entries or {})
    given = {job.id for job in jobs if job.id}

    for job_id in withdraw:
        if job_id not in known:
            out.append(
                Diagnostic(
                    errors.UNKNOWN_WITHDRAWAL,
                    f"job {job_id!r} is not in the document's roster, so there is no "
                    f"entry to withdraw"
                    + (f" (it names {sorted(known)})" if known else " (it names none)"),
                    "jobs",
                )
            )
            continue
        if job_id in given:
            out.append(
                Diagnostic(
                    errors.UNKNOWN_WITHDRAWAL,
                    f"job {job_id!r} was given a workflow to plan and named for "
                    f"withdrawal; it cannot both leave and be planned",
                    "jobs",
                )
            )
            continue
        # Unfinished work is work somebody asked for, and running work is on a
        # machine now -- `occupied` says a spot is taken, and cannot say that.
        #
        # 🔴 **`failed` is finished.** Its interval has ended, so it holds nothing the
        # withdrawal could take away, and the status never changes again -- refusing it
        # would shut a job that died out of the plan for good, which is the case this
        # feature exists for. What it left behind is `occupied`'s to say, and an
        # occupancy outlives the job that left it (§6.12).
        unfinished = sorted(
            {
                (a.get("status") or "pending")
                for a in activities
                if a.get("job") == job_id and (a.get("status") or "pending") not in _FINISHED
            }
        )
        if unfinished:
            out.append(
                Diagnostic(
                    errors.WITHDRAWAL_NOT_FINISHED,
                    f"job {job_id!r} still has {', '.join(unfinished)} work: a job "
                    f"leaves the plan when there is nothing of it left to do",
                    "activities",
                )
            )
    if known and not (known - set(withdraw)):
        out.append(
            Diagnostic(
                errors.WITHDRAWAL_EMPTIES_ROSTER,
                f"withdrawing {sorted(withdraw)} would leave no job to plan",
                "jobs",
            )
        )
    return out


def _carried_levels(env, levels: dict[tuple[str, str], int], now: int) -> dict:
    """`inventories` restated as of `now` (§6.10), for a plan a job has left.

    A level is replayed from the stated one, and the history a withdrawing job takes
    with it is part of what there was to replay. So the levels move forward to the
    moment the job leaves and say so, where the next replan will read them.

    🔴 **Not the numbers the solver started from.** Those are the levels after
    everything the history did, `now`'s draws included, because a started activity
    has taken its consumption and the model does not re-model it. What `at = now`
    means is the other point of that instant -- after its refills, before its draws
    (§6.10) -- and writing the solver's number under that label would hand the next
    replan a level with `now`'s draws already in it, which it would then replay
    again. `Fixation` carries both for exactly this reason.
    """
    out: dict[str, dict[str, int]] = {}
    for (device, resource), level in sorted(levels.items()):
        out.setdefault(device, {})[resource] = level
    return {"levels": out, "at": now}


def _check_document_contradictions(
    activities: list[dict], entries: dict | None, occupied
) -> list:
    """Two ways a document can disagree with itself about what it already says.

    Neither is about the schedule being hard: both are the document stating something
    its own history denies, which no arrangement of the work could satisfy. Left
    unchecked they come back as `infeasible` (or worse, as a plan built on the stated
    half), and the reader has nothing to go on.
    """
    out: list[Diagnostic] = []

    # 1. An occupancy on a spot a running activity is using. §6.12 is for what the plan
    #    does **not otherwise account for**, and a running activity accounts for its
    #    spots perfectly well: the two hold the one spot over overlapping intervals.
    in_use: set[str] = set()
    for activity in activities:
        if activity.get("status") == "running":
            in_use |= _activity_spots(activity)
    for entry in occupied or []:
        if not isinstance(entry, dict):
            continue
        spot = entry.get("spot")
        if isinstance(spot, str) and spot in in_use:
            out.append(
                Diagnostic(
                    errors.OCCUPIED_SPOT_IN_USE,
                    f"{spot!r} is declared occupied and is also being used by an "
                    f"activity that is running: an occupancy is for a hold the plan "
                    f"does not otherwise account for",
                    "occupied",
                )
            )

    # 2. A release later than the job's own history. §J1 holds only *pending* work to a
    #    release -- history is pinned by what happened, and re-holding it would make the
    #    past infeasible rather than say anything about the future -- so a release after
    #    a started activity constrains nothing and contradicts the document that carries
    #    it. Silently tolerated, it is a stated intention nothing enforces.
    earliest: dict[str, int] = {}
    for activity in activities:
        if activity.get("status") not in ("completed", "running", "failed"):
            continue
        job = _job_of_activity(activity)
        start = activity.get("start")
        if job is None or not isinstance(start, int):
            continue
        earliest[job] = min(earliest.get(job, start), start)
    for job_id, entry in sorted((entries or {}).items()):
        release = entry.get("release")
        if not isinstance(release, int) or job_id not in earliest:
            continue
        if release > earliest[job_id]:
            out.append(
                Diagnostic(
                    errors.RELEASE_AFTER_HISTORY,
                    f"job {job_id!r} is released at {release} but its history begins at "
                    f"{earliest[job_id]}: a release holds back work that has not run, "
                    f"and this job's had already started",
                    "jobs",
                )
            )
    return out


def _carry_levels_to(target: int, now: int, current_at: int, diags: list) -> int | None:
    """The moment a plan's `inventories` should be stated for, or None if `target` is
    not a moment it could be stated for.

    🔴 **The scheduler never moves `at` of its own accord.** Which moment a document's
    levels are stated for is the caller's to decide -- they are the one who knows
    whether the history before it may be let go of -- so this is reached only when they
    ask (`carry_levels_to_now`). The scheduler's part is to work out the levels, which
    is the half it can do and they cannot (§4.7.2).

    Two targets are refused rather than adjusted:

    - **later than `now`**, which is the same mistake as a document stating a moment in
      the future, and carries the same code: the history can only have happened before
      the present;
    - **earlier than the moment the levels are already stated for**, because the history
      before that moment is exactly what the caller is entitled to have let go of
      (§4.7.2 requires it complete only *since* `at`), so there may be nothing left to
      replay back to.

    Neither is reachable through `carry_levels_to_now`, which asks for `now` and nothing
    else, and both would be if a caller could name the moment. They are refusals rather
    than a clamp for that reason: a quietly adjusted target is a mistake that keeps
    working until the day it matters.
    """
    if target > now:
        diags.append(
            Diagnostic(
                errors.INVENTORY_MOMENT_IN_FUTURE,
                f"the levels cannot be stated as of {target}: it is later than now "
                f"({now}), and the history can only have happened before the present",
                "inventories.at",
            )
        )
        return None
    if target < current_at:
        diags.append(
            Diagnostic(
                errors.INVENTORY_MOMENT_RETREATS,
                f"the levels cannot be restated as of {target}: they are already stated "
                f"as of {current_at}, and the history before that is not required to be "
                f"here to replay back to it",
                "inventories.at",
            )
        )
        return None
    return target


def _job_specs(jobs, workflows, roster: dict[str, dict] | None, now: int) -> list[JobSpec]:
    """Resolve each job's planning parameters (§6.11) from the roster it appears in.

    A job the roster does not name is **arriving now**, so it starts with no promise
    (`bound = None`, assigned by this solve) and, unless the roster says otherwise,
    cannot begin before `now` -- it did not exist earlier, and a schedule that started
    it in the past would be describing work nobody could have done.

    The fingerprint is always the one just computed from the workflow handed over, not
    the one the roster carries; the two are compared separately (`_check_fingerprints`)
    so a mismatch is reported rather than silently overwritten.
    """
    specs = []
    for job, workflow in zip(jobs, workflows, strict=True):
        entry = (roster or {}).get(job.id) or {}
        arriving = roster is not None and job.id not in roster
        release = entry.get("release")
        if not isinstance(release, int):
            release = now if arriving else 0
        bound = entry.get("bound")
        specs.append(
            JobSpec(
                id=job.id,
                release=release,
                bound=bound if isinstance(bound, int) else None,
                fingerprint=fingerprint(workflow),
                interface=copy.deepcopy(entry.get("interface")),
            )
        )
    return specs


def _check_fingerprints(specs, roster: dict[str, dict]) -> list[Diagnostic]:
    """Each job must be running the workflow its roster entry was planned for (§6.11).

    Nothing else ties an id to a workflow: two jobs handed over in the other order have
    ids that match as a set, so the roster check passes and each job would be matched
    against the other's history. An entry with no recorded fingerprint is left alone --
    it was written before this was recorded, and refusing it would strand documents
    that are otherwise perfectly replannable.
    """
    out = []
    for spec in specs:
        stated = (roster.get(spec.id) or {}).get("fingerprint")
        if isinstance(stated, str) and stated != spec.fingerprint:
            out.append(
                Diagnostic(
                    errors.JOB_WORKFLOW_MISMATCH,
                    f"job {spec.id!r} was planned for a different workflow "
                    f"(roster says {stated}, this one is {spec.fingerprint})",
                    "jobs",
                )
            )
    return out


def _boundary_spots(spec: JobSpec, side: str) -> dict[str, str]:
    """One job's `interface` bindings on one side, as spot -> port name."""
    bindings = (spec.interface or {}).get(side) or {}
    return {
        spot: port
        for port, spot in bindings.items()
        if isinstance(port, str) and isinstance(spot, str)
    }


def _check_boundary_spots(specs: tuple[JobSpec, ...]) -> list[Diagnostic]:
    """Two jobs binding one boundary spot (SPEC §6.8, §6.11).

    One question decides both sides: **is there a moment at which both bindings claim
    the spot?** Two Objects cannot be in one place, so where the answer is yes the
    document asks for something no arrangement of the work provides — and that is
    refused here, rather than sent to a solver whose only answer is `infeasible`.

    **Outputs: always.** A delivered Object holds its spot to the end of the plan
    (§6.8), so two deliveries to one spot overlap there however the work is arranged.
    The only way for the document to come true is for one of the jobs not to deliver —
    which is to say, for something to go wrong — and **a plan that succeeds only if a
    job fails is not one to accept**. A job that is not going to deliver leaves the
    plan instead (`withdraw`, §6.11); a leaving job is not among the specs handed here,
    so "that one is going, this one takes the spot" is a document this accepts in one
    call, and it is the caller saying what happened rather than the scheduler inferring
    it from a failure.

    🔴 This side warned between 0.7.0 and 0.10.0 (design.md D43), on the argument that
    a *stopped* job does not deliver and that this check, handed the roster before any
    status is read, cannot see whether one has stopped. It cannot — and no longer needs
    to. Accepting the document while the stopped job is still in the roster leaves two
    live claims on one spot, held apart only by a fact recorded elsewhere; a job that
    recovered, or a reader who trusted the roster, would find the document contradicting
    itself. Withdrawal is what resolves it, and withdrawal is available without waiting
    for the material to be collected: what a leaving job is still holding is written
    down (§6.12) rather than assumed gone (D51, D52).

    **Inputs: only where the releases coincide.** Entry material is *there*, given, from
    its job's release until the move that collects it (§6.8), so one loading bay serves
    two runs whose releases leave room — whether they do is what the history says and
    the solver decides, which is a warning. Released **together**, both samples are on
    the bay at that instant whatever the schedule does, and that is the same refusal as
    the output side, reached by two jobs rather than by one interface
    (`interface_duplicate_spot`).

    🔴 Every earlier binder is compared against, not only the first: three jobs on one
    bay at 0, 30 and 30 have a coinciding pair the first of them is not part of.
    """
    out: list[Diagnostic] = []

    # The output side: the first binder owns the spot and every later one contradicts
    # it. Which pair is named hardly matters -- all of them are refusals -- so the
    # first is named, as the shape of the entry-side report does.
    owner: dict[str, tuple[str, str]] = {}
    for spec in specs:
        for spot, port in sorted(_boundary_spots(spec, "outputs").items()):
            if spot not in owner:
                owner[spot] = (spec.id, port)
                continue
            first_job, first_port = owner[spot]
            out.append(
                Diagnostic(
                    errors.INTERFACE_SHARED_OUTPUT_SPOT,
                    f"job {first_job!r} ({first_port}) and job {spec.id!r} ({port}) "
                    f"both bind {spot!r}: a delivered Object holds its spot to the end "
                    f"of the plan, so no arrangement of the work puts both there. A job "
                    f"that is not going to deliver leaves the plan (withdraw, §6.11) "
                    f"rather than having its binding planned around",
                    "jobs",
                )
            )

    # The entry side, where the release decides. Collected per spot in roster order, so
    # a binder is judged against the ones already there: a coinciding release refuses,
    # and anything else is the warning the shared bay has always carried.
    binders: dict[str, list[tuple[int, str, str]]] = {}
    for spec in specs:
        for spot, port in sorted(_boundary_spots(spec, "inputs").items()):
            binders.setdefault(spot, []).append((spec.release, spec.id, port))
    for spot in sorted(binders):
        entries = binders[spot]
        for index, (release, job, port) in enumerate(entries):
            together = next((e for e in entries[:index] if e[0] == release), None)
            if together is not None:
                _, first_job, first_port = together
                out.append(
                    Diagnostic(
                        errors.INTERFACE_SIMULTANEOUS_INPUT_SPOT,
                        f"job {first_job!r} ({first_port}) and job {job!r} ({port}) "
                        f"both bind {spot!r} and are released together (at {release}): "
                        f"entry material is there from its job's release, so both are "
                        f"on the spot at that moment. Release one of them later, or "
                        f"give it a spot of its own",
                        "jobs",
                    )
                )
            elif index:
                _, first_job, first_port = entries[0]
                out.append(
                    Diagnostic(
                        errors.INTERFACE_SHARED_INPUT_SPOT,
                        f"job {first_job!r} ({first_port}) and job {job!r} ({port}) "
                        f"both bind {spot!r} -- their releases must leave the first "
                        f"job's material time to be collected before the second's "
                        f"arrives",
                        "jobs",
                        severity=WARNING,
                    )
                )
    return out


def _without(fixation: Fixation, instance, job: str, membership) -> Fixation:
    """`fixation` with every unfinished activity of `job` cancelled -- the job taken
    out of the plan without any index moving.

    Cancelling is how a stopped job's remaining work is already expressed (§6.2): a
    zero-length interval at `now`, holding no spot and no device and drawing no
    consumption. Reusing it here means "what if this job were not being planned?" costs
    a fixation and a solve rather than a second instance.
    """
    activities = dict(fixation.activities)
    for i, owner in enumerate(membership):
        if owner != job or i in activities:
            continue
        boundary = instance.activities[i].boundary
        # A workflow activity, or the residue this job's own history left behind -- a
        # **derived** held node, which carries its owner for exactly this (§6.12). The
        # job's *boundary* nodes stay: they are where its material entered and where
        # its outputs rest, and the question here is what the others could do without
        # this job's work, not what the laboratory would look like without the job.
        if boundary is None or (boundary.kind == "held" and boundary.job == job):
            activities[i] = ActivityFixation("cancelled", fixation.now, fixation.now, 0)
    arcs = dict(fixation.arcs)
    for r, arc in enumerate(instance.arcs):
        if r in arcs:
            continue
        ends = (arc.src_activity, arc.dst_activity)
        if any(activities.get(i) is not None and activities[i].status == "cancelled" for i in ends):
            arcs[r] = ArcFixation("cancelled", fixation.now, fixation.now, 0)
    return replace(fixation, activities=activities, arcs=arcs)


def _unplannable(instance, specs: tuple[JobSpec, ...], solve_kwargs: dict) -> list[Diagnostic]:
    """Which job, if any, is why nothing can be planned (§6.11).

    Reached only when the instance is infeasible with no bound in force, so the
    promises are not the cause and something about the work itself is. Each job is
    taken out in turn: if the rest can then be planned, that job is why -- and saying
    which one beats `infeasible`, which says only that the lab cannot do what it was
    asked and leaves the reader to bisect it by hand.

    🔴 **It reports and does nothing else.** Dropping the job would be the scheduler
    quietly discarding work somebody asked for; whether to withdraw it is the
    caller's to decide (design.md, withdrawal).
    """
    if len(specs) < 2:
        return []
    fixation = solve_kwargs["fixation"]
    if fixation is None:
        return []
    membership = job_membership(instance, [spec.id for spec in specs])
    unbounded = tuple(replace(spec, bound=None) for spec in specs)
    kwargs = dict(solve_kwargs)

    culprits = []
    for spec in specs:
        kwargs["fixation"] = _without(fixation, instance, spec.id, membership)
        if solve(instance, jobs=unbounded, **kwargs).outcome in _SOLVED:
            culprits.append(spec.id)
    if not culprits:
        return [
            Diagnostic(
                errors.JOBS_NOT_PLANNABLE_TOGETHER,
                "no single job accounts for this: the jobs cannot be planned together "
                f"({sorted(spec.id for spec in specs)})",
                "jobs",
            )
        ]
    return [
        Diagnostic(
            errors.JOBS_NOT_PLANNABLE_TOGETHER,
            f"the rest can be planned without job {job!r}; it is not being removed "
            "-- that is the caller's decision",
            "jobs",
        )
        for job in culprits
    ]


def _promised(specs: tuple[JobSpec, ...], solution: Solution) -> tuple[JobSpec, ...]:
    """The roster with every job's promise settled (§6.11).

    A job that arrived with no bound gets the completion this solve achieved for it.
    A job that already had one keeps it: bounds do not ratchet. Tightening them each
    time the search happened to place a job early would turn ordinary variation in
    how long things take into a relaxation on the very next replan, and B_j would stop
    meaning "what this job was promised when it arrived" (design.md D38).
    """
    return tuple(
        spec
        if spec.bound is not None
        else replace(spec, bound=solution.job_completions.get(spec.id))
        for spec in specs
    )


def _solve_within_bounds(
    instance, specs: tuple[JobSpec, ...], solve_kwargs: dict
) -> tuple[Solution, tuple[JobSpec, ...], list[Diagnostic]]:
    """Solve subject to every job's promised bound, relaxing as little as possible if
    they cannot all be kept.

    Normally this is **one** solve: the promises hold, and the jobs that arrived
    without one are given the completion it found. The rest of this runs only when
    reality has moved -- work took longer than planned, a machine went out of service --
    and some promise can no longer be met.

    Then it takes three steps. First, drop every bound and solve once: if that is still
    unschedulable the promises were never the problem, and reporting a relaxation would
    send the reader to the wrong place. Otherwise walk the roster **in order**, keeping
    each promise that still admits a schedule given the ones already kept, and dropping
    the first that does not -- so a job is relaxed only when no schedule keeps it, and
    an earlier job is never relaxed to spare a later one. The dropped job is then
    re-promised from what the final solve achieves, like any job without a bound.

    (Within a batch of jobs submitted together, which are peers, the roster's order is
    the tie-break. That is a choice among equals, not a violation of anything.)
    """
    def attempt(trial: tuple[JobSpec, ...]) -> Solution:
        return solve(instance, jobs=trial, **solve_kwargs)

    solution = attempt(specs)
    if solution.outcome in _SOLVED:
        return solution, _promised(specs, solution), []

    unbounded = tuple(replace(spec, bound=None) for spec in specs)
    if not any(spec.bound is not None for spec in specs):
        return solution, specs, []
    probe = attempt(unbounded)
    if probe.outcome not in _SOLVED:
        # Not the promises. Hand back the original attempt, whose stats describe the
        # instance the caller actually asked about.
        return solution, specs, []

    kept: list[JobSpec] = []
    relaxed: list[JobSpec] = []
    for i, spec in enumerate(specs):
        trial = (*kept, spec, *unbounded[i + 1 :])
        if spec.bound is None or attempt(trial).outcome in _SOLVED:
            kept.append(spec)
        else:
            kept.append(replace(spec, bound=None))
            relaxed.append(spec)

    final = attempt(tuple(kept))
    settled = _promised(tuple(kept), final) if final.outcome in _SOLVED else tuple(kept)
    diagnostics = [
        Diagnostic(
            errors.JOB_BOUND_RELAXED,
            f"job {spec.id!r} could no longer finish by {spec.bound}, the completion it "
            "was promised; its bound was re-derived",
            "jobs",
            severity=WARNING,
        )
        for spec in relaxed
    ]
    return final, settled, diagnostics


def _attribute(items, job, jobs) -> list[Diagnostic]:
    """Name the job a per-workflow diagnostic came from.

    Only in a joint plan, and only in the message: the code and the source position
    stay exactly what the single-workflow pipeline produces, so nothing that matches
    on them has to know about jobs. Without this, two jobs running the same workflow
    report the same finding twice with no way to tell which is which.
    """
    if len(jobs) < 2:
        return list(items)
    return [replace(d, message=f"job {job.id!r}: {d.message}") for d in items]


def _provenance(value, source: str | None) -> str:
    """What the plan's `meta` records for one input: the display name the caller
    gave, else the path it was read from -- or `<in-memory>` for a document that
    was handed over already loaded and so has no path to name."""
    if source is not None:
        return source
    return "<in-memory>" if isinstance(value, dict) else str(value)


@dataclass(frozen=True)
class JobInput:
    """One workflow to plan as part of a joint plan (SPEC §6.11).

    `id` names the job in the plan: every activity of this workflow carries it as
    its `job`, and it is what tells two jobs' activities apart when both run the
    same workflow (so it must be unique within one call, and non-empty). `workflow`
    is a path or an already-loaded document, exactly as `schedule` accepts, and
    `source` is the optional display path recorded as provenance.
    """

    id: str
    workflow: object
    source: str | None = None


def schedule(
    workflow_path,
    environment_path,
    *,
    document_path=None,
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
    random_seed: int | None = None,
    ignore_resources: bool = False,
    max_transport_legs: int = 1,
    carry_levels_to_now: bool = False,
    collect_solutions: bool = False,
    workflow_source: str | None = None,
    environment_source: str | None = None,
    document_source: str | None = None,
) -> ScheduleReport:
    """Plan one workflow. See `schedule_jobs` for several at once.

    Each of `workflow_path`, `environment_path` and `document_path` is a path to
    a file or an already-loaded document (a mapping) -- e.g. an import-expanded
    workflow, or the status a runner just rendered from its own history.

    `workflow_source` / `environment_source` / `document_source` are optional
    display paths recorded as the plan's `meta` provenance: a caller that passes an
    in-memory document (so nothing is read from disk here) can still name the
    original file, instead of the plan showing `<in-memory>`.

    `ignore_resources` switches the consumable model off (§4.7.3): the environment's
    resource declarations are still shape-checked, but nothing is applied and the
    plan is shaped as it would be from an environment that never declared one. It is
    a relaxation, so it never turns a solvable instance unsolvable. This is how a
    resource-bearing environment can drive a consumer that does not know about
    resources -- though such a caller has to pass it, so driving `ofplang.run` this
    way is not possible until run does.

    `max_transport_legs` is how many transport activities one Object-bearing arc may
    be moved in (SPEC §4.5). One -- the default -- is a single hop per arc, which is
    what this has always planned. Above one, an arc whose endpoints are further apart
    is moved through **relays** (§6.4.1): a device reachable at one position only, a
    plate that has to cross a hand-off station. Only routes of the fewest possible
    moves are offered, so raising it never makes a move that could be direct go
    round; it only makes reachable what was `arc_unreachable`.

    `collect_solutions` records every improving solution the search found, into
    `report.stats.phases[-1].history`, which is what an anytime measurement (how good
    was the schedule at time t?) reads. Off by default: a solution callback runs
    inside the search and can perturb the timings it is there to measure, so only a
    caller that wants the curve pays for it. The rest of `report.stats` -- timings,
    bound, model size -- costs nothing and is always there.

    In-memory documents are read, never written to."""
    # The single-workflow call is the one-job case with an empty id, which is what
    # leaves node paths unprefixed and the plan free of any `job` field -- so a plan
    # for one workflow is exactly what it was before joint planning existed.
    return _run(
        [JobInput("", workflow_path, workflow_source)],
        environment_path,
        document_path=document_path,
        running_task_margin=running_task_margin,
        max_time_seconds=max_time_seconds,
        random_seed=random_seed,
        ignore_resources=ignore_resources,
        max_transport_legs=max_transport_legs,
        carry_levels_to_now=carry_levels_to_now,
        collect_solutions=collect_solutions,
        environment_source=environment_source,
        document_source=document_source,
    )


def schedule_jobs(
    jobs,
    environment_path,
    *,
    document_path=None,
    withdraw=(),
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
    random_seed: int | None = None,
    ignore_resources: bool = False,
    max_transport_legs: int = 1,
    carry_levels_to_now: bool = False,
    collect_solutions: bool = False,
    environment_source: str | None = None,
    document_source: str | None = None,
) -> ScheduleReport:
    """Plan several workflows together, against one environment (SPEC §6.11).

    `jobs` is a sequence of `JobInput`. The jobs share everything the environment
    describes -- devices, spots, transporters -- and share the consumable stocks the
    execution document's `inventories` starts them at, because a stock belongs to a
    device rather than to a workflow. That sharing is the reason to plan jointly:
    the jobs compete for machines, and a refill that neither workflow needs on its
    own is planned once for both.

    Every other argument means what it does for `schedule`. What tells the jobs
    apart from one another lives in the document's roster (§6.11): their order is
    their priority, `release` is the earliest any of a job's activities may start,
    and `bound` is the completion time an earlier job was promised, which a later
    arrival may not spoil (design.md D38). Each job carries its own `interface`
    too, because a boundary binds one workflow's ports and two jobs of the same
    workflow bind the same port names to different spots -- so a document that
    lists jobs and still puts `interface` at the top level is refused
    (`multi_job_interface`), and an entry whose inputs are Object-bearing is
    planned jointly like any other.

    `withdraw` names jobs that are **leaving** the plan: their roster entry and their
    history go, and the document's levels are carried forward to `now` so that the
    stocks their work drew on stay right (`inventories.at`, §6.10). A job leaves when
    there is nothing of it left in the laboratory, which the scheduler cannot see --
    so it is asked for, never inferred from a job going quiet, and refused where the
    document itself says otherwise: work still to be done, or still running. A job
    that **failed** may leave; the status never changes again, and the activity's
    interval has ended, so it holds nothing the withdrawal could take away. Do not
    pass a workflow for a job being withdrawn; there is nothing left to plan for it,
    and needing one would mean it should not be leaving.

    Its **holds** go the same way, with one exception. A final output the caller bound
    to a spot (§6.8) is freed: they named the place, so leaving says they collected it
    there. An unbound one is not, because the *schedule* chose where it came to rest
    and the caller was never told -- so that spot becomes an `occupied` entry (§6.12),
    reported by name in the `job_withdrawn` warning. An occupancy the document already
    carried is untouched either way: it says a spot is held and outlives whatever put
    the material there.

    What a joint plan does *not* have yet is a per-job objective: the stages the
    document names are minimised over all the jobs at once (§4.8).
    """
    jobs = list(jobs)
    if not jobs:
        raise ValueError("schedule_jobs needs at least one job")
    ids = [job.id for job in jobs]
    if not all(ids):
        raise ValueError("every job needs a non-empty id (it names the job in the plan)")
    if len(set(ids)) != len(ids):
        raise ValueError(f"job ids must be unique within one plan: {ids}")
    return _run(
        jobs,
        environment_path,
        withdraw=tuple(withdraw),
        document_path=document_path,
        running_task_margin=running_task_margin,
        max_time_seconds=max_time_seconds,
        random_seed=random_seed,
        ignore_resources=ignore_resources,
        max_transport_legs=max_transport_legs,
        carry_levels_to_now=carry_levels_to_now,
        collect_solutions=collect_solutions,
        environment_source=environment_source,
        document_source=document_source,
    )


def _run(
    jobs,
    environment_path,
    *,
    document_path=None,
    withdraw: tuple[str, ...] = (),
    running_task_margin: int = 0,
    max_time_seconds: float | None = None,
    random_seed: int | None = None,
    ignore_resources: bool = False,
    max_transport_legs: int = 1,
    carry_levels_to_now: bool = False,
    collect_solutions: bool = False,
    environment_source: str | None = None,
    document_source: str | None = None,
) -> ScheduleReport:
    """The pipeline both entry points run. One job with an empty id is the
    single-workflow case; several named jobs are a joint plan (§6.11)."""
    diagnostics: list[Diagnostic] = []

    # 1. Environment: schema-validate, then load into the model. One environment
    # serves every job -- that is what makes them compete for the same machines.
    env, env_result = load_environment(environment_path)
    diagnostics += env_result.diagnostics
    if env is None:
        return ScheduleReport(None, None, None, diagnostics)

    # 2. Workflows: our own minimal parse (D17), one per job. Every job is parsed
    # before any is rejected, so a caller with two broken workflows hears about both.
    workflows: list[Workflow] = []
    for job in jobs:
        workflow, wf_diags = parse_workflow(job.workflow)
        diagnostics += _attribute(wf_diags.items, job, jobs)
        if workflow is not None and not _has_error(wf_diags.items):
            workflows.append(workflow)
    if len(workflows) != len(jobs):
        return ScheduleReport(None, None, None, diagnostics)

    # Unified execution-document input (SPEC §6.1). Shape-validate it once, then read
    # `interface` (the boundary constraint, §6.8). There is no separate
    # initial-vs-replan path: an initial plan is a replan with empty history and
    # now = 0, so the same normalize + solve handles both. `had_now` only drives
    # whether the output echoes `now`.
    doc_path = document_path
    interface = None
    inventories = None
    declared_objective = None
    roster = None
    occupied = None
    had_now = False
    now_value = 0
    root = None
    if doc_path is not None:
        root = yamlnode.load_source(doc_path)
        doc_result = validate_document_node(root)
        diagnostics += doc_result.diagnostics
        if not doc_result.ok:
            return ScheduleReport(None, None, None, diagnostics)
        # Read the two fields the pipeline needs off what was already parsed. From a
        # file, `to_plain` builds fresh objects, so the plan cannot end up sharing a
        # subtree with anything; an in-memory document is the caller's own, so its
        # `interface` is copied before being echoed into the plan (below).
        if isinstance(doc_path, dict):
            interface = copy.deepcopy(doc_path.get("interface"))
            inventories = copy.deepcopy(doc_path.get("inventories"))
            declared_objective = (doc_path.get("objective") or {}).get("kind")
            roster = copy.deepcopy(doc_path.get("jobs"))
            occupied = copy.deepcopy(doc_path.get("occupied"))
            had_now = "now" in doc_path
            stated_now = doc_path.get("now")
            now_value = stated_now if isinstance(stated_now, int) else 0
        elif isinstance(root, YMap):
            interface = yamlnode.to_plain(root.get("interface"))
            inventories = yamlnode.to_plain(root.get("inventories"))
            stated = yamlnode.to_plain(root.get("objective"))
            declared_objective = stated.get("kind") if isinstance(stated, dict) else None
            roster = yamlnode.to_plain(root.get("jobs")) if "jobs" in root else None
            occupied = yamlnode.to_plain(root.get("occupied")) if "occupied" in root else None
            had_now = "now" in root
            stated_now = yamlnode.to_plain(root.get("now"))
            now_value = stated_now if isinstance(stated_now, int) else 0

    # A document that names its jobs (§6.11) has to name *these* jobs. It is a
    # replanning input describing work done by particular workflows, and matching that
    # history against a different set would pin it onto activities that never ran it.
    # Compared as sets: the roster's order is the record of how the jobs were given,
    # and re-stating them in another order is not a different plan.
    entries = _roster_entries(roster) if roster is not None else None
    if entries is not None:
        # The single-workflow call has no job identity at all, so it matches only an
        # empty roster -- never one that names jobs. An empty roster and no roster say
        # the same thing there and are both accepted.
        #
        # A *superset* is the arrival of new jobs (§6.11): the roster names the jobs
        # already being planned, and anything beyond it is joining them now. Missing
        # the other way stays an error -- a job the document has history for cannot
        # simply be dropped, or that history would have nowhere to land.
        given_ids = {job.id for job in jobs if job.id}
        # A job being withdrawn is the one kind of entry that may go unmatched: it is
        # leaving, so there is no workflow to plan for it (design.md D42).
        if not set(entries) - set(withdraw) <= given_ids:
            missing = sorted(set(entries) - set(withdraw) - given_ids)
            given = f"{sorted(given_ids)} were given" if given_ids else "one unnamed workflow"
            diagnostics.append(
                Diagnostic(
                    errors.JOB_ROSTER_MISMATCH,
                    f"the document plans jobs {sorted(entries)}, but {given}"
                    f" -- {missing} would be dropped",
                    "jobs",
                )
            )
            return ScheduleReport(None, None, None, diagnostics)

    # Each job's planning parameters, and the check that it is running the workflow
    # its entry was planned for. The order of these two matters: the specs carry the
    # fingerprint just computed, so comparing them against the roster is what turns a
    # swap into a diagnostic rather than a silent overwrite.
    specs = _job_specs(jobs, workflows, entries, now_value)
    if entries is not None:
        mismatched = _check_fingerprints(specs, entries)
        if mismatched:
            diagnostics += mismatched
            return ScheduleReport(None, None, None, diagnostics)

    # `interface` binds *one* workflow's boundary ports, so in a joint plan it belongs
    # to a job rather than to the document. The test is whether *this call* names jobs,
    # not whether the input document happens to list them: an initial joint plan is
    # given a document with no roster yet, and sharing one boundary across its jobs is
    # the very ambiguity being refused. One rule, so there is never a question of which
    # applies -- named jobs mean per-job, an unnamed single workflow means top-level.
    if any(job.id for job in jobs) and interface:
        diagnostics.append(
            Diagnostic(
                errors.MULTI_JOB_INTERFACE,
                "interface binds one workflow's boundary ports, so a document that "
                "lists jobs carries it per job (jobs[].interface), not at the top level",
                "interface",
            )
        )
        return ScheduleReport(None, None, None, diagnostics)

    if withdraw:
        diagnostics += _check_withdrawals(
            withdraw, entries, jobs, _document_activities(doc_path, root)
        )

    diagnostics += _check_boundary_spots(tuple(specs))
    if _has_error(diagnostics):
        return ScheduleReport(None, None, None, diagnostics)

    # 3. Build one instance per job (boundary nodes/arcs from interface always
    # re-created, like relays), prefix each job's node paths with its id so the two
    # cannot collide, and merge. Everything downstream sees a single instance and
    # never learns that jobs exist -- which is what lets one refill candidate serve
    # activities from several jobs (`merge_instances`).
    bases = []
    for job, workflow, spec in zip(jobs, workflows, specs, strict=True):
        base, inst_diags = build_instance(
            # A named job brings its own boundary (`spec.interface`); the unnamed
            # single-workflow call has the document's, which is refused above wherever
            # jobs are named, so exactly one of the two is ever set.
            workflow, env, interface=spec.interface or interface, check_reachability=False
        )
        diagnostics += _attribute(inst_diags.items, job, jobs)
        if base is not None:
            bases.append(prefix_instance(base, (job.id,) if job.id else ()))
    # Every job is built before any rejection, for the same reason every one is
    # parsed first: a caller whose environment is missing two capabilities should
    # hear about both, not be sent round the loop once per job.
    if len(bases) != len(jobs):
        return ScheduleReport(None, None, None, diagnostics)
    base = merge_instances(bases)

    document_activities = _document_activities(doc_path, root)

    contradictions = _check_document_contradictions(document_activities, entries, occupied)
    if contradictions:
        diagnostics += contradictions
        return ScheduleReport(None, None, None, diagnostics)

    # 🔴 A job may not take its draws with it. A stock's level is replayed from the
    # moment `inventories` states (§4.7.2), so a departing job's draws are undone unless
    # that moment has already absorbed them -- and the caller is the one who decides
    # whether it moves (`carry_levels_to_now`). Refused rather than quietly carried
    # forward: moving the moment is the caller's to ask for, and a scheduler that did it
    # for them would be deciding how much history they may let go of.
    if withdraw and not carry_levels_to_now:
        stated_at = (inventories or {}).get("at")
        stated_at = stated_at if isinstance(stated_at, int) else 0
        unabsorbed = sorted(
            {
                job
                for activity in document_activities
                if activity.get("job") in set(withdraw)
                and activity.get("status") in ("completed", "running")
                and (activity.get("consumption") or {})
                and activity.get("start", 0) >= stated_at
                for job in [activity["job"]]
            }
        )
        if unabsorbed:
            diagnostics.append(
                Diagnostic(
                    errors.WITHDRAWAL_UNDOES_DRAWS,
                    f"{', '.join(repr(job) for job in unabsorbed)} drew on a stock after "
                    f"the moment the levels are stated for ({stated_at}), so leaving "
                    f"would give it back; ask for the levels to be carried forward",
                    "jobs",
                )
            )
            return ScheduleReport(None, None, None, diagnostics)

    # What the jobs a terminal status stopped are still holding. Derived rather
    # than declared, and **not** written into the plan: it is derivable from the
    # history this document carries, so stating it would be the same claim twice --
    # which is what `occupied_already_derived` refuses below. A job that is leaving is
    # excluded: its history is about to go, so `frozen` writes its holds down instead.
    #
    # Worked out **before** the withdrawal's write-out, because that write-out is
    # allowed to say only what nobody will be able to derive afterwards -- and these
    # spots are exactly the ones that will still be derivable.
    residue: list[dict] = []
    seen_residue: dict[str, dict] = {}
    for job_id in sorted(_stopped_jobs(document_activities) - set(withdraw)):
        for spot, since in sorted(
            _holds_of(document_activities, entries, [job_id], now_value).items()
        ):
            # One spot, one hold. Two stopped jobs can claim the same one -- a failed
            # transport claims both its ends, and the other end may be where another
            # job's history left something -- and the earlier moment is the one that
            # happened.
            previous = seen_residue.get(spot)
            if previous is None:
                entry = {"spot": spot, "since": since, "job": job_id}
                seen_residue[spot] = entry
                residue.append(entry)
            elif since < previous["since"]:
                previous["since"] = since
    stated_spots = {
        entry["spot"]
        for entry in (occupied or [])
        if isinstance(entry, dict) and isinstance(entry.get("spot"), str)
    }
    clashing = sorted(stated_spots & set(seen_residue))
    if clashing:
        # Two holds on one spot contend over the same interval, so the document would
        # come back `infeasible` with nothing to say why -- and the half that is
        # derived never appears on the page, so the reader would see a single entry and
        # an unschedulable plan. The refusal is what turns that into an explanation
        # (the same reasoning as `occupied_duplicate_spot`, §6.12).
        diagnostics.append(
            Diagnostic(
                errors.OCCUPIED_ALREADY_DERIVED,
                f"{', '.join(repr(spot) for spot in clashing)} "
                + ("is" if len(clashing) == 1 else "are")
                + " already held by a stopped job's own history, so an `occupied` entry"
                " says it a second time; drop the entry and let it be derived",
                "occupied",
            )
        )
        return ScheduleReport(None, None, None, diagnostics)

    # What the withdrawing jobs are still holding, before the model is built: held
    # here as well as echoed, because a spot the model does not know is taken is a
    # spot this plan will use.
    frozen = (
        _frozen_holds(
            withdraw,
            entries,
            document_activities,
            occupied,
            now_value,
            derived=set(seen_residue),
        )
        if withdraw
        else []
    )

    instance, fixation, norm_diags = normalize(
        base,
        root,
        env,
        ignore_resources=ignore_resources,
        max_transport_legs=max_transport_legs,
        jobs=tuple(spec.id for spec in specs if spec.id),
        withdrawn=frozenset(withdraw),
        frozen=tuple(frozen),
        derived=tuple(residue),
    )
    diagnostics += norm_diags.items
    if instance is None or fixation is None:
        return ScheduleReport(None, None, None, diagnostics)

    reach = Diagnostics()
    report_unreachable(instance, set(fixation.arcs), reach)
    diagnostics += reach.items
    if _has_error(reach.items):
        return ScheduleReport(None, None, None, diagnostics)

    # The levels move only when the caller asks (`carry_levels_to_now`), and then they
    # are stated as of the moment asked for rather than echoed. Every other plan echoes
    # `inventories` unchanged, which is what keeps the section stable across replans.
    #
    # 🔴 **The scheduler does not decide this.** It used to: a withdrawal moved `at` to
    # `now` on its own, because a departing job takes history the levels were made of.
    # That is a true reason to move them and not a reason for this side to choose -- the
    # caller may want the moment left where it is, and they are the one who knows
    # whether the history before it may be let go of. So the reason is now theirs to act
    # on, and withdrawing without acting on it is refused above rather than patched over.
    carried = inventories
    if carry_levels_to_now and fixation.levels:
        carried = _carried_levels(env, fixation.stated_levels, fixation.now)
    if withdraw:
        # 🔴 **Reported before the solve, not after.** A frozen spot can be the reason
        # nothing can be planned -- the material really is in the way -- and a report
        # that only survives a successful solve would go missing in exactly that case,
        # leaving the caller an `infeasible` and no account of what their withdrawal
        # did. What a withdrawal did to the document does not depend on the answer.
        #
        # The frozen spots are named out loud. They are the one part of a withdrawal
        # the caller cannot have anticipated -- they never said where those outputs
        # go -- so this is also the answer to "where is my material": it is here, and
        # the plan is holding it until somebody says otherwise.
        diagnostics.append(
            Diagnostic(
                errors.JOB_WITHDRAWN,
                f"{', '.join(sorted(withdraw))} left the plan"
                + (
                    f"; the levels are now stated as of {fixation.now}"
                    if carried is not inventories
                    else ""
                )
                + (_frozen_note(frozen) if frozen else ""),
                "jobs",
                severity=WARNING,
            )
        )

    # 4. Solve, then 5. render the plan (only when feasible). One pass unless a
    # promised bound can no longer be kept (`_solve_within_bounds`).
    named = tuple(specs) if any(job.id for job in jobs) else ()
    solve_kwargs = {
        "fixation": fixation,
        "running_task_margin": running_task_margin,
        "max_time_seconds": max_time_seconds,
        "random_seed": random_seed,
        "objective": _objective_of(declared_objective, len(named)),
        "collect_solutions": collect_solutions,
    }
    solution, settled, relax_diags = _solve_within_bounds(instance, named, solve_kwargs)
    diagnostics += relax_diags
    if solution.outcome not in ("optimal", "feasible"):
        diagnostics.append(Diagnostic(errors.INFEASIBLE, "no feasible schedule found"))
        diagnostics += _unplannable(instance, named, solve_kwargs)
        return ScheduleReport(solution.outcome, None, None, diagnostics, solution.stats)

    # One job's provenance is the string it always was; a joint plan's is the list of
    # its workflows, in job order, so `meta` still names everything the plan came from.
    provenance = [_provenance(job.workflow, job.source) for job in jobs]
    plan = render_plan(
        instance,
        solution,
        workflow=provenance[0] if len(jobs) == 1 else provenance,
        environment=_provenance(environment_path, environment_source),
        status=_provenance(doc_path, document_source) if root is not None else None,
        now=fixation.now if had_now else None,
        interface=interface,
        inventories=carried,
        occupied=(list(occupied or []) + frozen) or None,
        ignore_resources=ignore_resources,
        jobs=settled,
        stopped=frozenset(_stopped_jobs(document_activities) - set(withdraw)),
    )

    # 6. Check what is about to be handed out. The refill amounts in the document are
    # derived from the solved model rather than read off it (§4.7.1), so the document
    # is a second computation over the same answer -- and two computations that must
    # agree are worth checking rather than arguing about. A finding is a defect here,
    # not bad input, but it is reported instead of shipped: a plan that under-fills a
    # stock schedules cleanly, says `optimal`, and runs dry in a real lab.
    for message in check_plan_inventories(plan, env, carried):
        diagnostics.append(Diagnostic(errors.PLAN_INVENTORY_INCONSISTENT, message, "activities"))
    if _has_error(diagnostics):
        return ScheduleReport(solution.outcome, None, None, diagnostics, solution.stats)

    return ScheduleReport(
        solution.outcome, solution.makespan, plan, diagnostics, solution.stats
    )
