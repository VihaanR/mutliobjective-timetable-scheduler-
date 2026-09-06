"""FastAPI app: the bespoke SY-reference CP-SAT pipeline (startup solve + read-only endpoints,
unchanged from before this merge) alongside the ported DB-backed dashboard platform (auth,
entity CRUD, generic Greedy/MIP/GA/CP-SAT solves, calendar, Pareto sweep, timetable-image OCR)
that can handle any branch/division/year, not just the one hardcoded SY dataset.

Run:  python -m uvicorn webapp.server:app --port 8750
"""
import sys
import os
import threading
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from ortools.sat.python import cp_model
from sqlmodel import Session

from data import DataBundle
from model import build_model
from solver import solve_and_report, check_hard_violations

from webapp.auth import get_current_principal
from webapp.db import init_db, get_engine
from webapp.jobs import sweep_stale_running
from webapp.routers import (
    auth as auth_router, branches, faculty, courses, rooms, allocations, slots, runs, calendar,
    students, pareto,
)
from webapp import seed

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Global state for the bespoke SY-reference pipeline's read-only endpoints.
schedule_data = {
    "divisions": [],
    "teachers": {},
    "classrooms": {},
    "labs": {},
    "stats": {}
}


def _run_sy_reference_solver():
    """Startup solve for the bespoke DataBundle/CP-SAT pipeline (SY reference dataset).
    Unchanged behavior from before this merge — see git history."""
    global schedule_data
    print("Running SY-reference solver before starting web server...")
    data = DataBundle.default()
    model, vars_dict = build_model(data)
    solver, status, _ = solve_and_report(model, vars_dict, data)

    status_name = solver.StatusName(status)
    if status not in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        print("Solver failed to find a solution.")
        schedule_data["stats"] = {"status": status_name, "error": "No solution"}
        return

    tv = vars_dict['tv']
    oe1 = vars_dict['oe1']
    oe2 = vars_dict['oe2']
    lv = vars_dict['lv']
    lr = vars_dict['lr']
    div_day_hours = vars_dict['div_day_hours']

    violations = check_hard_violations(solver, tv, oe1, oe2, lv, lr, div_day_hours, data)

    schedule_data["stats"] = {
        "status": status_name,
        "wall_time": round(solver.WallTime(), 2),
        "penalty": int(solver.ObjectiveValue()),
        "hard_violations": len(violations)
    }

    from extract_schedule import extract_schedule
    extracted = extract_schedule(solver, vars_dict, data)

    schedule_data["divisions"] = extracted["divisions"]
    schedule_data["teachers"] = extracted["teachers"]
    schedule_data["classrooms"] = extracted["classrooms"]
    schedule_data["labs"] = extracted["labs"]

    print("SY-reference solver data extracted and ready!")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # create the platform's SQLite tables if new
    calendar.get_upload_dir()  # ensure webapp/uploads/ exists
    with Session(get_engine()) as session:
        sweep_stale_running(session)  # fail any "running" run orphaned by a previous restart
    threading.Thread(target=_run_sy_reference_solver, daemon=True).start()
    yield


app = FastAPI(title="Timetable Scheduler", lifespan=lifespan)

for _router in (auth_router.router, branches.router, faculty.router, students.router, courses.router,
                rooms.router, allocations.router, slots.router, seed.router, runs.router,
                calendar.router, pareto.router):
    app.include_router(_router)


# --------------------------------------------------------------------------- bespoke SY-reference
@app.get("/api/timetable/divisions")
def get_divisions():
    return schedule_data["divisions"]

    threading.Thread(target=_run_sy_reference_solver, daemon=True).start()
def get_teachers():
    return schedule_data["teachers"]

@app.get("/api/timetable/classrooms")
def get_classrooms():
    return schedule_data["classrooms"]

@app.get("/api/timetable/labs")
def get_labs():
    return schedule_data["labs"]

@app.get("/api/stats")
def get_stats():
    return schedule_data["stats"]

@app.get("/api/pareto")
def get_pareto():
    import json
    file_path = os.path.join(os.path.dirname(__file__), "pareto_solutions.json")
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            return json.load(f)
    return []

@app.get("/sy-reference")
def sy_reference_page():
    """The original bespoke-pipeline viewer, moved off '/' to make room for the ported
    dashboard platform's home page."""
    return FileResponse(str(TEMPLATES_DIR / "index.html"))


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
