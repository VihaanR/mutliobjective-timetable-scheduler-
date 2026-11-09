"""Background job runner for `POST /api/runs` (design.md §5.3, CLAUDE.md §11).

`run_generation` is handed to Starlette's `BackgroundTasks`, which executes it in a threadpool
*after* the request has already returned its response — so it cannot reuse the request's DB
session (that session is closed by then) and opens its own. It must never raise: a solver
exception is caught and recorded on the run row (`status="failed"`, `error=...`) so the SPA can
show it, rather than being lost to a background-thread traceback that nothing observes.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

from sqlmodel import Session, select

from dataclasses import asdict

from engine.io_json import problem_from_dict, solution_from_dict, solution_to_dict
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


from engine.judge import evaluate_aqwi, compute_pareto_front, AQWIReport


def _get_warm_start(problem, session: Session | None = None) -> Solution | None:
    from engine.models import expand_requirements, Solution
    req_ids = {r.id for r in getattr(problem, "requirements", []) or []}
    if not req_ids:
        req_ids = {r.id for r in expand_requirements(problem)}

    candidates_to_try: list[Solution] = []

    if session is not None:
        try:
            recent_done = session.exec(
                select(TimetableRun).where(TimetableRun.status == "done", TimetableRun.hard == 0).order_by(TimetableRun.id.desc())
            ).all()
            for r in recent_done:
                sol_raw = r.solution or (r.candidate_solutions[0] if (r.candidate_solutions and len(r.candidate_solutions) > 0) else None)
                if sol_raw:
                    sol_dict = sol_raw if isinstance(sol_raw, dict) else json.loads(sol_raw)
                    candidates_to_try.append(solution_from_dict(sol_dict))
        except Exception:
            pass

    for ref_p in [
        Path(__file__).resolve().parent.parent / "data" / "reference" / "initial_even_semesters_run.json",
        Path("data/reference/initial_even_semesters_run.json"),
    ]:
        try:
            if ref_p.exists():
                with open(ref_p, "r", encoding="utf-8") as f:
                    ref_dict = json.load(f)
                if "solution" in ref_dict:
                    candidates_to_try.append(solution_from_dict(ref_dict["solution"]))
                elif "candidate_solutions" in ref_dict and len(ref_dict["candidate_solutions"]) > 0:
                    candidates_to_try.append(solution_from_dict(ref_dict["candidate_solutions"][0]))
        except Exception:
            pass

    best_partial = None
    best_partial_count = 0

    for cand in candidates_to_try:
        try:
            sc = score(cand, problem)
            if sc.hard_violations == 0:
                return cand
        except Exception:
            pass

        matching_assignments = [a for a in cand.assignments if a.session_id in req_ids]
        if len(matching_assignments) > best_partial_count and len(matching_assignments) >= len(req_ids) * 0.5:
            best_partial_count = len(matching_assignments)
            best_partial = Solution(assignments=matching_assignments, status="FEASIBLE")

    if best_partial is not None:
        return best_partial

    return None


def _solve_single(run: TimetableRun, problem, solver_name: str, time_limit: float, opt_mode: str, seed: int = 42, session: Session | None = None):
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
        warm = _get_warm_start(problem, session=session)
        workers = int(os.environ.get("CP_SAT_WORKERS", "8"))
        solution = solver.solve(
            problem,
            time_limit_s=time_limit,
            warm_start=warm,
            extra_solver_params={"random_seed": seed, "num_search_workers": workers},
        )
        stage_reports = None
        if solution.extra_data and "history" in solution.extra_data:
            stage_reports = [
                {
                    "name": f"Adaptive Iteration {item['iteration']}",
                    "status": item.get("solver_status", "FEASIBLE"),
                    "wall_clock_s": round(item.get("solve_time", 0.0), 1),
                    "hard": item.get("hard_violations", 0),
                    "soft": round(item.get("weighted_soft_cost", item.get("soft_cost", 0.0)), 1),
                    "best_hard": item.get("hard_violations", 0),
                    "best_soft": round(item.get("weighted_soft_cost", item.get("soft_cost", 0.0)), 1),
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
            is_large_solve = len(problem.divisions) >= 6
            if os.environ.get("VERCEL"):
                # On serverless, cap to 1 candidate and 18s to guarantee sub-20s HTTP response
                num_candidates = 1
                per_candidate_time = min(18.0, max(5.0, run.time_limit))
            else:
                min_floor = 95.0 if is_large_solve else 10.0
                per_candidate_time = max(min_floor, run.time_limit / num_candidates) if num_candidates > 1 else max(run.time_limit, min_floor)

            candidate_entries = []
            total_wall_clock = 0.0
            primary_stage_reports = None

            for i in range(num_candidates):
                seed = 42 + i * 10007
                # For large institutional runs, if Candidate 0 is already feasible, candidate generation succeeded
                sol, stage_reps, wall = _solve_single(
                    run, problem, run.solver, per_candidate_time, opt_mode, seed=seed, session=session
                )
                total_wall_clock += wall
                if i == 0:
                    primary_stage_reports = stage_reps

                if sol.status in {"FEASIBLE", "OPTIMAL", "PARTIAL"}:
                    sc = score(sol, problem)
                    is_eligible = (sc.hard_violations == 0)
                    aqwi = evaluate_aqwi(
                        sol,
                        problem,
                        candidate_id=i,
                        hard_violations=sc.hard_violations,
                    )
                    candidate_entries.append({
                        "candidate_id": i,
                        "solution": sol,
                        "score_result": sc,
                        "report": aqwi,
                        "wall_clock": wall,
                        "is_eligible": is_eligible,
                    })
                elif is_large_solve and candidate_entries and any(c["is_eligible"] for c in candidate_entries):
                    # For institution-scale problems, do not fail the run if secondary candidate time-out occurs
                    break
                import gc; gc.collect()

            if not candidate_entries:
                run.status = "failed"
                scale_hint = ""
                if hasattr(problem, "divisions") and len(problem.divisions) >= 4:
                    scale_hint = (
                        f" (Institution scale: {len(problem.divisions)} divisions, {len(problem.courses)} courses). "
                        "Recommendation: Increase time limit to >= 120s for Adaptive CP-SAT convergence."
                    )
                run.error = (
                    f"{run.solver} failed to find any feasible candidate timetables "
                    f"within the time limit.{scale_hint}"
                )
                run.wall_clock = total_wall_clock
                session.add(run)
                session.commit()
                return

            # Hard-constraint validation as the first stage gate:
            # Only candidates satisfying all mandatory hard constraints enter the eligible pool.
            # If zero-hard candidates exist, strictly restrict the eligible pool to them;
            # otherwise (e.g. heuristic/greedy runs) retain candidate entries with documented violation status.
            zero_hard_entries = [c for c in candidate_entries if c["is_eligible"]]
            eligible_entries = zero_hard_entries if zero_hard_entries else candidate_entries
            ineligible_entries = [c for c in candidate_entries if c not in eligible_entries]

            # Identify Pareto front across the 7 AQWI metrics among eligible candidates
            compute_pareto_front(eligible_entries)

            # Partition into non-dominated (Pareto-optimal) and dominated candidates
            non_dominated = [c for c in eligible_entries if c["report"].pareto_status == "non_dominated"]
            dominated = [c for c in eligible_entries if c["report"].pareto_status != "non_dominated"]

            # Rank non-dominated candidates by descending AQWI score, breaking ties by lower unrounded total penalty, then candidate ID
            non_dominated.sort(
                key=lambda c: (
                    -c["report"].quality_score,
                    c["report"].total_penalty,
                    c["candidate_id"],
                )
            )

            # Rank dominated candidates similarly
            dominated.sort(
                key=lambda c: (
                    -c["report"].quality_score,
                    c["report"].total_penalty,
                    c["candidate_id"],
                )
            )

            # Combined ranked order: Non-dominated Pareto front first, followed by dominated candidates, then any ineligible
            ordered_entries = non_dominated + dominated + ineligible_entries

            # Update rank numbers, recommended status, and active status in reports
            serialized_reports = []
            serialized_solutions = []
            for rank_idx, entry in enumerate(ordered_entries, start=1):
                entry["report"].rank = rank_idx
                # The highest-ranked non-dominated candidate is selected as the recommended active timetable
                is_recommended = (rank_idx == 1 and entry in non_dominated)
                entry["report"].is_recommended = is_recommended
                entry["report"].is_active = (rank_idx == 1)

                rep_dict = entry["report"].to_dict()
                serialized_reports.append(rep_dict)
                serialized_solutions.append(solution_to_dict(entry["solution"]))

            # Select recommended candidate as the default active timetable
            best_entry = ordered_entries[0]
            best_sol = best_entry["solution"]
            best_sc = best_entry["score_result"]
            grids = annotate_grids(solution_to_grids(best_sol, problem), run.division_meta or {})

            run.solution = serialized_solutions[0]
            run.grids = grids
            run.stage_reports = primary_stage_reports
            run.num_candidates = num_candidates
            run.candidate_solutions = serialized_solutions
            run.judge_reports = serialized_reports
            run.selected_candidate_idx = 0
            run.hard = best_sc.hard_violations
            run.soft = best_sc.soft_cost
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
    """True if a run is queued or running — the single-flight guard for `POST /api/runs`.
    
    Includes auto-expiry for orphaned / killed background runs to prevent permanent 409 locks."""
    from datetime import datetime, timezone
    active = session.exec(
        select(TimetableRun).where(TimetableRun.status.in_(["queued", "running"]))
    ).all()
    if not active:
        return False

    now = datetime.now(timezone.utc)
    still_active = False
    for r in active:
        max_duration = max(300.0, (getattr(r, "time_limit", 30.0) or 30.0) * (getattr(r, "num_candidates", 1) or 1) + 120.0)
        created = getattr(r, "created_at", None)
        if created:
            if created.tzinfo is None:
                elapsed = (now.replace(tzinfo=None) - created).total_seconds()
            else:
                elapsed = (now - created).total_seconds()
            if elapsed > max_duration:
                r.status = "failed"
                r.error = "run timed out or background worker process was interrupted"
                session.add(r)
                continue
        still_active = True

    if not still_active:
        session.commit()
        return False
    return True
