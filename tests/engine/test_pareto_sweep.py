"""Tests for sweep_pair() / ParetoSession behaviour, using a fake session (no CP-SAT).

Spec §4 / TDD: these tests cover:
- loose→tight sweep order
- bypass skips exactly [v, ε) when OPTIMAL, not when FEASIBLE
- early exit only on proven INFEASIBLE; UNKNOWN continues
- pareto_filter is applied to the pair's points
"""
from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from engine.models import ProblemInstance, Solution
from engine.pareto_sweep import FrontierPoint, sweep_pair, _epsilon_grid, DEFAULT_TIME_LIMIT_S


# ── fake session ─────────────────────────────────────────────────────────────

@dataclass
class _FakeSolveResult:
    bound_value: int | None
    minimize_value: int | None
    status: str = "OPTIMAL"  # or FEASIBLE, INFEASIBLE, UNKNOWN


class FakeSession:
    """Minimal fake of ParetoSession; records calls and returns scripted results."""

    def __init__(self, results: list[_FakeSolveResult]):
        self._results = list(results)
        self._idx = 0
        self.solve_calls: list[int] = []  # epsilons passed to solve()
        self.solve_hints: list = []       # hint passed to each solve()
        self.returned: list = []          # every (solution, bound_value) handed back, in order

    def solve(self, epsilon, time_limit_s, hint=None):
        self.solve_calls.append(epsilon)
        self.solve_hints.append(hint)
        r = self._results[self._idx]
        self._idx += 1
        sol = _make_solution(r.status)
        self.returned.append((sol, r.bound_value))
        if r.bound_value is None:
            vals = {"faculty": None, "students": None, "labs": None, "rooms": None}
        else:
            vals = {"faculty": r.bound_value, "students": r.minimize_value, "labs": 0, "rooms": 0}
        return sol, vals, r.status

    def solve_unbounded(self, minimize, fix=None, time_limit_s=30, hint=None):
        """Payoff-table solves: always succeed with scripted payoff values."""
        r = self._results[self._idx]
        self._idx += 1
        sol = _make_solution("OPTIMAL")
        self.returned.append((sol, r.bound_value))
        if r.bound_value is None:
            vals = {"faculty": None, "students": None, "labs": None, "rooms": None}
        else:
            vals = {"faculty": r.bound_value, "students": r.minimize_value, "labs": 0, "rooms": 0}
        return sol, vals, "OPTIMAL"


def _make_solution(status: str) -> Solution:
    # Keep assignments empty in ALL cases so score() is never invoked on the
    # MagicMock problem — sweep_pair guards: `score(sol, problem) if sol.assignments`.
    # The FrontierPoint.hard_violations will be -1, which is fine for sweep tests.
    return Solution(
        assignments=[],
        solver_name="fake",
        wall_clock_seconds=0.1,
        objective_value=0 if status in ("OPTIMAL", "FEASIBLE") else None,
        status=status,
    )


# ── _epsilon_grid ────────────────────────────────────────────────────────────

class TestEpsilonGrid:
    def test_single_point_when_tight_equals_loose(self):
        assert _epsilon_grid(5, 5, 5) == [5]

    def test_single_point_when_tight_greater_loose(self):
        assert _epsilon_grid(5, 3, 5) == [5]

    def test_grid_includes_endpoints(self):
        grid = _epsilon_grid(2, 10, 5)
        assert grid[0] == 2
        assert grid[-1] == 10

    def test_grid_length(self):
        grid = _epsilon_grid(0, 8, 5)
        assert len(grid) == 5


# ── sweep_pair: order and bypass ─────────────────────────────────────────────

def _small_problem():
    """Return a stub ProblemInstance (no real data needed — we mock ParetoSession)."""
    return MagicMock(spec=ProblemInstance)


