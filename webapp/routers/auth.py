"""Login/logout/bootstrap (design.md §11 Auth). See `webapp/auth.py` for the hashing/token
primitives. Two roles identified by email: `"faculty"` (teacher) and `"student"`. Every login sets
an httponly `tt_session` cookie carrying `{role, id}` - `id` is always the DB int primary key, not
the engine's string faculty code / division name (see the type-precision note in the session plan).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from webapp.auth import (
    DEV_SAMPLE_FACULTY_CODE, matches_dev_sample_teacher_credentials, SESSION_COOKIE,
    SESSION_TTL_SECONDS, create_session_token, get_current_principal, get_secret, hash_password,
    verify_password,
)
from webapp.db import get_session
from webapp.models_db import Faculty, Student

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    role: str          # "faculty" | "student"
    email: str
    password: str


class BootstrapRequest(BaseModel):
    faculty_code: str   # the existing Faculty.code ("AR") this account claims
    email: str
    password: str


def _set_session_cookie(response: Response, session: Session, role: str, user_id: int) -> None:
    secret = get_secret(session)
    token = create_session_token({"role": role, "id": user_id}, secret)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS,
        # secure=True belongs here once this runs behind HTTPS - see webapp/auth.py's honest-gaps note
    )


@router.post("/login")
def login(body: LoginRequest, response: Response, session: Session = Depends(get_session)):
    if body.role == "faculty":
        if matches_dev_sample_teacher_credentials(body.email, body.password):
            row = session.exec(select(Faculty).where(Faculty.code == DEV_SAMPLE_FACULTY_CODE)).first()
            if row is None:
                raise HTTPException(status_code=401, detail="invalid email or password")
            _set_session_cookie(response, session, body.role, row.id)
            return {"role": body.role, "id": row.id, "name": row.name}
        row = session.exec(select(Faculty).where(Faculty.email == body.email)).first()
    elif body.role == "student":
        row = session.exec(select(Student).where(Student.email == body.email)).first()
    else:
        raise HTTPException(status_code=400, detail="role must be 'faculty' or 'student'")

    if row is None or not row.password_hash or (
        not verify_password(body.password, row.password_hash)
    ):
        raise HTTPException(status_code=401, detail="invalid email or password")

    _set_session_cookie(response, session, body.role, row.id)
    return {"role": body.role, "id": row.id, "name": row.name}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(principal: dict | None = Depends(get_current_principal), session: Session = Depends(get_session)):
    if principal is None:
        raise HTTPException(status_code=401, detail="not logged in")
    model = Faculty if principal["role"] == "faculty" else Student
    row = session.get(model, principal["id"])
    if row is None:
        raise HTTPException(status_code=401, detail="account no longer exists")
    return {"role": principal["role"], "id": row.id, "name": row.name}


@router.post("/bootstrap")
def bootstrap(body: BootstrapRequest, response: Response, session: Session = Depends(get_session)):
    """One-time setup: claims an existing Faculty row's login credentials. Works only while NO
    faculty has credentials set yet - solves the chicken-and-egg problem of a login-gated
    dashboard with no one able to log in. Every call after the first 403s permanently."""
    already_setup = session.exec(
        select(Faculty).where(Faculty.password_hash.is_not(None))
    ).first()
    if already_setup is not None:
        raise HTTPException(status_code=403, detail="bootstrap already used; log in normally")

    faculty = session.exec(select(Faculty).where(Faculty.code == body.faculty_code)).first()
    if faculty is None:
        raise HTTPException(status_code=404, detail=f"no faculty with code {body.faculty_code!r}")

    faculty.email = body.email
    faculty.password_hash = hash_password(body.password)
    session.add(faculty)
    session.commit()
    session.refresh(faculty)

    _set_session_cookie(response, session, "faculty", faculty.id)
    return {"role": "faculty", "id": faculty.id, "name": faculty.name}
