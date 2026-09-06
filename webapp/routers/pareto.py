"""POST /api/pareto -- generic epsilon-constraint Pareto sweep against any branch/year (not just
the bespoke SY-only sweep at the legacy /api/pareto endpoint served from server.py). Reuses
`engine.pareto_sweep.sweep()`, which is itself a thin driver over the CP-SAT model's existing
`solve_pareto_point()`. A sweep does ~3 pairs x ~5 points x 2 CP-SAT solves each, so it runs as a
background job (same queued -> running -> done/failed pattern as `POST /api/runs`).
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from engine.io_json import problem_to_dict
from engine.pareto_sweep import DEFAULT_PAIRS, DEFAULT_SWEEP_POINTS, DEFAULT_TIME_LIMIT_S
from webapp.auth import require_faculty
from webapp.db import get_session
from webapp.jobs import run_pareto_job
from webapp.models_db import ParetoRun
from webapp.problem_builder import readiness, spans_multiple_branches

router = APIRouter(prefix="/api", tags=["pareto"])


class ParetoRequest(BaseModel):
    label: str = ""
    branch_ids: list[int] | None = None
    pairs: list[list[str]] | None = None  # e.g. [["faculty", "students"], ...]
    time_limit_s: float = DEFAULT_TIME_LIMIT_S
    sweep_points: int = DEFAULT_SWEEP_POINTS


@router.post("/pareto")
def start_pareto_sweep(
    body: ParetoRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    qualify_ids = spans_multiple_branches(session, body.branch_ids)
    problem, issues = readiness(session, body.branch_ids, qualify_ids=qualify_ids)
    if problem is None or issues:
        raise HTTPException(status_code=400, detail=issues)

    pairs = [tuple(p) for p in body.pairs] if body.pairs else DEFAULT_PAIRS
    valid_categories = {"rooms", "labs", "students", "faculty"}
    bad_pairs = [p for p in pairs if len(p) != 2 or not valid_categories.issuperset(p)]
    if bad_pairs:
        raise HTTPException(
            status_code=400,
            detail=f"invalid pair(s) {bad_pairs}; each pair must be two of {sorted(valid_categories)}",
        )

    run = ParetoRun(
        label=body.label,
        branch_ids=body.branch_ids or [],
        time_limit_s=body.time_limit_s,
        sweep_points=body.sweep_points,
        status="queued",
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    background.add_task(run_pareto_job, run.id, problem_to_dict(problem))
    return {"run_id": run.id}


@router.get("/pareto/{run_id}")
def get_pareto_run(run_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    run = session.get(ParetoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"pareto run {run_id} not found")
    return {
        "id": run.id,
        "label": run.label,
        "branch_ids": run.branch_ids,
        "status": run.status,
        "time_limit_s": run.time_limit_s,
        "sweep_points": run.sweep_points,
        "points": run.points,
        "error": run.error,
        "created_at": run.created_at,
    }


@router.get("/pareto/runs")
def list_pareto_runs(session: Session = Depends(get_session), _=Depends(require_faculty)):
    # NOTE: not "/pareto" -- that GET path is already the legacy bespoke SY-only pareto endpoint
    # defined directly on `app` in server.py; this route must not collide with it.
    runs = session.exec(select(ParetoRun).order_by(ParetoRun.created_at.desc())).all()
    return [
        {
            "id": r.id, "label": r.label, "branch_ids": r.branch_ids, "status": r.status,
            "created_at": r.created_at,
        }
        for r in runs
    ]
