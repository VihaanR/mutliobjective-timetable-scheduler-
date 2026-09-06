"""Faculty CRUD (global — shared across branches). design.md §5.1.

Every read route uses `response_model=FacultyPublic` so `password_hash` never serializes (Auth,
design.md §11). Routes use `require_faculty_or_pre_bootstrap` rather than plain `require_faculty`:
before the very first `POST /api/auth/bootstrap`, an admin needs to be able to create/browse
Faculty rows (to have a code worth bootstrapping against) with no one yet able to log in - see
`webapp/auth.py`'s docstring on that guard. Once any faculty has credentials, it behaves exactly
like `require_faculty`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from webapp.auth import hash_password, require_faculty, require_faculty_or_pre_bootstrap
from webapp.db import get_session
from webapp.models_db import (
    Allocation, Faculty, FacultyCreate, FacultyPublic, FacultyUpdate, TimetableRun,
)
from webapp.routers._crud import get_or_404, unique_or_400

router = APIRouter(prefix="/api", tags=["faculty"])


@router.get("/faculty/me/timetable")
def my_timetable(session: Session = Depends(get_session), principal: dict = Depends(require_faculty)):
    """Read-only: the caller's own personal timetable - every session they teach, merged across
    every division AND every branch (year/semester) they're allocated to (unlike the student
    version, which is scoped to exactly one division). Regroups the already-stored `run.grids` by
    `entry["faculty"] == Faculty.code` rather than re-solving - `run.grids` is precomputed once at
    generation time (webapp/jobs.py) and each entry already carries the engine faculty code, its
    `division_id`, and (as of the multi-year feature) branch identity fields stamped by
    `webapp.grid_meta.annotate_grids` - `class_label`, `year_label`, `semester`, etc.

    A teacher who teaches e.g. SY and TY only sees both years here if a single run's `grids`
    actually contains both — which is exactly what the "Generate All Years Timetable" button on
    the platform page produces (`POST /api/runs` with `branch_ids: null`). A run scoped to one
    branch can only ever show that one branch's sessions, however recent it is; this endpoint
    always reads the single latest "done" run, so generating a fresh single-branch run after an
    all-years one will narrow this view back down to that one branch until an all-years run is
    generated again.
    """
    faculty = get_or_404(session, Faculty, principal["id"])

    run = session.exec(
        select(TimetableRun)
        .where(TimetableRun.status == "done")
        .order_by(TimetableRun.created_at.desc())
    ).first()
    if run is None or not run.grids:
        raise HTTPException(status_code=404, detail="no generated timetable available yet")

    cells: dict[str, list] = {}
    for division in run.grids["divisions"]:
        for key, entries in division["cells"].items():
            mine = [e for e in entries if e.get("faculty") == faculty.code]
            if mine:
                cells.setdefault(key, []).extend(mine)

    classes = sorted({
        e.get("class_label") for entries in cells.values() for e in entries
        if e.get("class_label")
    })

    return {
        "run_id": run.id,
        "faculty_name": faculty.name,
        "days": run.grids["days"],
        "periods": run.grids["periods"],
        "cells": cells,
        "classes": classes,
        "spans_multiple_branches": len({e.get("branch_id") for v in cells.values() for e in v}) > 1,
    }


@router.get("/faculty", response_model=list[FacultyPublic])
def list_faculty(session: Session = Depends(get_session), _=Depends(require_faculty_or_pre_bootstrap)):
    return session.exec(select(Faculty)).all()


@router.post("/faculty", status_code=201, response_model=FacultyPublic)
def create_faculty(body: FacultyCreate, session: Session = Depends(get_session),
                   _=Depends(require_faculty_or_pre_bootstrap)):
    unique_or_400(session, Faculty, "code", body.code)
    data = body.model_dump(exclude={"password"})
    faculty = Faculty(**data)
    if body.password:
        faculty.password_hash = hash_password(body.password)
    session.add(faculty)
    session.commit()
    session.refresh(faculty)
    return faculty


@router.get("/faculty/{faculty_id}", response_model=FacultyPublic)
def get_faculty(faculty_id: int, session: Session = Depends(get_session),
                _=Depends(require_faculty_or_pre_bootstrap)):
    return get_or_404(session, Faculty, faculty_id)


@router.put("/faculty/{faculty_id}", response_model=FacultyPublic)
def update_faculty(faculty_id: int, body: FacultyUpdate, session: Session = Depends(get_session),
                   _=Depends(require_faculty_or_pre_bootstrap)):
    faculty = get_or_404(session, Faculty, faculty_id)
    if body.code is not None:
        unique_or_400(session, Faculty, "code", body.code, exclude_id=faculty_id)
    for key, value in body.model_dump(exclude_unset=True, exclude={"password"}).items():
        setattr(faculty, key, value)
    if body.password:
        faculty.password_hash = hash_password(body.password)
    session.add(faculty)
    session.commit()
    session.refresh(faculty)
    return faculty


@router.delete("/faculty/{faculty_id}")
def delete_faculty(faculty_id: int, session: Session = Depends(get_session),
                   _=Depends(require_faculty_or_pre_bootstrap)):
    faculty = get_or_404(session, Faculty, faculty_id)
    # blocked while any allocation references this faculty (any of the three FK slots)
    for alloc in session.exec(select(Allocation)).all():
        if faculty_id in (alloc.faculty_id, alloc.batch1_faculty_id, alloc.batch2_faculty_id):
            raise HTTPException(
                status_code=400,
                detail=f"faculty {faculty_id} is referenced by an allocation; reassign it first",
            )
    session.delete(faculty)
    session.commit()
    return {"deleted": faculty_id}
