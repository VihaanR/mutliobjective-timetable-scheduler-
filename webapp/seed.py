"""POST /api/seed/{dataset} -- import a reference JSON dataset (data/reference/{dataset}.json)
as an editable branch (design.md §5.1, satisfies R7).

Generalized beyond the original single-dataset bootstrap so any number of years/programs can
coexist as separate branches: faculty/rooms/slot-template are GLOBAL and shared across the whole
institution (CLAUDE.md §3 -- "solves always cover the whole institution"), so seeding a second
dataset UPSERTS into those shared tables by code (reusing a faculty/room/slot that already exists
rather than duplicating it) while Branch/Course/Division/Allocation are always created fresh,
scoped to that dataset's own branch. This is the same one-click onboarding as before, just no
longer limited to a single branch existing at a time.

Refuses to run when a branch with the same code already exists, unless `?force=true`, which wipes
only THAT branch's own courses/divisions/allocations first (never other branches' data, and never
the shared faculty/room/slot tables).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from webapp.auth import require_faculty_or_pre_bootstrap
from webapp.db import get_session
from webapp.models_db import (
    Allocation, Branch, Course, Division, Faculty, Room, SlotTemplate,
)

router = APIRouter(prefix="/api", tags=["seed"])

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"

# name -> (dataset filename stem, branch code). The original single-branch bootstrap stays at its
# original URL for backward compatibility; every other year/program gets its own dataset name.
KNOWN_DATASETS: dict[str, tuple[str, str]] = {
    # Jan-May 2026/2027 (even semester) -- DJSCE reference datasets
    "reference": ("djsce_cse_ds_sy_sem4", "CSE-DS-SY-SEM4"),
    "ty-sem6": ("djsce_ty_sem6_even_2027", "CSE-DS-TY-SEM6"),
    "btech-sem8": ("djsce_btech_sem8_even_2027", "CSE-DS-BTECH-SEM8"),
    # July-Dec 2026 (odd semester) -- 2nd/3rd/final year, transcribed from photographed sheets
    "sy-sem3": ("djsce_sy_sem3_jul_dec_2026", "CSE-DS-SY-SEM3"),
    "ty-sem5": ("djsce_ty_d1_sem5_jul_dec_2026", "CSE-DS-TY-SEM5"),
    "btech-sem7": ("djsce_btech_d2_sem7_jul_dec_2026", "CSE-DS-BTECH-SEM7"),
}


def _wipe_branch(session: Session, branch: Branch) -> None:
    """Remove one branch's own scoped rows (courses/divisions/allocations) -- never faculty/room/
    slot_template, which are shared globally across every branch (see module docstring)."""
    division_ids = [d.id for d in session.exec(select(Division).where(Division.branch_id == branch.id)).all()]
    if division_ids:
        for alloc in session.exec(select(Allocation).where(Allocation.division_id.in_(division_ids))).all():
            session.delete(alloc)
    for division in session.exec(select(Division).where(Division.branch_id == branch.id)).all():
        session.delete(division)
    for course in session.exec(select(Course).where(Course.branch_id == branch.id)).all():
        session.delete(course)
    session.delete(branch)
    session.flush()


def _upsert_faculty(session: Session, cache: dict[str, Faculty], code: str, name: str,
                     unavailable_slots: list[int]) -> Faculty:
    """Reuse an existing Faculty row by code (shared across branches) instead of duplicating it."""
    if code in cache:
        return cache[code]
    existing = session.exec(select(Faculty).where(Faculty.code == code)).first()
    if existing is not None:
        cache[code] = existing
        return existing
    fac = Faculty(code=code, name=name, unavailable_slot_ids=list(unavailable_slots))
    session.add(fac)
    session.flush()
    cache[code] = fac
    return fac


def _upsert_room(session: Session, cache: dict[str, Room], code: str, name: str, capacity: int,
                  room_type: str) -> Room:
    if code in cache:
        return cache[code]
    existing = session.exec(select(Room).where(Room.code == code)).first()
    if existing is not None:
        cache[code] = existing
        return existing
    room = Room(code=code, name=name, capacity=capacity, room_type=room_type)
    session.add(room)
    session.flush()
    cache[code] = room
    return room


def _ensure_slot_template(session: Session, cache: dict[tuple[int, int], SlotTemplate],
                           day: int, period: int, start: str, end: str) -> None:
    """Every dataset shares ONE institutional slot grid: only insert a (day, period) cell the
    first time any dataset defines it, so a second/third seed doesn't fragment the grid."""
    key = (day, period)
    if key in cache:
        return
    existing = session.exec(
        select(SlotTemplate).where(SlotTemplate.day == day, SlotTemplate.period == period)
    ).first()
    if existing is not None:
        cache[key] = existing
        return
    slot = SlotTemplate(day=day, period=period, start=start, end=end)
    session.add(slot)
    session.flush()
    cache[key] = slot


