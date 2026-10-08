"""Unit tests for the 7-Point Academic Quality & Welfare Index (AQWI) Judge and Visiting Faculty priority."""
import pytest
from engine.judge import evaluate_aqwi, AQWIReport
from engine.models import (
    Assignment,
    Course,
    CourseCategory,
    Division,
    Faculty,
    ProblemInstance,
    ProgramType,
    Room,
    SessionRequirement,
    SessionType,
    Solution,
    TimeSlot,
    expand_requirements,
)
from engine.solvers.greedy import GreedySolver
from engine.solvers.candidates import build_candidates


def _make_test_problem(time_slots, courses=None, faculty=None, rooms=None):
    if courses is None:
        courses = [Course(code="C1", title="Math", credits=3, category=CourseCategory.MAJOR, theory_sessions_per_week=2)]
    if faculty is None:
        faculty = [Faculty(id="F1", name="Prof A")]
    if rooms is None:
        rooms = [Room(id="R1", name="R1", capacity=60, room_type="classroom")]
    course_codes = tuple(c.code for c in courses)
    fac_map = {c.code: faculty[0].id for c in courses}
    div = Division(
        id="D1",
        program=ProgramType.FYUP,
        semester=1,
        student_count=60,
        course_codes=course_codes,
        faculty_by_course=fac_map,
    )
    return ProblemInstance(
        time_slots=time_slots,
        rooms=rooms,
        faculty=faculty,
        courses=courses,
        divisions=[div],
    )


def test_aqwi_evaluation_on_greedy_solution(small_problem):
    solver = GreedySolver()
    solution = solver.solve(small_problem)
    assert solution.status in ("FEASIBLE", "OPTIMAL", "PARTIAL")

    report = evaluate_aqwi(solution, small_problem)
    assert isinstance(report, AQWIReport)
    assert 0.0 <= report.quality_score <= 100.0
    assert report.total_penalty >= 0.0

    # Ensure all 7 criteria are evaluated and reported
    expected_criteria = {
        "c1_avoid_8_6_span",
        "c2_student_idle_gaps",
        "c3_same_day_lec_lab",
        "c4_faculty_load_variance",
        "c5_honours_boundary",
        "c6_three_consecutive_days",
        "c7_faculty_gaps_over_2h",
    }
    assert set(report.criteria.keys()) == expected_criteria

    # Check to_dict serialization
    d = report.to_dict()
    assert "quality_score" in d
    assert "total_penalty" in d
    assert "criteria" in d
    assert len(d["criteria"]) == 7


def test_criterion_1_student_day_span_avoid_8_6():
    """Criterion 1: Days spanning > 8 slots (e.g. 10 hours from 8 to 6) must be penalized."""
    slots = [TimeSlot(id=i, day=0, period=i, start=f"{8+i:02d}:00", end=f"{9+i:02d}:00") for i in range(10)]
    c = Course(code="C1", title="Math", credits=3, category=CourseCategory.MAJOR, theory_sessions_per_week=2)
    problem = _make_test_problem(slots, courses=[c])
    reqs = [r for r in expand_requirements(problem) if not r.is_break]

    solution = Solution(
        assignments=[Assignment(reqs[0].id, 0, "R1"), Assignment(reqs[1].id, 9, "R1")],
        solver_name="test",
        wall_clock_seconds=0.0,
    )

    report = evaluate_aqwi(solution, problem)
    crit = report.criteria["c1_avoid_8_6_span"]
    assert crit.raw_metric > 0.0
    assert crit.weighted_penalty > 0.0


def test_criterion_2_student_free_gaps():
    """Criterion 2: Free waiting gaps between classes must be penalized."""
    slots = [TimeSlot(id=i, day=0, period=i, start=f"{8+i:02d}:00", end=f"{9+i:02d}:00") for i in range(5)]
    c = Course(code="C1", title="Math", credits=3, category=CourseCategory.MAJOR, theory_sessions_per_week=2)
    problem = _make_test_problem(slots, courses=[c])
    reqs = [r for r in expand_requirements(problem) if not r.is_break]

    # Assignments at period 0 and period 3 -> gap at period 1 and 2
    solution = Solution(
        assignments=[Assignment(reqs[0].id, 0, "R1"), Assignment(reqs[1].id, 3, "R1")],
        solver_name="test",
        wall_clock_seconds=0.0,
    )

    report = evaluate_aqwi(solution, problem)
    crit = report.criteria["c2_student_idle_gaps"]
    assert crit.raw_metric == 2.0  # 2 unoccupied slots
    assert crit.weighted_penalty == 2.0 * crit.weight


