"""Run timetable generation for all odd semester reference datasets.

Supports both:
  --mode pipeline (default): 4-stage hybrid chain (Greedy -> MIP -> GA -> CP-SAT)
  --mode cpsat: Direct CP-SAT solver run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path regardless of where this script is executed
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.io_json import problem_from_dict
from engine.pipeline import PipelineConfig, run_pipeline
from engine.solvers.cpsat import CPSATSolver

# Dataset files for odd semesters relative to repo root
ODD_DATASETS = {
    "SY-SEM3": REPO_ROOT / "data" / "reference" / "djsce_sy_sem3_jul_dec_2026.json",
    "TY-SEM5": REPO_ROOT / "data" / "reference" / "djsce_ty_d1_sem5_jul_dec_2026.json",
    "BTECH-SEM7": REPO_ROOT / "data" / "reference" / "djsce_btech_d2_sem7_jul_dec_2026.json",
}


def run_all_odd_semesters(
    mode: str = "pipeline",
    greedy_time: float = 2.0,
    mip_time: float = 30.0,
    ga_time: float = 15.0,
    cpsat_time: float = 60.0,
) -> dict:
    print(f"Starting timetable generation for all odd semesters (Mode: {mode})...")
    results = {}

    for sem_name, file_path in ODD_DATASETS.items():
        print(f"\n{'=' * 10} Processing {sem_name} {'=' * 10}")

        if not file_path.exists():
            print(f"Error: dataset file not found at {file_path}")
            results[sem_name] = {"status": "FAILED", "error": f"File not found: {file_path}"}
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        problem = problem_from_dict(data)

        try:
            if mode == "pipeline":
                config = PipelineConfig(
                    greedy_time_limit_s=greedy_time,
                    mip_time_limit_s=mip_time,
                    ga_time_limit_s=ga_time,
                    cpsat_time_limit_s=cpsat_time,
                )
                result = run_pipeline(problem, config=config)
                last_rep = result.reports[-1] if result.reports else None
                results[sem_name] = {
                    "status": result.final.status,
                    "stages_completed": len(result.reports),
                    "hard_violations": last_rep.hard_violations if last_rep else "N/A",
                    "soft_cost": round(last_rep.soft_cost, 2) if last_rep else "N/A",
                }
                print(
                    f"Finished {sem_name}: Status={result.final.status}, "
                    f"Hard Violations={results[sem_name]['hard_violations']}, "
                    f"Soft Cost={results[sem_name]['soft_cost']}"
                )

            elif mode == "cpsat":
                solver = CPSATSolver()
                solution = solver.solve(problem, time_limit_s=cpsat_time)
                results[sem_name] = {
                    "status": solution.status,
                    "objective": solution.objective_value,
                }
                print(f"Finished {sem_name}: Status={solution.status}, Objective={solution.objective_value}")

            else:
                raise ValueError(f"Unknown mode '{mode}'")

        except Exception as e:
            print(f"Failed to process {sem_name}: {e}")
            results[sem_name] = {"status": "FAILED", "error": str(e)}

    print("\n" + "=" * 30)
    print("Summary of Results:")
    print(json.dumps(results, indent=2))
    return results


def main():
    parser = argparse.ArgumentParser(description="Run timetable generation for all odd semesters.")
    parser.add_argument(
        "--mode",
        choices=["pipeline", "cpsat"],
        default="pipeline",
        help="Solving mode: 'pipeline' (4-stage hybrid chain) or 'cpsat' (direct CP-SAT solver)",
    )
    parser.add_argument("--greedy-time", type=float, default=2.0, help="Greedy timeout in seconds")
    parser.add_argument("--mip-time", type=float, default=30.0, help="MIP timeout in seconds")
    parser.add_argument("--ga-time", type=float, default=15.0, help="GA timeout in seconds")
    parser.add_argument("--cpsat-time", type=float, default=60.0, help="CP-SAT timeout in seconds")

    args = parser.parse_args()
    run_all_odd_semesters(
        mode=args.mode,
        greedy_time=args.greedy_time,
        mip_time=args.mip_time,
        ga_time=args.ga_time,
        cpsat_time=args.cpsat_time,
    )


if __name__ == "__main__":
    main()