class TestSweepPairOrder:
    """Verify sweep goes loose→tight (AUGMECON2 canonical order)."""

    def test_loose_to_tight_order(self):
        """Epsilons must decrease from loose_end toward tight_end."""
        # payoff: tight_end=2, loose_end=10 → grid (5 pts): [2,4,6,8,10]
        # sweep from loose to tight: 10, 8, 6, 4, 2
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        solve_results = [
            _FakeSolveResult(10, 3),  # ε=10
            _FakeSolveResult(8, 3),   # ε=8
            _FakeSolveResult(6, 3),   # ε=6
            _FakeSolveResult(4, 3),   # ε=4
            _FakeSolveResult(2, 3),   # ε=2
        ]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=5, warm_start=None,
            )

        # extract epsilons in solve call order
        # first 3 calls are payoff-table (solve_unbounded), then bounded solves
        assert session.solve_calls == [10, 8, 6, 4, 2]

    def test_all_points_returned(self):
        """sweep_pair returns one FrontierPoint per ε (no silent drops)."""
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        solve_results = [_FakeSolveResult(i, 3) for i in [10, 8, 6, 4, 2]]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=5, warm_start=None,
            )
        # 5 bounded solve calls → 5 FrontierPoints
        assert len(pts) == 5


class TestSweepPairBypass:
    """Bypass: OPTIMAL solve achieving bound_value=v skips [v, ε) grid points."""

    def test_bypass_after_optimal(self):
        """Grid [2,4,6,8,10], solve ε=10 achieves bound_value=6 (OPTIMAL).
        Grid points ε=8 (8≥6 and 8<10) should be skipped; solve continues from ε<6 i.e. ε=4,2."""
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        solve_results = [
            # ε=10 → achieves bound_value=6 (OPTIMAL), should bypass ε=8
            _FakeSolveResult(6, 3, status="OPTIMAL"),
            # ε=6 → achieves bound_value=4 (OPTIMAL), should bypass ε=4 maybe not, 4 < 6
            _FakeSolveResult(4, 2, status="OPTIMAL"),
            # ε=2 → solved directly
            _FakeSolveResult(2, 1, status="OPTIMAL"),
        ]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=5, warm_start=None,
            )

        # ε=8 was bypassed → corresponding FrontierPoint has skipped_by_bypass=True
        byp = [p for p in pts if p.skipped_by_bypass]
        assert len(byp) >= 1
        # epsilons that were bypassed: ε=8 (since bound_value=6 at ε=10, bypass covers [6,10)
        # i.e. ε=8 is in [6,10))
        bypassed_epsilons = {p.epsilon for p in byp}
        assert 8 in bypassed_epsilons

    def test_no_bypass_after_feasible(self):
        """FEASIBLE (non-optimal) solve: bypass must NOT fire even if bound_value < ε."""
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        # ε=10 → achieves bound_value=6 but status=FEASIBLE — no bypass
        solve_results = [
            _FakeSolveResult(6, 3, status="FEASIBLE"),
            _FakeSolveResult(8, 3, status="OPTIMAL"),
            _FakeSolveResult(6, 2, status="OPTIMAL"),
            _FakeSolveResult(4, 1, status="OPTIMAL"),
            _FakeSolveResult(2, 1, status="OPTIMAL"),
        ]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=5, warm_start=None,
            )

        byp = [p for p in pts if p.skipped_by_bypass]
        # ε=10 was FEASIBLE → no bypass; so all 5 points should be actually solved
        assert len(byp) == 0
        assert session.solve_calls == [10, 8, 6, 4, 2]

    def test_bypass_copies_solved_values(self):
        """A bypassed point must copy the solved point's objective values."""
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        # ε=10 → bound_value=6 OPTIMAL; ε=8 should be bypassed with same values
        solve_results = [
            _FakeSolveResult(6, 3, status="OPTIMAL"),
            _FakeSolveResult(4, 2, status="OPTIMAL"),
            _FakeSolveResult(2, 1, status="OPTIMAL"),
        ]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=5, warm_start=None,
            )

        byp = [p for p in pts if p.skipped_by_bypass]
        for b in byp:
            assert b.bound_value == 6
            assert b.minimize_value == 3
            assert b.wall_s == 0