def _seed_dataset(session: Session, data: dict, branch_code: str, force: bool) -> Branch:
    existing = session.exec(select(Branch).where(Branch.code == branch_code)).first()
    if existing is not None:
        if not force:
            raise HTTPException(
                status_code=409,
                detail=f"branch {branch_code!r} already exists; pass ?force=true to wipe and re-seed it",
            )
        _wipe_branch(session, existing)

    branch = Branch(
        code=branch_code,
        name=data.get("institution", branch_code),
        semester_label=data.get("class_term", ""),
        relaxed_days=list(data.get("relaxed_days", [])),
        notice=data.get("branch_notice", ""),
        department=data.get("department", ""),
        department_name=data.get("department_name", ""),
        year_label=data.get("year_label", ""),
        year_name=data.get("year_name", ""),
        # fall back to the first division's semester -- every division of a branch shares it
        semester=data.get("semester") or next(
            (d.get("semester", 0) for d in data.get("divisions", [])), 0),
    )
    session.add(branch)
    session.flush()

    slot_cache: dict[tuple[int, int], SlotTemplate] = {}
    for t in data["time_slots"]:
        _ensure_slot_template(session, slot_cache, t["day"], t["period"], t["start"], t["end"])

    room_cache: dict[str, Room] = {}
    for r in data["rooms"]:
        _upsert_room(session, room_cache, r["id"], r["name"], r["capacity"], r["room_type"])

    faculty_cache: dict[str, Faculty] = {f.code: f for f in session.exec(select(Faculty)).all()}
    for f in data["faculty"]:
        _upsert_faculty(session, faculty_cache, f["id"], f["name"], f.get("unavailable_slots", []))

    course_by_code: dict[str, Course] = {}
    for c in data["courses"]:
        course = Course(
            branch_id=branch.id,
            code=c["code"],
            title=c["title"],
            credits=c["credits"],
            category=c["category"],
            theory_per_week=c.get("theory_sessions_per_week", 0),
            practical_per_week=c.get("practical_sessions_per_week", 0),
            tutorial_per_week=c.get("tutorial_sessions_per_week", 0),
            is_heavy=c.get("is_heavy", False),
        )
        session.add(course)
        course_by_code[c["code"]] = course
    session.flush()

    for d in data["divisions"]:
        batches = d.get("batches") or [f"{d['id']}1", f"{d['id']}2"]
        division = Division(
            branch_id=branch.id,
            name=d["id"],
            program=d.get("program", "FYUP"),
            semester=d["semester"],
            student_count=d["student_count"],
            batch1_name=batches[0],
            batch2_name=batches[1],
        )
        session.add(division)
        session.flush()

        for course_code, value in d["faculty_by_course"].items():
            course = course_by_code.get(course_code)
            if course is None:
                continue
            alloc = Allocation(division_id=division.id, course_id=course.id)
            if isinstance(value, list):
                for f_code in value:
                    if f_code and f_code not in faculty_cache:
                        _upsert_faculty(session, faculty_cache, f_code, f'Prof. {f_code}', [])
                if len(value) >= 3:
                    fac = faculty_cache.get(value[0])
                    b1 = faculty_cache.get(value[1])
                    b2 = faculty_cache.get(value[2])
                    alloc.faculty_id = fac.id if fac else None
                    alloc.batch1_faculty_id = b1.id if b1 else None
                    alloc.batch2_faculty_id = b2.id if b2 else None
                elif len(value) == 2:
                    b1 = faculty_cache.get(value[0])
                    b2 = faculty_cache.get(value[1])
                    alloc.batch1_faculty_id = b1.id if b1 else None
                    alloc.batch2_faculty_id = b2.id if b2 else None
                elif len(value) == 1:
                    fac = faculty_cache.get(value[0])
                    alloc.faculty_id = fac.id if fac else None
            else:
                if value and value not in faculty_cache:
                    _upsert_faculty(session, faculty_cache, value, f'Prof. {value}', [])
                fac = faculty_cache.get(value)
                alloc.faculty_id = fac.id if fac else None
            session.add(alloc)

    session.commit()
    session.refresh(branch)
    return branch


@router.post("/seed/reference")
def seed_reference(force: bool = False, session: Session = Depends(get_session),
                   _=Depends(require_faculty_or_pre_bootstrap)):
    """Backward-compatible URL for the original SY reference bootstrap."""
    return _seed_and_report(session, "reference", force)


@router.post("/seed/{dataset}")
def seed_named_dataset(dataset: str, force: bool = False, session: Session = Depends(get_session),
                       _=Depends(require_faculty_or_pre_bootstrap)):
    """Seed any known dataset (see KNOWN_DATASETS) as its own branch. Each year/program is just
    another reference JSON dropped into data/reference/ and registered here -- this is the
    extensible mechanism for 'accommodate all years', not a one-off hardcoded path."""
    if dataset not in KNOWN_DATASETS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown dataset {dataset!r}; known: {sorted(KNOWN_DATASETS)}",
        )
    return _seed_and_report(session, dataset, force)


def _seed_and_report(session: Session, dataset: str, force: bool) -> dict:
    filename, branch_code = KNOWN_DATASETS[dataset]
    path = REFERENCE_DIR / f"{filename}.json"
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"reference dataset missing at {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    branch = _seed_dataset(session, data, branch_code, force)

    return {
        "branch_id": branch.id,
        "branch_code": branch.code,
        "divisions": len(data["divisions"]),
        "faculty": len(data["faculty"]),
        "courses": len(data["courses"]),
        "rooms": len(data["rooms"]),
        "slots": len(data["time_slots"]),
        "forced": force,
    }


@router.get("/seed/datasets")
def list_datasets():
    """What's available to seed -- lets the dashboard build a picker instead of hardcoding names."""
    return [
        {"name": name, "file": f"{filename}.json", "branch_code": branch_code}
        for name, (filename, branch_code) in KNOWN_DATASETS.items()
    ]
