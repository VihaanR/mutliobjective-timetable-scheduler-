"""Adaptive CP-SAT objective controller for multiobjective university timetabling.

Dynamically adapts the relative weights of soft timetable constraints across repeated
CP-SAT solve iterations using deterministic feedback from candidate solutions.

Core loop:
    initial soft weights
            ↓
    CP-SAT solve (with warm-start hint from incumbent)
            ↓
    evaluate individual soft-constraint violations
            ↓
    calculate normalized violation pressure & persistence
            ↓
    update & clamp soft weights
            ↓
    rebuild objective and repeat until convergence or max iterations
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engine.models import ProblemInstance, Solution, SessionType, expand_requirements
from engine.scoring import ScoreResult, better, score

# Default calibrated base weights aligned with ground-truth scorer (scoring.py) and baseline CP-SAT
DEFAULT_ADAPTIVE_BASE_WEIGHTS: dict[str, float] = {
    "room_capacity_waste": 0.5,         # room_utilization (proportional to ground truth 0.1)
    "lab_not_before_final_slots": 10.0, # late lab avoidance
    "break_not_midmorning": 5.0,        # student_free_period_distribution
    "day_span": 20.0,                   # avoid_last_period / compact day span
    "idle_gaps": 150.0,                 # student gap minimization (baseline 150.0, scorer 100.0)
    "consecutive_gaps": 800.0,          # penalize multi-hour holes in student day (baseline 800.0)
    "teacher_workload_spread": 2.0,     # gentle workload balance without compromising student gaps
}

# Aliases to map external/research names to canonical engine constraint keys
CONSTRAINT_ALIASES: dict[str, str] = {
    "room_utilization": "room_capacity_waste",
    "lab_consecutive": "lab_not_before_final_slots",
    "student_free_period_distribution": "break_not_midmorning",
    "avoid_first_period": "break_not_midmorning",
    "avoid_last_period": "day_span",
    "workload_balance": "teacher_workload_spread",
    "faculty_preference": "teacher_workload_spread",
    "faculty_workload_balance": "teacher_workload_spread",
}


def canonical_constraint_name(name: str) -> str:
    """Resolve aliases to the canonical engine soft-constraint identifier."""
    return CONSTRAINT_ALIASES.get(name, name)


def compute_normalization_denominators(problem: ProblemInstance) -> dict[str, float]:
    """Calculate maximum opportunity scales for each soft constraint to normalize violations."""
    requirements = expand_requirements(problem)
    days = max(1, problem.days_per_week)
    num_divs = max(1, len(problem.divisions))
    num_faculty = max(1, len(problem.faculty))

    max_room_cap = max((r.capacity for r in problem.rooms), default=60)
    room_sessions = sum(req.duration_slots for req in requirements if req.room_type in ("classroom", "lab"))
    waste_denom = max(1.0, float(max_room_cap * room_sessions))

    practical_count = sum(1 for req in requirements if req.session_type == SessionType.PRACTICAL)
    lab_denom = max(1.0, float(practical_count))

    slots_per_day = len(problem.time_slots) // days if days > 0 else 8
    max_break_dist = max(1, slots_per_day // 2)
    break_denom = max(1.0, float(num_divs * days * max_break_dist))

    # Day span beyond compact span (7): at most ~2 hours excess per division per day
    span_denom = max(1.0, float(num_divs * days * 2.0))

    # Idle gaps across week: at most ~2 hours per division per day
    gaps_denom = max(1.0, float(num_divs * days * 2.0))
    consecutive_gaps_denom = max(1.0, float(num_divs * days * 1.0))

    # Max workload spread per faculty across week: bounded by daily cap (6)
    workload_denom = max(1.0, float(num_faculty * 6.0))

    return {
        "room_capacity_waste": waste_denom,
        "lab_not_before_final_slots": lab_denom,
        "break_not_midmorning": break_denom,
        "day_span": span_denom,
        "idle_gaps": gaps_denom,
        "consecutive_gaps": consecutive_gaps_denom,
        "teacher_workload_spread": workload_denom,
    }


@dataclass
class AdaptiveConfig:
    """Configuration for adaptive CP-SAT objective reweighting."""
    enabled: bool = True
    max_iterations: int = 3
    learning_rate: float = 0.15
    decay_factor: float = 0.95
    min_weight: float = 0.1
    max_weight: float = 50.0
    stall_iterations: int = 3
    current_weight_factor: float = 1.0     # factor 'a'
    previous_weight_factor: float = 0.5    # factor 'b'
    persistence_factor: float = 0.25       # factor 'c'
    improvement_epsilon: float = 0.01
    time_limit_per_iteration_s: float | None = None
    base_weights: dict[str, float] | None = None


class AdaptiveWeightController:
    """Manages dynamic soft-constraint weight adaptation across CP-SAT solve iterations."""

    def __init__(
        self,
        config: AdaptiveConfig | None = None,
        base_weights: dict[str, float] | None = None,
        normalization_denominators: dict[str, float] | None = None,
    ) -> None:
        self.config = config or AdaptiveConfig()

        # Initialize base weights
        raw_base = base_weights if base_weights is not None else (self.config.base_weights or DEFAULT_ADAPTIVE_BASE_WEIGHTS)
        self.base_weights: dict[str, float] = {}
        for k, v in raw_base.items():
            canonical = canonical_constraint_name(k)
            clamped_val = max(self.config.min_weight, float(v))
            self.base_weights[canonical] = clamped_val

        # Ensure all standard constraints have a base weight
        for k, v in DEFAULT_ADAPTIVE_BASE_WEIGHTS.items():
            if k not in self.base_weights:
                self.base_weights[k] = v

        self.current_weights: dict[str, float] = dict(self.base_weights)
        self.previous_normalized_violations: dict[str, float] = {k: 0.0 for k in self.base_weights}
        self.previous_raw_violations: dict[str, float] = {k: 0.0 for k in self.base_weights}
        self.consecutive_violations: dict[str, int] = {k: 0 for k in self.base_weights}
        self.normalization_denominators: dict[str, float] = normalization_denominators or {}

        self.history: list[dict[str, Any]] = []
        self.stall_count: int = 0
        self.best_solution: Solution | None = None
        self.best_score: ScoreResult | None = None
        self.best_iteration: int | None = None
        self.best_weights: dict[str, float] | None = None
        self.converged: bool = False
        self.convergence_reason: str = ""

    def set_normalization_denominators(self, denominators: dict[str, float]) -> None:
        self.normalization_denominators = dict(denominators)

    def normalize_violation(self, constraint: str, raw_violation: float) -> float:
        denom = self.normalization_denominators.get(constraint)
        if not denom or denom <= 0:
            denom = max(1.0, float(raw_violation))
        return float(raw_violation) / denom

    def calculate_pressure(
        self,
        current_norm: float,
        prev_norm: float,
        consecutive_count: int,
    ) -> float:
        return (
            self.config.current_weight_factor * current_norm
            + self.config.previous_weight_factor * prev_norm
            + self.config.persistence_factor * float(consecutive_count)
        )

    def update(
        self,
        iteration: int,
        solution: Solution,
        score_result: ScoreResult,
        solve_time: float,
        problem: ProblemInstance,
    ) -> dict[str, Any]:
        """Update weights based on iteration feedback, track best solution, and check convergence."""
        old_weights = dict(self.current_weights)
        is_feasible = solution.status in ("OPTIMAL", "FEASIBLE") and score_result.hard_violations == 0

        # 1. Best solution evaluation (lexicographic: hard feasibility dominates, soft cost breaks ties)
        if is_feasible:
            if self.best_solution is None or self.best_score is None:
                is_candidate_better = True
            else:
                is_candidate_better = (score_result.key() < self.best_score.key())
        else:
            is_candidate_better = False

        improved = False
        if is_candidate_better:
            if self.best_score is None:
                improved = True
                self.stall_count = 0
            else:
                hard_diff = self.best_score.hard_violations - score_result.hard_violations
                soft_diff = self.best_score.soft_cost - score_result.soft_cost
                if hard_diff > 0 or (hard_diff == 0 and soft_diff > self.config.improvement_epsilon):
                    improved = True
                    self.stall_count = 0
                else:
                    self.stall_count += 1

            self.best_solution = solution
            self.best_score = score_result
            self.best_iteration = iteration
            self.best_weights = dict(old_weights)
        else:
            self.stall_count += 1

        # 2. Constraint pressure and weight updates
        constraint_meta: dict[str, dict[str, Any]] = {}
        max_weight_delta = 0.0
        all_soft_zero = is_feasible and (len(score_result.details) > 0)

        for k in self.base_weights:
            if is_feasible:
                v_raw = float(score_result.details.get(k, 0.0))
            else:
                v_raw = self.previous_raw_violations.get(k, 0.0)

            if v_raw > 0:
                all_soft_zero = False

            v_norm = self.normalize_violation(k, v_raw)
            v_prev_norm = self.previous_normalized_violations.get(k, 0.0)
            v_prev_raw = self.previous_raw_violations.get(k, 0.0)

            curr_w = old_weights[k]
            base_w = self.base_weights[k]

            if is_feasible:
                # Persistence tracking
                if v_raw > 0:
                    if v_raw >= v_prev_raw and v_prev_raw > 0:
                        self.consecutive_violations[k] = self.consecutive_violations.get(k, 0) + 1
                    else:
                        self.consecutive_violations[k] = max(1, self.consecutive_violations.get(k, 0) - 1)
                else:
                    self.consecutive_violations[k] = 0

                pers_count = self.consecutive_violations[k]
                pressure = self.calculate_pressure(v_norm, v_prev_norm, pers_count)

                if v_raw > 0:
                    upper_bound = max(self.config.max_weight, base_w)
                    new_w = min(upper_bound, curr_w * (1.0 + self.config.learning_rate * pressure))
                else:
                    # Controlled decay toward base weight for satisfied constraints: max[W(k,0), 0.95 * W(k,t)]
                    new_w = max(base_w, curr_w * self.config.decay_factor)

                # Lower bound clamping
                new_w = max(self.config.min_weight, new_w)
                self.current_weights[k] = new_w

                delta = abs(new_w - curr_w)
                if delta > max_weight_delta:
                    max_weight_delta = delta

                # Update history state
                self.previous_normalized_violations[k] = v_norm
                self.previous_raw_violations[k] = v_raw
            else:
                # Infeasible or timeout: preserve previous weights
                new_w = old_weights[k]
                pers_count = self.consecutive_violations.get(k, 0)
                pressure = 0.0

            constraint_meta[k] = {
                "violations": v_raw if is_feasible else None,
                "normalized": round(v_norm, 4) if is_feasible else None,
                "old_weight": round(curr_w, 4),
                "new_weight": round(new_w, 4),
                "persistence": pers_count,
                "pressure": round(pressure, 4),
            }

        # 3. Convergence detection
        if not self.converged:
            if is_feasible and all_soft_zero:
                self.converged = True
                self.convergence_reason = "All soft constraints fully satisfied (zero violations)"
            elif self.stall_count >= self.config.stall_iterations:
                self.converged = True
                self.convergence_reason = (
                    f"No meaningful improvement for {self.stall_count} consecutive iterations"
                )
            elif is_feasible and iteration > 1 and max_weight_delta < 1e-4:
                self.converged = True
                self.convergence_reason = "Weight adjustments stabilized (delta < 1e-4)"

        iteration_entry: dict[str, Any] = {
            "iteration": iteration,
            "solver_status": solution.status,
            "solve_time": round(solve_time, 3),
            "objective": solution.objective_value,
            "weighted_soft_cost": round(score_result.soft_cost, 2),
            "hard_violations": score_result.hard_violations,
            "constraints": constraint_meta,
            "improved": improved,
            "converged": self.converged,
            "convergence_reason": self.convergence_reason if self.converged else None,
        }
        self.history.append(iteration_entry)
        return iteration_entry
