"""JSON (de)serialization for ProblemInstance and Solution.

The same `problem_from_dict` parses both the transcribed real-world reference dataset
(`data/reference/*.json`) and any synthetic instance saved via `problem_to_dict`, since the JSON
shape mirrors the dataclass fields directly.
"""
from __future__ import annotations

import json
from pathlib import Path

from engine.models import (
    Assignment, Course, CourseCategory, Division, Faculty, ProblemInstance, ProgramType, Room,
    Solution, SpecialSession, TimeSlot,
)


def _faculty_value_from_json(value):
    if isinstance(value, list):
        return tuple(value)
    return value


def problem_from_dict(data: dict) -> ProblemInstance:
    time_slots = [TimeSlot(**t) for t in data["time_slots"]]
    rooms = [Room(**r) for r in data["rooms"]]
    faculty = [
        Faculty(
            id=f["id"],
            name=f["name"],
            max_load_hours_per_week=f.get("max_load_hours_per_week", 20),
            max_consecutive_sessions=f.get("max_consecutive_sessions", 2),
            unavailable_slots=frozenset(f.get("unavailable_slots", [])),
            preferred_slots=frozenset(f.get("preferred_slots", [])),
            is_visiting=bool(f.get("is_visiting", False)),
            visiting_days=tuple(f.get("visiting_days", ())),
            visiting_start_time=f.get("visiting_start_time", ""),
            visiting_end_time=f.get("visiting_end_time", ""),
        )
        for f in data["faculty"]
    ]
    courses = [Course(**{**c, "category": CourseCategory(c["category"])}) for c in data["courses"]]
    divisions = [
        Division(
            id=d["id"],
            program=ProgramType(d.get("program", "FYUP")),
            semester=d["semester"],
            student_count=d["student_count"],
            course_codes=tuple(d["course_codes"]),
            faculty_by_course={k: _faculty_value_from_json(v) for k, v in d["faculty_by_course"].items()},
            batches=tuple(d["batches"]) if d.get("batches") else None,
        )
        for d in data["divisions"]
    ]
    special_sessions = [SpecialSession(**{**s, "fixed_time_slot_ids": tuple(s["fixed_time_slot_ids"])})
                         for s in data.get("special_sessions", [])]

    return ProblemInstance(
        time_slots=time_slots, rooms=rooms, faculty=faculty, courses=courses, divisions=divisions,
        special_sessions=special_sessions, days_per_week=data.get("days_per_week", 5),
        protected_notes=data.get("protected_notes", []),
        # disruption support — optional, defaulted so pre-existing JSON (no such keys) still parses
        blocked_slot_ids=frozenset(data.get("blocked_slot_ids", [])),
        relaxed_days=frozenset(data.get("relaxed_days", [])),
        pinned_slots={str(k): int(v) for k, v in data.get("pinned_slots", {}).items()},
    )


def _faculty_value_to_json(value):
    # tuple (batch1, batch2) -> list; single faculty id -> unchanged. Inverse of
    # _faculty_value_from_json so a problem round-trips through problem_to_dict/problem_from_dict.
    if isinstance(value, tuple):
        return list(value)
    return value


def problem_to_dict(problem: ProblemInstance) -> dict:
    """Serialize a ProblemInstance to the exact JSON shape `problem_from_dict` parses.

    This is the inverse referenced in this module's docstring; it is the keystone for storing a
    per-run `problem_snapshot` (design.md §4.2) so a generate/adjust run is reproducible. The two
    disruption frozensets and `pinned_slots` are emitted only when non-empty, so a plain
    (undisrupted) instance produces the same JSON it was loaded from.
    """
    data: dict = {
        "time_slots": [
            {"id": t.id, "day": t.day, "period": t.period, "start": t.start, "end": t.end}
            for t in problem.time_slots
        ],
        "rooms": [
            {"id": r.id, "name": r.name, "capacity": r.capacity, "room_type": r.room_type}
            for r in problem.rooms
        ],
        "faculty": [
            {
                "id": f.id,
                "name": f.name,
                "max_load_hours_per_week": f.max_load_hours_per_week,
                "max_consecutive_sessions": f.max_consecutive_sessions,
                "unavailable_slots": sorted(f.unavailable_slots),
                "preferred_slots": sorted(f.preferred_slots),
                "is_visiting": f.is_visiting,
                "visiting_days": list(f.visiting_days),
                "visiting_start_time": f.visiting_start_time,
                "visiting_end_time": f.visiting_end_time,
            }
            for f in problem.faculty
        ],
        "courses": [
            {
                "code": c.code,
                "title": c.title,
                "credits": c.credits,
                "category": c.category.value,
                "theory_sessions_per_week": c.theory_sessions_per_week,
                "practical_sessions_per_week": c.practical_sessions_per_week,
                "tutorial_sessions_per_week": c.tutorial_sessions_per_week,
                "is_heavy": c.is_heavy,
            }
            for c in problem.courses
        ],
        "divisions": [
            {
                "id": d.id,
                "program": d.program.value,
                "semester": d.semester,
                "student_count": d.student_count,
                "course_codes": list(d.course_codes),
                "faculty_by_course": {
                    k: _faculty_value_to_json(v) for k, v in d.faculty_by_course.items()
                },
                "batches": list(d.batches) if d.batches else None,
            }
            for d in problem.divisions
        ],
        "special_sessions": [
            {
                "id": s.id,
                "course_code": s.course_code,
                "division_id": s.division_id,
                "duration_slots": s.duration_slots,
                "fixed_time_slot_ids": list(s.fixed_time_slot_ids),
                "requires_room": s.requires_room,
            }
            for s in problem.special_sessions
        ],
        "days_per_week": problem.days_per_week,
        "protected_notes": list(problem.protected_notes),
    }
    if problem.blocked_slot_ids:
        data["blocked_slot_ids"] = sorted(problem.blocked_slot_ids)
    if problem.relaxed_days:
        data["relaxed_days"] = sorted(problem.relaxed_days)
    if problem.pinned_slots:
        data["pinned_slots"] = dict(problem.pinned_slots)
    return data


def save_problem(problem: ProblemInstance, path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(problem_to_dict(problem), f, indent=2)


def load_problem(path: str | Path) -> ProblemInstance:
    with open(path, encoding="utf-8") as f:
        return problem_from_dict(json.load(f))


def solution_to_dict(solution: Solution) -> dict:
    d = {
        "solver_name": solution.solver_name,
        "wall_clock_seconds": solution.wall_clock_seconds,
        "objective_value": solution.objective_value,
        "status": solution.status,
        "assignments": [
            {"session_id": a.session_id, "time_slot_id": a.time_slot_id, "room_id": a.room_id}
            for a in solution.assignments
        ],
    }
    if solution.extra_data is not None:
        d["extra_data"] = solution.extra_data
    return d


def solution_from_dict(data: dict) -> Solution:
    return Solution(
        assignments=[Assignment(**a) for a in data["assignments"]],
        solver_name=data["solver_name"],
        wall_clock_seconds=data["wall_clock_seconds"],
        objective_value=data.get("objective_value"),
        status=data.get("status", "UNKNOWN"),
        extra_data=data.get("extra_data"),
    )


def save_solution(solution: Solution, path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(solution_to_dict(solution), f, indent=2)


def load_solution(path: str | Path) -> Solution:
    with open(path, encoding="utf-8") as f:
        return solution_from_dict(json.load(f))
