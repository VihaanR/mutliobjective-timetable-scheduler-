"""FastAPI app: the bespoke SY-reference CP-SAT pipeline (startup solve + read-only endpoints,
unchanged from before this merge) alongside the ported DB-backed dashboard platform (auth,
entity CRUD, generic Greedy/MIP/GA/CP-SAT solves, calendar, and timetable-image OCR)
that can handle any branch/division/year, not just the one hardcoded SY dataset.

Run:  python -m uvicorn webapp.server:app --port 8750
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager
from pathlib import Path

import json
from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from sqlmodel import Session, select


from webapp.auth import get_current_principal, require_faculty
from webapp.db import init_db, get_engine
from webapp.jobs import sweep_stale_running
from webapp.routers import (
    auth as auth_router, branches, faculty, courses, rooms, allocations, slots, runs, calendar,
    students, constraints,
)
from webapp.models_db import Branch, TimetableRun
from webapp.problem_builder import readiness
from webapp import seed

STATIC_DIR = Path(__file__).resolve().parent / "static"
try:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()  # create the platform's SQLite tables if new
        calendar.get_upload_dir()  # ensure webapp/uploads/ exists
        with Session(get_engine()) as session:
            sweep_stale_running(session)  # fail any "running" run orphaned by a previous restart
            is_testing = bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TESTING"))
            if not is_testing:
                if session.exec(select(Branch)).first() is None:
                    for ds in ("reference", "ty-sem6", "btech-sem8"):
                        try:
                            seed._seed_and_report(session, ds, force=False)
                        except Exception as exc:
                            print(f"Auto-seed warning for {ds}: {exc}")
                else:
                    try:
                        _, issues = readiness(session, qualify_ids=True)
                        if issues:
                            for ds in ("reference", "ty-sem6", "btech-sem8"):
                                seed._seed_and_report(session, ds, force=True)
                    except Exception as exc:
                        print(f"Auto-repair check warning: {exc}")
            # Auto-update SY SEM4 course weekly sessions to authentic 30-slot curriculum load
            try:
                from webapp.models_db import Course as DbCourse, Branch as DbBranch
                sy = session.exec(select(DbBranch).where(DbBranch.code == "CSE-DS-SY-SEM4")).first()
                if sy:
                    sy_courses = session.exec(select(DbCourse).where(DbCourse.branch_id == sy.id)).all()
                    course_updates = {
                        "DS": {"theory_per_week": 3, "practical_per_week": 1},
                        "ML-I": {"theory_per_week": 3, "practical_per_week": 1},
                        "SDS": {"theory_per_week": 0, "practical_per_week": 1},
                        "WE": {"theory_per_week": 0, "practical_per_week": 1},
                        "CMPM": {"theory_per_week": 3, "practical_per_week": 1},
                        "OE": {"theory_per_week": 3, "practical_per_week": 0},
                        "EFM": {"theory_per_week": 2, "practical_per_week": 0},
                        "PBC": {"theory_per_week": 1, "practical_per_week": 0, "tutorial_per_week": 0},
                    }
                    updated_c = False
                    for c in sy_courses:
                        if c.code in course_updates:
                            vals = course_updates[c.code]
                            for attr, val in vals.items():
                                if getattr(c, attr) != val:
                                    setattr(c, attr, val)
                                    updated_c = True
                    if updated_c:
                        session.commit()
            except Exception as exc:
                print(f"SY Sem 4 course session auto-update warning: {exc}")

            # Auto-repair any allocations where course has theory but faculty_id is None
            try:
                from webapp.models_db import Allocation, Course
                allocs_to_fix = session.exec(
                    select(Allocation).where(Allocation.faculty_id == None, Allocation.batch1_faculty_id != None)
                ).all()
                fixed_count = 0
                for a in allocs_to_fix:
                    crs = session.get(Course, a.course_id)
                    if crs and crs.theory_per_week > 0:
                        a.faculty_id = a.batch1_faculty_id
                        fixed_count += 1
                if fixed_count > 0:
                    session.commit()
            except Exception as exc:
                print(f'Allocation auto-repair warning: {exc}')

            if not is_testing and session.exec(select(TimetableRun)).first() is None:
                initial_run_file = Path(__file__).resolve().parent.parent / "data" / "reference" / "initial_even_semesters_run.json"
                if initial_run_file.exists():
                    try:
                        with open(initial_run_file, "r", encoding="utf-8") as f:
                            run_dict = json.load(f)
                        initial_run = TimetableRun(**run_dict)
                        session.add(initial_run)
                        session.commit()
                    except Exception as exc:
                        print(f"Initial run seed warning: {exc}")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        app.state.lifespan_error = str(exc)
    yield


app = FastAPI(title="Timetable Scheduler", lifespan=lifespan, debug=True)

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    import traceback
    return JSONResponse(
        status_code=500,
        content={
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "url": str(request.url),
        },
    )

@app.get("/api/health")
def health():
    return {"status": "ok", "lifespan_error": getattr(app.state, "lifespan_error", None)}


for _router in (auth_router.router, branches.router, faculty.router, students.router, courses.router,
                rooms.router, allocations.router, slots.router, seed.router, runs.router,
                calendar.router, constraints.router):
    app.include_router(_router)


EVOLUTION_DATA_FILE = Path(__file__).resolve().parent / "evolution_data.json"


@app.get("/api/evolution")
def get_evolution(_: dict = Depends(require_faculty)):
    if not EVOLUTION_DATA_FILE.exists():
        raise HTTPException(status_code=404, detail="No evolution data found. Run solver first.")
    try:
        with open(EVOLUTION_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read evolution data: {exc}")


@app.get("/api/evolution/step/{n}")
def get_evolution_step(n: int, _: dict = Depends(require_faculty)):
    if not EVOLUTION_DATA_FILE.exists():
        raise HTTPException(status_code=404, detail="No evolution data found. Run solver first.")
    try:
        with open(EVOLUTION_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read evolution data: {exc}")

    if not isinstance(data, list) or len(data) == 0:
        raise HTTPException(status_code=404, detail="No evolution data found. Run solver first.")
    if n < 1 or n > len(data):
        raise HTTPException(status_code=404, detail=f"Step {n} not found. Available steps: 1 to {len(data)}.")

    return data[n - 1]["timetable_data"]


@app.get("/api/runs/{run_id}/evolution")
def get_run_evolution(run_id: int, _: dict = Depends(require_faculty)):
    """Fetch solver progress evolution history specific to a run."""
    # If run-specific evolution tracking is stored in evolution_data.json, return it
    if EVOLUTION_DATA_FILE.exists():
        try:
            with open(EVOLUTION_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read evolution data: {exc}")
    raise HTTPException(status_code=404, detail=f"No evolution data found for run #{run_id}")


@app.get("/evolution")
@app.get("/index.html")
def evolution_page():
    return RedirectResponse(url="/platform#benchmarks", status_code=307)


# --------------------------------------------------------------------------- ported dashboard platform
def _teacher_page(filename: str, principal: dict | None):
    if principal is None:
        return RedirectResponse(url="/login", status_code=307)
    if principal.get("role") not in ("faculty", "admin"):
        return RedirectResponse(url="/student", status_code=307)
    return FileResponse(str(STATIC_DIR / filename))


@app.get("/")
def index(principal: dict | None = Depends(get_current_principal)):
    if principal is None:
        return RedirectResponse(url="/login", status_code=307)
    role = principal.get("role")
    if role == "student":
        return RedirectResponse(url="/student", status_code=307)
    if role in ("faculty", "admin"):
        return RedirectResponse(url="/platform", status_code=307)
    return RedirectResponse(url="/login", status_code=307)


@app.get("/platform")
def platform_page(principal: dict | None = Depends(get_current_principal)):
    return _teacher_page("platform.html", principal)


@app.get("/dashboard")
def dashboard_page(principal: dict | None = Depends(get_current_principal)):
    return _teacher_page("dashboard.html", principal)


@app.get("/my-timetable")
def my_timetable_page(principal: dict | None = Depends(get_current_principal)):
    return _teacher_page("faculty_timetable.html", principal)


@app.get("/profile")
def profile_page(principal: dict | None = Depends(get_current_principal)):
    return _teacher_page("profile.html", principal)


@app.get("/login")
def login_page():
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/student")
def student_page(principal: dict | None = Depends(get_current_principal)):
    if principal is None:
        return RedirectResponse(url="/login", status_code=307)
    if principal.get("role") != "student":
        return RedirectResponse(url="/platform", status_code=307)
    return FileResponse(str(STATIC_DIR / "student.html"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp.server:app", host="127.0.0.1", port=8750, reload=True)
