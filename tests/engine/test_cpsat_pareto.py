"""Tests for the CP-SAT objective-category split and the epsilon-constraint sweep support
(design.md Sec 15.3 / research/pareto_sweep.py). Time limits kept small per the project
convention (tests/test_solvers.py's docstring) so the suite runs quickly."""
from unittest.mock import patch

import pytest

from engine.scoring import score
from engine.pareto_sweep import sweep
from engine.solvers.cpsat import OBJECTIVE_CATEGORIES, CPSATSolver, ParetoSession, _build_model


def test_solve_default_still_zero_hard_violations(small_problem):
    """The category-split refactor must not change solve()'s existing contract."""
    sol = CPSATSolver().solve(small_problem, time_limit_s=30)
    assert sol.status in ("OPTIMAL", "FEASIBLE")
    result = score(sol, small_problem)
    assert result.hard_violations == 0


def test_solve_pareto_point_respects_generous_bound(small_problem):
    sol, values = CPSATSolver().solve_pareto_point(
        small_problem, bounds={"faculty": 10_000}, minimize="students", time_limit_s=25)
    assert sol.status in ("OPTIMAL", "FEASIBLE")
    assert set(values) == set(OBJECTIVE_CATEGORIES)
    assert values["faculty"] is not None
    assert values["faculty"] <= 10_000
    assert score(sol, small_problem).hard_violations == 0


def test_solve_pareto_point_tight_bound_never_raises(small_problem):
    """A very tight bound may make the instance harder or infeasible for this category, but the
    solver must never raise (matches CLAUDE.md's "solvers never raise on infeasible" rule)."""
    sol, values = CPSATSolver().solve_pareto_point(
        small_problem, bounds={"faculty": 0}, minimize="students", time_limit_s=25)
    assert sol.status in ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "TIMEOUT")
    if sol.status in ("OPTIMAL", "FEASIBLE"):
        assert values["faculty"] is not None
        assert values["faculty"] <= 0
    else:
        assert all(v is None for v in values.values())


def test_solve_pareto_point_unknown_category_raises(small_problem):
    with pytest.raises(ValueError):
        CPSATSolver().solve_pareto_point(small_problem, bounds={"bogus": 1}, minimize="rooms", time_limit_s=5)
    with pytest.raises(ValueError):
        CPSATSolver().solve_pareto_point(small_problem, bounds={}, minimize="bogus", time_limit_s=5)



# ---------- ParetoSession: one model, many solves ----------

def test_pareto_session_unknown_category_raises(small_problem):
    with pytest.raises(ValueError):
        ParetoSession(small_problem, "bogus", "students")
    with pytest.raises(ValueError):
        ParetoSession(small_problem, "faculty", "bogus")
    session = ParetoSession(small_problem, "faculty", "students")
    with pytest.raises(ValueError):
        session.solve_unbounded(minimize="labs", time_limit_s=5)  # not part of this pair


def test_pareto_session_builds_model_once_across_many_solves(small_problem):
    """The point of the session: payoff and epsilon solves all reuse one built model. Also guards
    the hint bug -- _solve_and_decode appends hints, and a reused model that doesn't clear them is
    MODEL_INVALID on the second hinted solve. small_problem rarely proves optimality within these
    budgets, so feasibility itself isn't asserted -- only what must hold whenever a solve returns."""
    with patch("engine.solvers.cpsat._build_model", wraps=_build_model) as build:
        session = ParetoSession(small_problem, "faculty", "students")
        sol, vals, status = session.solve_unbounded(minimize="faculty", time_limit_s=10)
        assert status != "MODEL_INVALID"
        if vals["faculty"] is None:
            pytest.skip("no feasible solution within budget")
        for epsilon in (vals["faculty"] + 50, vals["faculty"] + 10, vals["faculty"]):
            sol, vals_eps, status = session.solve(epsilon, time_limit_s=10, hint=sol)
            assert status != "MODEL_INVALID"
            if vals_eps["faculty"] is not None:
                assert vals_eps["faculty"] <= epsilon
        _sol, _vals, status = session.solve_unbounded(minimize="students", time_limit_s=5, hint=sol)
        assert status != "MODEL_INVALID"
    assert build.call_count == 1


def test_pareto_session_fix_pins_category(small_problem):
    session = ParetoSession(small_problem, "faculty", "students")
    sol, vals, _status = session.solve_unbounded(minimize="students", time_limit_s=10)
    if vals["students"] is None:
        pytest.skip("no feasible solution within budget")
    # the hint is feasible for the fixed value, so the pinned solve has a solution to start from
    _sol, fixed, status = session.solve_unbounded(
        minimize="faculty", fix={"students": vals["students"]}, time_limit_s=10, hint=sol)
    assert status in ("OPTIMAL", "FEASIBLE")
    assert fixed["students"] == vals["students"]


def test_pareto_session_feasible_hint_always_yields_a_solution(reference_problem):
    """Regression: a hint covering only the placement variables is not a usable incumbent for
    CP-SAT, so solves bounded at the frontier's edge came back TIMEOUT with nothing even though
    the hint itself satisfied the bound. The session must complete a feasible hint into a full
    one, so such a solve can never come back empty. Runs on the reference instance because
    that is where it reproduces -- small_problem finds a solution either way."""
    session = ParetoSession(reference_problem, "faculty", "students")
    sol, vals, _status = session.solve_unbounded(minimize="faculty", time_limit_s=20)
    if vals["faculty"] is None:
        pytest.skip("no feasible solution within budget")
    edge = vals["faculty"]
    _s, bounded, status = session.solve(edge, time_limit_s=20, hint=sol)
    assert status in ("OPTIMAL", "FEASIBLE") and bounded["faculty"] <= edge
    _s, pinned, status = session.solve_unbounded(
        minimize="students", fix={"faculty": edge}, time_limit_s=20, hint=sol)
    assert status in ("OPTIMAL", "FEASIBLE") and pinned["faculty"] == edge


def test_sweep_frontier_is_non_dominated_and_monotone(small_problem):
    results = sweep(small_problem, pairs=[("faculty", "students")], time_limit_s=8, sweep_points=4)
    points = results["faculty<=eps,min=students"]
    frontier = sorted((p for p in points if not p.dominated), key=lambda p: p.bound_value)
    if not frontier:
        pytest.skip("no feasible frontier point within budget")
    for p in frontier:
        assert p.hard_violations == 0
        assert p.bound_value <= p.epsilon
    # 2-D minimisation frontier: as the bound loosens, the minimized objective must strictly improve
    for a, b in zip(frontier, frontier[1:]):
        assert a.bound_value < b.bound_value and a.minimize_value > b.minimize_value
