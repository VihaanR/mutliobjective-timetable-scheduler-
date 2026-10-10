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
    unit: str = "violations"
    lower_is_better: bool = True
    breakdown: dict[str, Any] = field(default_factory=dict)


@dataclass
class AQWIReport:
    """Judge evaluation report for a candidate timetable."""
    total_penalty: float
    quality_score: float  # 0 to 100% (100 = flawless)
    cohort_baseline: float = 1000.0
    rank: int = 1
    criteria: dict[str, CriterionDetail] = field(default_factory=dict)
    pareto_status: str = "non_dominated"  # "non_dominated" | "dominated"
    pareto_rank: int = 1
    is_recommended: bool = False
    is_active: bool = False
    candidate_id: int = 0
    hard_violations: int = 0
    resource_utilization: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "hard_violations": self.hard_violations,
            "hard_constraint_status": "passed" if self.hard_violations == 0 else "failed",
            "total_penalty": round(self.total_penalty, 2),
            "unrounded_total_penalty": self.total_penalty,
            "cohort_baseline": round(self.cohort_baseline, 1),
            "quality_score": round(self.quality_score, 1),
            "rank": self.rank,
            "pareto_status": self.pareto_status,
            "pareto_front_rank_or_group": self.pareto_rank,
            "is_recommended": self.is_recommended,
            "is_active": self.is_active,
            "selection_status": "recommended" if self.is_recommended else ("active" if self.is_active else "alternative"),
            "criterion_metrics": {
                f"M{i}": round(v.raw_metric, 2)
                for i, v in enumerate(self.criteria.values(), start=1)
            },
            "raw_metrics": {
                k: round(v.raw_metric, 4)
                for k, v in self.criteria.items()
            },
            "criteria": {
                k: {
                    "name": v.name,
                    "weight": v.weight,
                    "raw_metric": round(v.raw_metric, 2),
                    "weighted_penalty": round(v.weighted_penalty, 2),
                    "unit": v.unit,
                    "lower_is_better": v.lower_is_better,
                    "description": v.description,
                    "breakdown": v.breakdown,
                }
                for k, v in self.criteria.items()
            },
            "resource_utilization": self.resource_utilization,
        }


# Simplified AQWI weights: all seven criteria calibrated with unit weight 1.0
AQWI_WEIGHTS: dict[str, float] = {
    "c1_avoid_8_6_span": 1.0,
    "c2_student_idle_gaps": 1.0,
    "c3_same_day_lec_lab": 1.0,
    "c4_faculty_load_variance": 1.0,
    "c5_honours_boundary": 1.0,
    "c6_three_consecutive_days": 1.0,
    "c7_faculty_gaps_over_2h": 1.0,
}

DEFAULT_JUDGE_WEIGHTS: dict[str, float] = dict(AQWI_WEIGHTS)
DEFAULT_COHORT_SCALE: float = 1000.0

CRITERION_KEYS: list[str] = [
    "c1_avoid_8_6_span",
    "c2_student_idle_gaps",
    "c3_same_day_lec_lab",
    "c4_faculty_load_variance",
    "c5_honours_boundary",
    "c6_three_consecutive_days",
    "c7_faculty_gaps_over_2h",
]


def compute_cohort_baseline(problem: ProblemInstance | None, scale: float = DEFAULT_COHORT_SCALE) -> float:
    """Simplified cohort baseline K = scale * (N_divisions + N_faculty).

    scale is a configurable institution scaling constant (default 1000.0).
    Safely handles empty or invalid datasets.
    """
    n_divisions = len(problem.divisions) if (problem and getattr(problem, "divisions", None)) else 0
    n_faculty = len(problem.faculty) if (problem and getattr(problem, "faculty", None)) else 0
    count = n_divisions + n_faculty
    if count == 0:
        return max(1.0, float(scale))
    return float(scale * count)


def dominates(vec_a: list[float], vec_b: list[float], tolerance: float = 1e-6) -> bool:
    """Candidate A dominates Candidate B iff:
    A is no worse than B on every criterion (A[i] <= B[i] + tolerance)
    AND
    A is strictly better than B on at least one criterion (A[i] < B[i] - tolerance).
    """
    no_worse = all(a <= b + tolerance for a, b in zip(vec_a, vec_b))
    strictly_better = any(a < b - tolerance for a, b in zip(vec_a, vec_b))
    return no_worse and strictly_better


