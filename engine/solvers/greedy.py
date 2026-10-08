"""Deterministic constructive heuristic — a sub-second feasible-ish seed for the pipeline.

Processes the most constrained sessions first (breaks, then lab batch-pairs, then cross-division
open-elective sync groups, then remaining theory/tutorial), scanning slots in a fixed day/period
order and taking the first structurally-valid placement. Sessions with no remaining feasible slot
are left unassigned rather than raising — `scoring.py` penalizes unassigned sessions as a hard
violation, so downstream stages (GA/CP-SAT) can still improve on a partial seed.
"""
from __future__ import annotations

import time

from engine.models import (
    Assignment, ProblemInstance, Solution, SessionRequirement, SessionType, expand_requirements,
)
from engine.solvers.base import SolverBase

NO_ROOM = "NONE"


class _Trackers:
    def __init__(self, problem: ProblemInstance):
        self.problem = problem
        self.room_busy: set[tuple[str, int]] = set()
        self.faculty_busy: set[tuple[str, int]] = set()
        self.whole_division_busy: set[tuple[str, int]] = set()
        self.batch_busy: set[tuple[str, str, int]] = set()
        self.division_day_occurrence: set[tuple[str, str, int]] = set()
        self.faculty_day_hours: dict[tuple[str, int], float] = {}
        self.faculty_week_hours: dict[str, float] = {}
        self.division_by_id = problem.division_by_id()
        self.faculty_by_id = problem.faculty_by_id()
        self._slots_by_id = {t.id: t for t in problem.time_slots}

    def division_free(self, division_id: str, batch_id: str | None, slot_ids: list[int]) -> bool:
        division = self.division_by_id.get(division_id)
        batches = division.batch_pair() if division else ()
        for sid in slot_ids:
            if (division_id, sid) in self.whole_division_busy:
                return False
            if batch_id is None:
                for b in batches:
                    if (division_id, b, sid) in self.batch_busy:
                        return False
            else:
                if (division_id, batch_id, sid) in self.batch_busy:
                    return False
        return True

    def faculty_free(self, faculty_id: str | None, day: int, slot_ids: list[int], duration: int) -> bool:
        if faculty_id is None:
            return True
        for sid in slot_ids:
            if (faculty_id, sid) in self.faculty_busy:
                return False
        fac = self.faculty_by_id.get(faculty_id)
        if fac:
            if any(sid in fac.unavailable_slots for sid in slot_ids):
                return False
            if fac.is_visiting:
                if fac.visiting_days and day not in fac.visiting_days:
                    return False
                for sid in slot_ids:
                    ts = self._slots_by_id.get(sid)
                    if ts:
                        if fac.visiting_start_time and ts.start < fac.visiting_start_time:
                            return False
                        if fac.visiting_end_time and ts.end > fac.visiting_end_time:
                            return False
        if self.faculty_day_hours.get((faculty_id, day), 0) + duration > 6:
            return False
        if fac and self.faculty_week_hours.get(faculty_id, 0) + duration > fac.max_load_hours_per_week:
            return False
        return True

    def commit(self, division_id: str, batch_id: str | None, faculty_id: str | None,
               room_id: str | None, day: int, slot_ids: list[int], duration: int) -> None:
        division = self.division_by_id.get(division_id)
        batches = division.batch_pair() if division else ()
        for sid in slot_ids:
            if batch_id is None:
                # whole-division sessions block the entire division's calendar, including both
                # batch tracks; batch sessions only block their OWN batch track, so simultaneous
                # batch pairs never falsely collide with each other here
                self.whole_division_busy.add((division_id, sid))
                for b in batches:
                    self.batch_busy.add((division_id, b, sid))
            else:
                self.batch_busy.add((division_id, batch_id, sid))
            if room_id and room_id != NO_ROOM:
                self.room_busy.add((room_id, sid))
            if faculty_id:
                self.faculty_busy.add((faculty_id, sid))
        if faculty_id:
            self.faculty_day_hours[(faculty_id, day)] = self.faculty_day_hours.get((faculty_id, day), 0) + duration
            self.faculty_week_hours[faculty_id] = self.faculty_week_hours.get(faculty_id, 0) + duration