class TestSweepPairEarlyExit:
    """Early exit on INFEASIBLE; UNKNOWN continues."""

    def test_infeasible_stops_sweep(self):
        """INFEASIBLE at ε=10 → remaining tighter ε (8,6,4,2) not solved."""
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        solve_results = [
            _FakeSolveResult(None, None, status="INFEASIBLE"),
            # the solver should never reach these
            _FakeSolveResult(8, 3),
            _FakeSolveResult(6, 3),
        ]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=5, warm_start=None,
            )

        # only ε=10 was actually solved; rest are not present
        assert session.solve_calls == [10]
        # The infeasible point should still be emitted
        assert any(p.epsilon == 10 for p in pts)

    def test_unknown_continues(self):
        """UNKNOWN (time-limit, no solution) at ε=10 → tighter ε still attempted."""
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        solve_results = [
            _FakeSolveResult(None, None, status="UNKNOWN"),  # ε=10 unknown
            _FakeSolveResult(8, 3, status="OPTIMAL"),        # ε=8 solved
            _FakeSolveResult(6, 2, status="OPTIMAL"),
            _FakeSolveResult(4, 1, status="OPTIMAL"),
            _FakeSolveResult(2, 1, status="OPTIMAL"),
        ]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=5, warm_start=None,
            )

        # all 5 epsilons must be attempted
        assert session.solve_calls == [10, 8, 6, 4, 2]

    def test_model_invalid_continues(self):
        """MODEL_INVALID is not INFEASIBLE — sweep continues."""
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        solve_results = [
            _FakeSolveResult(None, None, status="MODEL_INVALID"),
            _FakeSolveResult(8, 3, status="OPTIMAL"),
            _FakeSolveResult(6, 2, status="OPTIMAL"),
            _FakeSolveResult(4, 1, status="OPTIMAL"),
            _FakeSolveResult(2, 1, status="OPTIMAL"),
        ]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=5, warm_start=None,
            )

        assert session.solve_calls == [10, 8, 6, 4, 2]


class TestSweepPairFilterApplied:
    """pareto_filter is called on the collected points."""

    def test_dominated_points_flagged(self):
        """A point that achieves the same (bound, minimize) as another should be dominated."""
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        # Two points with same (bound_value, minimize_value) → one should be dominated
        solve_results = [
            _FakeSolveResult(5, 3, status="OPTIMAL"),
            _FakeSolveResult(5, 3, status="OPTIMAL"),  # duplicate
            _FakeSolveResult(4, 2, status="OPTIMAL"),
            _FakeSolveResult(3, 2, status="OPTIMAL"),
            _FakeSolveResult(2, 1, status="OPTIMAL"),
        ]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=5, warm_start=None,
            )

        # At least one point should be flagged as dominated
        dominated = [p for p in pts if p.dominated]
        assert len(dominated) >= 1

    def test_optimal_field_set(self):
        """FrontierPoint.optimal must be True for OPTIMAL, False for FEASIBLE."""
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(6, 3),
        ]
        solve_results = [
            _FakeSolveResult(6, 3, status="FEASIBLE"),
            _FakeSolveResult(4, 2, status="OPTIMAL"),
            _FakeSolveResult(2, 1, status="OPTIMAL"),
        ]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            pts = sweep_pair(
                None, _small_problem(), "faculty", "students",
                time_limit_s=5, sweep_points=3, warm_start=None,
            )

        statuses = {p.epsilon: p.optimal for p in pts if not p.skipped_by_bypass}
        eps_solved = session.solve_calls
        # first ε was FEASIBLE → optimal=False
        assert statuses[eps_solved[0]] is False
        # second was OPTIMAL → optimal=True
        assert statuses[eps_solved[1]] is True


