"""POST /api/pareto -- generic epsilon-constraint Pareto sweep against any branch/year.
Includes live SSE streaming endpoint (POST /api/pareto/stream) that streams real-time updates
of all 8 steps of the multi-objective architecture directly to the client.
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from engine.io_json import problem_to_dict
from engine.pareto_sweep import (
    DEFAULT_PAIRS, DEFAULT_SWEEP_POINTS, DEFAULT_TIME_LIMIT_S, sweep_stream,
)
from webapp.auth import require_faculty
from webapp.db import get_session
from webapp.jobs import run_pareto_job
from webapp.models_db import ParetoRun, TimetableRun
from webapp.problem_builder import build_division_meta, readiness, spans_multiple_branches

router = APIRouter(prefix="/api", tags=["pareto"])


class ParetoRequest(BaseModel):
    label: str = ""
    branch_ids: list[int] | None = None
    pairs: list[list[str]] | None = None  # e.g. [["faculty", "students"], ...]
    time_limit_s: float = DEFAULT_TIME_LIMIT_S
    sweep_points: int = DEFAULT_SWEEP_POINTS


class SavePointRequest(BaseModel):
    label: str = "Pareto Optimal Timetable"
    solver: str = "cpsat (pareto)"
    solution: dict
    grids: dict
    hard: int = 0
    soft: float = 0.0
    wall_clock: float = 0.0
    branch_ids: list[int] | None = None


@router.post("/pareto/stream")
def stream_pareto(
    body: ParetoRequest,
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    """Server-Sent Events (SSE) live streaming of the 8-step Pareto Epsilon Sweep."""
    qualify_ids = spans_multiple_branches(session, body.branch_ids)
    problem, issues = readiness(session, body.branch_ids, qualify_ids=qualify_ids)
    if problem is None or issues:
        raise HTTPException(status_code=400, detail=issues)

    division_meta = build_division_meta(session, body.branch_ids)
    pairs = [tuple(p) for p in body.pairs] if body.pairs else DEFAULT_PAIRS

    valid_categories = {"rooms", "labs", "students", "faculty"}
    bad_pairs = [p for p in pairs if len(p) != 2 or not valid_categories.issuperset(p)]
    if bad_pairs:
        raise HTTPException(
            status_code=400,
            detail=f"invalid pair(s) {bad_pairs}; each pair must be two of {sorted(valid_categories)}",
        )

    def event_stream():
        try:
            for event in sweep_stream(
                problem,
                pairs=pairs,
                time_limit_s=body.time_limit_s,
                sweep_points=body.sweep_points,
                division_meta=division_meta,
            ):
                payload = json.dumps(event, default=str)
                yield f"data: {payload}\n\n"
        except Exception as exc:
            err_payload = json.dumps({"type": "error", "error": str(exc)})
            yield f"data: {err_payload}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/pareto/save-point-as-run")
def save_point_as_run(
    body: SavePointRequest,
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    """Save a chosen Pareto optimal point as a permanent TimetableRun for history, exports, & adjustments."""
    run = TimetableRun(
        solver=body.solver,
        time_limit=body.wall_clock,
        label=body.label,
        branch_ids=body.branch_ids or [],
        status="done",
        solution=body.solution,
        grids=body.grids,
        hard=body.hard,
        soft=body.soft,
        wall_clock=body.wall_clock,
        created_at=datetime.utcnow(),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return {"run_id": run.id, "message": "Saved successfully as active run"}


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

    background.add_task(run_pareto_job, run.id, problem_to_dict(problem), [list(p) for p in pairs])
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
    runs = session.exec(select(ParetoRun).order_by(ParetoRun.created_at.desc())).all()
    return [
        {
            "id": r.id, "label": r.label, "branch_ids": r.branch_ids, "status": r.status,
            "created_at": r.created_at,
        }
        for r in runs
    ]
