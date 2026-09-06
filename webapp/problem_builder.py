"""DB rows -> `ProblemInstance` snapshot (design.md §4.2, CLAUDE.md "Platform data flow").

`build_problem_dict` assembles exactly the JSON shape `timetable.io_json.problem_from_dict`
parses (see `data/reference/djsce_cse_ds_sy_sem4.json` for a concrete instance of that shape).
`readiness` wraps that with `problem.validate()` so routers can surface a plain-English readiness
banner without ever raising on a partially-populated DB.

Global entities (faculty, rooms, slot template) are shared across the whole institution and are
therefore always emitted in full; only `courses` and `divisions` are filtered by `branch_ids`
(CLAUDE.md §3 — "Solves always cover the whole institution... branch filtering is a view-only
concern"). This module only reads; it never writes (CLAUDE.md §15 — no files/rows created by code
outside `platform.db` / `uploads/`).
"""
from __future__ import annotations

from sqlmodel import Session, select

from engine.io_json import problem_from_dict
from engine.models import ProblemInstance
from webapp.models_db import Allocation, Branch, Course, Division, Faculty, Room, SlotTemplate


# Separator for branch-qualified engine ids (see `qualify` below). Chosen because no seeded
# division name, course code or branch code contains it, so `unqualify` can split unambiguously.
QUALIFIER = "::"


def qualify(branch_code: str, name: str) -> str:
    """Build the engine-facing id for a division/course inside `branch_code`.

    The engine keys divisions by `Division.id` and courses by `Course.code` in flat, global dicts
    (`ProblemInstance.division_by_id()` / `course_by_code()`), but those names are only unique
    WITHIN a branch -- every year at DJSCE reuses D1/D2/D3, and SY Sem III and SY Sem IV both offer
    a course coded `OE`. Solving several branches together therefore needs branch-qualified ids, or
    the two branches' D1s silently collapse into one division and one `OE` definition overwrites the
    other's weekly session counts.
    """
    return f"{branch_code}{QUALIFIER}{name}"


def unqualify(engine_id: str) -> str:
    """Recover the human-facing name from a possibly-qualified engine id ('X::D1' -> 'D1').

    Safe on unqualified ids ('D1' -> 'D1'), so the display layer can call it unconditionally
    without first knowing whether the run it is rendering was branch-qualified.
    """
    return engine_id.rsplit(QUALIFIER, 1)[-1]


def _branch_rows(session: Session, branch_ids: list[int] | None) -> list[Branch]:
    stmt = select(Branch)
    if branch_ids is not None:
        stmt = stmt.where(Branch.id.in_(branch_ids))
    return list(session.exec(stmt).all())


def spans_multiple_branches(session: Session, branch_ids: list[int] | None) -> bool:
    """Whether this selection covers more than one branch, and therefore needs qualified ids.

    Derived from the DB rather than from `len(branch_ids)` so that `branch_ids=None` (the
    whole-institution selection) is answered correctly, and so a single-branch solve keeps its
    plain, unqualified ids exactly as before this feature existed.
    """
    return len(_branch_rows(session, branch_ids)) > 1


def build_division_meta(session: Session, branch_ids: list[int] | None = None,
                        qualify_ids: bool = False) -> dict[str, dict]:
    """Map each emitted engine division id to the branch identity behind it.

    Stored alongside a run (`TimetableRun.division_meta`) because a solved grid otherwise carries
    only an opaque division id, leaving no way to label a session with its department/year/semester
    -- which is exactly what a cross-year teaching timetable has to show.
    """
    meta: dict[str, dict] = {}
    for branch in _branch_rows(session, branch_ids):
        divisions = session.exec(select(Division).where(Division.branch_id == branch.id)).all()
        for d in divisions:
            engine_id = qualify(branch.code, d.name) if qualify_ids else d.name
            meta[engine_id] = {
                "branch_id": branch.id,
                "branch_code": branch.code,
                "department": branch.department,
                "department_name": branch.department_name,
                "year_label": branch.year_label,
                "year_name": branch.year_name,
                "semester": branch.semester or d.semester,
                "division_name": d.name,
            }
    return meta


