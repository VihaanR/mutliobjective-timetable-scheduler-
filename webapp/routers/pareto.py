"""POST /api/pareto -- generic epsilon-constraint Pareto sweep against any branch/year.
Includes live SSE streaming endpoint (POST /api/pareto/stream) that streams real-time updates
of all 8 steps of the multi-objective architecture directly to the client.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import threading
import asyncio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
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
from webapp.models_db import ParetoRun, TimetableRun, utc_now
from webapp.problem_builder import build_division_meta, readiness, spans_multiple_branches

router = APIRouter(prefix="/api", tags=["pareto"])

# Global registry: token -> threading.Event so the /stop endpoint can cancel any active sweep
_active_sweeps: dict[str, threading.Event] = {}


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


class StopSweepRequest(BaseModel):
    token: str


@router.post("/pareto/stop")
def stop_pareto_sweep(body: StopSweepRequest, _=Depends(require_faculty)):
    """Explicitly cancel an active Pareto sweep identified by its token."""
    evt = _active_sweeps.get(body.token)
    if evt is None:
        return {"stopped": False, "detail": "No active sweep with that token"}
    evt.set()
    return {"stopped": True}


@router.post("/pareto/stream")
async def stream_pareto(
    body: ParetoRequest,
    request: Request,
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

    valid_categories = {"rooms", "labs", "students", "faculty", "resource"}
    bad_pairs = [p for p in pairs if len(p) != 2 or not valid_categories.issuperset(p)]
    if bad_pairs:
        raise HTTPException(
            status_code=400,
            detail=f"invalid pair(s) {bad_pairs}; each pair must be two of {sorted(valid_categories)}",
        )

    # --- Cancel infrastructure ---
    cancel_event = threading.Event()
    token = str(uuid.uuid4())
    _active_sweeps[token] = cancel_event

    async def event_stream():
        loop = asyncio.get_event_loop()
        # Bridge: asyncio.Queue receives events from the background thread
        aio_q: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        def thread_worker():
            """Runs sweep_stream() on a thread; pushes events into the asyncio queue."""
            try:
                for event in sweep_stream(
                    problem,
                    pairs=pairs,
                    time_limit_s=body.time_limit_s,
                    sweep_points=body.sweep_points,
                    division_meta=division_meta,
                    cancel_event=cancel_event,
                ):
                    if cancel_event.is_set():
                        break
                    loop.call_soon_threadsafe(aio_q.put_nowait, event)
            except Exception as exc:
                loop.call_soon_threadsafe(
                    aio_q.put_nowait, {"type": "error", "error": str(exc)}
                )
            finally:
                loop.call_soon_threadsafe(aio_q.put_nowait, SENTINEL)

        worker_thread = threading.Thread(target=thread_worker, daemon=True)
        worker_thread.start()

        try:
            # First event: send the token so the client can call /stop
            yield f"data: {json.dumps({'type': 'token', 'token': token})}\n\n"

            while True:
                try:
                    item = await asyncio.wait_for(aio_q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if not worker_thread.is_alive() and aio_q.empty():
                        break
                    # Keep-alive comment heartbeat: keeps TCP/HTTP stream active across proxies and browsers
                    yield ": ping\n\n"
                    continue

                if item is SENTINEL:
                    break

                event = item
                if event.get("type") == "complete":
                    try:
                        from webapp.db import engine as db_engine
                        from sqlmodel import Session as DBSession
                        with DBSession(db_engine) as save_session:
                            p_run = ParetoRun(
                                label=body.label or "Live Multi-Objective Pareto Sweep",
                                branch_ids=body.branch_ids or [],
                                time_limit_s=body.time_limit_s,
                                sweep_points=body.sweep_points,
                                status="done",
                                created_at=utc_now(),
                                points={
                                    "points": event.get("points", []),
                                    "payoff_tables": event.get("payoff_tables", {}),
                                    "recommendations": event.get("recommendations", {}),
                                },
                            )
                            save_session.add(p_run)
                            save_session.commit()
                            save_session.refresh(p_run)
                            event["run_id"] = p_run.id
                    except Exception:
                        pass

                payload = json.dumps(event, default=str)
                yield f"data: {payload}\n\n"

        except (asyncio.CancelledError, GeneratorExit):
            cancel_event.set()
        except Exception as exc:
            err_payload = json.dumps({"type": "error", "error": str(exc)})
            yield f"data: {err_payload}\n\n"
        finally:
            cancel_event.set()
            _active_sweeps.pop(token, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class SaveSweepRequest(BaseModel):
    label: str = "Pareto Optimization Sweep"
    branch_ids: list[int] | None = None
    time_limit_s: float = 240.0
    sweep_points: int = 7
    points: list[dict] = []
    payoff_tables: dict = {}
    recommendations: dict = {}


@router.post("/pareto/save-sweep")
def save_sweep_as_run(
    body: SaveSweepRequest,
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    """Save a full or partial Pareto sweep as a permanent ParetoRun record."""
    run = ParetoRun(
        label=body.label,
        branch_ids=body.branch_ids or [],
        time_limit_s=body.time_limit_s,
        sweep_points=body.sweep_points,
        status="done",
        created_at=utc_now(),
        points={
            "points": body.points,
            "payoff_tables": body.payoff_tables,
            "recommendations": body.recommendations,
        },
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return {"run_id": run.id, "message": "Pareto sweep saved successfully"}


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
        created_at=utc_now(),
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
    valid_categories = {"rooms", "labs", "students", "faculty", "resource"}
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


@router.get("/pareto/runs")
def list_pareto_runs(session: Session = Depends(get_session), _=Depends(require_faculty)):
    runs = session.exec(select(ParetoRun).order_by(ParetoRun.created_at.desc())).all()
    results = []
    for r in runs:
        pts = []
        if isinstance(r.points, list):
            pts = r.points
        elif isinstance(r.points, dict):
            pts = r.points.get("points", [])
        results.append({
            "id": r.id,
            "label": r.label,
            "branch_ids": r.branch_ids,
            "status": r.status,
            "points_count": len(pts),
            "created_at": (r.created_at.isoformat() + "Z") if r.created_at else None,
        })
    return results


@router.get("/pareto/{run_id}")
def get_pareto_run(run_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    run = session.get(ParetoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"pareto run {run_id} not found")
    points_data = run.points
    if isinstance(points_data, list):
        pts = points_data
        payoff_tables = {}
        recommendations = {}
    elif isinstance(points_data, dict):
        pts = points_data.get("points", [])
        payoff_tables = points_data.get("payoff_tables", {})
        recommendations = points_data.get("recommendations", {})
    else:
        pts = []
        payoff_tables = {}
        recommendations = {}

    return {
        "id": run.id,
        "label": run.label,
        "branch_ids": run.branch_ids,
        "status": run.status,
        "time_limit_s": run.time_limit_s,
        "sweep_points": run.sweep_points,
        "points": pts,
        "payoff_tables": payoff_tables,
        "recommendations": recommendations,
        "error": run.error,
        "created_at": (run.created_at.isoformat() + "Z") if run.created_at else None,
    }