def compute_pareto_front(candidates: list[dict[str, Any]], tolerance: float = 1e-6) -> list[dict[str, Any]]:
    """Identifies dominated and non-dominated candidates across the 7 AQWI metrics.

    Treats the 7 metrics as objectives to minimize.
    A candidate is non-dominated (Pareto-optimal within the evaluated candidate pool)
    iff no other evaluated candidate dominates it.
    Identical metric vectors correctly do NOT dominate each other.
    """
    if not candidates:
        return candidates

    def get_vector(cand: dict[str, Any]) -> list[float]:
        rep = cand.get("report")
        if isinstance(rep, AQWIReport):
            return [rep.criteria[k].raw_metric for k in CRITERION_KEYS]
        elif isinstance(rep, dict):
            if "raw_metrics" in rep:
                return [float(rep["raw_metrics"].get(k, 0.0)) for k in CRITERION_KEYS]
            crit = rep.get("criteria", {})
            return [float(crit.get(k, {}).get("raw_metric", 0.0)) for k in CRITERION_KEYS]
        return [0.0] * 7

    vectors = [get_vector(c) for c in candidates]
    n = len(candidates)
    is_dominated = [False] * n

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            if dominates(vectors[j], vectors[i], tolerance=tolerance):
                is_dominated[i] = True
                break

    for i, cand in enumerate(candidates):
        status = "dominated" if is_dominated[i] else "non_dominated"
        rank = 2 if is_dominated[i] else 1
        rep = cand.get("report")
        if isinstance(rep, AQWIReport):
            rep.pareto_status = status
            rep.pareto_rank = rank
        elif isinstance(rep, dict):
            rep["pareto_status"] = status
            rep["pareto_front_rank_or_group"] = rank
            rep["pareto_rank"] = rank

    return candidates


def compute_resource_utilization(solution: Solution, problem: ProblemInstance) -> dict[str, Any]:
    """Computes classroom and lab utilization statistics for the timetable solution.

    Tracks high-preference classrooms (51, 52, 53) vs auxiliary classrooms (e.g. 46),
    and preferred labs (L1, L2, L3) vs auxiliary labs (e.g. L4).
    """
    total_slots = len(problem.time_slots) if problem.time_slots else 45
    requirements = expand_requirements(problem)
    req_by_id = {r.id: r for r in requirements}
    assignments = solution.assignment_by_session()

    room_hours: dict[str, int] = {}
    for req_id, assignment in assignments.items():
        req = req_by_id.get(req_id)
        if not req or req.is_break or not assignment.room_id:
            continue
        room_hours[assignment.room_id] = room_hours.get(assignment.room_id, 0) + req.duration_slots

    classroom_stats = []
    lab_stats = []

    for r in problem.rooms:
        hours = room_hours.get(r.id, 0)
        utilization_pct = round((hours / max(1, total_slots)) * 100.0, 1)
        r_upper = r.id.upper()

        is_pref_classroom = any(x in r_upper for x in ["51", "52", "53"])
        # Preferred 5th floor CSE-DS labs: L1, L2, L3, L4 (explicitly excluding 4th floor ICB labs)
        is_pref_lab = (
            ("ICB" not in r_upper) and
            any(
                r_upper == x or r_upper.startswith(x + "-") or r_upper.startswith(x + "_")
                for x in ["L1", "L2", "L3", "L4", "LAB1", "LAB2", "LAB3", "LAB4", "LAB-1", "LAB-2", "LAB-3", "LAB-4"]
            )
        )

        item = {
            "room_id": r.id,
            "room_name": r.name,
            "room_type": r.room_type,
            "occupied_hours": hours,
            "total_slots": total_slots,
            "utilization_pct": utilization_pct,
            "is_preferred": is_pref_classroom if r.room_type == "classroom" else is_pref_lab,
        }

        if r.room_type == "classroom":
            classroom_stats.append(item)
        elif r.room_type == "lab":
            lab_stats.append(item)

    classroom_stats.sort(key=lambda x: (not x["is_preferred"], -x["occupied_hours"], x["room_id"]))
    lab_stats.sort(key=lambda x: (not x["is_preferred"], -x["occupied_hours"], x["room_id"]))

    return {
        "classrooms": classroom_stats,
        "labs": lab_stats,
    }


