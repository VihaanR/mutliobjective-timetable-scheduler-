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
    DEV_SAMPLE_FACULTY_CODE, DEV_SAMPLE_EMAIL, DEV_SAMPLE_PASSWORD,
    DEV_ADMIN_FACULTY_CODE, DEV_ADMIN_EMAIL, DEV_ADMIN_PASSWORD,
    is_dev_sample_credentials, matches_dev_sample_teacher_credentials,
    SESSION_COOKIE, ACCESS_TOKEN_COOKIE, SESSION_TTL_SECONDS,
    create_session_token, get_current_principal, get_secret, hash_password,
    verify_password,
)
from webapp.db import get_session
from webapp.models_db import Faculty, Student, Allocation, Course, Division, Branch

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    role: str          # "faculty" | "admin" | "student"
    email: str
    password: str


class BootstrapRequest(BaseModel):
    faculty_code: str   # the existing Faculty.code ("AR") this account claims
    email: str
    password: str


def _set_session_cookie(response: Response, session: Session, role: str, user_id: int) -> str:
    secret = get_secret(session)
    token = create_session_token({"role": role, "id": user_id}, secret)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS, path="/"
    )
    response.set_cookie(
        ACCESS_TOKEN_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS, path="/"
    )
    return token


@router.post("/login")
def login(body: LoginRequest, response: Response, session: Session = Depends(get_session)):
    clean_email = body.email.strip().lower()
    clean_password = body.password.strip()

    if body.role in ("faculty", "admin"):
        if matches_dev_sample_teacher_credentials(clean_email, clean_password):
            is_admin = (clean_email == DEV_ADMIN_EMAIL.lower())
            code = DEV_ADMIN_FACULTY_CODE if is_admin else DEV_SAMPLE_FACULTY_CODE
            name = "Institutional Administrator" if is_admin else "Prof. Nilesh Marathe"
            email = DEV_ADMIN_EMAIL if is_admin else DEV_SAMPLE_EMAIL
            assigned_role = "admin" if is_admin else "faculty"

            row = session.exec(select(Faculty).where(Faculty.code == code)).first()
            if row is None:
                row = Faculty(
                    code=code,
                    name=name,
                    email=email,
                    password_hash=None,
                )
                session.add(row)
                session.commit()
                session.refresh(row)
            elif row.password_hash is not None:
                row.password_hash = None
                session.add(row)
                session.commit()
                session.refresh(row)

            token = _set_session_cookie(response, session, assigned_role, row.id)
            return {
                "role": assigned_role,
                "id": row.id,
                "name": row.name,
                "access_token": token,
                "token_type": "bearer",
            }
        elif is_dev_sample_credentials(clean_email, clean_password):
            raise HTTPException(status_code=401, detail="dev fallback disabled or invalid credentials")

        row = session.exec(select(Faculty).where(Faculty.email == clean_email)).first()
        assigned_role = body.role
    elif body.role == "student":
        row = session.exec(select(Student).where(Student.email == clean_email)).first()
        assigned_role = "student"
    else:
        raise HTTPException(status_code=400, detail="role must be 'faculty', 'admin', or 'student'")

    if row is None or not row.password_hash or (
        not verify_password(clean_password, row.password_hash)
    ):
        raise HTTPException(status_code=401, detail="invalid email or password")

    token = _set_session_cookie(response, session, assigned_role, row.id)
    return {
        "role": assigned_role,
        "id": row.id,
        "name": row.name,
        "access_token": token,
        "token_type": "bearer",
    }


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(ACCESS_TOKEN_COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(ACCESS_TOKEN_COOKIE)
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, principal: dict | None = Depends(get_current_principal), session: Session = Depends(get_session)):
    if principal is None:
        raise HTTPException(status_code=401, detail="not logged in")
    model = Faculty if principal["role"] in ("faculty", "admin") else Student
    row = session.get(model, principal["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="account not found")
    if row.password_hash and not verify_password(body.old_password, row.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    row.password_hash = hash_password(body.new_password)
    session.add(row)
    session.commit()
    return {"ok": True, "message": "Password updated successfully"}


@router.get("/me")
def me(principal: dict | None = Depends(get_current_principal), session: Session = Depends(get_session)):
    if principal is None:
        raise HTTPException(status_code=401, detail="not logged in")
    model = Faculty if principal["role"] in ("faculty", "admin") else Student
    row = session.get(model, principal["id"])
    if row is None:
        raise HTTPException(status_code=401, detail="account no longer exists")

    workload = []
    total_hours = 0
    if principal["role"] in ("faculty", "admin"):
        allocs = session.exec(
            select(Allocation).where(
                (Allocation.faculty_id == row.id) |
                (Allocation.batch1_faculty_id == row.id) |
                (Allocation.batch2_faculty_id == row.id)
            )
        ).all()
        for a in allocs:
            div = session.get(Division, a.division_id)
            course = session.get(Course, a.course_id)
            branch = session.get(Branch, div.branch_id) if div else None
            is_theory = (a.faculty_id == row.id)
            is_b1 = (a.batch1_faculty_id == row.id)
            is_b2 = (a.batch2_faculty_id == row.id)

            batches_taught = []
            if is_b1: batches_taught.append(div.batch1_name or f"{div.name}1")
            if is_b2: batches_taught.append(div.batch2_name or f"{div.name}2")

            th_hrs = (course.theory_per_week if course else 0) if is_theory else 0
            pr_hrs = (course.practical_per_week if course else 0) * len(batches_taught)
            total_hours += (th_hrs + pr_hrs)

            workload.append({
                "allocation_id": a.id,
                "course_code": course.code if course else "",
                "course_title": course.title if course else "",
                "division": div.name if div else "",
                "class_label": f"{branch.code} · {div.name}" if branch and div else (div.name if div else ""),
                "semester": branch.semester if branch else (div.semester if div else None),
                "is_theory": is_theory,
                "batches": batches_taught,
                "theory_hours": th_hrs,
                "lab_hours": pr_hrs,
                "total_hours": th_hrs + pr_hrs
            })

    return {
        "role": principal["role"],
        "id": row.id,
        "name": row.name,
        "email": getattr(row, "email", None),
        "code": getattr(row, "code", None),
        "department": "Department of Computer Science & Engineering (Data Science)",
        "max_load_hours_per_week": getattr(row, "max_load_hours_per_week", 20),
        "workload": workload,
        "assigned_hours": total_hours
    }


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

    token = _set_session_cookie(response, session, "faculty", faculty.id)
    return {
        "role": "faculty",
        "id": faculty.id,
        "name": faculty.name,
        "access_token": token,
        "token_type": "bearer",
    }
