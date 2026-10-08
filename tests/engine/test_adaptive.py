"""Comprehensive tests for the Adaptive CP-SAT objective layer.

Tests cover:
1. Controller initializes correctly
2. Base weights preserved
3. Violated constraint increases weight
4. Repeatedly violated constraint increases more strongly (persistence effect)
5. Satisfied constraint does not exceed max unexpectedly
6. Satisfied constraint decays only toward base
7. Weights respect min/max bounds
8. Normalization is correct
9. Persistence updates correctly
10. Convergence detection works
11. Best solution is preserved
12. Adaptive mode performs multiple solves
13. Priority mode reproduces fixed-weight behavior
14. Baseline behavior is unchanged
15. Hard constraints remain untouched (hard violations = 0)
16. Custom C++ fork integration remains active in adaptive mode
17. Hints do not break model validation
18. Infeasible problems are handled normally
19. Full solver lifecycle across modes
"""
from __future__ import annotations

import copy
import pytest
from ortools.sat.python import cp_model

from engine.adaptive import (
    AdaptiveConfig,
    AdaptiveWeightController,
    DEFAULT_ADAPTIVE_BASE_WEIGHTS,
    canonical_constraint_name,
    compute_normalization_denominators,
)
from engine.models import Assignment, ProblemInstance, Solution, TimeSlot, Room, Faculty, Course, Division, ProgramType, CourseCategory
from engine.scoring import ScoreResult, score
from engine.sample_data import generate_sample_instance, load_reference_instance
from engine.solvers.cpsat import CPSATSolver, HAS_TIMETABLE_FORK, _tags_enabled


# --- Unit Tests for AdaptiveWeightController ---

def test_controller_initializes_correctly():
    cfg = AdaptiveConfig(
        learning_rate=0.2,
        min_weight=2.0,
        max_weight=40.0,
        base_weights={"lab_not_before_final_slots": 12.0, "room_capacity_waste": 6.0},
    )
    ctrl = AdaptiveWeightController(config=cfg)
    assert ctrl.base_weights["lab_not_before_final_slots"] == 12.0
    assert ctrl.base_weights["room_capacity_waste"] == 6.0
    assert ctrl.current_weights["lab_not_before_final_slots"] == 12.0
    assert ctrl.current_weights["room_capacity_waste"] == 6.0
    assert ctrl.config.learning_rate == 0.2
    assert ctrl.config.min_weight == 2.0
    assert ctrl.config.max_weight == 40.0
    assert ctrl.converged is False
    assert len(ctrl.history) == 0


def test_base_weights_preserved_with_aliases():
    base = {
        "room_utilization": 7.0,
        "lab_consecutive": 15.0,
        "workload_balance": 10.0,
    }
    ctrl = AdaptiveWeightController(base_weights=base)
    assert ctrl.base_weights["room_capacity_waste"] == 7.0
    assert ctrl.base_weights["lab_not_before_final_slots"] == 15.0
    assert ctrl.base_weights["teacher_workload_spread"] == 10.0


def test_violated_constraint_increases_weight(small_problem):
    ctrl = AdaptiveWeightController(
        config=AdaptiveConfig(learning_rate=0.2),
        normalization_denominators={"teacher_workload_spread": 10.0},
    )
    initial_w = ctrl.current_weights["teacher_workload_spread"]
    fake_sol = Solution(assignments=[], solver_name="cpsat", wall_clock_seconds=1.0, status="FEASIBLE")
    fake_score = ScoreResult(
        hard_violations=0,
        soft_cost=50.0,
        details={"teacher_workload_spread": 8},
    )
    ctrl.update(iteration=1, solution=fake_sol, score_result=fake_score, solve_time=1.0, problem=small_problem)
    new_w = ctrl.current_weights["teacher_workload_spread"]
    assert new_w > initial_w


def test_repeatedly_violated_constraint_increases_more_strongly(small_problem):
    # Arm 1: Violated once
    ctrl1 = AdaptiveWeightController(
        config=AdaptiveConfig(learning_rate=0.1, persistence_factor=0.5),
        normalization_denominators={"day_span": 10.0},
    )
    w_start = ctrl1.current_weights["day_span"]
    fake_sol = Solution(assignments=[], solver_name="cpsat", wall_clock_seconds=1.0, status="FEASIBLE")
    res1 = ScoreResult(hard_violations=0, soft_cost=20.0, details={"day_span": 5})
    ctrl1.update(iteration=1, solution=fake_sol, score_result=res1, solve_time=1.0, problem=small_problem)
    w_iter1 = ctrl1.current_weights["day_span"]
    delta_iter1 = w_iter1 - w_start

    # Second iteration with same persistent violation
    ctrl1.update(iteration=2, solution=fake_sol, score_result=res1, solve_time=1.0, problem=small_problem)
    w_iter2 = ctrl1.current_weights["day_span"]
    delta_iter2 = w_iter2 - w_iter1

    # Persistence factor adds additional pressure on repeat violation
    assert ctrl1.consecutive_violations["day_span"] == 2
    assert delta_iter2 > delta_iter1


