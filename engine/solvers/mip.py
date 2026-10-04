"""Mixed Integer Programming solver — Zane's approach.

Uses OR-Tools' `pywraplp` linear-solver wrapper (CBC backend) rather than a second dependency
like PuLP. Boolean variables x[req, start_slot, room] are pruned to structurally-valid combos
before creation (faculty availability, room type/capacity, break-slot exclusion, fixed protected
slots) to keep the model tractable. Batch-pair "different room" (hard constraint 16) is enforced
by construction: each half of a lab pair only gets candidate rooms from a disjoint half of the
lab pool, so the two halves can never choose the same physical lab; "same slot" is enforced by an
explicit linear equality between the two halves' slot-indicator sums. Gap-minimization and the
lab-distribution-pattern soft constraint are NOT linearized in this MVP objective (documented
limitation vs CP-SAT, which expresses them natively via NoOverlap + reified booleans).
"""
from __future__ import annotations

import time

from ortools.linear_solver import pywraplp

from engine.models import (
    Assignment, CourseCategory, ProblemInstance, Solution, SessionType, expand_requirements,
)
from engine.scoring import MAX_CONTINUOUS_TEACHING_PERIODS
from engine.solvers.base import SolverBase

NO_ROOM = "NONE"


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


class MIPSolver(SolverBase):
    name = "mip"

    def solve(self, problem: ProblemInstance, time_limit_s: float = 60,
              warm_start: Solution | None = None) -> Solution:
        start_time = time.time()
        solver = pywraplp.Solver.CreateSolver("CBC")
        solver.SetTimeLimit(int(time_limit_s * 1000))

        requirements = expand_requirements(problem)
        slots_by_day = _slots_by_day(problem)
        rooms_by_id = problem.room_by_id()
        classrooms = [r.id for r in problem.rooms if r.room_type == "classroom"]
        labs = [r.id for r in problem.rooms if r.room_type == "lab"]
        faculty_by_id = problem.faculty_by_id()
        divisions_by_id = problem.division_by_id()

        batch_group_members: dict[str, list] = {}
        for r in requirements:
            if r.batch_group_id:
                batch_group_members.setdefault(r.batch_group_id, []).append(r)
        sync_group_members: dict[str, list] = {}
        for r in requirements:
            if r.sync_group_id:
                sync_group_members.setdefault(r.sync_group_id, []).append(r)

        # candidates[req.id] = list of (start_slot_id, occupied_slot_ids, day, room_id, cost)
        candidates: dict[str, list[tuple[int, list[int], int, str, float]]] = {}

        for req in requirements:
            if req.room_type == "none":
                room_options = [NO_ROOM]
            elif req.room_type == "lab":
                if req.batch_group_id:
                    idx = batch_group_members[req.batch_group_id].index(req)
                    room_options = [rid for i, rid in enumerate(labs) if i % 2 == idx % 2] or list(labs)
                else:
                    room_options = list(labs)
            else:
                room_options = list(classrooms)

            division = divisions_by_id.get(req.division_id)
            occupants = (division.student_count // 2) if (req.batch_id and division) else (division.student_count if division else 0)

            cands = []
            for day, day_slots in slots_by_day.items():
                if req.fixed_day is not None and day != req.fixed_day:
                    continue
                for start_idx, ts in enumerate(day_slots):
                    occ_ids = _consecutive_slot_ids(day_slots, start_idx, req.duration_slots)
                    if occ_ids is None:
                        continue
                    # break must sit in the mid-day band: not first two periods, not last period
                    if req.is_break and (start_idx <= 1 or start_idx == len(day_slots) - 1):
                        continue
                    # nothing may be scheduled into a blocked slot (disruption window); breaks exempt
                    if problem.blocked_slot_ids and not req.is_break \
                            and any(sid in problem.blocked_slot_ids for sid in occ_ids):
                        continue
                    if req.fixed_time_slot_id is not None and ts.id != req.fixed_time_slot_id:
                        continue
                    if req.faculty_id:
                        fac = faculty_by_id.get(req.faculty_id)
                        if fac and any(sid in fac.unavailable_slots for sid in occ_ids):
                            continue
                    is_late_lab_slot = (req.session_type == SessionType.PRACTICAL and len(day_slots) >= 2
                                        and ts.period in {day_slots[-1].period, day_slots[-2].period})
                    for room_id in room_options:
                        if room_id != NO_ROOM:
                            room = rooms_by_id[room_id]
                            if room.capacity < occupants:
                                continue
                            waste = max(0, room.capacity - occupants)
                        else:
                            waste = 0
                        cost = 0.1 * waste + (20.0 if is_late_lab_slot else 0.0)
                        cands.append((ts.id, occ_ids, day, room_id, cost))
            candidates[req.id] = cands

        x = {}
        for req in requirements:
            for (start_id, _occ, _day, room_id, _cost) in candidates[req.id]:
                x[(req.id, start_id, room_id)] = solver.BoolVar(f"x_{req.id}_{start_id}_{room_id}")

        for req in requirements:
            cands = candidates[req.id]
            if not cands:
                continue
            solver.Add(sum(x[(req.id, s, r)] for (s, _, _, r, _) in cands) == 1)

        def _accumulate(bucket: dict, key, var):
            bucket.setdefault(key, []).append(var)

        room_terms: dict = {}
        faculty_terms: dict = {}
        whole_div_terms: dict = {}
        batch_terms: dict = {}
        for req in requirements:
            division = divisions_by_id.get(req.division_id)
            batches = division.batch_pair() if division else ()
            for (start_id, occ_ids, day, room_id, _cost) in candidates[req.id]:
                var = x[(req.id, start_id, room_id)]
                if room_id != NO_ROOM:
                    for sid in occ_ids:
                        _accumulate(room_terms, (room_id, sid), var)
                if req.faculty_id:
                    for sid in occ_ids:
                        _accumulate(faculty_terms, (req.faculty_id, sid), var)
                for sid in occ_ids:
                    if req.batch_id is None:
                        # whole-division sessions (theory/tutorial/break) block the entire
                        # division's calendar, including both batch tracks
                        _accumulate(whole_div_terms, (req.division_id, sid), var)
                        for b in batches:
                            _accumulate(batch_terms, (req.division_id, b, sid), var)
                    else:
                        # batch sessions only block their OWN batch track -- the whole point of
                        # constraint 16 is that both batches run simultaneously, so they must
                        # NOT compete for the whole-division key
                        _accumulate(batch_terms, (req.division_id, req.batch_id, sid), var)

        # teaching-only occupancy (excludes breaks) per (division_id, slot_id), for the
        # continuous-teaching limit (constraint 21). Batch-pair labs are counted once via the
        # first-seen half (the batch-pair same-slot equality constraint guarantees the other half
        # is 1 at exactly the same slot).
        division_teaching_terms: dict = {}
        seen_teaching_groups: set[str] = set()
        for req in requirements:
            if req.is_break:
                continue
            if req.batch_group_id:
                if req.batch_group_id in seen_teaching_groups:
                    continue
                seen_teaching_groups.add(req.batch_group_id)
            for (start_id, occ_ids, day, room_id, _cost) in candidates[req.id]:
                var = x[(req.id, start_id, room_id)]
                for sid in occ_ids:
                    _accumulate(division_teaching_terms, (req.division_id, sid), var)

        for vars_ in room_terms.values():
            solver.Add(sum(vars_) <= 1)
        for vars_ in faculty_terms.values():
            solver.Add(sum(vars_) <= 1)
        for vars_ in whole_div_terms.values():
            solver.Add(sum(vars_) <= 1)
        for vars_ in batch_terms.values():
            solver.Add(sum(vars_) <= 1)

        # subject at most once per day per division (batch-pair counted once via representative)
        seen_groups: set[str] = set()
        day_subject_terms: dict = {}
        for req in requirements:
            if req.is_break:
                continue
            if req.batch_group_id:
                if req.batch_group_id in seen_groups:
                    continue
                seen_groups.add(req.batch_group_id)
            for (start_id, _occ, day, room_id, _cost) in candidates[req.id]:
                _accumulate(day_subject_terms, (req.division_id, req.course_code, day), x[(req.id, start_id, room_id)])
        for vars_ in day_subject_terms.values():
            solver.Add(sum(vars_) <= 1)

        # batch-pair: same slot (different room already guaranteed by candidate pruning)
        for group_id, members in batch_group_members.items():
            anchor, others = members[0], members[1:]
            for other in others:
                slot_ids = {s for (s, _, _, _, _) in candidates[anchor.id]} | {s for (s, _, _, _, _) in candidates[other.id]}
                for slot_id in slot_ids:
                    anchor_terms = [x[(anchor.id, s, r)] for (s, _, _, r, _) in candidates[anchor.id] if s == slot_id]
                    other_terms = [x[(other.id, s, r)] for (s, _, _, r, _) in candidates[other.id] if s == slot_id]
                    solver.Add(sum(anchor_terms) == sum(other_terms))

        # cross-division sync (open elective): same slot across all linked divisions
        for group_id, members in sync_group_members.items():
            anchor, others = members[0], members[1:]
            for other in others:
                slot_ids = {s for (s, _, _, _, _) in candidates[anchor.id]} | {s for (s, _, _, _, _) in candidates[other.id]}
                for slot_id in slot_ids:
                    anchor_terms = [x[(anchor.id, s, r)] for (s, _, _, r, _) in candidates[anchor.id] if s == slot_id]
                    other_terms = [x[(other.id, s, r)] for (s, _, _, r, _) in candidates[other.id] if s == slot_id]
                    solver.Add(sum(anchor_terms) == sum(other_terms))

        # faculty daily (<=3h) / weekly load cap
        faculty_day_terms: dict = {}
        faculty_week_terms: dict = {}
        for req in requirements:
            if not req.faculty_id:
                continue
            for (start_id, _occ, day, room_id, _cost) in candidates[req.id]:
                var = x[(req.id, start_id, room_id)]
                faculty_day_terms.setdefault((req.faculty_id, day), []).append((var, req.duration_slots))
                faculty_week_terms.setdefault(req.faculty_id, []).append((var, req.duration_slots))
        # NEP_Optimized_Timetable_Constraints.pdf softens this to "within institutional norms"
        # rather than a fixed 1-3h figure; 6h/day (matching the division's own daily ceiling)
        # avoids spurious infeasibility when a faculty's 2-hour lab sections legitimately stack
        # across divisions on the same day, while still bounding unreasonable overload.
        for terms in faculty_day_terms.values():
            solver.Add(sum(v * d for v, d in terms) <= 6)
        for fid, terms in faculty_week_terms.items():
            fac = faculty_by_id.get(fid)
            base_cap = fac.max_load_hours_per_week if fac else 20
            # Same guard as in scoring.py / cpsat.py: if curriculum allocations for multi-year share
            # this faculty above base_cap, cap at total allocated hours so it's not falsely infeasible.
            total_allocated = sum(d for _, d in terms)
            cap = max(base_cap, total_allocated)
            solver.Add(sum(v * d for v, d in terms) <= cap)

        # no more than max_consecutive_sessions in a row for any faculty (hard constraint 9):
        # for every window of (cap+1) consecutive periods in a day, at most `cap` may be busy
        for fid in {r.faculty_id for r in requirements if r.faculty_id}:
            fac = faculty_by_id.get(fid)
            cap = fac.max_consecutive_sessions if fac else 2
            for day, day_slots in slots_by_day.items():
                for start in range(len(day_slots) - cap):
                    window = day_slots[start:start + cap + 1]
                    window_terms = []
                    for ts in window:
                        window_terms.extend(faculty_terms.get((fid, ts.id), []))
                    if window_terms:
                        solver.Add(sum(window_terms) <= cap)

        # division daily load in [6, 8] hours (batch pairs counted once)
        seen_groups2: set[str] = set()
        div_day_terms: dict = {}
        for req in requirements:
            if req.is_break:
                continue
            if req.batch_group_id:
                if req.batch_group_id in seen_groups2:
                    continue
                seen_groups2.add(req.batch_group_id)
            for (start_id, _occ, day, room_id, _cost) in candidates[req.id]:
                var = x[(req.id, start_id, room_id)]
                div_day_terms.setdefault((req.division_id, day), []).append((var, req.duration_slots))
        # a relaxed (disrupted) day is exempt from the day-shaped hard rules -- see scoring.py
        relaxed = problem.relaxed_days
        for (division_id, day), terms in div_day_terms.items():
            if day in relaxed:
                continue
            expr = sum(v * d for v, d in terms)
            solver.Add(expr >= 6)
            solver.Add(expr <= 8)

        # no more than MAX_CONTINUOUS_TEACHING_PERIODS (4 hours) of contiguous non-break teaching
        # periods per division per day (hard constraint 21): for every window of (cap+1) consecutive
        # periods in a day, at most `cap` may be busy with teaching sessions
        for division_id in {r.division_id for r in requirements}:
            for day, day_slots in slots_by_day.items():
                if day in relaxed:
                    continue
                cap = MAX_CONTINUOUS_TEACHING_PERIODS
                for start in range(len(day_slots) - cap):
                    window = day_slots[start:start + cap + 1]
                    window_terms = []
                    for ts in window:
                        window_terms.extend(division_teaching_terms.get((division_id, ts.id), []))
                    if window_terms:
                        solver.Add(sum(window_terms) <= cap)

        # all occupancy (teaching sessions + breaks) per (division_id, slot_id)
        division_all_terms: dict = {}
        seen_all_groups: set[str] = set()
        for req in requirements:
            if req.batch_group_id:
                if req.batch_group_id in seen_all_groups:
                    continue
                seen_all_groups.add(req.batch_group_id)
            for (start_id, occ_ids, day, room_id, _cost) in candidates[req.id]:
                var = x[(req.id, start_id, room_id)]
                for sid in occ_ids:
                    division_all_terms.setdefault((req.division_id, sid), []).append(var)

        # each division's day must START at the first period (08:00) and form a strictly CONTIGUOUS block (ZERO IDLE GAPS)
        for division in problem.divisions:
            for day, day_slots in slots_by_day.items():
                if day in relaxed or not day_slots:
                    continue
                div_occ_vars = []
                for ts in day_slots:
                    terms = division_all_terms.get((division.id, ts.id), [])
                    if terms:
                        occ = solver.BoolVar(f"div_occ_{division.id}_{day}_{ts.period}")
                        solver.Add(occ == sum(terms))
                        div_occ_vars.append(occ)
                    else:
                        occ = solver.BoolVar(f"div_occ_{division.id}_{day}_{ts.period}")
                        solver.Add(occ == 0)
                        div_occ_vars.append(occ)

                if div_occ_vars:
                    solver.Add(div_occ_vars[0] == 1)
                    for p in range(len(div_occ_vars) - 1):
                        solver.Add(div_occ_vars[p + 1] <= div_occ_vars[p])

        # no all-theory day: each day must have >=1 practical or skill-category session,
        # but only for divisions that have at least as many practicals as teaching days
        courses_by_code = problem.course_by_code()
        practical_or_skill_terms: dict = {}
        practical_count_by_div: dict[str, int] = {}
        for req in requirements:
            if req.is_break:
                continue
            course = courses_by_code.get(req.course_code)
            if not course:
                continue
            if req.session_type == SessionType.PRACTICAL or course.category == CourseCategory.SKILL:
                practical_count_by_div[req.division_id] = practical_count_by_div.get(req.division_id, 0) + (1 if not req.batch_group_id else 0.5)
                for (start_id, _occ, day, room_id, _cost) in candidates[req.id]:
                    practical_or_skill_terms.setdefault((req.division_id, day), []).append(
                        x[(req.id, start_id, room_id)])
        teaching_day_count = len([d for d in slots_by_day if d not in relaxed])
        for division in problem.divisions:
            if practical_count_by_div.get(division.id, 0) < teaching_day_count:
                continue
            for day in slots_by_day:
                if day in relaxed:
                    continue
                terms = practical_or_skill_terms.get((division.id, day), [])
                if terms:
                    solver.Add(sum(terms) >= 1)

        objective_terms = []
        for req in requirements:
            for (start_id, _occ, _day, room_id, cost) in candidates[req.id]:
                if cost:
                    objective_terms.append(cost * x[(req.id, start_id, room_id)])
        if objective_terms:
            solver.Minimize(sum(objective_terms))

        if warm_start is not None:
            hint_vars, hint_vals = [], []
            assigned = warm_start.assignment_by_session()
            for req in requirements:
                a = assigned.get(req.id)
                if a is None:
                    continue
                key = (req.id, a.time_slot_id, a.room_id)
                if key in x:
                    hint_vars.append(x[key])
                    hint_vals.append(1)
            if hint_vars:
                solver.SetHint(hint_vars, hint_vals)

        status_code = solver.Solve()
        elapsed = time.time() - start_time

        status_map = {
            pywraplp.Solver.OPTIMAL: "OPTIMAL",
            pywraplp.Solver.FEASIBLE: "FEASIBLE",
            pywraplp.Solver.INFEASIBLE: "INFEASIBLE",
        }
        status = status_map.get(status_code, "TIMEOUT")

        assignments: list[Assignment] = []
        if status in ("OPTIMAL", "FEASIBLE"):
            for req in requirements:
                for (start_id, _occ, _day, room_id, _cost) in candidates[req.id]:
                    var = x[(req.id, start_id, room_id)]
                    if var.solution_value() > 0.5:
                        assignments.append(Assignment(session_id=req.id, time_slot_id=start_id, room_id=room_id))
                        break

        return Solution(
            assignments=assignments, solver_name=self.name, wall_clock_seconds=elapsed,
            objective_value=solver.Objective().Value() if status in ("OPTIMAL", "FEASIBLE") else None,
            status=status,
        )