class TestSweepPairHints:
    """Every bounded solve must be hinted with a solution that satisfies its bound: CP-SAT only
    turns a hint into an incumbent when it is feasible, and without one, solves near the
    frontier's edge time out empty on the reference instance."""

    def test_hint_falls_back_to_tight_end_solution_when_previous_violates_bound(self):
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        solve_results = [_FakeSolveResult(b, 3, status="FEASIBLE") for b in (9, 7, 5, 3, 2)]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            sweep_pair(None, _small_problem(), "faculty", "students",
                       time_limit_s=5, sweep_points=5, warm_start=None)

        tight_sol = session.returned[0][0]
        by_eps = dict(zip(session.solve_calls, session.solve_hints))
        # previous point landed at bound 9 > 8, so it cannot seed epsilon=8: the tight-end
        # solution (bound 2) must be used instead -- and likewise at every tighter epsilon
        for eps in (8, 6, 4, 2):
            assert by_eps[eps] is tight_sol, eps

    def test_hint_uses_previous_solution_when_it_satisfies_bound(self):
        payoff_calls = [
            _FakeSolveResult(2, 5),
            _FakeSolveResult(5, 3),
            _FakeSolveResult(10, 3),
        ]
        # FEASIBLE (no bypass) and landing well under the next epsilon
        solve_results = [_FakeSolveResult(b, 3, status="FEASIBLE") for b in (7, 5, 3, 2, 2)]
        session = FakeSession(payoff_calls + solve_results)

        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            sweep_pair(None, _small_problem(), "faculty", "students",
                       time_limit_s=5, sweep_points=5, warm_start=None)

        first_point_sol = session.returned[3][0]   # solved at epsilon=10, landed at bound 7
        assert session.solve_hints[1] is first_point_sol   # epsilon=8: 7 <= 8, reuse it


class TestSeedingAcrossSolves:
    """Payoff solves have no epsilon bound, so any feasible timetable found earlier is a valid
    complete hint for them. The greedy warm start is usually not (it carries hard violations), and
    seeding only from it let every pair come back empty on a busy machine."""

    def _payoff_hints(self):
        hints = []
        session = FakeSession([_FakeSolveResult(2, 5), _FakeSolveResult(5, 3), _FakeSolveResult(10, 3)]
                              + [_FakeSolveResult(b, 3, status="FEASIBLE") for b in (9, 7, 5, 3, 2)])
        real = session.solve_unbounded

        def spy(minimize, fix=None, time_limit_s=30, hint=None):
            hints.append(hint)
            return real(minimize, fix, time_limit_s, hint)

        session.solve_unbounded = spy
        return session, hints

    def test_minimize_side_payoff_is_seeded_from_first_payoff_solution(self):
        session, hints = self._payoff_hints()
        greedy = object()
        with patch("engine.pareto_sweep.ParetoSession", return_value=session):
            sweep_pair(None, _small_problem(), "faculty", "students",
                       time_limit_s=5, sweep_points=5, warm_start=greedy)
        first_sol = session.returned[0][0]
        assert hints[0] is greedy        # nothing better exists yet
        assert hints[1] is first_sol     # min `minimize`: seeded from the feasible first solve

    def test_sweep_carries_a_feasible_solution_into_the_next_pair(self):
        from engine import pareto_sweep

        sessions = []

        def factory(problem, bound, minimize):
            session, hints = self._payoff_hints()
            session.hints = hints
            sessions.append(session)
            return session

        greedy = object()
        with patch("engine.pareto_sweep.ParetoSession", side_effect=factory), \
                patch("engine.solvers.greedy.GreedySolver") as greedy_cls:
            greedy_cls.return_value.solve.return_value = greedy
            pareto_sweep.sweep(_small_problem(), pairs=[("faculty", "students"), ("faculty", "labs")],
                               time_limit_s=5, sweep_points=5)
        assert sessions[0].hints[0] is greedy
        assert sessions[1].hints[0] is sessions[0].returned[0][0]
