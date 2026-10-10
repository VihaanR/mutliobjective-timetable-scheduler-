"""Shared candidate-slot/room generation, used by GA and CP-SAT (mirrors the pruning logic in
mip.py so all solvers agree on what counts as a structurally-valid placement)."""
from __future__ import annotations

from engine.models import ProblemInstance, SessionRequirement

NO_ROOM = "NONE"


def slots_by_day(problem: ProblemInstance) -> dict[int, list]:
    out: dict[int, list] = {}
    for t in problem.time_slots:
        out.setdefault(t.day, []).append(t)
    for day in out:
        out[day].sort(key=lambda t: t.period)
    return out


def consecutive_slot_ids(day_slots: list, start_idx: int, duration: int) -> list[int] | None:
    if start_idx + duration > len(day_slots):
        return None
    window = day_slots[start_idx:start_idx + duration]
    for a, b in zip(window, window[1:]):
        if b.period != a.period + 1:
            return None
    return [t.id for t in window]


def batch_group_members(requirements: list[SessionRequirement]) -> dict[str, list[SessionRequirement]]:
    groups: dict[str, list[SessionRequirement]] = {}
    for r in requirements:
        if r.batch_group_id:
            groups.setdefault(r.batch_group_id, []).append(r)
    return groups


def sync_group_members(requirements: list[SessionRequirement]) -> dict[str, list[SessionRequirement]]:
    groups: dict[str, list[SessionRequirement]] = {}
    for r in requirements:
        if r.sync_group_id:
            groups.setdefault(r.sync_group_id, []).append(r)
    return groups


def _classroom_pref_key(rid: str) -> tuple[int, str]:
    r = rid.upper()
    if "51" in r:
        return (0, rid)
    if "52" in r:
        return (1, rid)
    if "53" in r:
        return (2, rid)
    return (10, rid)


def _lab_pref_key(rid: str) -> tuple[int, str]:
    r = rid.upper()
    # 4th floor ICB labs are auxiliary/external to CSE-DS and must have lowest preference
    if "ICB" in r:
        return (99, rid)
    # 5th floor CSE-DS home labs: L1, L2, L3, L4
    if r == "L1" or r.startswith("L1-") or r.startswith("L1_") or r.startswith("LAB1"):
        return (0, rid)
    if r == "L2" or r.startswith("L2-") or r.startswith("L2_") or r.startswith("LAB2"):
        return (1, rid)
    if r == "L3" or r.startswith("L3-") or r.startswith("L3_") or r.startswith("LAB3"):
        return (2, rid)
    if r == "L4" or r.startswith("L4-") or r.startswith("L4_") or r.startswith("LAB4"):
        return (3, rid)
    return (10, rid)


def room_options_for(req: SessionRequirement, problem: ProblemInstance,
                      groups: dict[str, list[SessionRequirement]]) -> list[str]:
    # Highest soft preference: classrooms 51, 52, 53 and labs L1, L2, L3
    classrooms = sorted([r.id for r in problem.rooms if r.room_type == "classroom"], key=_classroom_pref_key)
    labs = sorted([r.id for r in problem.rooms if r.room_type == "lab"], key=_lab_pref_key)
    if req.room_type == "none":
        return [NO_ROOM]
    if req.room_type == "lab":
        if req.batch_group_id:
            members = groups.get(req.batch_group_id, [req])
            idx = members.index(req) if req in members else 0
            partitioned = [rid for i, rid in enumerate(labs) if i % 2 == idx % 2]
            return partitioned or list(labs)
        return list(labs)
    if req.room_type == "classroom" or req.room_type not in ("none", "lab"):
        if classrooms and req.division_id:
            div_ids = sorted([d.id for d in problem.divisions])
            if req.division_id in div_ids:
                idx = div_ids.index(req.division_id)
                primary = classrooms[idx % len(classrooms)]
                if not problem.blocked_room_slots:
                    return [primary]
                return [primary] + [c for c in classrooms if c != primary]
        return list(classrooms)
    return list(classrooms)


def build_candidates(problem: ProblemInstance,
                      requirements: list[SessionRequirement]) -> dict[str, list[tuple[int, list[int], int, str]]]:
    """req.id -> list of (start_slot_id, occupied_slot_ids, day, room_id)."""
    days = slots_by_day(problem)
    groups = batch_group_members(requirements)
    faculty_by_id = problem.faculty_by_id()
    divisions_by_id = problem.division_by_id()
    courses_by_code = problem.course_by_code()
    rooms_by_id = problem.room_by_id()
    out: dict[str, list[tuple[int, list[int], int, str]]] = {}

    for req in requirements:
        room_options = room_options_for(req, problem, groups)
        division = divisions_by_id.get(req.division_id)
        occupants = (division.student_count // 2) if (req.batch_id and division) else (division.student_count if division else 0)
        course = courses_by_code.get(req.course_code)
        is_oe = "OE" in req.course_code or (course and getattr(course, "category", None) == "open_elective")
        cands: list[tuple[int, list[int], int, str]] = []
        for day, day_slots in days.items():
            if req.fixed_day is not None and day != req.fixed_day:
                continue
            for start_idx, ts in enumerate(day_slots):
                occ_ids = consecutive_slot_ids(day_slots, start_idx, req.duration_slots)
                if occ_ids is None:
                    continue
                # break must sit in the mid-day band: not first two periods, not last period
                if req.is_break and (start_idx <= 1 or start_idx == len(day_slots) - 1):
                    continue
                # Open Elective must sit at the start of day (period 0) or end of day (period >= 5)
                if is_oe and ts.period != 0 and ts.period < 5:
                    continue
                # nothing may be scheduled into a blocked slot (disruption window); breaks are
                # exempt (a blocked slot is simply untaught, which a break already represents)
                if problem.blocked_slot_ids and not req.is_break \
                        and any(sid in problem.blocked_slot_ids for sid in occ_ids):
                    continue
                if req.fixed_time_slot_id is not None and ts.id != req.fixed_time_slot_id:
                    continue
                if req.faculty_id:
                    fac = faculty_by_id.get(req.faculty_id)
                    if fac:
                        if any(sid in fac.unavailable_slots for sid in occ_ids):
                            continue
                        if fac.is_visiting:
                            # Visiting faculty hard window filtering
                            if fac.visiting_days and day not in fac.visiting_days:
                                continue
                            if fac.visiting_start_time and ts.start < fac.visiting_start_time:
                                continue
                            if fac.visiting_end_time:
                                last_slot = day_slots[start_idx + req.duration_slots - 1]
                                if last_slot.end > fac.visiting_end_time:
                                    continue
                for room_id in room_options:
                    if room_id != NO_ROOM:
                        room = rooms_by_id[room_id]
                        if room.capacity < occupants:
                            continue
                        if problem.blocked_room_slots and any((room_id, sid) in problem.blocked_room_slots for sid in occ_ids):
                            continue
                    cands.append((ts.id, occ_ids, day, room_id))
        out[req.id] = cands
    return out
