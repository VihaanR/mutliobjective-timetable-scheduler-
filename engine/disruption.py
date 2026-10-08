"""Disruption engine (design.md §7) — the "rain day / holiday" re-plan flow.

Two modes, selected by `replan(..., mode=...)`:

**`mode="resolve"` (the DEFAULT).** Block the disrupted slots, exempt that day from the day-shaped
hard rules, and **re-solve the whole week from scratch**, warm-started from the baseline. A
rained-out lab is therefore rescheduled somewhere else in the week rather than discarded. This gives
up the "unaffected days are byte-identical" guarantee in exchange for "nothing is silently lost" —
the owner's explicit call, because dropping a session is worse than reshuffling one. Warm-starting
means most unaffected sessions do land back where they were, but that is emergent, not pinned.
Because the exact solvers force every session to be placed, a genuinely over-constrained window
(e.g. a whole-day holiday, whose load cannot fit into four days under the 6–8h/day caps) falls back
to a greedy best-effort plan and reports `hard_violations` / unplaced sessions honestly.

**`mode="minimal"`.** The original P4 minimal-change patcher, kept for comparison:

When part of a day becomes unusable (sudden rain from 2 pm, a declared holiday), we do NOT
regenerate the whole week — that would move classes students and faculty have already planned
around. Instead we make a **minimal-change** adjustment:

  1. Every session on an UNDISRUPTED day is copied verbatim from the baseline — identical slot AND
     room. Those days are untouched, guaranteed.
  2. On the disrupted day, sessions whose baseline slot is NOT in the blocked window (the surviving
     morning classes, say) keep their exact placement.
  3. Sessions whose baseline slot IS blocked are displaced. We try to relocate each into a free,
     non-blocked slot on the same day (respecting no double-booking, batch-pair sync, subject-
     once-per-day). Any that don't fit are **dropped** for that occurrence — you cannot make up a
     rained-out afternoon by overloading another day past its 6-8h hard cap, so a genuinely lost
     session is reported as dropped rather than silently forced somewhere invalid.

This is built directly (not via the exact MIP/CP-SAT solvers) on purpose: the exact solvers force
*every* session to be placed (`AddExactlyOne`), which makes "some classes are simply cancelled by
the rain" an infeasible model. Direct construction is always feasible, deterministic, and truly
minimal-change. The master run is never mutated — the caller stores this as a dated overlay.

The engine-level `blocked_slot_ids` / `relaxed_days` / `pinned_slots` support added alongside this
module still lets an admin manually re-solve a blocked-window scenario with any solver if they want
a full re-optimization instead of a minimal-change patch.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from engine.models import (
    Assignment, ProblemInstance, SessionRequirement, Solution, expand_requirements,
)
from engine.scoring import better, score
from engine.solvers import SOLVERS
from engine.solvers.greedy import (
    NO_ROOM, GreedySolver, _Trackers, _consecutive_slot_ids, _rooms_of_type, _slots_by_day,
)


@dataclass
class MovedSession:
    session_id: str
    from_slot_id: int | None
    to_slot_id: int | None
    from_room_id: str | None
    to_room_id: str | None


@dataclass
class AdjustmentResult:
    solution: Solution
    affected_slot_ids: frozenset[int]
    relaxed_days: frozenset[int]
    moved_sessions: list[MovedSession]
    dropped_session_ids: list[str]
    hard_violations: int          # includes the dropped sessions (each is an unmet weekly requirement)
    soft_cost: float
    notes: list[str] = field(default_factory=list)
    # Human identity of every session in `dropped_session_ids` — course/division/faculty, not just a
    # count. An admin needs to know exactly WHICH classes have no home so they can rearrange them by
    # hand; a bare "7 sessions dropped" is not actionable (design.md §7).
    unplaced_sessions: list[dict] = field(default_factory=list)

    @property
    def conflict_violations(self) -> int:
        """Hard violations among the sessions that ARE placed — i.e. genuine scheduling clashes,
        excluding the intentional rained-out drops. A well-formed adjustment has this == 0."""
        return self.hard_violations - len(self.dropped_session_ids)

    @property
    def is_valid(self) -> bool:
        """True when every placed session is conflict-free (only dropped sessions are 'unmet')."""
        return self.conflict_violations == 0


def affected_slots_for_day(problem: ProblemInstance, day: int,
                           from_period: int | None = None) -> frozenset[int]:
    """Slot ids to block on a disrupted day. `from_period=None` blocks the whole day (holiday);
    `from_period=N` blocks periods >= N (e.g. 'rain from 2 pm')."""
    return frozenset(
        t.id for t in problem.time_slots
        if t.day == day and (from_period is None or t.period >= from_period)
    )


def _occupied_slot_ids(slots_by_id, slots_by_day_period, slot_id: int, duration_slots: int) -> list[int]:
    ts = slots_by_id.get(slot_id)
    if ts is None:
        return []
    ids = [ts.id]
    for k in range(1, duration_slots):
        nxt = slots_by_day_period.get((ts.day, ts.period + k))
        if nxt is not None:
            ids.append(nxt.id)
    return ids


def _describe_unplaced(problem: ProblemInstance, req_by_id: dict[str, SessionRequirement],
                       session_ids: list[str], baseline_by_session: dict) -> list[dict]:
    """Resolve unplaced session ids into admin-readable identities (design.md §7): course code +
    title, division + batch, faculty name, session type, and where it used to sit in the baseline.
    `label` is a ready-to-print one-liner so the UI needs no formatting logic of its own."""
    courses = problem.course_by_code()
    faculty = problem.faculty_by_id()
    slots_by_id = {t.id: t for t in problem.time_slots}
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    described: list[dict] = []
    for sid in session_ids:
        req = req_by_id.get(sid)
        if req is None:
            described.append({"session_id": sid, "label": sid})
            continue

        course = courses.get(req.course_code)
        fac = faculty.get(req.faculty_id) if req.faculty_id else None
        base = baseline_by_session.get(sid)
        base_slot = slots_by_id.get(base.time_slot_id) if base else None

        where = ""
        if base_slot is not None:
            day = day_names[base_slot.day] if base_slot.day < len(day_names) else f"day {base_slot.day}"
            where = f", was {day} {base_slot.start}-{base_slot.end}"
        who = f" — {fac.name}" if fac else ""
        batch = f", batch {req.batch_id}" if req.batch_id else ""

        described.append({
            "session_id": sid,
            "course_code": req.course_code,
            "course_title": course.title if course else None,
            "division_id": req.division_id,
            "batch_id": req.batch_id,
            "faculty_id": req.faculty_id,
            "faculty_name": fac.name if fac else None,
            "session_type": req.session_type.value,
            "from_slot_id": base.time_slot_id if base else None,
            "label": f"{req.course_code} {req.session_type.value.lower()} "
                     f"({req.division_id}{batch}){who}{where}",
        })
    return described


def _diff_moved(requirements, baseline_by_session, new_by_session) -> list[MovedSession]:
    """Baseline vs new placement diff. A session missing from the new solution is reported with
    `to_slot_id=None` (the UI reads that as 'not scheduled')."""
    moved: list[MovedSession] = []
    for req in requirements:
        base = baseline_by_session.get(req.id)
        new = new_by_session.get(req.id)
        if new is None and base is not None:
            moved.append(MovedSession(req.id, base.time_slot_id, None, base.room_id, None))
        elif new is not None and base is None:
            moved.append(MovedSession(req.id, None, new.time_slot_id, None, new.room_id))
        elif new is not None and base is not None and (
                base.time_slot_id != new.time_slot_id or base.room_id != new.room_id):
            moved.append(MovedSession(req.id, base.time_slot_id, new.time_slot_id,
                                      base.room_id, new.room_id))
    return moved


def _replan_resolve(problem: ProblemInstance, baseline: Solution, affected: frozenset[int],
                    disrupted_days: frozenset[int], time_limit_s: float,
                    solver: str) -> AdjustmentResult:
    """FULL RE-SOLVE (default). Blocks the disrupted window, relaxes that day's day-shaped rules,
    and re-solves the WHOLE week from scratch -- so a rained-out lab can be rescheduled anywhere in
    the week instead of being dropped. Warm-started from the baseline so unaffected sessions tend to
    stay put, but that is an emergent property, not a pin: nothing is fixed in place."""
    disrupted = replace(problem, blocked_slot_ids=affected, relaxed_days=disrupted_days)
    notes: list[str] = []

    solution = SOLVERS[solver]().solve(disrupted, time_limit_s=time_limit_s, warm_start=baseline)
    sc = score(solution, disrupted)

    # The exact solvers force EVERY session to be placed (`AddExactlyOne`), so a window that
    # genuinely cannot fit the full week's load -- a whole-day holiday, say -- comes back
    # INFEASIBLE/empty rather than partially placed. Greedy degrades gracefully (it places what
    # fits and leaves the rest unplaced), so fall back to it and keep whichever actually scores
    # better. This is why the result can report hard violations instead of silently dropping work.
    if sc.hard_violations:
        fallback = GreedySolver().solve(disrupted, time_limit_s=min(time_limit_s, 10))
        best = better(fallback, solution, disrupted)
        if best is fallback:
            notes.append(
                f"{solver} could not satisfy the disrupted week (the blocked window leaves too "
                f"little capacity)" + ("; used a greedy best-effort plan instead."
                                       if solver != "greedy" else ".")
            )
            solution = fallback
            sc = score(solution, disrupted)

    solution = Solution(assignments=list(solution.assignments), solver_name=f"disruption-resolve:{solution.solver_name}",
                        wall_clock_seconds=solution.wall_clock_seconds, status="ADJUSTED")

    requirements = expand_requirements(problem)
    baseline_by_session = baseline.assignment_by_session()
    new_by_session = solution.assignment_by_session()
    moved = _diff_moved(requirements, baseline_by_session, new_by_session)

    # Nothing is *intentionally* dropped in this mode. Anything the solver could not place is
    # reported so the counts stay consistent with `moved`, and it is called out as unplaced rather
    # than "rained out" -- an honest signal that the instance is over-constrained.
    unplaced = [r.id for r in requirements if r.id not in new_by_session]
    unplaced_sessions = _describe_unplaced(problem, {r.id: r for r in requirements}, unplaced,
                                           baseline_by_session)
    if unplaced:
        notes.append(f"{len(unplaced)} session(s) could not be placed anywhere in the week; the "
                     f"disruption leaves the timetable over-constrained. Re-run allowing extra "
                     f"relaxed days, or rearrange these by hand:")
        notes.extend(f"  • {d['label']}" for d in unplaced_sessions)

    return AdjustmentResult(
        solution=solution,
        affected_slot_ids=affected,
        relaxed_days=disrupted_days,
        moved_sessions=moved,
        dropped_session_ids=unplaced,
        hard_violations=sc.hard_violations,
        soft_cost=sc.soft_cost,
        notes=notes,
        unplaced_sessions=unplaced_sessions,
    )


def replan(problem: ProblemInstance, baseline: Solution, affected_slot_ids: frozenset[int],
           relaxed_days: frozenset[int] | None = None, time_limit_s: float = 60,
           mode: str = "resolve", solver: str = "cpsat",
           constraint_query: str | None = None) -> AdjustmentResult:
    """Re-plan around a disruption. Returns an overlay; neither `problem` nor `baseline` is mutated.

    `mode="resolve"` (default) re-solves the whole week from scratch around the blocked window --
    nothing is silently dropped. `mode="minimal"` keeps the original minimal-change patcher, which
    only reshuffles within the disrupted day and drops what does not fit.
    """
    affected = frozenset(affected_slot_ids)
    if not affected:
        # nothing is blocked -> the baseline is already the answer; re-solving would churn the
        # timetable for no reason.
        return AdjustmentResult(
            solution=baseline, affected_slot_ids=affected,
            relaxed_days=frozenset(relaxed_days or ()), moved_sessions=[], dropped_session_ids=[],
            hard_violations=score(baseline, problem).hard_violations,
            soft_cost=score(baseline, problem).soft_cost,
        )

    if mode == "minimal":
        return _replan_minimal(problem, baseline, affected, relaxed_days, time_limit_s)
    if mode != "resolve":
        raise ValueError(f"unknown replan mode {mode!r} (expected 'resolve' or 'minimal')")
    if solver != "pipeline" and solver not in SOLVERS:
        raise ValueError(f"unknown solver {solver!r}")

    slots_by_id = {t.id: t for t in problem.time_slots}
    disrupted_days = frozenset(relaxed_days) if relaxed_days else frozenset(
        slots_by_id[s].day for s in affected if s in slots_by_id
    )
    return _replan_resolve(problem, baseline, affected, disrupted_days, time_limit_s, solver)


def _replan_minimal(problem: ProblemInstance, baseline: Solution, affected_slot_ids: frozenset[int],
                    relaxed_days: frozenset[int] | None = None,
                    time_limit_s: float = 30) -> AdjustmentResult:
    """MINIMAL-CHANGE re-plan (the original P4 behavior, kept for comparison and for callers that
    explicitly want it via `mode="minimal"`). Copies undisrupted days verbatim, keeps disrupted-day
    survivors in place, relocates blocked-window sessions within that same day if they fit, and
    DROPS the ones that don't. `time_limit_s` is accepted for API symmetry with the solvers but the
    direct construction is effectively instant. Neither `problem` nor `baseline` is mutated."""
    affected = frozenset(affected_slot_ids)
    requirements = expand_requirements(problem)
    req_by_id = {r.id: r for r in requirements}
    baseline_by_session = baseline.assignment_by_session()

    slots_by_id = {t.id: t for t in problem.time_slots}
    slots_by_day_period = {(t.day, t.period): t for t in problem.time_slots}
    slots_by_day = _slots_by_day(problem)
    classrooms = _rooms_of_type(problem, "classroom")
    labs = _rooms_of_type(problem, "lab")

    disrupted_days = frozenset(relaxed_days) if relaxed_days else frozenset(
        slots_by_id[s].day for s in affected if s in slots_by_id
    )

    trackers = _Trackers(problem)
    result: list[Assignment] = []
    notes: list[str] = []

    def _occ(slot_id: int, dur: int) -> list[int]:
        return _occupied_slot_ids(slots_by_id, slots_by_day_period, slot_id, dur)

    def _commit(req: SessionRequirement, slot_id: int, room_id: str) -> None:
        occ = _occ(slot_id, req.duration_slots)
        ts = slots_by_id[slot_id]
        trackers.commit(req.division_id, req.batch_id, req.faculty_id, room_id, ts.day, occ, req.duration_slots)
        if not req.is_break:
            trackers.division_day_occurrence.add((req.division_id, req.course_code, ts.day))
        result.append(Assignment(session_id=req.id, time_slot_id=slot_id, room_id=room_id))

    # ---- 1. copy everything on undisrupted days verbatim (identical slot AND room) ----
    disrupted_reqs: list[SessionRequirement] = []
    for req in requirements:
        base = baseline_by_session.get(req.id)
        if base is None:
            continue
        ts = slots_by_id.get(base.time_slot_id)
        if ts is None:
            continue
        if ts.day in disrupted_days:
            disrupted_reqs.append(req)
        else:
            _commit(req, base.time_slot_id, base.room_id)

    # ---- 2. disrupted-day survivors (baseline slot not blocked) keep their exact placement ----
    displaced: list[SessionRequirement] = []
    displaced_groups: dict[str, list[SessionRequirement]] = {}
    for req in disrupted_reqs:
        base = baseline_by_session[req.id]
        occ = _occ(base.time_slot_id, req.duration_slots)
        if any(sid in affected for sid in occ):
            if req.batch_group_id:
                displaced_groups.setdefault(req.batch_group_id, []).append(req)
            else:
                displaced.append(req)
        else:
            _commit(req, base.time_slot_id, base.room_id)

    # ---- 3. try to relocate displaced sessions into free non-blocked slots on the same day ----
    dropped: list[str] = []

    def _free_room(room_pool: list[str], occ: list[int], used: set[str]) -> str | None:
        for r in room_pool:
            if r in used:
                continue
            if all((r, sid) not in trackers.room_busy for sid in occ):
                return r
        return None

    def _try_place_single(req: SessionRequirement) -> bool:
        base = baseline_by_session[req.id]
        day = slots_by_id[base.time_slot_id].day
        room_pool = labs if req.room_type == "lab" else classrooms
        for ts in slots_by_day.get(day, []):
            occ = _occ(ts.id, req.duration_slots)
            if len(occ) < req.duration_slots:
                continue
            if any(sid in affected for sid in occ):
                continue
            if (req.division_id, req.course_code, day) in trackers.division_day_occurrence:
                continue
            if not trackers.division_free(req.division_id, req.batch_id, occ):
                continue
            if not trackers.faculty_free(req.faculty_id, day, occ, req.duration_slots):
                continue
            if req.room_type == "none":
                _commit(req, ts.id, NO_ROOM)
                return True
            room_id = _free_room(room_pool, occ, set())
            if room_id is not None:
                _commit(req, ts.id, room_id)
                return True
        return False

    def _try_place_batch_group(reqs: list[SessionRequirement]) -> bool:
        base = baseline_by_session[reqs[0].id]
        day = slots_by_id[base.time_slot_id].day
        for ts in slots_by_day.get(day, []):
            occ = _occ(ts.id, reqs[0].duration_slots)
            if len(occ) < reqs[0].duration_slots:
                continue
            if any(sid in affected for sid in occ):
                continue
            if (reqs[0].division_id, reqs[0].course_code, day) in trackers.division_day_occurrence:
                continue
            if not all(trackers.division_free(r.division_id, r.batch_id, occ) for r in reqs):
                continue
            if not all(trackers.faculty_free(r.faculty_id, day, occ, r.duration_slots) for r in reqs):
                continue
            free_labs = [l for l in labs if all((l, sid) not in trackers.room_busy for sid in occ)]
            if len(free_labs) < len(reqs):
                continue
            for r, room_id in zip(reqs, free_labs):
                _commit(r, ts.id, room_id)
            return True
        return False

    for req in displaced:
        if not _try_place_single(req):
            dropped.append(req.id)
    for group_id, reqs in displaced_groups.items():
        if not _try_place_batch_group(reqs):
            dropped.extend(r.id for r in reqs)

    dropped_sessions = _describe_unplaced(problem, req_by_id, dropped, baseline_by_session)
    if dropped:
        notes.append(f"{len(dropped)} session(s) could not be re-placed within the disrupted day "
                     f"and were dropped for this occurrence (rained out):")
        notes.extend(f"  • {d['label']}" for d in dropped_sessions)

    # ---- 4. score the overlay (blocked+relaxed context) and diff vs baseline ----
    disrupted = replace(problem, blocked_slot_ids=affected, relaxed_days=disrupted_days)
    solution = Solution(assignments=result, solver_name="disruption-replan",
                        wall_clock_seconds=0.0, status="ADJUSTED")
    sc = score(solution, disrupted)

    moved = _diff_moved(requirements, baseline_by_session, solution.assignment_by_session())

    return AdjustmentResult(
        solution=solution,
        affected_slot_ids=affected,
        relaxed_days=disrupted_days,
        moved_sessions=moved,
        dropped_session_ids=dropped,
        hard_violations=sc.hard_violations,
        soft_cost=sc.soft_cost,
        notes=notes,
        unplaced_sessions=dropped_sessions,
    )