def _faculty_by_course_value(alloc: Allocation, faculty_code_by_id: dict[int, str]):
    """Resolve one allocation's faculty payload for `Division.faculty_by_course`.

    Returns a single faculty code, a `[batch1_code, batch2_code]` 2-list, or `None` when no
    faculty is resolvable (missing FK entirely, or one referenced faculty row no longer exists) —
    callers skip the course on `None` so `ProblemInstance.validate()` flags the gap instead of this
    module papering over it.
    """
    if alloc.faculty_id is not None:
        return faculty_code_by_id.get(alloc.faculty_id)
    if alloc.batch1_faculty_id is not None and alloc.batch2_faculty_id is not None:
        b1 = faculty_code_by_id.get(alloc.batch1_faculty_id)
        b2 = faculty_code_by_id.get(alloc.batch2_faculty_id)
        if b1 is not None and b2 is not None:
            return [b1, b2]
        return None
    return None


def build_problem_dict(session: Session, branch_ids: list[int] | None = None,
                       qualify_ids: bool = False) -> dict:
    """Assemble the `problem_from_dict`-shaped dict from the current DB state.

    `branch_ids=None` means "no filter" (used for the whole-institution solve); an explicit list
    filters `courses`/`divisions` to those branches while `faculty`/`rooms`/`time_slots` are always
    global (see module docstring).

    `qualify_ids=True` prefixes every emitted division id and course code with its branch code (see
    `qualify`), which is what makes a multi-branch solve possible at all. Left off for a
    single-branch solve so its ids stay plain and its stored runs keep the pre-existing shape.
    """
    slot_rows = sorted(session.exec(select(SlotTemplate)).all(), key=lambda s: (s.day, s.period))
    time_slots = [
        {"id": idx, "day": s.day, "period": s.period, "start": s.start, "end": s.end}
        for idx, s in enumerate(slot_rows)
    ]
    days_per_week = (max(s.day for s in slot_rows) + 1) if slot_rows else 5

    rooms = [
        {"id": r.code, "name": r.name, "capacity": r.capacity, "room_type": r.room_type}
        for r in session.exec(select(Room)).all()
    ]

    faculty_rows = session.exec(select(Faculty)).all()
    faculty = [
        {
            "id": f.code,
            "name": f.name,
            "max_load_hours_per_week": f.max_load_hours_per_week,
            "max_consecutive_sessions": f.max_consecutive_sessions,
            "unavailable_slots": list(f.unavailable_slot_ids),
            "preferred_slots": [],
        }
        for f in faculty_rows
    ]
    faculty_code_by_id = {f.id: f.code for f in faculty_rows}

    # Branch code per branch id, needed to qualify division/course ids. Read across ALL branches
    # (not just the selected ones) for the same reason `course_code_by_id` below is global: an
    # allocation's FK may point at a row outside the current filter, and resolving it to `None`
    # would silently drop the course instead of surfacing it.
    branch_code_by_id = {b.id: b.code for b in session.exec(select(Branch)).all()}

    def engine_course_code(course: Course) -> str:
        if not qualify_ids:
            return course.code
        return qualify(branch_code_by_id.get(course.branch_id, "?"), course.code)

    # Course/division lookups: course codes are resolved against ALL courses (an allocation's FK
    # is trustworthy regardless of which branch is being filtered into the emitted `courses` list),
    # but the emitted `courses` list itself is branch-filtered.
    all_course_rows = session.exec(select(Course)).all()
    course_code_by_id = {c.id: engine_course_code(c) for c in all_course_rows}

    course_stmt = select(Course)
    if branch_ids is not None:
        course_stmt = course_stmt.where(Course.branch_id.in_(branch_ids))
    courses = [
        {
            "code": engine_course_code(c),
            "title": c.title,
            "credits": c.credits,
            "category": c.category,
            "theory_sessions_per_week": c.theory_per_week,
            "practical_sessions_per_week": c.practical_per_week,
            "tutorial_sessions_per_week": c.tutorial_per_week,
            "is_heavy": c.is_heavy,
        }
        for c in session.exec(course_stmt).all()
    ]

    division_stmt = select(Division)
    if branch_ids is not None:
        division_stmt = division_stmt.where(Division.branch_id.in_(branch_ids))
    division_rows = session.exec(division_stmt).all()

    divisions = []
    for d in division_rows:
        alloc_rows = session.exec(
            select(Allocation).where(Allocation.division_id == d.id).order_by(Allocation.id)
        ).all()

        course_codes: list[str] = []
        faculty_by_course: dict[str, object] = {}
        for alloc in alloc_rows:
            code = course_code_by_id.get(alloc.course_id)
            if code is None:
                continue
            if code not in course_codes:
                course_codes.append(code)
            value = _faculty_by_course_value(alloc, faculty_code_by_id)
            if value is not None:
                faculty_by_course[code] = value

        division_id = qualify(branch_code_by_id.get(d.branch_id, "?"), d.name) if qualify_ids else d.name
        divisions.append({
            "id": division_id,
            "program": d.program,
            "semester": d.semester,
            "student_count": d.student_count,
            "course_codes": course_codes,
            "faculty_by_course": faculty_by_course,
            "batches": [d.batch1_name or f"{d.name}1", d.batch2_name or f"{d.name}2"],
        })

    # Non-teaching weekdays declared by the included branches (e.g. Final Year's Friday is
    # Final Project / Research Work). Unioned across branches: a day only needs exempting for
    # the branch that declares it, and a branch that teaches normally that day is unaffected by
    # the exemption (relaxing a day only removes constraints, never adds any).
    branch_stmt = select(Branch)
    if branch_ids is not None:
        branch_stmt = branch_stmt.where(Branch.id.in_(branch_ids))
    relaxed_days = sorted({d for b in session.exec(branch_stmt).all() for d in (b.relaxed_days or [])})

    return {
        "time_slots": time_slots,
        "rooms": rooms,
        "faculty": faculty,
        "courses": courses,
        "divisions": divisions,
        "days_per_week": days_per_week,
        "protected_notes": [],
        "special_sessions": [],
        "relaxed_days": relaxed_days,
    }


