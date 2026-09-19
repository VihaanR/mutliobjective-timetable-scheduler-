"""FastAPI app: the bespoke SY-reference CP-SAT pipeline (startup solve + read-only endpoints,
unchanged from before this merge) alongside the ported DB-backed dashboard platform (auth,
entity CRUD, generic Greedy/MIP/GA/CP-SAT solves, calendar, Pareto sweep, timetable-image OCR)
that can handle any branch/division/year, not just the one hardcoded SY dataset.

Run:  python -m uvicorn webapp.server:app --port 8750
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel import Session


from webapp.auth import get_current_principal
from webapp.db import init_db, get_engine
from webapp.jobs import sweep_stale_running
from webapp.routers import (
    auth as auth_router, branches, faculty, courses, rooms, allocations, slots, runs, calendar,
    students, pareto,
)
from webapp import seed

STATIC_DIR = Path(__file__).resolve().parent / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # create the platform's SQLite tables if new
    calendar.get_upload_dir()  # ensure webapp/uploads/ exists
    with Session(get_engine()) as session:
        sweep_stale_running(session)  # fail any "running" run orphaned by a previous restart
    yield


app = FastAPI(title="Timetable Scheduler", lifespan=lifespan)

for _router in (auth_router.router, branches.router, faculty.router, students.router, courses.router,
                rooms.router, allocations.router, slots.router, seed.router, runs.router,
                calendar.router, pareto.router):
    app.include_router(_router)


# --------------------------------------------------------------------------- ported dashboard platform
def _teacher_page(filename: str, principal: dict | None):
    if principal is None:
        return RedirectResponse("/login")
    if principal["role"] != "faculty":
        return RedirectResponse("/student")
    return FileResponse(str(STATIC_DIR / filename))


@app.get("/")
def index(principal: dict | None = Depends(get_current_principal)):
    return _teacher_page("dashboard.html", principal)


@app.get("/platform")
def platform_page(principal: dict | None = Depends(get_current_principal)):
    return _teacher_page("platform.html", principal)


@app.get("/dashboard")
def dashboard_page(principal: dict | None = Depends(get_current_principal)):
    return _teacher_page("dashboard.html", principal)


@app.get("/my-timetable")
def my_timetable_page(principal: dict | None = Depends(get_current_principal)):
    return _teacher_page("faculty_timetable.html", principal)


@app.get("/login")
def login_page():
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/student")
def student_page(principal: dict | None = Depends(get_current_principal)):
    if principal is None:
        return RedirectResponse("/login")
    if principal["role"] != "student":
        return RedirectResponse("/")
    return FileResponse(str(STATIC_DIR / "student.html"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp.server:app", host="127.0.0.1", port=8750, reload=True)