def test_criterion_3_same_day_lab_lecture():
    """Criterion 3: Lecture and Lab of the same subject on the same day must be penalized."""
    slots = [TimeSlot(id=i, day=0, period=i, start=f"{8+i:02d}:00", end=f"{9+i:02d}:00") for i in range(4)]
    c = Course(
        code="C1", title="Physics", credits=4, category=CourseCategory.MAJOR,
        theory_sessions_per_week=1, practical_sessions_per_week=1,
    )
    rooms = [
        Room(id="R1", name="R1", capacity=60, room_type="classroom"),
        Room(id="L1", name="L1", capacity=30, room_type="lab"),
        Room(id="L2", name="L2", capacity=30, room_type="lab"),
    ]
    problem = _make_test_problem(slots, courses=[c], rooms=rooms)
    reqs = [r for r in expand_requirements(problem) if not r.is_break]

    # Place theory and lab on the same day (Day 0)
    assignments = []
    for r in reqs:
        if r.session_type == SessionType.THEORY:
            assignments.append(Assignment(r.id, 0, "R1"))
        elif r.session_type == SessionType.PRACTICAL:
            assignments.append(Assignment(r.id, 1, "L1" if r.batch_id and "1" in r.batch_id else "L2"))

    solution = Solution(assignments=assignments, solver_name="test", wall_clock_seconds=0.0)

    report = evaluate_aqwi(solution, problem)
    crit = report.criteria["c3_same_day_lec_lab"]
    assert crit.raw_metric == 1.0
    assert crit.weighted_penalty == 1.0 * crit.weight


def test_criterion_5_honours_boundary_placement():
    """Criterion 5: Honours/Electives in middle periods must be penalized."""
    slots = [TimeSlot(id=i, day=0, period=i, start=f"{8+i:02d}:00", end=f"{9+i:02d}:00") for i in range(6)]
    c_hon = Course(code="HON101", title="AI Honours", credits=3, category=CourseCategory.OPEN_ELECTIVE, theory_sessions_per_week=1)
    problem = _make_test_problem(slots, courses=[c_hon])
    reqs = [r for r in expand_requirements(problem) if not r.is_break]

    # Placed at period 2 (middle of day)
    solution = Solution(
        assignments=[Assignment(reqs[0].id, 2, "R1")],
        solver_name="test",
        wall_clock_seconds=0.0,
    )

    report = evaluate_aqwi(solution, problem)
    crit = report.criteria["c5_honours_boundary"]
    assert crit.raw_metric == 1.0
    assert crit.weighted_penalty == 1.0 * crit.weight


def test_criterion_6_subject_3_consecutive_days():
    """Criterion 6: Same subject on 3+ consecutive days must be penalized."""
    slots = [TimeSlot(id=d, day=d, period=0, start="08:00", end="09:00") for d in range(5)]
    c = Course(code="C1", title="Math", credits=3, category=CourseCategory.MAJOR, theory_sessions_per_week=3)
    problem = _make_test_problem(slots, courses=[c])
    reqs = [r for r in expand_requirements(problem) if not r.is_break]

    # Scheduled on Day 0, Day 1, Day 2 (3 consecutive days)
    solution = Solution(
        assignments=[Assignment(reqs[0].id, 0, "R1"), Assignment(reqs[1].id, 1, "R1"), Assignment(reqs[2].id, 2, "R1")],
        solver_name="test",
        wall_clock_seconds=0.0,
    )

    report = evaluate_aqwi(solution, problem)
    crit = report.criteria["c6_three_consecutive_days"]
    assert crit.raw_metric == 1.0
    assert crit.weighted_penalty == 1.0 * crit.weight