def readiness(
    session: Session, branch_ids: list[int] | None = None, qualify_ids: bool = False
) -> tuple[ProblemInstance | None, list[str]]:
    """Build a `ProblemInstance` and validate it, without ever raising on a sparse DB.

    Returns `(None, issues)` when the DB is too empty to even attempt a build (no divisions, or no
    time slots) — checked defensively up front rather than relying on downstream code to fail
    gracefully. Otherwise returns `(problem, problem.validate())`; an empty issue list means the
    instance is ready to solve.
    """
    issues: list[str] = []

    division_stmt = select(Division)
    if branch_ids is not None:
        division_stmt = division_stmt.where(Division.branch_id.in_(branch_ids))
    division_rows = session.exec(division_stmt).all()
    if len(division_rows) == 0:
        issues.append("no divisions defined")

    slot_count = len(session.exec(select(SlotTemplate)).all())
    if slot_count == 0:
        issues.append("no time slots defined")

    if issues:
        return None, issues

    # Guard against id collisions: with `qualify_ids=False`, `build_problem_dict` emits the bare
    # `Division.name`/`Course.code` as the engine id (unique only *within* a branch — see the
    # module docstring), so a multi-branch solve would silently merge two branches' divisions and
    # courses in the engine's id-keyed lookups (`division_by_id()`/`course_by_code()`) whenever
    # their names/codes collide. Detected here, against the same branch_ids-filtered rows that will
    # actually be emitted, and BEFORE `problem_from_dict` so a collision surfaces as a readiness
    # issue instead of corrupting the snapshot silently.
    #
    # `qualify_ids=True` prefixes every id with its branch code, which makes such a collision
    # impossible by construction — so the guard is skipped rather than reporting names that are no
    # longer ambiguous. This is what lets the all-years solve run at all: DJSCE reuses D1/D2/D3 in
    # every year, so an unqualified whole-institution solve is permanently blocked here.
    if not qualify_ids:
        division_name_counts: dict[str, int] = {}
        for d in division_rows:
            division_name_counts[d.name] = division_name_counts.get(d.name, 0) + 1
        for name in sorted(division_name_counts):
            if division_name_counts[name] > 1:
                issues.append(
                    f"division name '{name}' is used by more than one included branch — rename so "
                    "the whole-institution solve has unique division ids"
                )

        course_stmt = select(Course)
        if branch_ids is not None:
            course_stmt = course_stmt.where(Course.branch_id.in_(branch_ids))
        course_rows = session.exec(course_stmt).all()
        course_code_counts: dict[str, int] = {}
        for c in course_rows:
            course_code_counts[c.code] = course_code_counts.get(c.code, 0) + 1
        for code in sorted(course_code_counts):
            if course_code_counts[code] > 1:
                issues.append(
                    f"course code '{code}' is used by more than one included branch — rename so "
                    "the whole-institution solve has unique course ids"
                )

        if issues:
            return None, issues

    data = build_problem_dict(session, branch_ids=branch_ids, qualify_ids=qualify_ids)
    problem = problem_from_dict(data)
    return problem, problem.validate()