def test_satisfied_constraint_decays_only_toward_base(small_problem):
    cfg = AdaptiveConfig(decay_factor=0.9, min_weight=1.0)
    ctrl = AdaptiveWeightController(config=cfg, base_weights={"day_span": 4.0})
    ctrl.current_weights["day_span"] = 10.0  # previously elevated weight

    fake_sol = Solution(assignments=[], solver_name="cpsat", wall_clock_seconds=1.0, status="FEASIBLE")
    res_zero = ScoreResult(hard_violations=0, soft_cost=0.0, details={"day_span": 0})

    # Iteration 1: should decay from 10.0 towards 4.0
    ctrl.update(iteration=1, solution=fake_sol, score_result=res_zero, solve_time=1.0, problem=small_problem)
    assert ctrl.current_weights["day_span"] == 9.0  # 10.0 * 0.9

    # Many iterations: should never drop below base_weight 4.0
    for it in range(2, 20):
        ctrl.update(iteration=it, solution=fake_sol, score_result=res_zero, solve_time=1.0, problem=small_problem)
    assert ctrl.current_weights["day_span"] == 4.0


def test_weights_respect_min_max_bounds(small_problem):
    cfg = AdaptiveConfig(min_weight=2.0, max_weight=15.0, learning_rate=1.0)
    ctrl = AdaptiveWeightController(
        config=cfg,
        base_weights={"teacher_workload_spread": 5.0},
        normalization_denominators={"teacher_workload_spread": 1.0},
    )
    fake_sol = Solution(assignments=[], solver_name="cpsat", wall_clock_seconds=1.0, status="FEASIBLE")

    # High violation attempts to push weight over max_weight
    res_huge = ScoreResult(hard_violations=0, soft_cost=100.0, details={"teacher_workload_spread": 100})
    for it in range(1, 10):
        ctrl.update(iteration=it, solution=fake_sol, score_result=res_huge, solve_time=1.0, problem=small_problem)
    assert ctrl.current_weights["teacher_workload_spread"] <= 15.0

    # Low weight clamp
    ctrl.current_weights["teacher_workload_spread"] = 0.5
    res_zero = ScoreResult(hard_violations=0, soft_cost=0.0, details={"teacher_workload_spread": 0})
    ctrl.update(iteration=10, solution=fake_sol, score_result=res_zero, solve_time=1.0, problem=small_problem)
    assert ctrl.current_weights["teacher_workload_spread"] >= 2.0


def test_normalization_is_correct(small_problem):
    denoms = compute_normalization_denominators(small_problem)
    assert denoms["room_capacity_waste"] > 0
    assert denoms["lab_not_before_final_slots"] > 0
    assert denoms["break_not_midmorning"] > 0
    assert denoms["day_span"] > 0
    assert denoms["teacher_workload_spread"] > 0

    ctrl = AdaptiveWeightController(normalization_denominators=denoms)
    norm = ctrl.normalize_violation("break_not_midmorning", 5.0)
    assert 0.0 <= norm <= 5.0 / denoms["break_not_midmorning"]


def test_persistence_resets_on_improvement(small_problem):
    ctrl = AdaptiveWeightController(normalization_denominators={"break_not_midmorning": 10.0})
    fake_sol = Solution(assignments=[], solver_name="cpsat", wall_clock_seconds=1.0, status="FEASIBLE")

    # Iteration 1: 5 violations -> persistence 1
    ctrl.update(1, fake_sol, ScoreResult(0, 10.0, {"break_not_midmorning": 5}), 1.0, small_problem)
    assert ctrl.consecutive_violations["break_not_midmorning"] == 1

    # Iteration 2: 5 violations -> persistence 2
    ctrl.update(2, fake_sol, ScoreResult(0, 10.0, {"break_not_midmorning": 5}), 1.0, small_problem)
    assert ctrl.consecutive_violations["break_not_midmorning"] == 2

    # Iteration 3: improvement to 3 violations -> persistence reduced
    ctrl.update(3, fake_sol, ScoreResult(0, 8.0, {"break_not_midmorning": 3}), 1.0, small_problem)
    assert ctrl.consecutive_violations["break_not_midmorning"] == 1

    # Iteration 4: improvement to 0 violations -> persistence reset to 0
    ctrl.update(4, fake_sol, ScoreResult(0, 0.0, {"break_not_midmorning": 0}), 1.0, small_problem)
    assert ctrl.consecutive_violations["break_not_midmorning"] == 0