def evaluate_aqwi(
    solution: Solution,
    problem: ProblemInstance,
    weights: dict[str, float] | None = None,
    candidate_id: int = 0,
    hard_violations: int = 0,
    cohort_scale: float = DEFAULT_COHORT_SCALE,
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
            unit="excess span points",
            lower_is_better=True,
            description="Penalizes student days stretched across 10 hours (08:00 to 18:00).",
            breakdown={"violations": c1_details},
        ),
        "c2_student_idle_gaps": CriterionDetail(
            name="Student Gap Minimization",
            weight=w["c2_student_idle_gaps"],
            raw_metric=c2_raw,
            weighted_penalty=w["c2_student_idle_gaps"] * c2_raw,
            unit="idle gap hours",
            lower_is_better=True,
            description="Penalizes idle unallotted gap hours between student lectures.",
            breakdown={"violations": c2_details},
        ),
        "c3_same_day_lec_lab": CriterionDetail(
            name="Same-Day Lec & Lab Separation",
            weight=w["c3_same_day_lec_lab"],
            raw_metric=c3_raw,
            weighted_penalty=w["c3_same_day_lec_lab"] * c3_raw,
            unit="same-day overlaps",
            lower_is_better=True,
            description="Prevents single-subject overload by separating theory and lab onto different days.",
            breakdown={"violations": c3_details},
        ),
        "c4_faculty_load_variance": CriterionDetail(
            name="Faculty Workload Balance",
            weight=w["c4_faculty_load_variance"],
            raw_metric=c4_raw,
            weighted_penalty=w["c4_faculty_load_variance"] * c4_raw,
            unit="daily load variance (hours²)",
            lower_is_better=True,
            description="Evenly spreads teaching hours across a faculty member's active days.",
            breakdown={"violations": c4_details},
        ),
        "c5_honours_boundary": CriterionDetail(
            name="Honours Boundary Placement",
            weight=w["c5_honours_boundary"],
            raw_metric=c5_raw,
            weighted_penalty=w["c5_honours_boundary"] * c5_raw,
            unit="boundary violations",
            lower_is_better=True,
            description="Places Honours and Open Electives at start (08:00) or end of day to avoid midday gaps.",
            breakdown={"violations": c5_details},
        ),
        "c6_three_consecutive_days": CriterionDetail(
            name="3-Day Consecutive Subject Spread",
            weight=w["c6_three_consecutive_days"],
            raw_metric=c6_raw,
            weighted_penalty=w["c6_three_consecutive_days"] * c6_raw,
            unit="3+ day streaks",
            lower_is_better=True,
            description="Penalizes clustering the same subject on 3 or more consecutive weekdays.",
            breakdown={"violations": c6_details},
        ),
        "c7_faculty_gaps_over_2h": CriterionDetail(
            name="Faculty Long Gap Elimination",
            weight=w["c7_faculty_gaps_over_2h"],
            raw_metric=c7_raw,
            weighted_penalty=w["c7_faculty_gaps_over_2h"] * c7_raw,
            unit="excess waiting hours",
            lower_is_better=True,
            description="Prevents faculty from waiting idle on campus for more than 2 hours between classes.",
            breakdown={"violations": c7_details},
        ),
    }

    # Total unrounded penalty: P_total = sum(AQWI_WEIGHTS[key] * metrics[key]) = M1 + ... + M7
    total_penalty = sum(c.weighted_penalty for c in criteria.values())

    # Simplified cohort baseline: K = scale * (N_divisions + N_faculty)
    k_baseline = compute_cohort_baseline(problem, scale=cohort_scale)

    # Simplified AQWI score: round(max(0.0, min(100.0, 100.0 * exp(-P_total / K))), 1)
    quality_score = round(max(0.0, min(100.0, 100.0 * math.exp(-total_penalty / k_baseline))), 1)

    # Resource utilization breakdown
    res_util = compute_resource_utilization(solution, problem)

    return AQWIReport(
        total_penalty=total_penalty,
        quality_score=quality_score,
        cohort_baseline=k_baseline,
        rank=1,
        criteria=criteria,
        candidate_id=candidate_id,
        hard_violations=hard_violations,
        resource_utilization=res_util,
    )
