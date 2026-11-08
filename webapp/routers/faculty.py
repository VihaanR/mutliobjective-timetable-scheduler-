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
def my_timetable(faculty_id: int | None = None, session: Session = Depends(get_session), principal: dict = Depends(require_faculty)):
    target_id = principal["id"]
    if principal["role"] == "admin":
        if faculty_id is not None:
            target_id = faculty_id
        else:
            admin_fac = session.get(Faculty, target_id)
            if admin_fac and admin_fac.code == "ADMIN":
                nm_fac = session.exec(select(Faculty).where(Faculty.code == "NM")).first()
                if nm_fac:
                    target_id = nm_fac.id

    faculty = get_or_404(session, Faculty, target_id)

    runs = session.exec(
        select(TimetableRun)
        .where(TimetableRun.status == "done")
        .order_by(TimetableRun.created_at.desc())
    ).all()
    if not runs:
        raise HTTPException(status_code=404, detail="no generated timetable available yet")

    # Prefer comprehensive run with maximum divisions (e.g. all even semesters)
    run = next((r for r in runs if r.grids and len(r.grids.get("divisions", [])) >= 3), runs[0])
    if not run.grids:
        raise HTTPException(status_code=404, detail="no generated timetable available yet")

    cells: dict[str, list] = {}
    semesters_found = set()

    for division in run.grids.get("divisions", []):
        for key, entries in division.get("cells", {}).items():
            mine = []
            for e in entries:
                if e.get("faculty") == faculty.code:
                    entry_copy = dict(e)
                    label = str(e.get("class_label") or division.get("class_label") or division.get("id") or "").upper()
                    sem = None
                    if "SEM4" in label or "SEM 4" in label or "SEM IV" in label or "SY" in label:
                        sem = 4
                    elif "SEM6" in label or "SEM 6" in label or "SEM VI" in label or "TY" in label:
                        sem = 6
                    elif "SEM8" in label or "SEM 8" in label or "SEM VIII" in label or "BTECH" in label:
                        sem = 8
                    entry_copy["semester"] = sem
                    if sem:
                        semesters_found.add(sem)
                    mine.append(entry_copy)
            if mine:
                cells.setdefault(key, []).extend(mine)

    classes = sorted({
        e.get("class_label") for entries in cells.values() for e in entries
        if e.get("class_label")
    })

    all_faculties = []
    if principal["role"] == "admin":
        fac_rows = session.exec(select(Faculty).order_by(Faculty.name)).all()
        all_faculties = [{"id": f.id, "code": f.code, "name": f.name} for f in fac_rows if f.code != "ADMIN"]

    return {
        "run_id": run.id,
        "faculty_id": faculty.id,
        "faculty_name": faculty.name,
        "faculty_code": faculty.code,
        "days": run.grids["days"],
        "periods": run.grids["periods"],
        "cells": cells,
        "classes": classes,
        "semesters": sorted(list(semesters_found)),
        "spans_multiple_branches": len(semesters_found) > 1 or len({e.get("branch_id") for v in cells.values() for e in v}) > 1,
        "is_admin": principal["role"] == "admin",
        "all_faculties": all_faculties,
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