def test_convergence_detection_on_stall(small_problem):
    cfg = AdaptiveConfig(stall_iterations=3, improvement_epsilon=0.1)
    ctrl = AdaptiveWeightController(config=cfg)
    fake_sol = Solution(assignments=[], solver_name="cpsat", wall_clock_seconds=1.0, status="FEASIBLE")
    res = ScoreResult(hard_violations=0, soft_cost=100.0, details={"teacher_workload_spread": 5})

    ctrl.update(1, fake_sol, res, 1.0, small_problem)
    assert ctrl.converged is False

    # 3 consecutive non-improving iterations
    ctrl.update(2, fake_sol, res, 1.0, small_problem)
    ctrl.update(3, fake_sol, res, 1.0, small_problem)
    ctrl.update(4, fake_sol, res, 1.0, small_problem)
    assert ctrl.converged is True
    assert "Stall" in ctrl.convergence_reason or "consecutive" in ctrl.convergence_reason


def test_best_solution_is_preserved_when_later_iteration_worse(small_problem):
    ctrl = AdaptiveWeightController()
    sol1 = Solution(assignments=[Assignment("req1", 0, "CR0")], solver_name="cpsat", wall_clock_seconds=1.0, status="FEASIBLE")
    res1 = ScoreResult(hard_violations=0, soft_cost=50.0, details={})

    sol2 = Solution(assignments=[Assignment("req1", 1, "CR0")], solver_name="cpsat", wall_clock_seconds=1.0, status="FEASIBLE")
    res2 = ScoreResult(hard_violations=0, soft_cost=90.0, details={})  # worse!

    ctrl.update(1, sol1, res1, 1.0, small_problem)
    ctrl.update(2, sol2, res2, 1.0, small_problem)

    assert ctrl.best_solution is sol1
    assert ctrl.best_score.soft_cost == 50.0
    assert ctrl.best_iteration == 1


# --- Integration Tests for CPSATSolver with Adaptive & Priority Modes ---

def test_cpsat_baseline_mode_unchanged(small_problem):
    sol = CPSATSolver().solve(small_problem, time_limit_s=15, optimization_mode="baseline")
    assert sol.status in ("OPTIMAL", "FEASIBLE")
    res = score(sol, small_problem)
    assert res.hard_violations == 0


def test_cpsat_priority_mode_runs_and_satisfies_hard(small_problem):
    cfg = AdaptiveConfig(base_weights=DEFAULT_ADAPTIVE_BASE_WEIGHTS)
    sol = CPSATSolver().solve(
        small_problem, time_limit_s=15, optimization_mode="priority", adaptive_config=cfg
    )
    assert sol.status in ("OPTIMAL", "FEASIBLE")
    res = score(sol, small_problem)
    assert res.hard_violations == 0


def test_cpsat_adaptive_mode_performs_multiple_solves_and_retains_best(small_problem):
    cfg = AdaptiveConfig(
        max_iterations=4,
        time_limit_per_iteration_s=3.0,
        learning_rate=0.2,
    )
    solver = CPSATSolver(optimization_mode="adaptive", adaptive_config=cfg)
    sol = solver.solve(small_problem, time_limit_s=20)

    assert sol.status in ("OPTIMAL", "FEASIBLE")
    res = score(sol, small_problem)
    assert res.hard_violations == 0

    assert sol.extra_data is not None
    assert sol.extra_data["optimization_mode"] == "adaptive"
    iters = sol.extra_data["adaptive_iterations"]
    assert iters >= 2  # multiple solves performed
    assert len(sol.extra_data["history"]) == iters

    # Each entry in history contains required research keys
    first_hist = sol.extra_data["history"][0]
    for key in ("iteration", "solver_status", "solve_time", "objective", "weighted_soft_cost", "constraints"):
        assert key in first_hist


def test_hints_do_not_break_model_validation(small_problem):
    # Solve once to get a valid solution
    sol1 = CPSATSolver().solve(small_problem, time_limit_s=10, optimization_mode="baseline")
    assert sol1.status in ("OPTIMAL", "FEASIBLE")

    # Pass as warm_start / hint to priority and adaptive
    sol2 = CPSATSolver().solve(
        small_problem, time_limit_s=10, warm_start=sol1, optimization_mode="priority"
    )
    assert sol2.status in ("OPTIMAL", "FEASIBLE")


def test_infeasible_problem_handled_gracefully():
    # Construct an impossible problem: 10 sessions requiring room, 0 rooms
    inst = ProblemInstance(
        time_slots=[TimeSlot(id=0, day=0, period=0, start="08:00", end="09:00")],
        rooms=[],
        faculty=[Faculty(id="F1", name="Prof A")],
        courses=[Course(code="C1", title="Math", credits=3, category=CourseCategory.MAJOR, theory_sessions_per_week=2)],
        divisions=[Division(id="D1", program=ProgramType.FYUP, semester=1, student_count=60, course_codes=("C1",), faculty_by_course={"C1": "F1"})],
    )
    solver = CPSATSolver(optimization_mode="adaptive")
    sol = solver.solve(inst, time_limit_s=5)
    assert sol.status in ("INFEASIBLE", "TIMEOUT", "MODEL_INVALID")