def test_criterion_7_faculty_idle_gap_gt_2h():
    """Criterion 7: Faculty waiting gap > 2 hours must be penalized."""
    slots = [TimeSlot(id=i, day=0, period=i, start=f"{8+i:02d}:00", end=f"{9+i:02d}:00") for i in range(6)]
    c = Course(code="C1", title="Math", credits=3, category=CourseCategory.MAJOR, theory_sessions_per_week=2)
    problem = _make_test_problem(slots, courses=[c])
    reqs = [r for r in expand_requirements(problem) if not r.is_break]

    # Faculty teaches at period 0, then next class at period 4 -> gap = 3 hours (> 2 hours)
    solution = Solution(
        assignments=[Assignment(reqs[0].id, 0, "R1"), Assignment(reqs[1].id, 4, "R1")],
        solver_name="test",
        wall_clock_seconds=0.0,
    )

    report = evaluate_aqwi(solution, problem)
    crit = report.criteria["c7_faculty_gaps_over_2h"]
    assert crit.raw_metric == 1.0  # 1 long waiting period
    assert crit.weighted_penalty == 1.0 * crit.weight


def test_visiting_faculty_candidates_and_greedy_priority():
    """Visiting faculty should have candidates filtered to their days and window, and be placed first."""
    slots = []
    sid = 0
    for d in range(5):
        for p in range(8):
            start_h = 8 + p
            end_h = start_h + 1
            slots.append(TimeSlot(
                id=sid,
                day=d,
                period=p,
                start=f"{start_h:02d}:00",
                end=f"{end_h:02d}:00",
            ))
            sid += 1

    # Visiting faculty: available only on Tuesday (day 1) and Thursday (day 3), between 10:00 and 13:00 (periods 2, 3, 4)
    visiting_fac = Faculty(
        id="VF1",
        name="Guest Industry Expert",
        is_visiting=True,
        visiting_days=(1, 3),
        visiting_start_time="10:00",
        visiting_end_time="13:00",
    )
    reg_fac = Faculty(id="F1", name="Regular Faculty")
    c_visiting = Course(code="C_V", title="Special Topics", credits=2, category=CourseCategory.MAJOR, theory_sessions_per_week=1)
    c_reg = Course(code="C_R", title="Regular Subject", credits=2, category=CourseCategory.MAJOR, theory_sessions_per_week=1)

    div = Division(
        id="D1",
        program=ProgramType.FYUP,
        semester=1,
        student_count=60,
        course_codes=("C_V", "C_R"),
        faculty_by_course={"C_V": "VF1", "C_R": "F1"},
    )
    problem = ProblemInstance(
        time_slots=slots,
        rooms=[Room(id="R1", name="R1", capacity=60, room_type="classroom")],
        faculty=[visiting_fac, reg_fac],
        courses=[c_visiting, c_reg],
        divisions=[div],
    )

    reqs = expand_requirements(problem)
    v_req = next(r for r in reqs if r.course_code == "C_V")
    assert v_req.is_visiting_faculty is True

    # 1. Candidate filtering verification
    candidates = build_candidates(problem, reqs)
    v_cands = candidates[v_req.id]
    assert len(v_cands) > 0

    slot_by_id = {s.id: s for s in slots}
    for (start_slot_id, occ_ids, day, room_id) in v_cands:
        assert day in [1, 3], f"Visiting faculty candidate day {day} not in visiting_days [1, 3]"
        ts = slot_by_id[start_slot_id]
        assert "10:00" <= ts.start and ts.end <= "13:00", f"Slot {ts} outside 10:00-13:00"

    # 2. Greedy solve places visiting faculty cleanly
    solver = GreedySolver()
    sol = solver.solve(problem)
    assert sol.status in ("FEASIBLE", "OPTIMAL")
    v_assignment = next(a for a in sol.assignments if a.session_id == v_req.id)
    v_ts = slot_by_id[v_assignment.time_slot_id]
    assert v_ts.day in [1, 3]
    assert "10:00" <= v_ts.start and v_ts.end <= "13:00"
