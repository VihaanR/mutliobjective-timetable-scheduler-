"""Academic Quality & Welfare Index (AQWI) - Timetable Judge Engine.

Evaluates and ranks generated candidate timetables across 7 pedagogical, student-welfare,
and faculty-balance criteria:

1. Avoid 8-6 Student Day: Penalize days spanning excessively beyond a compact academic day,
   specifically discouraging 10-hour spans (08:00 to 18:00).
2. Student Free Gap Elimination: Strictly penalize idle unoccupied slots between a division's
   first and last class on any day.
3. No Same-Day Lecture & Lab: Penalize scheduling both a theory lecture and a practical lab
   of the same course on the same day for a student division.
4. Even Faculty Workload Distribution: Minimize variance in daily teaching hours for each
   faculty member across the working week.
5. Honours / Elective Boundary Placement: Honours and cross-department electives should sit at the
   day boundaries (start of day period 0 or closing periods) to prevent mid-day voids for other students.
6. 3 Consecutive Days Subject Spread: Penalize scheduling the same subject on 3 or more consecutive
   days (e.g., Mon, Tue, Wed) rather than distributing across the week.
7. Faculty Idle Gaps > 2 Hours: Penalize long waiting periods where a professor has an idle gap
   exceeding 2 hours between their morning and afternoon classes on the same day.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from engine.models import CourseCategory, ProblemInstance, SessionType, Solution, expand_requirements


@dataclass
class CriterionDetail:
    name: str
    weight: float
    raw_metric: float
    weighted_penalty: float
    description: str
    breakdown: dict[str, Any] = field(default_factory=dict)


@dataclass
class AQWIReport:
    """Judge evaluation report for a candidate timetable."""
    total_penalty: float
    quality_score: float  # 0 to 100% (100 = flawless)
    rank: int = 1
    criteria: dict[str, CriterionDetail] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_penalty": round(self.total_penalty, 2),
            "quality_score": round(self.quality_score, 1),
            "rank": self.rank,
            "criteria": {
                k: {
                    "name": v.name,
                    "weight": v.weight,
                    "raw_metric": round(v.raw_metric, 2),
                    "weighted_penalty": round(v.weighted_penalty, 2),
                    "description": v.description,
                    "breakdown": v.breakdown,
                }
                for k, v in self.criteria.items()
            },
        }


# Default weights calibrated for AQWI judge criteria
DEFAULT_JUDGE_WEIGHTS: dict[str, float] = {
    "c1_avoid_8_6_span": 50.0,
    "c2_student_idle_gaps": 100.0,
    "c3_same_day_lec_lab": 60.0,
    "c4_faculty_load_variance": 20.0,
    "c5_honours_boundary": 45.0,
    "c6_three_consecutive_days": 35.0,
    "c7_faculty_gaps_over_2h": 40.0,
}


def evaluate_aqwi(
    solution: Solution,
    problem: ProblemInstance,
    weights: dict[str, float] | None = None,
) -> AQWIReport:
    """Evaluate a candidate solution using the 7-point AQWI Judge Criteria."""
    w = dict(DEFAULT_JUDGE_WEIGHTS)
    if weights:
        w.update(weights)

    requirements = expand_requirements(problem)
    req_by_id = {r.id: r for r in requirements}
    assignments = solution.assignment_by_session()
    slots_by_id = {t.id: t for t in problem.time_slots}
    courses_by_code = problem.course_by_code()
    divisions_by_id = problem.division_by_id()

    # Trackers
    division_day_periods: dict[tuple[str, int], set[int]] = {}
    division_day_courses: dict[tuple[str, int], dict[str, set[SessionType]]] = {}
    division_course_days: dict[tuple[str, str], set[int]] = {}
    faculty_day_hours: dict[tuple[str, int], float] = {}
    faculty_day_periods: dict[tuple[str, int], set[int]] = {}
    honours_placements: list[dict[str, Any]] = []

    for req_id, assignment in assignments.items():
        req = req_by_id.get(req_id)
        if not req:
            continue
        ts = slots_by_id.get(assignment.time_slot_id)
        if not ts:
            continue

        day = ts.day
        start_p = ts.period
        end_p = start_p + req.duration_slots
        div_key = (req.division_id, day)

        # Track division occupied periods (including breaks so breaks count as occupied block)
        division_day_periods.setdefault(div_key, set()).update(range(start_p, end_p))

        if not req.is_break and req.course_code != "BREAK":
            # Track course sessions per division per day
            c_dict = division_day_courses.setdefault(div_key, {})
            c_dict.setdefault(req.course_code, set()).add(req.session_type)

            # Track which days this course appears for the division
            division_course_days.setdefault((req.division_id, req.course_code), set()).add(day)

            # Track faculty teaching load and periods
            if req.faculty_id:
                f_key = (req.faculty_id, day)
                faculty_day_hours[f_key] = faculty_day_hours.get(f_key, 0.0) + req.duration_slots
                faculty_day_periods.setdefault(f_key, set()).update(range(start_p, end_p))

            # Track Honours / Open Elective course placements
            course = courses_by_code.get(req.course_code)
            is_honours_or_oe = (
                course and (
                    course.category == CourseCategory.OPEN_ELECTIVE or
                    "honour" in course.title.lower() or
                    "honors" in course.title.lower() or
                    "oe" in course.code.lower()
                )
            )
            if is_honours_or_oe or req.sync_group_id is not None:
                honours_placements.append({
                    "course_code": req.course_code,
                    "division_id": req.division_id,
                    "day": day,
                    "period": start_p,
                })

    # -------------------------------------------------------------------------
    # Criterion 1: Avoid 8-6 Student Day Span
    # -------------------------------------------------------------------------
    c1_raw = 0.0
    c1_details: list[dict[str, Any]] = []
    for (div_id, day), periods in division_day_periods.items():
        if day in problem.relaxed_days or not periods:
            continue
        min_p = min(periods)
        max_p = max(periods)
        span = max_p - min_p + 1

        day_slots = problem.slots_for_day(day)
        max_day_period = max((t.period for t in day_slots), default=9)

        # Severe penalty if span extends across the whole day (08:00 to 18:00)
        is_full_day_stretch = (min_p == 0 and max_p >= max_day_period - 1)
        excess_span = max(0, span - 8)

        if is_full_day_stretch or excess_span > 0:
            penalty_val = excess_span + (2.0 if is_full_day_stretch else 0.0)
            c1_raw += penalty_val
            c1_details.append({
                "division": div_id,
                "day": day,
                "span": span,
                "start_period": min_p,
                "end_period": max_p,
                "is_full_day_stretch": is_full_day_stretch,
            })

    # -------------------------------------------------------------------------
    # Criterion 2: Student Free Gap Elimination
    # -------------------------------------------------------------------------
    c2_raw = 0.0
    c2_details: list[dict[str, Any]] = []
    for (div_id, day), periods in division_day_periods.items():
        if not periods or day in problem.relaxed_days:
            continue
        min_p, max_p = min(periods), max(periods)
        gaps = [p for p in range(min_p, max_p + 1) if p not in periods]
        if gaps:
            c2_raw += len(gaps)
            c2_details.append({
                "division": div_id,
                "day": day,
                "gap_count": len(gaps),
                "gap_periods": gaps,
            })

    # -------------------------------------------------------------------------
    # Criterion 3: No Same-Day Lecture & Practical Lab of Same Course
    # -------------------------------------------------------------------------
    c3_raw = 0.0
    c3_details: list[dict[str, Any]] = []
    for (div_id, day), courses in division_day_courses.items():
        for course_code, session_types in courses.items():
            has_theory = SessionType.THEORY in session_types or SessionType.TUTORIAL in session_types
            has_lab = SessionType.PRACTICAL in session_types
            if has_theory and has_lab:
                c3_raw += 1.0
                c3_details.append({
                    "division": div_id,
                    "day": day,
                    "course": course_code,
                })

    # -------------------------------------------------------------------------
    # Criterion 4: Even Faculty Workload Distribution across Active Days
    # -------------------------------------------------------------------------
    c4_raw = 0.0
    c4_details: list[dict[str, Any]] = []
    for fac in problem.faculty:
        daily_hours = [faculty_day_hours.get((fac.id, d), 0.0) for d in range(problem.days_per_week)]
        worked_hours = [h for h in daily_hours if h > 0.0]
        if len(worked_hours) > 1:
            mean_hrs = sum(worked_hours) / len(worked_hours)
            variance = sum((h - mean_hrs) ** 2 for h in worked_hours) / len(worked_hours)
            c4_raw += variance
            if variance > 1.5:
                c4_details.append({
                    "faculty_id": fac.id,
                    "variance": round(variance, 2),
                    "daily_distribution": daily_hours,
                })

    # -------------------------------------------------------------------------
    # Criterion 5: Honours / Electives at Day Boundaries
    # -------------------------------------------------------------------------
    c5_raw = 0.0
    c5_details: list[dict[str, Any]] = []
    for item in honours_placements:
        day = item["day"]
        period = item["period"]
        day_slots = problem.slots_for_day(day)
        last_period = max((t.period for t in day_slots), default=9)

        # Ideal positions: Period 0 (08:00 AM) or the final 2 periods of the day
        is_boundary = (period == 0 or period >= last_period - 1)
        if not is_boundary:
            c5_raw += 1.0
            c5_details.append({
                "course": item["course_code"],
                "division": item["division_id"],
                "day": day,
                "period": period,
                "distance_to_edge": min(period, last_period - period),
            })

    # -------------------------------------------------------------------------
    # Criterion 6: Same Subject Not on 3 Consecutive Days
    # -------------------------------------------------------------------------
    c6_raw = 0.0
    c6_details: list[dict[str, Any]] = []
    for (div_id, course_code), days in division_course_days.items():
        sorted_days = sorted(days)
        # Check for 3 consecutive days: day, day+1, day+2
        consec_streak = 1
        for i in range(1, len(sorted_days)):
            if sorted_days[i] == sorted_days[i - 1] + 1:
                consec_streak += 1
                if consec_streak >= 3:
                    c6_raw += 1.0
                    c6_details.append({
                        "division": div_id,
                        "course": course_code,
                        "consecutive_days": sorted_days[i - 2:i + 1],
                    })
            else:
                consec_streak = 1

    # -------------------------------------------------------------------------
    # Criterion 7: No Faculty Idle Gap > 2 Hours
    # -------------------------------------------------------------------------
    c7_raw = 0.0
    c7_details: list[dict[str, Any]] = []
    for fac in problem.faculty:
        for day in range(problem.days_per_week):
            periods = sorted(faculty_day_periods.get((fac.id, day), set()))
            if len(periods) <= 1:
                continue

            # Find interior gaps between assigned periods
            occupied_set = set(periods)
            min_p, max_p = min(periods), max(periods)

            current_gap = 0
            for p in range(min_p, max_p + 1):
                if p not in occupied_set:
                    current_gap += 1
                else:
                    if current_gap > 2:
                        excess = current_gap - 2
                        c7_raw += excess
                        c7_details.append({
                            "faculty_id": fac.id,
                            "day": day,
                            "gap_hours": current_gap,
                            "excess_hours": excess,
                        })
                    current_gap = 0
            if current_gap > 2:
                excess = current_gap - 2
                c7_raw += excess
                c7_details.append({
                    "faculty_id": fac.id,
                    "day": day,
                    "gap_hours": current_gap,
                    "excess_hours": excess,
                })

    # -------------------------------------------------------------------------
    # Composite Score Aggregation
    # -------------------------------------------------------------------------
    criteria: dict[str, CriterionDetail] = {
        "c1_avoid_8_6_span": CriterionDetail(
            name="8-6 Day Span Avoidance",
            weight=w["c1_avoid_8_6_span"],
            raw_metric=c1_raw,
            weighted_penalty=w["c1_avoid_8_6_span"] * c1_raw,
            description="Penalizes student days stretched across 10 hours (08:00 to 18:00).",
            breakdown={"violations": c1_details},
        ),
        "c2_student_idle_gaps": CriterionDetail(
            name="Student Gap Minimization",
            weight=w["c2_student_idle_gaps"],
            raw_metric=c2_raw,
            weighted_penalty=w["c2_student_idle_gaps"] * c2_raw,
            description="Penalizes idle unallotted gap hours between student lectures.",
            breakdown={"violations": c2_details},
        ),
        "c3_same_day_lec_lab": CriterionDetail(
            name="Same-Day Lec & Lab Separation",
            weight=w["c3_same_day_lec_lab"],
            raw_metric=c3_raw,
            weighted_penalty=w["c3_same_day_lec_lab"] * c3_raw,
            description="Prevents single-subject overload by separating theory and lab onto different days.",
            breakdown={"violations": c3_details},
        ),
        "c4_faculty_load_variance": CriterionDetail(
            name="Faculty Workload Balance",
            weight=w["c4_faculty_load_variance"],
            raw_metric=c4_raw,
            weighted_penalty=w["c4_faculty_load_variance"] * c4_raw,
            description="Evenly spreads teaching hours across a faculty member's active days.",
            breakdown={"violations": c4_details},
        ),
        "c5_honours_boundary": CriterionDetail(
            name="Honours Boundary Placement",
            weight=w["c5_honours_boundary"],
            raw_metric=c5_raw,
            weighted_penalty=w["c5_honours_boundary"] * c5_raw,
            description="Places Honours and Open Electives at start (08:00) or end of day to avoid midday gaps.",
            breakdown={"violations": c5_details},
        ),
        "c6_three_consecutive_days": CriterionDetail(
            name="3-Day Consecutive Subject Spread",
            weight=w["c6_three_consecutive_days"],
            raw_metric=c6_raw,
            weighted_penalty=w["c6_three_consecutive_days"] * c6_raw,
            description="Penalizes clustering the same subject on 3 or more consecutive weekdays.",
            breakdown={"violations": c6_details},
        ),
        "c7_faculty_gaps_over_2h": CriterionDetail(
            name="Faculty Long Gap Elimination",
            weight=w["c7_faculty_gaps_over_2h"],
            raw_metric=c7_raw,
            weighted_penalty=w["c7_faculty_gaps_over_2h"] * c7_raw,
            description="Prevents faculty from waiting idle on campus for more than 2 hours between classes.",
            breakdown={"violations": c7_details},
        ),
    }

    total_penalty = sum(c.weighted_penalty for c in criteria.values())

    # Map total penalty to an intuitive quality score [0% - 100%]
    # Scaled to institution cohort size (divisions and faculty count)
    k_baseline = max(2000.0, float(len(problem.divisions) * 1200.0 + len(problem.faculty) * 600.0))
    quality_score = round(max(0.0, min(100.0, 100.0 * math.exp(-total_penalty / k_baseline))), 1)

    return AQWIReport(
        total_penalty=total_penalty,
        quality_score=quality_score,
        rank=1,
        criteria=criteria,
    )
