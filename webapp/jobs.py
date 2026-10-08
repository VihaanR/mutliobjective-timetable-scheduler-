"""Background job runner for `POST /api/runs` (design.md §5.3, CLAUDE.md §11).

`run_generation` is handed to Starlette's `BackgroundTasks`, which executes it in a threadpool
*after* the request has already returned its response — so it cannot reuse the request's DB
session (that session is closed by then) and opens its own. It must never raise: a solver
exception is caught and recorded on the run row (`status="failed"`, `error=...`) so the SPA can
show it, rather than being lost to a background-thread traceback that nothing observes.
"""
from __future__ import annotations

from sqlmodel import Session, select

from dataclasses import asdict

from engine.io_json import problem_from_dict, solution_to_dict
from engine.pipeline import PipelineConfig, run_pipeline
from engine.scoring import score
from engine.solvers import SOLVERS
from engine.view import solution_to_grids
from webapp.db import get_engine
from webapp.grid_meta import annotate_grids
from webapp.models_db import TimetableRun


def _stage_reports_from(result) -> list[dict]:
    """Same shape the legacy `/api/generate` handler in server.py builds — reused here so pipeline
    reports render consistently whether they came from the old showcase endpoint or this job."""
    return [
        {
            "name": rep.name,
            "status": rep.solver_status,
            "wall_clock_s": round(rep.wall_clock_s, 1),
            "hard": rep.hard_violations,
            "soft": round(rep.soft_cost, 1),
            "best_hard": rep.running_best_hard,
            "best_soft": round(rep.running_best_soft, 1),
            "improved": rep.improved,
        }
        for rep in result.reports
    ]


from engine.judge import evaluate_aqwi


def _solve_single(run: TimetableRun, problem, solver_name: str, time_limit: float, opt_mode: str, seed: int = 42):
    if solver_name == "pipeline":
        config = PipelineConfig(
            cpsat_time_limit_s=time_limit,
            ga_time_limit_s=min(time_limit, 30),
            mip_time_limit_s=min(time_limit, 60),
        )
        result = run_pipeline(problem, config)
        return result.final, _stage_reports_from(result), result.total_wall_clock_s
    elif solver_name == "cpsat":
        from engine.solvers.cpsat import CPSATSolver
        solver = CPSATSolver(optimization_mode=opt_mode)
        solution = solver.solve(
            problem,
            time_limit_s=time_limit,
            extra_solver_params={"random_seed": seed},
        )
        stage_reports = None
        if solution.extra_data and "history" in solution.extra_data:
            stage_reports = [
                {
                    "name": f"Adaptive Iteration {item['iteration']}",
                    "status": item.get("solver_status", "FEASIBLE"),
                    "wall_clock_s": round(item.get("solve_time", 0.0), 1),
                    "hard": 0,
                    "soft": round(item.get("soft_cost", 0.0), 1),
                    "best_hard": 0,
                    "best_soft": round(item.get("best_score", {}).get("soft_cost", item.get("soft_cost", 0.0)), 1),
                    "improved": item.get("improved", False),
                }
                for item in solution.extra_data["history"]
            ]
        return solution, stage_reports, solution.wall_clock_seconds
    else:
        solver_cls = SOLVERS[solver_name]
        try:
            solution = solver_cls(seed=seed).solve(problem, time_limit_s=time_limit)
        except TypeError:
            solution = solver_cls().solve(problem, time_limit_s=time_limit)
        return solution, None, solution.wall_clock_seconds


def run_generation(run_id: int) -> None:
    """The background worker: load the run, solve, store the result. Opens its OWN session."""
    with Session(get_engine()) as session:
        run = session.get(TimetableRun, run_id)
        if run is None:
            return  # nothing to do; the row vanished (shouldn't happen in practice)

        run.status = "running"
        session.add(run)
        session.commit()

        try:
            problem = problem_from_dict(run.problem_snapshot)
            opt_mode = (run.division_meta or {}).get("_optimization_mode", "baseline")
            num_candidates = max(1, getattr(run, "num_candidates", 1) or 1)

            # Allocate time per candidate budget
            per_candidate_time = max(5.0, run.time_limit / num_candidates) if num_candidates > 1 else run.time_limit

            candidate_entries = []
            total_wall_clock = 0.0
            primary_stage_reports = None

            for i in range(num_candidates):
                seed = 42 + i * 10007
                sol, stage_reps, wall = _solve_single(
                    run, problem, run.solver, per_candidate_time, opt_mode, seed=seed
                )
                total_wall_clock += wall
                if i == 0:
                    primary_stage_reports = stage_reps

                if sol.status in {"FEASIBLE", "OPTIMAL", "PARTIAL"}:
                    aqwi = evaluate_aqwi(sol, problem)
                    candidate_entries.append({
                        "solution": sol,
                        "report": aqwi,
                        "wall_clock": wall,
                    })

            if not candidate_entries:
                run.status = "failed"
                run.error = (
                    f"{run.solver} failed to find any feasible candidate timetables "
                    "within the time limit."
                )
                run.wall_clock = total_wall_clock
                session.add(run)
                session.commit()
                return

            # Rank candidate solutions by AQWI quality score (highest first), breaking ties by lower total penalty
            candidate_entries.sort(
                key=lambda c: (-c["report"].quality_score, c["report"].total_penalty)
            )

            # Update rank numbers in reports
            serialized_reports = []
            serialized_solutions = []
            for rank_idx, entry in enumerate(candidate_entries, start=1):
                entry["report"].rank = rank_idx
                rep_dict = entry["report"].to_dict()
                serialized_reports.append(rep_dict)
                serialized_solutions.append(solution_to_dict(entry["solution"]))

            # Select top-ranked candidate as the default active timetable
            best_sol = candidate_entries[0]["solution"]
            sc = score(best_sol, problem)
            grids = annotate_grids(solution_to_grids(best_sol, problem), run.division_meta or {})

            run.solution = serialized_solutions[0]
            run.grids = grids
            run.stage_reports = primary_stage_reports
            run.num_candidates = num_candidates
            run.candidate_solutions = serialized_solutions
            run.judge_reports = serialized_reports
            run.selected_candidate_idx = 0
            run.hard = sc.hard_violations
            run.soft = sc.soft_cost
            run.wall_clock = total_wall_clock
            run.status = "done"
            session.add(run)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
            run.status = "failed"
            run.error = str(exc)
            session.add(run)
            session.commit()


def sweep_stale_running(session: Session) -> int:
    """Startup recovery: mark orphaned "queued"/"running" platform runs as failed.

    BackgroundTasks live only in this process's threadpool, so a "running" OR "queued"
    TimetableRun found at startup implies the process died mid-solve or between inserting
    the queued row and the background task flipping it to running.

    Leaving these rows untouched would permanently block new runs via has_active_run().
    Returns the number of runs swept."""
    stale = session.exec(
        select(TimetableRun).where(TimetableRun.status.in_(["running", "queued"]))
    ).all()
    for run in stale:
        run.status = "failed"
        run.error = "orphaned by restart"
        session.add(run)
    if stale:
        session.commit()
    return len(stale)


def has_active_run(session: Session) -> bool:
    """True if a run is queued or running — the single-flight guard for `POST /api/runs`."""
    active = session.exec(
        select(TimetableRun).where(TimetableRun.status.in_(["queued", "running"]))
    ).first()
    return active is not None
