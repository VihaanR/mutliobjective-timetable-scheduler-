"""Generic AUGMECON2-style epsilon-constraint Pareto sweep, reusing the CP-SAT model's four named
objective categories (`engine.solvers.cpsat.OBJECTIVE_CATEGORIES`: rooms, labs, students, faculty)
via `ParetoSession` (the same augmented objective as `CPSATSolver.solve_pareto_point()`). Works against ANY
`ProblemInstance` (any branch/division/year), not just one hardcoded dataset.

Supports both synchronous execution (`sweep()`, `sweep_pair()`) and real-time live streaming
(`sweep_stream()`) covering the 8-step End-to-End NEP-Aligned Multi-Objective Architecture:
1. Initiation
2. CP-SAT Model Construction
3. Payoff Table Calculation (Tight & Loose End)
4. Epsilon Grid Generation (Loose to Tight)
5. Slack-Augmented Lexicographic Epsilon Solves + AUGMECON2 Bypass
6. Rotating Epsilon Across All 3 Trade-off Pairs
7. 3D Pareto Dominance Filtering
8. Multi-Criteria Decision Support & Profile Recommendations (Faculty Friendly, Balanced, Student Friendly)
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Generator

from engine.io_json import solution_to_dict
from engine.models import ProblemInstance, Solution
from engine.pareto import pareto_filter
from engine.scoring import score
from engine.solvers.cpsat import CPSATSolver, ParetoSession
from engine.view import solution_to_grids
from webapp.grid_meta import annotate_grids

DEFAULT_TIME_LIMIT_S = 30
DEFAULT_SWEEP_POINTS = 5

DEFAULT_PAIRS: list[tuple[str, str]] = [
    ("faculty", "students"),
    ("faculty", "labs"),
    ("students", "labs"),
]


@dataclass
class FrontierPoint:
    pair: str
    epsilon: int
    bound_category: str
    minimize_category: str
    bound_value: int | None
    minimize_value: int | None
    hard_violations: int
    wall_s: float
    optimal: bool = True            # True iff CP-SAT status was OPTIMAL
    dominated: bool = False         # set by pareto_filter()
    skipped_by_bypass: bool = False  # ε never solved; AUGMECON2 bypass proved redundant
    faculty_score: int | None = None
    student_score: int | None = None
    resource_score: int | None = None
    total_penalty: int | None = None
    slack: int | None = None
    status_name: str = "OPTIMAL"
    solution_dict: dict | None = None
    grids: dict | None = None


def _lexicographic_payoff(session: ParetoSession, bound_category: str, minimize_category: str,
                          time_limit_s: float,
                          warm_start: Solution | None,
                          on_progress: Callable[[dict], None] | None = None) -> tuple[int, int, Solution]:
    """Lexicographic payoff table: the loose end of the epsilon range is the bound value at a
    lexicographic optimum of `minimize` (minimize it, then minimize `bound` holding it there). The
    non-lexicographic version took an arbitrary optimum of `minimize`, which can be weakly
    dominated and so widens the grid past the true frontier.

    The tight end needs only one solve. Its lexicographic second stage would be exactly the grid's
    epsilon == tight_end point, which the augmented sweep solve reaches anyway, so it is not solved
    twice. That first solve's solution has bound value `tight_end`, so it satisfies every epsilon
    on the grid and is returned as the always-feasible hint. Raises RuntimeError if any stage
    finds nothing."""
    def value_of(category: str, result: tuple) -> int:
        _sol, vals, _status = result
        if vals[category] is None:
            raise RuntimeError(
                f"payoff table solve was infeasible for pair ({bound_category}, {minimize_category})")
        return vals[category]

    if on_progress:
        on_progress({"substep": "tight_end", "desc": f"Solving Tight End: Minimize {bound_category} alone..."})
    first = session.solve_unbounded(minimize=bound_category, time_limit_s=time_limit_s, hint=warm_start)
    tight_end = value_of(bound_category, first)

    if on_progress:
        on_progress({"substep": "best_min", "desc": f"Solving Loose End Stage 1: Minimize {minimize_category} alone..."})
    best = session.solve_unbounded(minimize=minimize_category, time_limit_s=time_limit_s, hint=first[0])
    m_star = value_of(minimize_category, best)

    if on_progress:
        on_progress({"substep": "loose_end", "desc": f"Solving Loose End Stage 2: Minimize {bound_category} with {minimize_category} fixed to {m_star}..."})
    loose_solve = session.solve_unbounded(
        minimize=bound_category, fix={minimize_category: m_star},
        time_limit_s=time_limit_s, hint=best[0])
    loose_end = value_of(bound_category, loose_solve)

    return tight_end, loose_end, first[0]


def _epsilon_grid(tight_end: int, loose_end: int, n: int) -> list[int]:
    if loose_end <= tight_end:
        return [tight_end]
    step = (loose_end - tight_end) / (n - 1)
    return sorted({round(tight_end + i * step) for i in range(n)})


def compute_3d_scores(vals: dict[str, int | None]) -> tuple[int | None, int | None, int | None, int | None]:
    """Derive (faculty_score, student_score, resource_score, total_penalty) from category dictionary."""
    fac = vals.get("faculty")
    stu = vals.get("students")
    labs = vals.get("labs") or 0
    rooms = vals.get("rooms") or 0
    res = (labs + rooms) if (labs is not None and rooms is not None) else None
    tot = (fac + stu + res) if (fac is not None and stu is not None and res is not None) else None
    return fac, stu, res, tot


def pareto_filter_3d(points: list[FrontierPoint]) -> list[FrontierPoint]:
    """3D Dominance filter across (faculty, student, resource) scores."""
    for p in points:
        p.dominated = False
        if p.hard_violations != 0 or p.faculty_score is None or p.student_score is None or p.resource_score is None:
            p.dominated = True

    eligible = [p for p in points if not p.dominated]

    # Deduplicate exact triplets
    seen = {}
    reps = []
    for p in eligible:
        key = (p.faculty_score, p.student_score, p.resource_score)
        if key in seen:
            p.dominated = True
        else:
            seen[key] = p
            reps.append(p)

    # Check 3D dominance
    for a in reps:
        for b in reps:
            if a is b or a.dominated:
                continue
            # Does b dominate a?
            # b dominates a iff b <= a on all 3 and strictly less on at least one
            if (b.faculty_score <= a.faculty_score and
                b.student_score <= a.student_score and
                b.resource_score <= a.resource_score and
                (b.faculty_score < a.faculty_score or
                 b.student_score < a.student_score or
                 b.resource_score < a.resource_score)):
                a.dominated = True
                break

    return points


def compute_recommendations(points_data: list[dict]) -> dict[str, Any]:
    """Compute multi-criteria recommendations for Faculty-Friendly, Balanced, and Student-Friendly profiles."""
    eligible = [p for p in points_data if not p.get("dominated") and p.get("hard_violations", -1) == 0 and p.get("faculty_score") is not None]
    if not eligible:
        eligible = [p for p in points_data if p.get("hard_violations", -1) == 0 and p.get("faculty_score") is not None]
    if not eligible:
        return {"profiles": {}, "recommended_id": None, "bounds": {}}

    fac_vals = [p["faculty_score"] for p in eligible]
    stu_vals = [p["student_score"] for p in eligible]
    res_vals = [p["resource_score"] for p in eligible]

    f_min, f_max = min(fac_vals), max(fac_vals)
    s_min, s_max = min(stu_vals), max(stu_vals)
    r_min, r_max = min(res_vals), max(res_vals)

    def norm(v, lo, hi):
        return 0.0 if hi <= lo else (v - lo) / (hi - lo)

    profiles = {
        "faculty_friendly": (0.8, 0.2, 0.0),
        "balanced": (0.33, 0.33, 0.34),
        "student_friendly": (0.2, 0.8, 0.0),
    }

    best_by_profile = {}
    for prof, (wf, ws, wr) in profiles.items():
        best_p = min(eligible, key=lambda p: (
            wf * norm(p["faculty_score"], f_min, f_max) +
            ws * norm(p["student_score"], s_min, s_max) +
            wr * norm(p["resource_score"], r_min, r_max)
        ))
        best_by_profile[prof] = best_p.get("id")

    return {
        "profiles": best_by_profile,
        "recommended_id": best_by_profile.get("balanced"),
        "bounds": {
            "faculty": [f_min, f_max],
            "students": [s_min, s_max],
            "resource": [r_min, r_max],
        },
    }


def sweep_pair(solver: CPSATSolver, problem: ProblemInstance, bound_category: str,
               minimize_category: str, time_limit_s: float = DEFAULT_TIME_LIMIT_S,
               sweep_points: int = DEFAULT_SWEEP_POINTS,
               warm_start: Solution | None = None) -> list[FrontierPoint]:
    """AUGMECON2 sweep of one pair on a single reused `ParetoSession`."""
    return _sweep_pair(problem, bound_category, minimize_category, time_limit_s, sweep_points,
                       warm_start)[0]


def _sweep_pair(problem: ProblemInstance, bound_category: str, minimize_category: str,
                time_limit_s: float, sweep_points: int,
                warm_start: Solution | None,
                division_meta: dict | None = None,
                on_event: Callable[[dict], None] | None = None) -> tuple[list[FrontierPoint], Solution]:
    """`sweep_pair`, returning a feasible solution for `sweep()` to seed the next pair with."""
    pair_label = f"{bound_category}<=eps,min={minimize_category}"
    session = ParetoSession(problem, bound_category, minimize_category)

    if on_event:
        on_event({"type": "payoff_start", "pair": pair_label, "bound_category": bound_category, "minimize_category": minimize_category})

    def payoff_cb(data):
        if on_event:
            on_event({"type": "payoff_progress", "pair": pair_label, **data})

    tight_end, loose_end, tight_sol = _lexicographic_payoff(
        session, bound_category, minimize_category, time_limit_s, warm_start, on_progress=payoff_cb)

    grid = sorted(_epsilon_grid(tight_end, loose_end, sweep_points), reverse=True)  # loose -> tight

    if on_event:
        on_event({
            "type": "payoff_done",
            "pair": pair_label,
            "bound_category": bound_category,
            "minimize_category": minimize_category,
            "tight_end": tight_end,
            "loose_end": loose_end,
            "grid": grid,
        })

    def make_point(epsilon: int, vals: dict, hard: int, wall: float, sol: Solution | None, **flags) -> FrontierPoint:
        fac, stu, res, tot = compute_3d_scores(vals)
        bound_val = vals.get(bound_category)
        min_val = vals.get(minimize_category)
        slack_val = max(0, epsilon - bound_val) if bound_val is not None else None
        sol_dict = solution_to_dict(sol) if sol else None
        grids = None
        if sol and sol.assignments:
            try:
                grids = annotate_grids(solution_to_grids(sol, problem), division_meta or {})
            except Exception:
                grids = None

        status_n = flags.pop("status_name", "OPTIMAL")
        return FrontierPoint(
            pair=pair_label, epsilon=epsilon, bound_category=bound_category,
            minimize_category=minimize_category, bound_value=bound_val,
            minimize_value=min_val, hard_violations=hard, wall_s=wall,
            faculty_score=fac, student_score=stu, resource_score=res,
            total_penalty=tot, slack=slack_val, status_name=status_n,
            solution_dict=sol_dict, grids=grids, **flags,
        )

    points: list[FrontierPoint] = []
    prev_sol, prev_bound = None, None
    i = 0
    while i < len(grid):
        epsilon = grid[i]
        hint = prev_sol if prev_bound is not None and prev_bound <= epsilon else tight_sol

        if on_event:
            on_event({
                "type": "point_start",
                "pair": pair_label,
                "epsilon": epsilon,
                "point_index": i + 1,
                "total_points": len(grid),
                "remaining": len(grid) - i,
            })

        def intermediate_cb(int_sol, int_vals, count, w_time):
            if on_event:
                fac, stu, res, tot = compute_3d_scores(int_vals)
                int_grids = None
                if int_sol and int_sol.assignments:
                    try:
                        int_grids = annotate_grids(solution_to_grids(int_sol, problem), division_meta or {})
                    except Exception:
                        int_grids = None
                on_event({
                    "type": "intermediate",
                    "pair": pair_label,
                    "epsilon": epsilon,
                    "count": count,
                    "wall_s": round(w_time, 2),
                    "faculty_score": fac,
                    "student_score": stu,
                    "resource_score": res,
                    "total_penalty": tot,
                    "grids": int_grids,
                })

        t0 = time.time()
        if on_event:
            try:
                sol, vals, status = session.solve(epsilon, time_limit_s, hint=hint, solution_callback=intermediate_cb)
            except TypeError:
                sol, vals, status = session.solve(epsilon, time_limit_s, hint=hint)
        else:
            sol, vals, status = session.solve(epsilon, time_limit_s, hint=hint)
        wall = time.time() - t0
        hard = score(sol, problem).hard_violations if sol.assignments else -1
        pt = make_point(epsilon, vals, hard, wall, sol, optimal=status == "OPTIMAL", status_name=status)
        points.append(pt)

        if on_event:
            on_event({
                "type": "point_done",
                "pair": pair_label,
                "epsilon": epsilon,
                "point": asdict(pt),
                "remaining": len(grid) - (i + 1),
            })

        i += 1

        if status == "INFEASIBLE":
            if on_event:
                on_event({"type": "early_exit", "pair": pair_label, "reason": "INFEASIBLE proven, tighter bounds skipped"})
            break

        if vals[bound_category] is not None:
            prev_sol, prev_bound = sol, vals[bound_category]

        bound_value = vals[bound_category]
        if status == "OPTIMAL" and bound_value is not None:
            while i < len(grid) and grid[i] >= bound_value:
                bypass_pt = make_point(
                    grid[i], vals, hard, 0.0, sol, optimal=True, skipped_by_bypass=True, status_name="OPTIMAL (BYPASS)")
                points.append(bypass_pt)
                if on_event:
                    on_event({
                        "type": "bypass",
                        "pair": pair_label,
                        "epsilon": grid[i],
                        "point": asdict(bypass_pt),
                        "reason": f"AUGMECON2 bypass: actual bound {bound_value} <= {grid[i]}",
                    })
                i += 1

    pareto_filter(points)
    pareto_filter_3d(points)
    return points, tight_sol


def sweep(problem: ProblemInstance, pairs: list[tuple[str, str]] | None = None,
          time_limit_s: float = DEFAULT_TIME_LIMIT_S,
          sweep_points: int = DEFAULT_SWEEP_POINTS) -> dict[str, list[FrontierPoint]]:
    """Synchronous full Pareto sweep across `pairs`."""
    from engine.solvers.greedy import GreedySolver

    warm_start = GreedySolver().solve(problem)
    results: dict[str, list[FrontierPoint]] = {}
    for bound_category, minimize_category in (pairs or DEFAULT_PAIRS):
        pair_label = f"{bound_category}<=eps,min={minimize_category}"
        try:
            results[pair_label], warm_start = _sweep_pair(
                problem, bound_category, minimize_category, time_limit_s, sweep_points, warm_start)
        except RuntimeError:
            results[pair_label] = []
    return results


def sweep_stream(problem: ProblemInstance,
                 pairs: list[tuple[str, str]] | None = None,
                 time_limit_s: float = DEFAULT_TIME_LIMIT_S,
                 sweep_points: int = DEFAULT_SWEEP_POINTS,
                 division_meta: dict | None = None) -> Generator[dict, None, None]:
    """Live streaming generator executing the 8-step End-to-End Multi-Objective Architecture."""
    from engine.solvers.greedy import GreedySolver

    active_pairs = list(pairs or DEFAULT_PAIRS)
    all_points: list[FrontierPoint] = []
    payoff_tables: dict[str, dict] = {}

    # Step 1: Initiated
    yield {
        "type": "step",
        "step_num": 1,
        "step_name": "User Initiates Pareto Optimization",
        "status": "active",
        "detail": {
            "pairs": [list(p) for p in active_pairs],
            "sweep_points": sweep_points,
            "time_limit_s": time_limit_s,
            "divisions_count": len(problem.divisions),
            "faculty_count": len(problem.faculty),
            "rooms_count": len(problem.rooms),
            "slots_count": len(problem.slot_template.slots),
        },
    }

    # Step 2: CP-SAT Model Construction
    t_start = time.time()
    warm_start = GreedySolver().solve(problem)
    yield {
        "type": "step",
        "step_num": 2,
        "step_name": "CP-SAT Model Construction",
        "status": "active",
        "detail": {
            "hard_constraints": [
                "Every session assigned exactly once",
                "No faculty clash",
                "No room clash",
                "No division clash",
                "Open Elective synchronized at H1",
                "Honours courses placed at day boundaries",
                "Sibling batch practical synchronization",
            ],
            "soft_penalty_variables": ["faculty_score", "student_score", "resource_score"],
            "warm_start_wall_s": round(warm_start.wall_clock_seconds, 2),
        },
    }

    # Execute sweeps across pairs
    total_expected_points = len(active_pairs) * sweep_points
    completed_points_count = 0

    for pair_idx, (bound_cat, min_cat) in enumerate(active_pairs):
        pair_label = f"{bound_cat}<=eps,min={min_cat}"

        # Step 3 & 4 notification
        yield {
            "type": "step",
            "step_num": 3,
            "step_name": f"Payoff Table Calculation: ({bound_cat}, {min_cat})",
            "status": "active",
            "detail": {"pair": pair_label, "pair_index": pair_idx + 1, "total_pairs": len(active_pairs)},
        }

        pair_events: list[dict] = []

        def capture_event(ev: dict):
            pair_events.append(ev)

        try:
            pair_points, warm_start = _sweep_pair(
                problem, bound_cat, min_cat, time_limit_s, sweep_points, warm_start,
                division_meta=division_meta, on_event=capture_event,
            )
        except Exception as exc:
            yield {"type": "pair_error", "pair": pair_label, "error": str(exc)}
            continue

        for ev in pair_events:
            ev_type = ev.get("type")
            if ev_type == "payoff_done":
                payoff_tables[pair_label] = {
                    "tight_end": ev.get("tight_end"),
                    "loose_end": ev.get("loose_end"),
                    "grid": ev.get("grid"),
                }
                yield {
                    "type": "step",
                    "step_num": 4,
                    "step_name": f"Epsilon Grid Generated for ({bound_cat}, {min_cat})",
                    "status": "active",
                    "detail": {
                        "pair": pair_label,
                        "tight_end": ev.get("tight_end"),
                        "loose_end": ev.get("loose_end"),
                        "grid": ev.get("grid"),
                        "order": "Loose -> Tight (descending)",
                    },
                }
            elif ev_type in ("point_done", "bypass"):
                completed_points_count += 1
                point_data = ev.get("point", {})
                point_data["id"] = len(all_points) + 1
                yield {
                    "type": "progress",
                    "completed": completed_points_count,
                    "total_expected": total_expected_points,
                    "current_pair": pair_label,
                    "current_epsilon": ev.get("epsilon"),
                    "remaining_in_pair": ev.get("remaining", 0),
                    "wall_elapsed_s": round(time.time() - t_start, 1),
                }
            yield ev

        all_points.extend(pair_points)

        yield {
            "type": "step",
            "step_num": 6,
            "step_name": "Rotating Epsilon Across Objectives",
            "status": "active",
            "detail": {
                "completed_pairs": pair_idx + 1,
                "total_pairs": len(active_pairs),
                "total_points_collected": len(all_points),
            },
        }

    # Step 7: 3D Pareto Dominance Filtering
    yield {
        "type": "step",
        "step_num": 7,
        "step_name": "Pareto Dominance Filtering",
        "status": "active",
        "detail": {"total_solutions": len(all_points), "criteria": ["hard_violations == 0", "Deduplication", "3D Dominance (f, s, r)"]},
    }

    pareto_filter_3d(all_points)

    # Format points for UI
    formatted_points: list[dict] = []
    for idx, p in enumerate(all_points, start=1):
        d = asdict(p)
        d["id"] = idx
        formatted_points.append(d)

    # Step 8: Multi-Criteria Decision Support & Profile Recommendations
    recs = compute_recommendations(formatted_points)

    yield {
        "type": "step",
        "step_num": 8,
        "step_name": "Admin Decision Support & Interactive Frontier",
        "status": "done",
        "detail": {
            "total_points": len(formatted_points),
            "non_dominated_count": len([p for p in formatted_points if not p.get("dominated")]),
            "dominated_count": len([p for p in formatted_points if p.get("dominated")]),
            "recommendations": recs,
        },
    }

    yield {
        "type": "complete",
        "points": formatted_points,
        "payoff_tables": payoff_tables,
        "recommendations": recs,
        "total_wall_s": round(time.time() - t_start, 2),
    }
