"""What counts as a job's own work, and therefore when a job completes.

$C_j$ is not "the last thing belonging to job $j$ to finish". Two kinds of thing
belong to a job and are not its work:

* **Boundary nodes** (SPEC §6.8). An output node ends at the makespan, so
  counting it would make every job finish when the last one does.
* **Everything on a delivery move past the point where it delivered.** Once the
  final output has arrived somewhere it may come to rest, the legs and junctions
  after that are the finished product being moved about -- a shelf being tidied,
  not work being done.

🔴 **This lives apart from the solver because two things measure it.** CP-SAT
constrains $C_j \\le B_j$ over its variables and the constructive pass checks the
same thing over concrete times, and a promise is either kept or broken -- there
is no room for the two to disagree about which ends count. A second copy of this
rule would show up as a plan that breaks a promise, which is the one failure a
promise exists to prevent.

The second exclusion was learned the hard way and the story is in `resting`.
"""

from __future__ import annotations

from ofplang.schedule.scheduler.instance import Instance
from ofplang.schedule.scheduler.model import slot_key
from ofplang.schedule.scheduler.status import Fixation


def final_output_spots(instance) -> dict[tuple[str | None, str], set[str]]:
    """Every spot each job's final outputs may come to rest on, keyed by (job, port).

    A **bound** output node offers one spot per port; an **unbound** one offers a mode
    per candidate resting place (§6.8), and staying where it was made is always among
    them. Read once per solve, because `resting` asks the same question of every leg
    of every boundary-output move.
    """
    spots: dict[tuple[str | None, str], set[str]] = {}
    for act in instance.activities:
        boundary = act.boundary
        if boundary is None or boundary.kind != "output":
            continue
        for mode in act.modes:
            for port, spot in mode.input_spots.items():
                spots.setdefault((boundary.job, port), set()).add(spot)
    return spots


def arrived_at(instance, fixation: Fixation | None, membership, final_spots) -> dict:
    """For each final-output move, the chain position (§6.6) of the first **completed**
    leg that put the Object somewhere it may come to rest — or no entry at all where
    none has yet.

    Read per logical move rather than per leg because that is what the question is
    about: once *some* leg has landed the Object where it was going, everything later
    on that chain is rearrangement rather than delivery (see `resting`). A leg of a
    multi-leg delivery that stopped at a hand-off station lands nowhere it may rest, so
    it records nothing and the rest of its chain still counts.
    """
    arrived: dict = {}
    if fixation is None:
        return arrived
    for r, arc in enumerate(instance.arcs):
        if arc.arc.dst.node != ():
            continue
        leg = fixation.arcs.get(r)
        if leg is None or leg.status != "completed" or not arc.options:
            continue
        job_id = membership[arc.src_activity] or membership[arc.dst_activity]
        # A fixed leg carries one frozen route (`ArcFixation`), so its destination is
        # where the Object actually is.
        # Keyed as the output node's modes key it: by port, or by element for an Array.
        if arc.options[0].to_spot not in final_spots.get((job_id, slot_key(arc.arc.dst)), ()):
            continue
        key = (job_id, arc.arc)
        seq = arc.seq or 0
        if key not in arrived or seq < arrived[key]:
            arrived[key] = seq
    return arrived


def resting(logical, seq, job_id, arrived: dict) -> bool:
    """Whether position `seq` of the move `logical` is past the point where the job's
    final output **arrived** where it was going (§6.8) — the finished product on its
    shelf rather than work.

    Asked of a transport leg and of a relay alike: a relay is a junction of the move it
    belongs to, so it is the job's work exactly when that part of the move is.

    A final output node is synthetic: it is re-created every solve and can never be
    reported in a status, so `normalize` can never mark it *fixed* the way a real
    processing node is. Once the delivering leg completes it therefore goes on, replan
    after replan, deriving a relay at the arrival spot and appending a pending
    zero-distance remainder to the node — which, like any pending arc, cannot start
    before `now`.

    🔴 That remainder is why this exists. Counted as the job's work it made **C_j =
    now** for a job whose every activity was history: the promise it was given
    (`bound`, §6.11) could not be kept by any schedule, so every replan ran the
    relaxation search, re-derived the promise as `now`, and reported a
    `job_bound_relaxed` the plan had not earned. The plan itself never showed it --
    rendering folds the relay and the no-op leg away (`plan._fold_relayed_zero_distance`).

    The test is positional: this leg comes **after** the one that arrived (`arrived_at`),
    on the same logical move. 🔴 Position, not "is it a no-op", because the two things
    that must be excluded look nothing alike. One is the zero-distance remainder. The
    other is a genuine later **move** -- the schedule shifting an unbound output aside
    because another job needs that spot (§6.8) -- and while that move is in flight the
    chain grows a *pending* relay and a further remainder behind it, which a test on the
    leg in front of the arc would miss. Asking "has this move already delivered?" once
    per chain catches every leg after the answer became yes.

    And 🔴 it must be *after*: a multi-leg delivery that has reached a hand-off station
    with one hop still to go has arrived nowhere it may rest, so nothing on its chain is
    excluded. Dropping that hop would under-state C_j, which is the same defect wearing
    the other face.

    That a later move does not count is the settled reading of C_j: the product was
    finished when it was made, where the laboratory then keeps it is the laboratory's
    business, and a completion that moved every time somebody else needed a shelf would
    not be a completion. Such a move is still in the makespan (`make_ends`), still holds
    its transporter and its spots, and is still dispatched; only `C_j` passes it over.
    """
    # Only a final output's move has the boundary as its logical destination (the empty
    # node path). An entry arc has it as the *source*, so the two are never confused.
    if logical.dst.node != ():
        return False
    delivered = arrived.get((job_id, logical))
    return delivered is not None and (seq or 0) > delivered


def job_end_parts(
    instance: Instance,
    fixation: Fixation | None,
    membership,
    stopped=frozenset(),
) -> dict[str, tuple[list[int], list[int]]]:
    """Per job, the activities and the arcs whose ends its completion is the last of.

    🔴 **The one statement of which ends count.** Both the solver and the
    constructive pass read it -- one to constrain $C_j$, the other to check it --
    so that a promise cannot be kept by one measure and broken by the other.

    A job that has stopped (§6.2) has no completion: it is not going to finish, and
    a bound derived from what it managed would be a promise about an abandoned run.
    """
    arrived = arrived_at(instance, fixation, membership, final_output_spots(instance))
    parts: dict[str, tuple[list[int], list[int]]] = {}

    def side(job_id: str) -> tuple[list[int], list[int]]:
        return parts.setdefault(job_id, ([], []))

    for i, job_id in enumerate(membership):
        act = instance.activities[i]
        # A boundary node belongs to a job (`job_membership` reports ownership) but is
        # not its work: an output node ends at the makespan, so counting it would make
        # every job finish when the last one does.
        if job_id is None or job_id in stopped or act.boundary is not None:
            continue
        # A relay is a junction of the move it belongs to (§6.4.1), so it is the job's
        # work exactly when that part of the move is.
        if act.relay is not None and resting(act.relay.arc, act.relay.seq, job_id, arrived):
            continue
        side(job_id)[0].append(i)
    for r, arc in enumerate(instance.arcs):
        src, dst = membership[arc.src_activity], membership[arc.dst_activity]
        # A boundary arc has one end in no job; the other names the job it serves.
        job_id = src if src is not None else dst
        if job_id is None or job_id in stopped:
            continue
        if resting(arc.arc, arc.seq, job_id, arrived):
            continue
        side(job_id)[1].append(r)
    return parts
