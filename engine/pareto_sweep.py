"""Generic AUGMECON2-style epsilon-constraint Pareto sweep, reusing the CP-SAT model's four named
objective categories (`engine.solvers.cpsat.OBJECTIVE_CATEGORIES`: rooms, labs, students, faculty)
via `ParetoSession` (the same augmented objective as `CPSATSolver.solve_pareto_point()`). Works against ANY
`ProblemInstance` (any branch/division/year), not just one hardcoded dataset.

Extracted as a pure, side-effect-free module (no CSV/matplotlib) so `webapp/routers/pareto.py` can
call `sweep()` directly from a background job. "rooms" is excluded from the default pairs by the
same reasoning as the original research script: on typical DJSCE-shaped instances it reaches its
own unconstrained minimum regardless of what else is optimized (very low soft-weight in
scoring.py), so it never trades off against anything.

Each pair is swept with the full AUGMECON2 procedure (lexicographic payoff table, loose-to-tight
epsilon order, bypass, early exit on proven infeasibility) on one reused `ParetoSession`, and every
returned point is flagged by `engine.pareto.pareto_filter` rather than dropped.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from engine.models import ProblemInstance, Solution
from engine.pareto import pareto_filter
from engine.scoring import score
from engine.solvers.cpsat import CPSATSolver, ParetoSession

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


def _lexicographic_payoff(session: ParetoSession, bound_category: str, minimize_category: str,
                          time_limit_s: float,
                          warm_start: Solution | None) -> tuple[int, int, Solution]:
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

    first = session.solve_unbounded(minimize=bound_category, time_limit_s=time_limit_s, hint=warm_start)
    tight_end = value_of(bound_category, first)

    # the first solve's solution is feasible and unbounded solves have no cap, so it is a complete
    # incumbent for this one -- unlike the greedy warm start, which usually has hard violations
    best = session.solve_unbounded(minimize=minimize_category, time_limit_s=time_limit_s, hint=first[0])
    m_star = value_of(minimize_category, best)
    loose_end = value_of(bound_category, session.solve_unbounded(
        minimize=bound_category, fix={minimize_category: m_star},
        time_limit_s=time_limit_s, hint=best[0]))
    return tight_end, loose_end, first[0]


def _epsilon_grid(tight_end: int, loose_end: int, n: int) -> list[int]:
    if loose_end <= tight_end:
        return [tight_end]
    step = (loose_end - tight_end) / (n - 1)
    return sorted({round(tight_end + i * step) for i in range(n)})


def sweep_pair(solver: CPSATSolver, problem: ProblemInstance, bound_category: str,
               minimize_category: str, time_limit_s: float = DEFAULT_TIME_LIMIT_S,
               sweep_points: int = DEFAULT_SWEEP_POINTS,
               warm_start: Solution | None = None) -> list[FrontierPoint]:
    """AUGMECON2 sweep of one pair on a single reused `ParetoSession`: lexicographic payoff table,
    then epsilon from loose to tight with bypass and early exit, then `pareto_filter`.

    `solver` is unused (the session owns the model) and kept only so existing callers don't break.
    """
    return _sweep_pair(problem, bound_category, minimize_category, time_limit_s, sweep_points,
                       warm_start)[0]


def _sweep_pair(problem: ProblemInstance, bound_category: str, minimize_category: str,
                time_limit_s: float, sweep_points: int,
                warm_start: Solution | None) -> tuple[list[FrontierPoint], Solution]:
    """`sweep_pair`, also returning a feasible solution (the tight-end payoff solution) for
    `sweep()` to seed the next pair with."""
    pair_label = f"{bound_category}<=eps,min={minimize_category}"
    session = ParetoSession(problem, bound_category, minimize_category)
    tight_end, loose_end, tight_sol = _lexicographic_payoff(
        session, bound_category, minimize_category, time_limit_s, warm_start)
    grid = sorted(_epsilon_grid(tight_end, loose_end, sweep_points), reverse=True)  # loose -> tight

    def point(epsilon: int, vals: dict, hard: int, wall: float, **flags) -> FrontierPoint:
        return FrontierPoint(
            pair=pair_label, epsilon=epsilon, bound_category=bound_category,
            minimize_category=minimize_category, bound_value=vals[bound_category],
            minimize_value=vals[minimize_category], hard_violations=hard, wall_s=wall, **flags,
        )

    points: list[FrontierPoint] = []
    # Every bounded solve is seeded with a solution that already satisfies its bound: CP-SAT only
    # turns a hint into an incumbent when it is feasible, and on the reference instance solves
    # near the frontier's edge otherwise time out with nothing. The previous point's solution is
    # the closer guess, but sweeping loose -> tight it can sit above the next epsilon; the
    # tight-end solution never does.
    prev_sol, prev_bound = None, None
    i = 0
    while i < len(grid):
        epsilon = grid[i]
        hint = prev_sol if prev_bound is not None and prev_bound <= epsilon else tight_sol
        t0 = time.time()
        sol, vals, status = session.solve(epsilon, time_limit_s, hint=hint)
        wall = time.time() - t0
        hard = score(sol, problem).hard_violations if sol.assignments else -1
        points.append(point(epsilon, vals, hard, wall, optimal=status == "OPTIMAL"))
        i += 1

        # Proven infeasible here means infeasible at every tighter epsilon. A time-limited solve
        # with no solution ("TIMEOUT") proves nothing, so the sweep carries on past it.
        if status == "INFEASIBLE":
            break
        if vals[bound_category] is not None:
            prev_sol, prev_bound = sol, vals[bound_category]
        # Bypass: an OPTIMAL solve landing at bound v <= epsilon is also the optimum for every
        # grid epsilon' in [v, epsilon) -- it stays feasible there and nothing better can be.
        # Not valid after a FEASIBLE (non-proven) solve.
        bound_value = vals[bound_category]
        if status == "OPTIMAL" and bound_value is not None:
            while i < len(grid) and grid[i] >= bound_value:
                points.append(point(grid[i], vals, hard, 0.0, optimal=True, skipped_by_bypass=True))
                i += 1

    pareto_filter(points)
    return points, tight_sol


def sweep(problem: ProblemInstance, pairs: list[tuple[str, str]] | None = None,
          time_limit_s: float = DEFAULT_TIME_LIMIT_S,
          sweep_points: int = DEFAULT_SWEEP_POINTS) -> dict[str, list[FrontierPoint]]:
    """Run the full sweep across `pairs` (default: DEFAULT_PAIRS) against `problem`. Returns
    {pair_label: [FrontierPoint, ...]}, skipping (not raising for) any pair whose payoff table
    solve is infeasible on this instance.

    Each pair's payoff solves are seeded with a feasible solution from the previous pair once one
    exists: they carry no epsilon bound, so any feasible timetable is a complete incumbent for
    them, whereas the greedy start usually has hard violations and seeds nothing."""
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
