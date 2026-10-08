"""Research benchmark comparing Priority CP-SAT vs Adaptive CP-SAT.

Measures:
- Hard violations
- Total soft penalty
- Individual soft violations (workload balance, room waste, lab slots, break placement, day span)
- Total solve time
- Adaptive iterations and convergence status
- Initial vs final weights and weight movement
- Solution quality and stability across seeds
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from engine.adaptive import AdaptiveConfig, DEFAULT_ADAPTIVE_BASE_WEIGHTS
from engine.sample_data import generate_sample_instance, load_reference_instance
from engine.scoring import score
from engine.solvers.cpsat import CPSATSolver, HAS_TIMETABLE_FORK


@dataclass
class TrialResult:
    mode: str
    seed: int
    hard_violations: int
    soft_penalty: float
    solve_time_s: float
    objective_value: float | None
    violations: dict[str, float]
    iterations: int
    converged: bool
    convergence_reason: str | None
    best_iteration: int | None
    initial_weights: dict[str, float]
    final_weights: dict[str, float]
    history: list[dict[str, Any]]


def run_trial(
    mode: str,
    dataset: str,
    seed: int,
    time_limit_s: float,
    max_iterations: int = 6,
    learning_rate: float = 0.15,
) -> TrialResult:
    if dataset == "reference":
        problem = load_reference_instance()
    else:
        problem = generate_sample_instance("small", seed=seed)

    base_weights = dict(DEFAULT_ADAPTIVE_BASE_WEIGHTS)
    config = AdaptiveConfig(
        max_iterations=max_iterations,
        learning_rate=learning_rate,
        base_weights=base_weights,
    )

    solver = CPSATSolver(
        optimization_mode=mode,
        adaptive_config=config,
    )

    extra_params = {
        "random_seed": seed,
    }

    t0 = time.time()
    solution = solver.solve(
        problem,
        time_limit_s=time_limit_s,
        extra_solver_params=extra_params,
        optimization_mode=mode,
        adaptive_config=config,
    )
    wall_clock = time.time() - t0

    sc = score(solution, problem)
    violations = {k: float(sc.details.get(k, 0)) for k in base_weights}

    extra = solution.extra_data or {}
    iterations = extra.get("adaptive_iterations", 1)
    converged = extra.get("converged", False)
    conv_reason = extra.get("convergence_reason")
    best_iter = extra.get("best_iteration")
    final_weights = extra.get("final_weights", base_weights)
    history = extra.get("history", [])

    return TrialResult(
        mode=mode,
        seed=seed,
        hard_violations=sc.hard_violations,
        soft_penalty=sc.soft_cost,
        solve_time_s=round(wall_clock, 2),
        objective_value=solution.objective_value,
        violations=violations,
        iterations=iterations,
        converged=converged,
        convergence_reason=conv_reason,
        best_iteration=best_iter,
        initial_weights=base_weights,
        final_weights=final_weights,
        history=history,
    )


def print_markdown_report(results: dict[str, list[TrialResult]]) -> None:
    print("\n" + "=" * 70)
    print("EXPERIMENTAL BENCHMARK REPORT: Priority CP-SAT vs Adaptive CP-SAT")
    print(f"OR-Tools Fork Detected: {HAS_TIMETABLE_FORK}")
    print("=" * 70 + "\n")

    modes = list(results.keys())
    if "priority" not in modes or "adaptive" not in modes:
        print("Both 'priority' and 'adaptive' results required for comparison table.")
        return

    p_trials = results["priority"]
    a_trials = results["adaptive"]

    def avg(lst, key):
        vals = [getattr(t, key) for t in lst]
        return round(statistics.mean(vals), 2)

    def avg_v(lst, vkey):
        vals = [t.violations.get(vkey, 0.0) for t in lst]
        return round(statistics.mean(vals), 2)

    p_hard = avg(p_trials, "hard_violations")
    a_hard = avg(a_trials, "hard_violations")

    p_soft = avg(p_trials, "soft_penalty")
    a_soft = avg(a_trials, "soft_penalty")

    p_time = avg(p_trials, "solve_time_s")
    a_time = avg(a_trials, "solve_time_s")

    a_iters = avg(a_trials, "iterations")
    a_conv = "yes" if any(t.converged for t in a_trials) else "no"

    print("### Summary Comparison Table\n")
    print("| Metric | Priority CP-SAT | Adaptive CP-SAT | Delta / Impact |")
    print("|---|---:|---:|---:|")
    print(f"| Hard Violations | {p_hard} | {a_hard} | {'Maintained 0' if p_hard == 0 and a_hard == 0 else (a_hard - p_hard)} |")
    print(f"| Soft Penalty | {p_soft} | {a_soft} | {round(a_soft - p_soft, 2)} ({'-' if a_soft < p_soft else '+'}{abs(round((p_soft - a_soft) / max(1, p_soft) * 100, 1))}%) |")
    print(f"| Workload Spread | {avg_v(p_trials, 'teacher_workload_spread')} | {avg_v(a_trials, 'teacher_workload_spread')} | {round(avg_v(a_trials, 'teacher_workload_spread') - avg_v(p_trials, 'teacher_workload_spread'), 2)} |")
    print(f"| Room Waste | {avg_v(p_trials, 'room_capacity_waste')} | {avg_v(a_trials, 'room_capacity_waste')} | {round(avg_v(a_trials, 'room_capacity_waste') - avg_v(p_trials, 'room_capacity_waste'), 2)} |")
    print(f"| Late Lab Sessions | {avg_v(p_trials, 'lab_not_before_final_slots')} | {avg_v(a_trials, 'lab_not_before_final_slots')} | {round(avg_v(a_trials, 'lab_not_before_final_slots') - avg_v(p_trials, 'lab_not_before_final_slots'), 2)} |")
    print(f"| Break Dist Penalty | {avg_v(p_trials, 'break_not_midmorning')} | {avg_v(a_trials, 'break_not_midmorning')} | {round(avg_v(a_trials, 'break_not_midmorning') - avg_v(p_trials, 'break_not_midmorning'), 2)} |")
    print(f"| Day Span Penalty | {avg_v(p_trials, 'day_span')} | {avg_v(a_trials, 'day_span')} | {round(avg_v(a_trials, 'day_span') - avg_v(p_trials, 'day_span'), 2)} |")
    print(f"| Solve Time (s) | {p_time}s | {a_time}s | {round(a_time - p_time, 2)}s |")
    print(f"| Adaptive Iterations | N/A | {a_iters} | N/A |")
    print(f"| Convergence | N/A | {a_conv} | N/A |")

    # Print Iteration Trace of first Adaptive trial
    first_adaptive = a_trials[0]
    if first_adaptive.history:
        print("\n### Adaptive Iteration Trace (Representative Trial)\n")
        for h in first_adaptive.history:
            it = h["iteration"]
            obj = h.get("objective")
            soft = h.get("weighted_soft_cost")
            impr = " [IMPROVED]" if h.get("improved") else ""
            print(f"**Iteration {it}** (Status: {h['solver_status']}, SolveTime: {h['solve_time']}s, SoftCost: {soft}{impr})")
            w_strs = []
            v_strs = []
            for c_name, c_data in h["constraints"].items():
                v_strs.append(f"{c_name}: {c_data['violations']}")
                w_strs.append(f"{c_name}: {c_data['old_weight']} -> {c_data['new_weight']}")
            print(f"  - Violations: {', '.join(v_strs)}")
            print(f"  - Weight Trajectory: {', '.join(w_strs)}")
            if h.get("converged"):
                print(f"  - Converged: {h.get('convergence_reason')}")
            print()


def main():
    parser = argparse.ArgumentParser(description="Adaptive CP-SAT Benchmark Runner")
    parser.add_argument("--dataset", choices=["reference", "small"], default="reference")
    parser.add_argument("--time-limit", type=float, default=25.0)
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--seeds", type=str, default="42,43,44")
    parser.add_argument("--out", type=str, default="research/adaptive_benchmark_results.json")
    args = parser.parse_args()

    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]
    results: dict[str, list[TrialResult]] = {"priority": [], "adaptive": []}

    print(f"Running benchmark on dataset='{args.dataset}', seeds={seeds}, time_limit={args.time_limit}s, iters={args.max_iterations}")

    for seed in seeds:
        print(f"\n--- Running Seed {seed} ---")
        print(f"  Solving Priority CP-SAT...")
        p_res = run_trial("priority", args.dataset, seed, args.time_limit, args.max_iterations)
        results["priority"].append(p_res)
        print(f"    Done: hard={p_res.hard_violations}, soft={p_res.soft_penalty}, time={p_res.solve_time_s}s")

        print(f"  Solving Adaptive CP-SAT...")
        a_res = run_trial("adaptive", args.dataset, seed, args.time_limit, args.max_iterations)
        results["adaptive"].append(a_res)
        print(f"    Done: hard={a_res.hard_violations}, soft={a_res.soft_penalty}, iters={a_res.iterations}, time={a_res.solve_time_s}s")

    # Serialize results to json
    serialized = {
        mode: [asdict(t) for t in trial_list]
        for mode, trial_list in results.items()
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(serialized, f, indent=2)
    print(f"\nSaved benchmark results to {out_path}")

    # Output markdown report
    print_markdown_report(results)


if __name__ == "__main__":
    main()
