"""Allocation CRUD — division×course → faculty (design.md §5.1).

If the course has practicals, both batch-faculty FKs are required (the two parallel lab sections);
otherwise the single whole-division faculty FK is required. This mirrors the engine's
`Division.faculty_by_course` single-vs-tuple form.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from webapp.auth import require_faculty
from webapp.db import get_session
from webapp.models_db import (
    Allocation, AllocationCreate, AllocationUpdate, Course, Division, Faculty,
)
from webapp.routers._crud import apply_update, get_or_404

router = APIRouter(prefix="/api", tags=["allocations"])


def _validate_faculty_shape(session: Session, course: Course, faculty_id, b1, b2) -> None:
    def _exists(fid):
        return fid is None or session.get(Faculty, fid) is not None

    for fid in (faculty_id, b1, b2):
        if not _exists(fid):
            raise HTTPException(status_code=400, detail=f"faculty {fid} not found")

    if course.practical_per_week and course.practical_per_week > 0:
        if b1 is None or b2 is None:
            raise HTTPException(
                status_code=400,
                detail="course has practicals: both batch1_faculty_id and batch2_faculty_id are required",
            )
    else:
        if faculty_id is None:
            raise HTTPException(
                status_code=400,
                detail="course has no practicals: faculty_id (whole-division) is required",
            )


@router.get("/allocations")
def list_allocations(division_id: int | None = None, session: Session = Depends(get_session),
                     _=Depends(require_faculty)):
    stmt = select(Allocation)
    if division_id is not None:
        stmt = stmt.where(Allocation.division_id == division_id)
    return session.exec(stmt).all()


@router.post("/allocations", status_code=201)
def create_allocation(body: AllocationCreate, session: Session = Depends(get_session),
                      _=Depends(require_faculty)):
    get_or_404(session, Division, body.division_id)
    course = get_or_404(session, Course, body.course_id)
    _validate_faculty_shape(session, course, body.faculty_id, body.batch1_faculty_id, body.batch2_faculty_id)
    alloc = Allocation.model_validate(body)
    session.add(alloc)
    session.commit()
    session.refresh(alloc)
    return alloc


@router.get("/allocations/{allocation_id}")
def get_allocation(allocation_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    return get_or_404(session, Allocation, allocation_id)


@router.put("/allocations/{allocation_id}")
def update_allocation(allocation_id: int, body: AllocationUpdate, session: Session = Depends(get_session),
                      _=Depends(require_faculty)):
    alloc = get_or_404(session, Allocation, allocation_id)
    merged = alloc.model_copy(update=body.model_dump(exclude_unset=True))
    course = get_or_404(session, Course, alloc.course_id)
    _validate_faculty_shape(session, course, merged.faculty_id, merged.batch1_faculty_id, merged.batch2_faculty_id)
    apply_update(alloc, body)
    session.add(alloc)
    session.commit()
    session.refresh(alloc)
    return alloc


@router.delete("/allocations/{allocation_id}")
def delete_allocation(allocation_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    alloc = get_or_404(session, Allocation, allocation_id)
    session.delete(alloc)
    session.commit()
    return {"deleted": allocation_id}


@router.post("/allocations/sync-master-xlsx")
def sync_master_xlsx(session: Session = Depends(get_session), _=Depends(require_faculty)):
    from webapp.allocation_sync import sync_master_xlsx_into_db
    return sync_master_xlsx_into_db(session)
