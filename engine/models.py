"""Shared data model for the timetable generator.

Every solver (greedy, MIP, GA, CP-SAT) reads a `ProblemInstance`, expands it into a fixed list of
`SessionRequirement`s via `expand_requirements()`, and produces a `Solution` (a list of
`Assignment`s mapping each requirement to a time slot + room). This is the one contract that
makes the three solvers' outputs directly comparable.

Faculty-to-course assignment is treated as fixed input data (`Division.faculty_by_course`), not a
solver decision: this matches the real DJSCE CSE-DS timetables, where each division's subject
already has one named instructor. Solvers only decide *when* (time slot) and *where* (room) each
session happens.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum


class ProgramType(str, Enum):
    FYUP = "FYUP"
    BED = "BEd"
    MED = "MEd"
    ITEP = "ITEP"


class CourseCategory(str, Enum):
    MAJOR = "Major"
    MINOR = "Minor"
    SKILL = "Skill-Based"
    AEC = "Ability-Enhancement"
    VAC = "Value-Added"
    OPEN_ELECTIVE = "Open-Elective"
    TEACHING_PRACTICE = "Teaching-Practice"
    FIELDWORK = "Fieldwork"
    INTERNSHIP = "Internship"
    PROJECT = "Project"


class SessionType(str, Enum):
    THEORY = "Theory"
    PRACTICAL = "Practical"
    TUTORIAL = "Tutorial"
    BREAK = "Break"


@dataclass(frozen=True, slots=True)
class TimeSlot:
    id: int
    day: int          # 0=Mon .. 4=Fri (Saturday is a protected institutional block, not scheduled)
    period: int       # 0-based period index within the day
    start: str
    end: str


@dataclass(frozen=True, slots=True)
class Room:
    id: str
    name: str
    capacity: int
    room_type: str    # "classroom" | "lab"


@dataclass(frozen=True, slots=True)
class Faculty:
    id: str
    name: str
    max_load_hours_per_week: int = 20
    max_consecutive_sessions: int = 2
    unavailable_slots: frozenset[int] = field(default_factory=frozenset)
    preferred_slots: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Course:
    code: str
    title: str
    credits: int
    category: CourseCategory
    theory_sessions_per_week: int = 0
    practical_sessions_per_week: int = 0   # each is a 2-consecutive-slot lab block, batch-split
    tutorial_sessions_per_week: int = 0
    is_heavy: bool = False                 # feeds the "avoid >2 heavy subjects consecutively" soft constraint

    @property
    def requires_room_type_for(self) -> dict[SessionType, str]:
        return {
            SessionType.THEORY: "classroom",
            SessionType.TUTORIAL: "classroom",
            SessionType.PRACTICAL: "lab",
        }


@dataclass(frozen=True, slots=True)
class Division:
    id: str                                # e.g. "D1"
    program: ProgramType
    semester: int
    student_count: int
    course_codes: tuple[str, ...]
    faculty_by_course: dict[str, str | tuple[str, str]]   # course_code -> faculty_id, or (batch1_id, batch2_id)
                                                            # for practicals with two parallel-section instructors
    batches: tuple[str, str] | None = None  # e.g. ("D11", "D12"), used only for lab sessions

    def batch_pair(self) -> tuple[str, str]:
        return self.batches or (f"{self.id}1", f"{self.id}2")

    def faculty_for(self, course_code: str, batch_id: str | None = None) -> str | None:
        value = self.faculty_by_course.get(course_code)
        if value is None:
            return None
        if isinstance(value, tuple):
            if batch_id is None:
                return value[0]
            batches = self.batch_pair()
            return value[batches.index(batch_id)] if batch_id in batches else value[0]
        return value


@dataclass(frozen=True, slots=True)
class SpecialSession:
    """Protected/fixed blocks (teaching practice, fieldwork, internship) that solvers never move."""
    id: str
    course_code: str
    division_id: str
    duration_slots: int
    fixed_time_slot_ids: tuple[int, ...]
    requires_room: bool = False


@dataclass(frozen=True, slots=True)
class SessionRequirement:
    """One schedulable unit. `expand_requirements()` produces the fixed, ordered list every
    solver assigns a (time_slot, room) to."""
    id: str
    division_id: str
    course_code: str
    faculty_id: str | None
    session_type: SessionType
    duration_slots: int = 1
    batch_id: str | None = None            # which batch this belongs to, if lab-split
    batch_group_id: str | None = None      # sessions sharing this id must get same slot, different room
    sync_group_id: str | None = None       # sessions sharing this id must get the same slot (e.g. cross-division OE)
    is_break: bool = False                 # if True, must land in the mid-day band (not first two / last)
    is_absent: bool = False                # if True, session is absent/cancelled
    fixed_day: int | None = None           # if set, session must be scheduled on exactly this day
                                            # (used to pin each division's daily break to its own day)
    fixed_time_slot_id: int | None = None  # if set, solver must use exactly this slot (protected blocks)
    room_type: str = "classroom"


@dataclass(frozen=True, slots=True)
class Assignment:
    session_id: str
    time_slot_id: int
    room_id: str


@dataclass(slots=True)
class Solution:
    assignments: list[Assignment]
    solver_name: str
    wall_clock_seconds: float
    objective_value: float | None = None
    status: str = "UNKNOWN"    # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE" | "TIMEOUT"

    def assignment_by_session(self) -> dict[str, Assignment]:
        return {a.session_id: a for a in self.assignments}


@dataclass(slots=True)
class ProblemInstance:
    time_slots: list[TimeSlot]
    rooms: list[Room]
    faculty: list[Faculty]
    courses: list[Course]
    divisions: list[Division]
    special_sessions: list[SpecialSession] = field(default_factory=list)
    days_per_week: int = 5
    protected_notes: list[str] = field(default_factory=list)   # e.g. "Saturday reserved for IPD/Project work"
    # --- disruption support (P4). Both default empty => identical to pre-disruption behavior, so
    #     every existing test and caller is unaffected (additive-only convention). ---
    blocked_slot_ids: frozenset[int] = field(default_factory=frozenset)   # slots nothing may occupy
                                                                          # (e.g. rain/holiday window)
    blocked_room_slots: frozenset[tuple[str, int]] = field(default_factory=frozenset) # (room_id, slot_id) pairs blocked
    relaxed_days: frozenset[int] = field(default_factory=frozenset)       # days exempt from the
                                                                          # day-shaped hard rules
                                                                          # (daily load 6-8, labs-
                                                                          # every-day, one-break-per-
                                                                          # day) so a disrupted-day
                                                                          # re-solve isn't scored
                                                                          # infeasible
    pinned_slots: dict[str, int] = field(default_factory=dict)           # session_id -> time_slot_id
                                                                          # to hold fixed (disruption
                                                                          # re-solve pins every
                                                                          # unaffected session to its
                                                                          # baseline slot). Applied in
                                                                          # expand_requirements via the
                                                                          # existing fixed_time_slot_id
                                                                          # mechanism -> no solver change

    def course_by_code(self) -> dict[str, Course]:
        return {c.code: c for c in self.courses}

    def faculty_by_id(self) -> dict[str, Faculty]:
        return {f.id: f for f in self.faculty}

    def room_by_id(self) -> dict[str, Room]:
        return {r.id: r for r in self.rooms}

    def division_by_id(self) -> dict[str, Division]:
        return {d.id: d for d in self.divisions}

    def slots_for_day(self, day: int) -> list[TimeSlot]:
        return sorted((t for t in self.time_slots if t.day == day), key=lambda t: t.period)

    def validate(self) -> list[str]:
        problems: list[str] = []
        courses = self.course_by_code()
        faculty = self.faculty_by_id()
        rooms = self.rooms

        for division in self.divisions:
            for code in division.course_codes:
                if code not in courses:
                    problems.append(f"Division {division.id} references unknown course {code}")
                if code not in division.faculty_by_course:
                    problems.append(f"Division {division.id} has no faculty assigned for course {code}")
                else:
                    value = division.faculty_by_course[code]
                    fids = value if isinstance(value, tuple) else (value,)
                    for fid in fids:
                        if fid not in faculty:
                            problems.append(f"Division {division.id} course {code} references unknown faculty {fid}")

            for code in division.course_codes:
                course = courses.get(code)
                if course and course.practical_sessions_per_week > 0 and division.batches is None:
                    if not division.batch_pair():
                        problems.append(f"Division {division.id} has practical course {code} but no batches")

        classroom_count = sum(1 for r in rooms if r.room_type == "classroom")
        lab_count = sum(1 for r in rooms if r.room_type == "lab")
        if classroom_count == 0:
            problems.append("No classrooms defined")
        if lab_count == 0 and any(c.practical_sessions_per_week > 0 for c in self.courses):
            problems.append("Practical sessions required but no labs defined")

        for division in self.divisions:
            categories = {courses[c].category for c in division.course_codes if c in courses}
            nep_categories = {
                CourseCategory.MAJOR, CourseCategory.MINOR, CourseCategory.SKILL,
                CourseCategory.AEC, CourseCategory.VAC,
            }
            if not (categories & nep_categories):
                problems.append(f"Division {division.id} has no NEP-category (Major/Minor/Skill/AEC/VAC) course")

        return problems


def expand_requirements(problem: ProblemInstance) -> list[SessionRequirement]:
    """Expand a ProblemInstance into the fixed, ordered list of SessionRequirements every solver
    assigns a (time_slot, room) to. Order is deterministic (division, then course, then session
    index) so GA gene positions are stable across runs."""
    courses = problem.course_by_code()
    requirements: list[SessionRequirement] = []

    for division in sorted(problem.divisions, key=lambda d: d.id):
        for day in range(problem.days_per_week):
            requirements.append(SessionRequirement(
                id=f"BREAK_{division.id}_D{day}",
                division_id=division.id,
                course_code="BREAK",
                faculty_id=None,
                session_type=SessionType.BREAK,
                duration_slots=1,
                is_break=True,
                fixed_day=day,          # pin this break to its own day: exactly one break per day
                room_type="none",
            ))

        for code in division.course_codes:
            course = courses.get(code)
            if course is None:
                continue
            faculty_id = division.faculty_for(code)
            is_open_elective = course.category == CourseCategory.OPEN_ELECTIVE

            # each weekly occurrence (i) gets its own sync group so only the SAME occurrence across
            # divisions is forced to a shared slot -- not every OE occurrence at once. Computed
            # per-occurrence here (not inside the theory loop) so a course with tutorials but no
            # theory can't read a stale sync_group_id from a previous course.
            def _sync(i: int) -> str | None:
                return f"OE_{code}_{i}" if is_open_elective else None

            for i in range(course.theory_sessions_per_week):
                requirements.append(SessionRequirement(
                    id=f"{division.id}_{code}_TH_{i}",
                    division_id=division.id,
                    course_code=code,
                    faculty_id=faculty_id,
                    session_type=SessionType.THEORY,
                    duration_slots=1,
                    sync_group_id=_sync(i),
                    room_type="classroom",
                ))

            for i in range(course.tutorial_sessions_per_week):
                requirements.append(SessionRequirement(
                    id=f"{division.id}_{code}_TUT_{i}",
                    division_id=division.id,
                    course_code=code,
                    faculty_id=faculty_id,
                    session_type=SessionType.TUTORIAL,
                    duration_slots=1,
                    sync_group_id=_sync(i),
                    room_type="classroom",
                ))

            for i in range(course.practical_sessions_per_week):
                batch_group_id = f"{division.id}_{code}_PR_{i}"
                for batch in division.batch_pair():
                    requirements.append(SessionRequirement(
                        id=f"{division.id}_{code}_PR_{i}_{batch}",
                        division_id=division.id,
                        course_code=code,
                        faculty_id=division.faculty_for(code, batch),
                        session_type=SessionType.PRACTICAL,
                        duration_slots=2,
                        batch_id=batch,
                        batch_group_id=batch_group_id,
                        room_type="lab",
                    ))

    for special in problem.special_sessions:
        requirements.append(SessionRequirement(
            id=special.id,
            division_id=special.division_id,
            course_code=special.course_code,
            faculty_id=None,
            session_type=SessionType.THEORY,
            duration_slots=special.duration_slots,
            fixed_time_slot_id=special.fixed_time_slot_ids[0] if special.fixed_time_slot_ids else None,
            room_type="classroom" if special.requires_room else "none",
        ))

    # apply disruption pins: any session named in problem.pinned_slots is fixed to that slot,
    # reusing the protected-block mechanism the solvers already honor (candidates.py filters to
    # fixed_time_slot_id; scoring.py flags protected_block_moved). No solver change needed.
    if problem.pinned_slots:
        for idx, req in enumerate(requirements):
            pin = problem.pinned_slots.get(req.id)
            if pin is not None and req.fixed_time_slot_id is None:
                requirements[idx] = replace(req, fixed_time_slot_id=pin)

    return requirements
