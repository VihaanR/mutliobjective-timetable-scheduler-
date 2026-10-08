"""Single source of truth for constraint scoring.

Implements the hard/soft constraint specification from `references/TIMETABLE_CONSTRAINTS.pdf`
and `references/NEP_Optimized_Timetable_Constraints.pdf`. Used by GA's fitness function, the
pipeline's `better()` comparator, and the benchmark script — so every solver is judged by the
exact same yardstick.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from engine.models import ProblemInstance, Solution, SessionType, CourseCategory, expand_requirements

# A compact day is the 6-hour minimum academic load (hard constraint 4) plus one break = 7
# occupied periods. Days spanning wider than this are softly penalised (see `day_span` below) so
# the optimizer prefers shorter/varied days over stretching every division across the whole
# 08:00-late template.
COMPACT_DAY_SPAN = 7

# Max continuous teaching periods (hours) before a break is mandatory (hard constraint 21).
# 1 slot = 1 hour, so this is directly the period count.
MAX_CONTINUOUS_TEACHING_PERIODS = 4

SOFT_WEIGHTS = {
    "heavy_subject_run": 5.0,
    "teacher_workload_spread": 1.0,
    "idle_gaps": 100.0,
    "earliest_latest_same_day": 3.0,
    "lab_not_before_final_slots": 4.0,
    "room_capacity_waste": 0.1,
    "division_load_inequity": 1.0,
    "lab_distribution_pattern": 3.0,
    "break_not_midmorning": 1.5,
    "day_span": 2.0,
}


@dataclass
class ScoreResult:
    hard_violations: int
    soft_cost: float
    details: dict[str, int] = field(default_factory=dict)

    def key(self) -> tuple[int, float]:
        """Lexicographic comparison key: hard violations dominate, soft cost breaks ties."""
        return (self.hard_violations, self.soft_cost)


def _occupied_slot_ids(time_slot, duration_slots, slots_by_day_period, hard_add):
    ids = [time_slot.id]
    day, period = time_slot.day, time_slot.period
    for k in range(1, duration_slots):
        nxt = slots_by_day_period.get((day, period + k))
        if nxt is None:
            hard_add("session_extends_past_day_end", 1)
            return ids
        ids.append(nxt.id)
    return ids


def score(solution: Solution, problem: ProblemInstance) -> ScoreResult:
    requirements = expand_requirements(problem)
    req_by_id = {r.id: r for r in requirements}
    assignments = solution.assignment_by_session()

    time_slots_by_id = {t.id: t for t in problem.time_slots}
    slots_by_day_period = {(t.day, t.period): t for t in problem.time_slots}
    rooms_by_id = problem.room_by_id()
    faculty_by_id = problem.faculty_by_id()
    divisions_by_id = problem.division_by_id()
    courses_by_code = problem.course_by_code()

    hard = 0
    hard_breakdown: dict[str, int] = {}
    soft: dict[str, float] = {k: 0.0 for k in SOFT_WEIGHTS}

    def hard_add(label, n=1):
        nonlocal hard
        hard += n
        hard_breakdown[label] = hard_breakdown.get(label, 0) + n

    # room/faculty/division(+batch) occupancy trackers
    room_busy: dict[tuple[str, int], str] = {}
    faculty_busy: dict[tuple[str, int], str] = {}
    whole_division_busy: dict[tuple[str, int], str] = {}
    batch_busy: dict[tuple[str, str, int], str] = {}

    # per-(division,course,day) occurrence tracking for "at most once/day" + daily mix
    day_occurrence: dict[tuple[str, str, int], set[str]] = {}
    division_day_has_practical_or_skill: dict[tuple[str, int], bool] = {}
    division_day_periods: dict[tuple[str, int], list[int]] = {}
    division_day_break_periods: dict[tuple[str, int], set[int]] = {}
    division_day_hours: dict[tuple[str, int], float] = {}
    faculty_day_hours: dict[tuple[str, int], float] = {}
    faculty_day_periods: dict[tuple[str, int], list[int]] = {}
    faculty_week_hours: dict[str, float] = {}
    batch_group_slots: dict[str, set] = {}
    batch_group_rooms: dict[str, set] = {}
    sync_group_slots: dict[str, set] = {}
    lab_hours_counted: set[tuple[str, int, str]] = set()  # (division, day, batch_group_id): avoid
                                                            # double-counting simultaneous batch pairs

    for req in requirements:
        assignment = assignments.get(req.id)
        if assignment is None:
            hard_add("unassigned_session")
            continue

        ts = time_slots_by_id.get(assignment.time_slot_id)
        if ts is None:
            hard_add("invalid_time_slot")
            continue

        if req.fixed_time_slot_id is not None and assignment.time_slot_id != req.fixed_time_slot_id:
            hard_add("protected_block_moved")

        if req.fixed_day is not None and ts.day != req.fixed_day:
            # each division's daily break is pinned to its own day -> enforces exactly one
            # break per day (never 0 on one day and 2 on another)
            hard_add("session_on_wrong_day")

        if req.is_break:
            day_slots = problem.slots_for_day(ts.day)
            # break must sit in a mid-day band: not the first two periods (so it never reads as
            # a "start of day" break) and not the last period. Matches the real DJSCE timetables,
            # where the break is a genuine mid-morning/lunch slot.
            if day_slots:
                first_two = {day_slots[0].period, day_slots[1].period} if len(day_slots) > 1 else {day_slots[0].period}
                if ts.period in first_two or ts.period == day_slots[-1].period:
                    hard_add("break_outside_midday_band")
            # Record break period for contiguous-teaching-limit checking (hard constraint 21)
            dkey = (req.division_id, ts.day)
            division_day_break_periods.setdefault(dkey, set()).add(ts.period)

        occupied_ids = _occupied_slot_ids(ts, req.duration_slots, slots_by_day_period, hard_add)

        # nothing may be scheduled into a blocked slot (disruption window). Breaks are exempt:
        # a blocked slot is simply not taught, which a break already represents.
        if problem.blocked_slot_ids and not req.is_break:
            if any(sid in problem.blocked_slot_ids for sid in occupied_ids):
                hard_add("scheduled_into_blocked_slot")

        room = rooms_by_id.get(assignment.room_id)
        needs_room = req.room_type in ("classroom", "lab")
        if needs_room:
            if room is None:
                hard_add("invalid_room")
            else:
                if room.room_type != req.room_type:
                    hard_add("wrong_room_type")
                division = divisions_by_id.get(req.division_id)
                occupants = division.student_count // 2 if req.batch_id and division else (division.student_count if division else 0)
                if division and room.capacity < occupants:
                    hard_add("room_capacity_exceeded")
                elif room:
                    soft["room_capacity_waste"] += max(0, room.capacity - occupants)

                for sid in occupied_ids:
                    key = (assignment.room_id, sid)
                    if key in room_busy and room_busy[key] != req.id:
                        hard_add("room_double_booked")
                    room_busy[key] = req.id

        if req.faculty_id:
            faculty = faculty_by_id.get(req.faculty_id)
            for sid in occupied_ids:
                key = (req.faculty_id, sid)
                if key in faculty_busy and faculty_busy[key] != req.id:
                    hard_add("faculty_double_booked")
                faculty_busy[key] = req.id
                if faculty and sid in faculty.unavailable_slots:
                    hard_add("faculty_unavailable_slot_used")
            fd_key = (req.faculty_id, ts.day)
            faculty_day_hours[fd_key] = faculty_day_hours.get(fd_key, 0) + req.duration_slots
            faculty_day_periods.setdefault(fd_key, []).extend(range(ts.period, ts.period + req.duration_slots))
            faculty_week_hours[req.faculty_id] = faculty_week_hours.get(req.faculty_id, 0) + req.duration_slots

        division = divisions_by_id.get(req.division_id)
        batches = division.batch_pair() if division else ()
        for sid in occupied_ids:
            if req.batch_id is None:
                wkey = (req.division_id, sid)
                if wkey in whole_division_busy and whole_division_busy[wkey] != req.id:
                    hard_add("division_double_booked")
                whole_division_busy[wkey] = req.id
                for b in batches:
                    bkey = (req.division_id, b, sid)
                    if bkey in batch_busy and batch_busy[bkey] != req.id:
                        hard_add("division_double_booked")
                    batch_busy[bkey] = req.id
            else:
                wkey = (req.division_id, sid)
                bkey = (req.division_id, req.batch_id, sid)
                if wkey in whole_division_busy and whole_division_busy[wkey] != req.id:
                    hard_add("division_double_booked")
                if bkey in batch_busy and batch_busy[bkey] != req.id:
                    hard_add("division_double_booked")
                batch_busy[bkey] = req.id

        # A break is a real scheduled event, not a hole in the day -- it must count as "occupied"
        # for gap/compactness purposes (idle_gaps below, and CP-SAT's native gap-minimization),
        # otherwise the optimizer is incentivized to shove the break to the day's edge (to avoid
        # being "sandwiched" between occupied periods) instead of a natural mid-day placement.
        dkey = (req.division_id, ts.day)
        division_day_periods.setdefault(dkey, []).extend(range(ts.period, ts.period + req.duration_slots))

        if not req.is_break and req.course_code != "BREAK":
            # simultaneous batch-pair lab sessions are one wall-clock block for the division,
            # not two -- only the first-seen half of a batch_group contributes to division hours
            lab_key = (req.division_id, ts.day, req.batch_group_id)
            if req.batch_group_id is None or lab_key not in lab_hours_counted:
                division_day_hours[dkey] = division_day_hours.get(dkey, 0) + req.duration_slots
                if req.batch_group_id is not None:
                    lab_hours_counted.add(lab_key)

            occ_key = req.batch_group_id or req.id
            oc_set = day_occurrence.setdefault((req.division_id, req.course_code, ts.day), set())
            oc_set.add(occ_key)

            course = courses_by_code.get(req.course_code)
            if req.session_type == SessionType.PRACTICAL or (course and course.category == CourseCategory.SKILL):
                division_day_has_practical_or_skill[(req.division_id, ts.day)] = True

            if req.batch_group_id:
                batch_group_slots.setdefault(req.batch_group_id, set()).add(assignment.time_slot_id)
                batch_group_rooms.setdefault(req.batch_group_id, set()).add(assignment.room_id)

            if req.sync_group_id:
                sync_group_slots.setdefault(req.sync_group_id, set()).add(assignment.time_slot_id)

    for (div_id, code, day), occurrences in day_occurrence.items():
        if len(occurrences) > 1:
            hard_add("subject_more_than_once_per_day", len(occurrences) - 1)

    for bg, slots in batch_group_slots.items():
        if len(slots) > 1:
            hard_add("batch_pair_not_simultaneous")
    for bg, rooms in batch_group_rooms.items():
        if len(rooms) < 2:
            hard_add("batch_pair_same_room")

    for sg, slots in sync_group_slots.items():
        if len(slots) > 1:
            hard_add("open_elective_slot_inconsistent_across_divisions")

    for (fid, day), hrs in faculty_day_hours.items():
        if hrs > 6:
            hard_add("faculty_daily_workload_exceeded")
        periods = sorted(faculty_day_periods[(fid, day)])
        run = 1
        for i in range(1, len(periods)):
            if periods[i] == periods[i - 1] + 1:
                run += 1
                fac = faculty_by_id.get(fid)
                cap = fac.max_consecutive_sessions if fac else 2
                if run > cap:
                    hard_add("faculty_too_many_consecutive_sessions")
            else:
                run = 1

    faculty_allocated_hours = {}
    for r in requirements:
        if r.faculty_id:
            faculty_allocated_hours[r.faculty_id] = faculty_allocated_hours.get(r.faculty_id, 0) + r.duration_slots

    for fid, hrs in faculty_week_hours.items():
        fac = faculty_by_id.get(fid)
        cap = fac.max_load_hours_per_week if fac else 20
        allowed_cap = max(cap, faculty_allocated_hours.get(fid, 0))
        if hrs > allowed_cap:
            hard_add("faculty_weekly_load_exceeded")

    unrelaxed_days_count = len([d for d in range(problem.days_per_week) if d not in problem.relaxed_days])
    for division in problem.divisions:
        div_courses = [courses_by_code[c] for c in division.course_codes if c in courses_by_code]
        total_prac_skill = sum(c.practical_sessions_per_week for c in div_courses) + sum(
            c.theory_sessions_per_week for c in div_courses if c.category == CourseCategory.SKILL
        )
        has_enough_practicals = total_prac_skill >= unrelaxed_days_count
        for day in range(problem.days_per_week):
            # a disrupted day (rain/holiday) is exempt from the day-shaped hard rules: with part
            # of the day blocked, a division legitimately can't hit 6-8h, start at period 0, have
            # a lab, or maintain the continuous-teaching limit -- scoring these would declare every
            # re-solve infeasible.
            if day in problem.relaxed_days:
                continue
            dkey = (division.id, day)
            hrs = division_day_hours.get(dkey, 0)
            if hrs == 0:
                continue
            if not (6 <= hrs <= 8):
                hard_add("division_daily_load_out_of_range")
            if has_enough_practicals and not division_day_has_practical_or_skill.get(dkey, False):
                hard_add("division_all_theory_day")
            # the day must start at the first period (08:00): no empty leading slot that would
            # make the day begin with a gap or a "start-of-day" break
            day_slots = problem.slots_for_day(day)
            if day_slots:
                occupied_periods = set(division_day_periods.get(dkey, []))
                if day_slots[0].period not in occupied_periods:
                    hard_add("division_day_has_empty_first_slot")
            # no more than MAX_CONTINUOUS_TEACHING_PERIODS (4 hours) of contiguous non-break
            # teaching periods per day (hard constraint 21): a division must have a break every
            # 4 hours or less, not just somewhere in the day (constraint 3).
            periods = sorted(set(division_day_periods.get(dkey, [])))
            break_periods = division_day_break_periods.get(dkey, set())
            if periods:
                # filter to only teaching periods (exclude breaks)
                teaching_periods = [p for p in periods if p not in break_periods]
                if teaching_periods:
                    # count contiguous runs of teaching periods
                    run = 1
                    for i in range(1, len(teaching_periods)):
                        if teaching_periods[i] == teaching_periods[i - 1] + 1:
                            run += 1
                            if run > MAX_CONTINUOUS_TEACHING_PERIODS:
                                hard_add("division_continuous_teaching_exceeds_limit")
                        else:
                            run = 1  # gap (or break) interrupts the run

    # ---- soft constraints ----
    for (div_id, day), periods in division_day_periods.items():
        if not periods:
            continue
        s, e = min(periods), max(periods)
        occupied = set(periods)
        gaps = sum(1 for p in range(s, e + 1) if p not in occupied)
        soft["idle_gaps"] += gaps
        day_slots = problem.slots_for_day(day)
        if day_slots and s == day_slots[0].period and e == day_slots[-1].period:
            soft["earliest_latest_same_day"] += 1
        # discourage a division's day stretching across the whole template (the "8am-to-late every
        # day" feel): penalise the occupied span beyond a compact target, so a 6h day is preferred
        # over an 8h day where the credit load allows, and day lengths vary across the week.
        span = e - s + 1
        soft["day_span"] += max(0, span - COMPACT_DAY_SPAN)

    for req in requirements:
        if not req.is_break:
            continue
        assignment = assignments.get(req.id)
        if not assignment:
            continue
        ts = time_slots_by_id.get(assignment.time_slot_id)
        if not ts:
            continue
        day_slots = problem.slots_for_day(ts.day)
        if not day_slots:
            continue
        # Vary the break's target across days so the optimizer is rewarded for SPREADING breaks
        # over the week rather than parking every day's break in the same period. The legal band is
        # the mid-day slots (never the first two periods or the last, per the hard rule); we rotate
        # the per-day target through that band by day index, mirroring the greedy seed. Falls back
        # to the ~mid-morning heuristic for very short days with no interior band.
        band = [t.period for t in day_slots[2:-1]]
        if band:
            target_period = band[ts.day % len(band)]
        else:
            target_period = day_slots[0].period + max(1, round(len(day_slots) * 0.35))
        soft["break_not_midmorning"] += abs(ts.period - target_period)

    for fid, hrs in faculty_week_hours.items():
        per_day = [faculty_day_hours.get((fid, d), 0) for d in range(problem.days_per_week)]
        worked_days = [h for h in per_day if h > 0]
        if worked_days:
            mean = sum(worked_days) / len(worked_days)
            soft["teacher_workload_spread"] += sum((h - mean) ** 2 for h in worked_days)

    daily_totals: dict[int, list[float]] = {}
    for (div_id, day), hrs in division_day_hours.items():
        daily_totals.setdefault(day, []).append(hrs)
    for day, totals in daily_totals.items():
        if len(totals) > 1:
            mean = sum(totals) / len(totals)
            soft["division_load_inequity"] += sum((t - mean) ** 2 for t in totals)

    for req in requirements:
        if req.session_type != SessionType.PRACTICAL:
            continue
        assignment = assignments.get(req.id)
        if not assignment:
            continue
        ts = time_slots_by_id.get(assignment.time_slot_id)
        if not ts:
            continue
        day_slots = problem.slots_for_day(ts.day)
        if day_slots and len(day_slots) >= 2:
            last_two = {day_slots[-1].period, day_slots[-2].period}
            if ts.period in last_two:
                soft["lab_not_before_final_slots"] += 1

    heavy_by_div_day: dict[tuple[str, int], list[int]] = {}
    for req in requirements:
        course = courses_by_code.get(req.course_code)
        if not course or not course.is_heavy:
            continue
        assignment = assignments.get(req.id)
        if not assignment:
            continue
        ts = time_slots_by_id.get(assignment.time_slot_id)
        if not ts:
            continue
        heavy_by_div_day.setdefault((req.division_id, ts.day), []).append(ts.period)
    for key, periods in heavy_by_div_day.items():
        periods = sorted(periods)
        run = 1
        for i in range(1, len(periods)):
            if periods[i] == periods[i - 1] + 1:
                run += 1
            else:
                run = 1
            if run > 2:
                soft["heavy_subject_run"] += 1

    labs_per_div_day: dict[tuple[str, int], set[str]] = {}
    for req in requirements:
        if req.session_type != SessionType.PRACTICAL or not req.batch_group_id:
            continue
        assignment = assignments.get(req.id)
        if not assignment:
            continue
        ts = time_slots_by_id.get(assignment.time_slot_id)
        if not ts:
            continue
        labs_per_div_day.setdefault((req.division_id, ts.day), set()).add(req.batch_group_id)
    for division in problem.divisions:
        counts = [len(labs_per_div_day.get((division.id, d), set())) for d in range(problem.days_per_week)]
        if sum(counts) == 0:
            continue
        days_with_2 = sum(1 for c in counts if c == 2)
        days_with_1 = sum(1 for c in counts if c == 1)
        days_with_0 = sum(1 for c in counts if c == 0)
        soft["lab_distribution_pattern"] += days_with_0 * 2 + abs(days_with_2 - 2) + abs(days_with_1 - 3)

    soft_cost = sum(SOFT_WEIGHTS[k] * v for k, v in soft.items())
    details = {k: int(round(v)) for k, v in soft.items()}
    details.update({f"hard:{k}": v for k, v in hard_breakdown.items()})
    return ScoreResult(hard_violations=hard, soft_cost=soft_cost, details=details)


def better(a: Solution, b: Solution | None, problem: ProblemInstance) -> Solution:
    """Returns whichever solution scores lexicographically better; never regresses."""
    if b is None:
        return a
    return a if score(a, problem).key() <= score(b, problem).key() else b