def _rooms_of_type(problem: ProblemInstance, room_type: str) -> list[str]:
    return [r.id for r in problem.rooms if r.room_type == room_type]


def _slots_by_day(problem: ProblemInstance) -> dict[int, list]:
    out: dict[int, list] = {}
    for t in problem.time_slots:
        out.setdefault(t.day, []).append(t)
    for day in out:
        out[day].sort(key=lambda t: t.period)
    return out


def _consecutive_slot_ids(day_slots: list, start_idx: int, duration: int) -> list[int] | None:
    if start_idx + duration > len(day_slots):
        return None
    window = day_slots[start_idx:start_idx + duration]
    for a, b in zip(window, window[1:]):
        if b.period != a.period + 1:
            return None
    return [t.id for t in window]


class GreedySolver(SolverBase):
    name = "greedy"

    def solve(self, problem: ProblemInstance, time_limit_s: float = 5,
              warm_start: Solution | None = None) -> Solution:
        start = time.time()
        requirements = expand_requirements(problem)
        slots_by_day = _slots_by_day(problem)
        classrooms = _rooms_of_type(problem, "classroom")
        labs = _rooms_of_type(problem, "lab")
        trackers = _Trackers(problem)
        assignments: list[Assignment] = []

        blocked = problem.blocked_slot_ids

        def _blocked(slot_ids: list[int]) -> bool:
            # nothing (except breaks) may occupy a disruption-blocked slot
            return bool(blocked) and any(sid in blocked for sid in slot_ids)

        breaks = [r for r in requirements if r.is_break]
        lab_groups: dict[str, list[SessionRequirement]] = {}
        sync_groups: dict[str, list[SessionRequirement]] = {}
        singles: list[SessionRequirement] = []

        for r in requirements:
            if r.is_break:
                continue
            if r.batch_group_id:
                lab_groups.setdefault(r.batch_group_id, []).append(r)
            elif r.sync_group_id:
                sync_groups.setdefault(r.sync_group_id, []).append(r)
            else:
                singles.append(r)

        # 1. breaks: one per division per day, in the mid-day band (not first two / not last slot)
        for req in breaks:
            day = req.fixed_day if req.fixed_day is not None else int(req.id.rsplit("D", 1)[-1])
            day_slots = slots_by_day.get(day, [])
            if len(day_slots) < 4:
                continue
            mid_choices = day_slots[2:-1]  # skip first two periods and the last period
            # prefer periods closest to a natural mid-morning slot (matching the real DJSCE
            # timetables' ~11:00 break), varying by day so it isn't identical every day
            target_period = day_slots[0].period + max(1, round(len(day_slots) * 0.35))
            by_closeness = sorted(mid_choices, key=lambda t: abs(t.period - target_period))
            pick = by_closeness[day % len(by_closeness)]
            slot_ids = [pick.id]
            trackers.commit(req.division_id, None, None, None, day, slot_ids, req.duration_slots)
            assignments.append(Assignment(session_id=req.id, time_slot_id=pick.id, room_id=NO_ROOM))

        # 2. lab batch-pairs: visiting faculty lab groups placed foremost
        sorted_lab_groups = sorted(
            lab_groups.items(),
            key=lambda item: not any(r.is_visiting_faculty for r in item[1])
        )
        for group_id, reqs in sorted_lab_groups:
            placed = False
            for day, day_slots in slots_by_day.items():
                if placed:
                    break
                for start_idx in range(len(day_slots)):
                    slot_ids = _consecutive_slot_ids(day_slots, start_idx, reqs[0].duration_slots)
                    if slot_ids is None:
                        continue
                    if _blocked(slot_ids):
                        continue
                    if not all(trackers.division_free(r.division_id, r.batch_id, slot_ids) for r in reqs):
                        continue
                    if not all(trackers.faculty_free(r.faculty_id, day, slot_ids, r.duration_slots) for r in reqs):
                        continue
                    key = (reqs[0].division_id, reqs[0].course_code, day)
                    if key in trackers.division_day_occurrence:
                        continue
                    available_labs = [l for l in labs if all((l, sid) not in trackers.room_busy for sid in slot_ids)]
                    if len(available_labs) < len(reqs):
                        continue
                    for req, room_id in zip(reqs, available_labs):
                        trackers.commit(req.division_id, req.batch_id, req.faculty_id, room_id, day, slot_ids, req.duration_slots)
                        assignments.append(Assignment(session_id=req.id, time_slot_id=slot_ids[0], room_id=room_id))
                    trackers.division_day_occurrence.add(key)
                    placed = True
                    break

        # 3. cross-division sync groups (open electives): same slot across all linked divisions
        for group_id, reqs in sync_groups.items():
            placed = False
            for day, day_slots in slots_by_day.items():
                if placed:
                    break
                for ts in day_slots:
                    slot_ids = [ts.id]
                    if _blocked(slot_ids):
                        continue
                    if not all(trackers.division_free(r.division_id, None, slot_ids) for r in reqs):
                        continue
                    if not all(trackers.faculty_free(r.faculty_id, day, slot_ids, r.duration_slots) for r in reqs):
                        continue
                    keys = [(r.division_id, r.course_code, day) for r in reqs]
                    if any(k in trackers.division_day_occurrence for k in keys):
                        continue
                    chosen_rooms = []
                    used = set()
                    ok = True
                    for _ in reqs:
                        found = next((c for c in classrooms if c not in used and (c, ts.id) not in trackers.room_busy), None)
                        if found is None:
                            ok = False
                            break
                        used.add(found)
                        chosen_rooms.append(found)
                    if not ok:
                        continue
                    for req, room_id in zip(reqs, chosen_rooms):
                        trackers.commit(req.division_id, None, req.faculty_id, room_id, day, slot_ids, req.duration_slots)
                        assignments.append(Assignment(session_id=req.id, time_slot_id=ts.id, room_id=room_id))
                    for k in keys:
                        trackers.division_day_occurrence.add(k)
                    placed = True
                    break

        # 4. remaining single theory/tutorial sessions: VISITING FACULTY SESSIONS SORTED FOREMOST
        singles.sort(key=lambda r: (not r.is_visiting_faculty, r.session_type != SessionType.THEORY, r.course_code))
        for req in singles:
            room_pool = classrooms
            placed = False
            for day, day_slots in slots_by_day.items():
                if placed:
                    break
                key = (req.division_id, req.course_code, day)
                if key in trackers.division_day_occurrence:
                    continue
                for ts in day_slots:
                    slot_ids = [ts.id]
                    if _blocked(slot_ids):
                        continue
                    if not trackers.division_free(req.division_id, None, slot_ids):
                        continue
                    if not trackers.faculty_free(req.faculty_id, day, slot_ids, req.duration_slots):
                        continue
                    room_id = next((r for r in room_pool if (r, ts.id) not in trackers.room_busy), None)
                    if room_id is None:
                        continue
                    trackers.commit(req.division_id, None, req.faculty_id, room_id, day, slot_ids, req.duration_slots)
                    assignments.append(Assignment(session_id=req.id, time_slot_id=ts.id, room_id=room_id))
                    trackers.division_day_occurrence.add(key)
                    placed = True
                    break

        elapsed = time.time() - start
        status = "FEASIBLE" if len(assignments) == len(requirements) else "PARTIAL"
        return Solution(assignments=assignments, solver_name=self.name, wall_clock_seconds=elapsed, status=status)
