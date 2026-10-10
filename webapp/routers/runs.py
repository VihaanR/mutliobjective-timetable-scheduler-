"""Generate job + poll + history (design.md §5.3, CLAUDE.md §11).

`POST /api/runs` builds and validates the `ProblemInstance` snapshot synchronously (the readiness
gate is cheap - no solving happens yet), stores it as `problem_snapshot`, then hands the actual
solve off to `BackgroundTasks` (`webapp.jobs.run_generation`) so the request returns immediately
with a `run_id` the SPA polls via `GET /api/runs/{id}`. Single-flight: solves cover the whole
institution's shared rooms/faculty, so only one run may be queued/running at a time.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from engine.disruption import affected_slots_for_day, replan
from engine.export import export_pdf, export_xlsx
from engine.io_json import problem_from_dict, problem_to_dict, solution_from_dict, solution_to_dict
from engine.models import Assignment, Solution
from engine.pipeline import PipelineConfig, run_pipeline
from engine.scoring import score
from engine.solvers import SOLVERS
from engine.view import solution_to_grids
from webapp.auth import require_faculty
from webapp.db import get_session
from webapp.grid_meta import annotate_grids
from webapp.jobs import has_active_run, run_generation
from webapp.models_db import Branch, Faculty, TimetableRun, ManualEdit
from webapp.problem_builder import build_division_meta, readiness, spans_multiple_branches

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MEDIA_TYPE = "application/pdf"

router = APIRouter(prefix="/api", tags=["runs"])


_DAY_MAP = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4}


class VisitingFacultyOverride(BaseModel):
    id: Optional[int] = None
    code: Optional[str] = None
    name: Optional[str] = None
    is_visiting: bool = True
    visiting_days: list[int] = []
    visiting_start_time: str = ""
    visiting_end_time: str = ""

    @field_validator("visiting_days", mode="before")
    @classmethod
    def _coerce_days(cls, v):
        if not v:
            return []
        res = []
        for item in v:
            if isinstance(item, int):
                res.append(item)
            elif isinstance(item, str):
                dl = item.lower().strip()
                if dl in _DAY_MAP:
                    res.append(_DAY_MAP[dl])
                elif dl.isdigit():
                    res.append(int(dl))
        return res


class GenerateRequest(BaseModel):
    solver: str = "cpsat"
    time_limit: float = 30.0
    label: str = ""
    branch_ids: list[int] | None = None
    optimization_mode: str = "baseline"
    num_candidates: int = 1
    visiting_faculty_overrides: list[VisitingFacultyOverride] | None = None


class CompareRequest(BaseModel):
    time_limit: float = 30.0
    label: str = ""
    branch_ids: list[int] | None = None
    solvers: list[str] | None = None


@router.post("/runs")
def generate(
    body: GenerateRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    # Cheapest check first: the single-flight guard is one indexed row lookup, so it must reject
    # a busy platform (409) before we pay for the expensive readiness build below (which assembles
    # and validates the whole-institution ProblemInstance).
    if has_active_run(session):
        raise HTTPException(status_code=409, detail="a run is already in progress")

    if body.solver != "pipeline" and body.solver not in SOLVERS:
        raise HTTPException(status_code=400, detail=f"unknown solver {body.solver!r}")

    # Persist any visiting faculty updates immediately so problem builder reflects them
    if body.visiting_faculty_overrides:
        for ov in body.visiting_faculty_overrides:
            fac = None
            if ov.id is not None:
                fac = session.get(Faculty, ov.id)
            elif ov.code:
                fac = session.exec(select(Faculty).where(Faculty.code == ov.code)).first()
            if fac:
                fac.is_visiting = ov.is_visiting
                fac.visiting_days = ov.visiting_days
                fac.visiting_start_time = ov.visiting_start_time
                fac.visiting_end_time = ov.visiting_end_time
                session.add(fac)
        session.commit()

    # A selection spanning several branches must use branch-qualified engine ids, or the years'
    # identically-named divisions (every year has a D1) collapse into one. Decided from the DB, not
    # from the request, so `branch_ids=null` (all years) is handled the same as an explicit list.
    qualify_ids = spans_multiple_branches(session, body.branch_ids)

    problem, issues = readiness(session, body.branch_ids, qualify_ids=qualify_ids)
    if problem is None or issues:
        raise HTTPException(status_code=400, detail=issues)

    covered = body.branch_ids if body.branch_ids is not None else [
        b.id for b in session.exec(select(Branch)).all()
    ]

    div_meta = build_division_meta(session, body.branch_ids, qualify_ids=qualify_ids)
    if body.optimization_mode:
        div_meta["_optimization_mode"] = body.optimization_mode

    run = TimetableRun(
        status="queued",
        solver=body.solver,
        time_limit=body.time_limit,
        label=body.label,
        problem_snapshot=problem_to_dict(problem),
        branch_ids=list(covered),
        division_meta=div_meta,
        num_candidates=max(1, body.num_candidates),
        selected_candidate_idx=0,
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    background.add_task(run_generation, run.id)
    return {"run_id": run.id}


def _run_solver_compare(problem, solver_name: str, time_limit: float,
                        division_meta: dict | None = None) -> dict:
    if solver_name == "pipeline":
        config = PipelineConfig(
            cpsat_time_limit_s=time_limit,
            ga_time_limit_s=min(time_limit, 30),
            mip_time_limit_s=min(time_limit, 60),
        )
        result = run_pipeline(problem, config)
        solution = result.final
        stage_reports = [{
            "name": rep.name,
            "status": rep.solver_status,
            "wall_clock_s": round(rep.wall_clock_s, 1),
            "hard": rep.hard_violations,
            "soft": round(rep.soft_cost, 1),
            "best_hard": rep.running_best_hard,
            "best_soft": round(rep.running_best_soft, 1),
            "improved": rep.improved,
        } for rep in result.reports]
        wall_clock_s = result.total_wall_clock_s
        notes = result.notes
    else:
        solver = SOLVERS[solver_name]()
        solution = solver.solve(problem, time_limit_s=time_limit)
        stage_reports = None
        wall_clock_s = solution.wall_clock_seconds
        notes = []

    sc = score(solution, problem)
    return {
        "solver": solver_name,
        "final_solver": solution.solver_name,
        "status": solution.status,
        "hard_violations": sc.hard_violations,
        "soft_cost": round(sc.soft_cost, 1),
        "wall_clock_s": round(wall_clock_s, 1),
        "stage_reports": stage_reports,
        "notes": notes,
        "grids": annotate_grids(solution_to_grids(solution, problem), division_meta or {}),
    }


@router.post("/compare")
def compare(body: CompareRequest, session: Session = Depends(get_session), _=Depends(require_faculty)):
    qualify_ids = spans_multiple_branches(session, body.branch_ids)
    problem, issues = readiness(session, body.branch_ids, qualify_ids=qualify_ids)
    if problem is None or issues:
        raise HTTPException(status_code=400, detail=issues)
    division_meta = build_division_meta(session, body.branch_ids, qualify_ids=qualify_ids)

    solvers = body.solvers or ["pipeline", "cpsat", "greedy"]
    unique_solvers: list[str] = []
    for solver_name in solvers:
        if solver_name not in unique_solvers:
            unique_solvers.append(solver_name)

    if len(unique_solvers) == 0:
        raise HTTPException(status_code=400, detail="compare mode needs at least one solver")

    invalid = [solver_name for solver_name in unique_solvers if solver_name != "pipeline" and solver_name not in SOLVERS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"unknown solver(s): {', '.join(invalid)}")

    results = [_run_solver_compare(problem, solver_name, body.time_limit, division_meta)
               for solver_name in unique_solvers]
    best_index = min(range(len(results)), key=lambda i: (results[i]["hard_violations"], results[i]["soft_cost"]))
    return {
        "label": body.label,
        "solvers": unique_solvers,
        "best_index": best_index,
        "best_solver": results[best_index]["solver"],
        "results": results,
    }


@router.get("/runs")
def list_runs(session: Session = Depends(get_session), _=Depends(require_faculty)):
    runs = session.exec(select(TimetableRun).order_by(TimetableRun.created_at.desc())).all()
    return [
        {
            "id": r.id,
            "label": r.label,
            "solver": r.solver,
            "status": r.status,
            "hard": r.hard,
            "soft": r.soft,
            "created_at": (r.created_at.isoformat() + "Z") if r.created_at else None,
            "branch_ids": r.branch_ids or [],
        }
        for r in runs
    ]


@router.get("/runs/{run_id}")
def get_run(run_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    run = session.get(TimetableRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return {
        "id": run.id,
        "status": run.status,
        "solver": run.solver,
        "label": run.label,
        "hard": run.hard,
        "soft": run.soft,
        "wall_clock": run.wall_clock,
        "grids": run.grids,
        "stage_reports": run.stage_reports,
        "num_candidates": run.num_candidates,
        "candidate_solutions": run.candidate_solutions or [],
        "judge_reports": run.judge_reports or [],
        "selected_candidate_idx": run.selected_candidate_idx,
        "error": run.error,
        "created_at": (run.created_at.isoformat() + "Z") if run.created_at else None,
        "branch_ids": run.branch_ids or [],
    }


class SelectCandidateRequest(BaseModel):
    candidate_idx: int


@router.post("/runs/{run_id}/select-candidate")
def select_candidate(
    run_id: int,
    body: SelectCandidateRequest,
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    run = session.get(TimetableRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    if run.status != "done":
        raise HTTPException(status_code=400, detail="run is not completed")

    candidates = run.candidate_solutions or []
    if not (0 <= body.candidate_idx < len(candidates)):
        raise HTTPException(
            status_code=400,
            detail=f"candidate_idx {body.candidate_idx} out of range (0..{len(candidates)-1})",
        )

    problem = problem_from_dict(run.problem_snapshot)
    sol_dict = candidates[body.candidate_idx]
    sol = solution_from_dict(sol_dict)
    grids = annotate_grids(solution_to_grids(sol, problem), run.division_meta or {})
    sc = score(sol, problem)

    run.selected_candidate_idx = body.candidate_idx
    run.solution = sol_dict
    run.grids = grids
    run.hard = sc.hard_violations
    run.soft = sc.soft_cost

    if run.judge_reports:
        updated_reps = []
        for idx, rep in enumerate(run.judge_reports):
            rep_copy = dict(rep)
            is_act = (idx == body.candidate_idx)
            is_rec = bool(rep_copy.get("is_recommended", False))
            rep_copy["is_active"] = is_act
            rep_copy["selection_status"] = "recommended" if (is_rec and is_act) else ("active" if is_act else ("recommended" if is_rec else "alternative"))
            updated_reps.append(rep_copy)
        run.judge_reports = updated_reps

    session.add(run)
    session.commit()
    session.refresh(run)

    return {
        "status": "ok",
        "selected_candidate_idx": run.selected_candidate_idx,
        "hard": run.hard,
        "soft": run.soft,
        "grids": run.grids,
    }


def _get_done_run(run_id: int, session: Session) -> TimetableRun:
    """Shared lookup for the export routes: 404 if the run doesn't exist, 409 if it hasn't
    finished solving yet (nothing to export)."""
    run = session.get(TimetableRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    if run.status != "done":
        raise HTTPException(status_code=409, detail=f"run {run_id} is not done (status={run.status!r})")
    return run


def _export_response(run: TimetableRun, export_fn, suffix: str, media_type: str) -> FileResponse:
    """Shared body for the xlsx/pdf export routes. Reconstructs the problem+solution from the
    already-fetched `run`'s stored snapshot, writes them to a fresh temp file via `export_fn`, and
    streams it back. If `export_fn` raises, the just-created (still-empty) temp file is unlinked
    before re-raising — otherwise the FileResponse (and its unlink-after-streaming background task)
    is never constructed, and the empty temp file leaks on every failed export."""
    problem = problem_from_dict(run.problem_snapshot)
    solution = solution_from_dict(run.solution)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    try:
        export_fn(solution, problem, tmp.name)
    except Exception:
        os.unlink(tmp.name)
        raise
    return FileResponse(
        tmp.name,
        filename=f"timetable_run_{run.id}{suffix}",
        media_type=media_type,
        background=BackgroundTask(os.unlink, tmp.name),  # delete the temp file after streaming
    )


@router.get("/runs/{run_id}/export.xlsx")
def export_run_xlsx(run_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    run = _get_done_run(run_id, session)
    return _export_response(run, export_xlsx, ".xlsx", XLSX_MEDIA_TYPE)


@router.get("/runs/{run_id}/export.pdf")
def export_run_pdf(run_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    run = _get_done_run(run_id, session)
    return _export_response(run, export_pdf, ".pdf", PDF_MEDIA_TYPE)


class AdjustRunRequest(BaseModel):
    constraint_query: Optional[str] = None
    day: int                          # 0=Mon .. 4=Fri — the disrupted weekday
    from_period: int | None = None    # None => whole-day holiday; N => rain from period N onward
    reason: str = ""                  # "holiday" | "rain" | free text (annotation only)
    solver: str = "cpsat"             # the re-solve is a full-week solve now — solver/budget matter
    time_limit_s: float = 60.0
    # Owner-decided escape hatch (design.md §7): when a disruption is over-constrained (a whole-day
    # holiday removes ~20% of weekly capacity, which no solver can absorb without breaking some
    # other day's 6-8h cap), the ADMIN — not the algorithm — names extra days to exempt from the
    # day-shaped hard rules so the overflow has somewhere legal to land. Default: only the
    # disrupted day, matching the previous behavior.
    extra_relaxed_days: list[int] = []


class MoveSessionRequest(BaseModel):
    # session_id is the engine's string id (e.g. "D1_MATH_TH_0"), carried as-is through the
    # frontend's data-session-id attribute and sent back here. Pydantic will coerce to str.
    session_id: str
    target_day: int      # 0=Mon .. 4=Fri
    target_period: int   # 0-based period index within the day
    target_room_id: str  # engine Room.id string (e.g. "R1", "LAB2")


@router.post("/runs/{run_id}/move-session")
def move_session(
    run_id: int,
    body: MoveSessionRequest,
    db: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    """Drag-and-drop: move a single session to a new (day, period, room) slot.

    Validates the move against hard constraints (slot exists, room type matches, no faculty
    double-book at the target slot), applies the move to a copy of the stored solution, rebuilds
    the display grids, persists a ManualEdit audit row, and writes the updated grids back to the
    run so subsequent polls/loads see the edited timetable without a full re-solve.

    Returns the full updated grids dict so the frontend can re-render in-place without a page
    reload.
    """
    run = _get_done_run(run_id, db)
    if run.solution is None:
        raise HTTPException(status_code=409, detail="run has no stored solution")

    problem = problem_from_dict(run.problem_snapshot)
    baseline = solution_from_dict(run.solution)

    # ── 1. Find the target time slot ──────────────────────────────────────────────────────────
    target_slot = next(
        (t for t in problem.time_slots if t.day == body.target_day and t.period == body.target_period),
        None,
    )
    if target_slot is None:
        raise HTTPException(
            status_code=400,
            detail=f"No time slot exists for day={body.target_day}, period={body.target_period}",
        )

    # ── 2. Find the session requirement to move ───────────────────────────────────────────────
    from engine.models import expand_requirements
    requirements = {r.id: r for r in expand_requirements(problem)}
    req = requirements.get(body.session_id)
    if req is None:
        raise HTTPException(
            status_code=400,
            detail=f"Session id {body.session_id!r} not found in this run's problem",
        )
    if req.is_break:
        raise HTTPException(status_code=400, detail="Break sessions cannot be moved")
    if req.fixed_time_slot_id is not None:
        raise HTTPException(
            status_code=400, detail="This session is pinned to a fixed slot and cannot be moved"
        )

    # ── 3. Validate the target room ──────────────────────────────────────────────────────────
    rooms_by_id = problem.room_by_id()
    target_room = rooms_by_id.get(body.target_room_id)
    if target_room is None:
        raise HTTPException(status_code=400, detail=f"Room {body.target_room_id!r} not found")

    # Check room type compatibility
    required_room_type = getattr(req, "room_type", "classroom")
    if required_room_type not in ("none", "") and target_room.room_type != required_room_type:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Session {body.session_id!r} requires a {required_room_type!r} but "
                f"room {body.target_room_id!r} is a {target_room.room_type!r}"
            ),
        )

    # ── 4. Build slot occupancy map from the current solution ─────────────────────────────────
    # Maps (slot_id) -> set of faculty_ids already scheduled there (for double-book check)
    # Maps (slot_id, room_id) -> session_id already occupying that cell (for room conflict)
    faculty_at_slot: dict[int, set[str]] = {}
    room_at_slot: dict[tuple[int, str], str] = {}  # (slot_id, room_id) -> session_id

    by_session: dict[str, Assignment] = baseline.assignment_by_session()
    for a in baseline.assignments:
        r = requirements.get(a.session_id)
        if r is None or r.is_break:
            continue
        # A lab (duration=2) occupies two consecutive slots
        for k in range(r.duration_slots):
            slot_in_run = next(
                (t for t in problem.time_slots if t.id == a.time_slot_id + k), None
            )
            sid = a.time_slot_id + k if slot_in_run else a.time_slot_id
            if r.faculty_id:
                faculty_at_slot.setdefault(sid, set()).add(r.faculty_id)
            cell_key = (a.time_slot_id + k, a.room_id)
            if cell_key not in room_at_slot:
                room_at_slot[cell_key] = a.session_id

    # Current assignment for the session being moved (we'll free it before checking conflicts)
    current_assignment = by_session.get(body.session_id)
    if current_assignment is None:
        raise HTTPException(
            status_code=409,
            detail=f"Session {body.session_id!r} has no assignment in the stored solution",
        )

    # Remove the session's current occupancy from the maps before conflict-checking the target
    current_req = requirements[body.session_id]
    for k in range(current_req.duration_slots):
        old_sid = current_assignment.time_slot_id + k
        if current_req.faculty_id and old_sid in faculty_at_slot:
            faculty_at_slot[old_sid].discard(current_req.faculty_id)
        old_room_key = (old_sid, current_assignment.room_id)
        if room_at_slot.get(old_room_key) == body.session_id:
            del room_at_slot[old_room_key]

    # ── 5. Conflict checks at the target slot ────────────────────────────────────────────────
    for k in range(req.duration_slots):
        check_slot_id = target_slot.id + k  # consecutive slots for labs
        # Faculty double-booking
        if req.faculty_id and req.faculty_id in faculty_at_slot.get(check_slot_id, set()):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Faculty {req.faculty_id!r} is already scheduled in "
                    f"day={body.target_day}, period={body.target_period + k}"
                ),
            )
        # Room already occupied by another session
        occupant = room_at_slot.get((check_slot_id, body.target_room_id))
        if occupant is not None and occupant != body.session_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Room {body.target_room_id!r} is already occupied at "
                    f"day={body.target_day}, period={body.target_period + k} "
                    f"by session {occupant!r}"
                ),
            )
        # Division already has a session at this slot (can't be in two places at once)
        for a2 in baseline.assignments:
            r2 = requirements.get(a2.session_id)
            if r2 is None or r2.is_break or r2.division_id != req.division_id or a2.session_id == body.session_id:
                continue
            for k2 in range(r2.duration_slots):
                if a2.time_slot_id + k2 == check_slot_id:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Division {req.division_id!r} already has session {a2.session_id!r} "
                            f"at day={body.target_day}, period={body.target_period + k}"
                        ),
                    )

    # ── 6. Apply the move — create a new solution with the updated assignment ─────────────────
    new_assignments = [
        Assignment(
            session_id=a.session_id,
            time_slot_id=target_slot.id,
            room_id=body.target_room_id,
        )
        if a.session_id == body.session_id
        else a
        for a in baseline.assignments
    ]
    new_solution = Solution(
        assignments=new_assignments,
        solver_name=baseline.solver_name,
        wall_clock_seconds=baseline.wall_clock_seconds,
        objective_value=baseline.objective_value,
        status=baseline.status,
    )

    # ── 7. Rebuild grids and score ────────────────────────────────────────────────────────────
    from engine.io_json import solution_to_dict
    new_grids = annotate_grids(
        solution_to_grids(new_solution, problem), run.division_meta or {}
    )
    sc = score(new_solution, problem)

    # ── 8. Persist: ManualEdit audit row + updated run fields ─────────────────────────────────
    edit = ManualEdit(
        run_id=run_id,
        session_id=body.session_id,
        from_slot_id=current_assignment.time_slot_id,
        from_room_id=current_assignment.room_id,
        to_slot_id=target_slot.id,
        to_room_id=body.target_room_id,
    )
    db.add(edit)

    # Mutate the run's stored solution + grids so future loads see the edit
    from sqlalchemy import text as sa_text
    import json as _json
    db.exec(  # type: ignore[attr-defined]
        sa_text(
            "UPDATE timetablerun SET solution = :sol, grids = :grids, "
            "hard = :hard, soft = :soft WHERE id = :rid"
        ).bindparams(
            sol=_json.dumps(solution_to_dict(new_solution)),
            grids=_json.dumps(new_grids),
            hard=sc.hard_violations,
            soft=round(sc.soft_cost, 1),
            rid=run_id,
        )
    )
    db.commit()

    return {
        "status": "ok",
        "moved_session": body.session_id,
        "from_slot_id": current_assignment.time_slot_id,
        "from_room_id": current_assignment.room_id,
        "to_slot_id": target_slot.id,
        "to_room_id": body.target_room_id,
        "hard": sc.hard_violations,
        "soft": round(sc.soft_cost, 1),
        "grids": new_grids,
    }

@router.post("/runs/{run_id}/adjust")
def adjust_run(run_id: int, body: AdjustRunRequest, session: Session = Depends(get_session),
               _=Depends(require_faculty)):
    """Disruption re-plan against a stored run (design.md §7). Loads the run's problem snapshot +
    baseline solution, blocks the disrupted window, and re-solves the whole week warm-started from
    the baseline — so a rained-out session is rescheduled elsewhere in the week rather than dropped.
    Anything that genuinely cannot be placed is reported by name in `unplaced_sessions`. The stored
    run is never mutated (stateless overlay)."""
    run = _get_done_run(run_id, session)
    problem = problem_from_dict(run.problem_snapshot)
    baseline = solution_from_dict(run.solution)
    if not (0 <= body.day < problem.days_per_week):
        raise HTTPException(status_code=400, detail=f"day must be 0..{problem.days_per_week - 1}")
    if body.solver != "pipeline" and body.solver not in SOLVERS:
        raise HTTPException(status_code=400, detail=f"unknown solver {body.solver!r}")
    bad_days = [d for d in body.extra_relaxed_days if not (0 <= d < problem.days_per_week)]
    if bad_days:
        raise HTTPException(status_code=400,
                            detail=f"extra_relaxed_days must be 0..{problem.days_per_week - 1}")

    affected = affected_slots_for_day(problem, body.day, from_period=body.from_period)
    result = replan(problem, baseline, affected, constraint_query=body.constraint_query,
                    relaxed_days=frozenset({body.day} | set(body.extra_relaxed_days)),
                    time_limit_s=body.time_limit_s, solver=body.solver)

    grids = annotate_grids(solution_to_grids(result.solution, problem), run.division_meta or {})
    moved = [
        {
            "session_id": m.session_id,
            "from_slot": m.from_slot_id, "to_slot": m.to_slot_id,
            "from_room": m.from_room_id, "to_room": m.to_room_id,
            "dropped": m.to_slot_id is None,
        }
        for m in result.moved_sessions
    ]
    scope = "whole day (holiday)" if body.from_period is None else f"from period {body.from_period} (rain)"
    return {
        "run_id": run_id,
        "disrupted_day": DAY_NAMES[body.day] if body.day < len(DAY_NAMES) else str(body.day),
        "scope": scope,
        "reason": body.reason,
        "solver": body.solver,
        "relaxed_days": sorted(result.relaxed_days),
        "affected_slot_ids": sorted(affected),
        "moved_count": len([m for m in moved if not m["dropped"]]),
        "dropped_count": len(result.dropped_session_ids),
        "dropped_session_ids": result.dropped_session_ids,
        "unplaced_sessions": result.unplaced_sessions,
        "hard_violations": result.hard_violations,
        "conflict_violations": result.conflict_violations,
        "is_valid": result.is_valid,
        "soft_cost": round(result.soft_cost, 1),
        "notes": result.notes,
        "moved": moved,
        "grids": grids,
    }


@router.get("/readiness")
def get_readiness(
    branch_ids: list[int] | None = Query(default=None),
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    qualify_ids = spans_multiple_branches(session, branch_ids)
    problem, issues = readiness(session, branch_ids, qualify_ids=qualify_ids)
    return {"ready": problem is not None and not issues, "issues": issues}
