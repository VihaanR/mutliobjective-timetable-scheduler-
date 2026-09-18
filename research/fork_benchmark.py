"""Stock-vs-fork benchmark for the timetabling fork of OR-Tools CP-SAT.

Runs each experimental arm over many seeds and reports a distribution, because a single run
from this system is not evidence: CP-SAT uses num_search_workers=8 and its parallel portfolio
is non-deterministic by default. Measured on this project, an *unmodified* model gave
PASS/PASS/PASS/FAIL/FAIL over five runs of one borderline test.

Usage
-----
    # confirm which build you are on first -- this exits non-zero on stock
    python third_party/or-tools-fork/verify_fork.py

    # the full sweep
    python -m research.fork_benchmark --seeds 5 --time-limit 60

    # one arm, e.g. while iterating
    python -m research.fork_benchmark --arms A E --seeds 3

Each trial runs in its own subprocess. That is deliberate: HAS_TIMETABLE_FORK is resolved at
import time, so switching arms in-process would not re-evaluate it, and a crashed or
OOM-killed trial must not take the whole sweep with it.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from dataclasses import dataclass, asdict


# Arms. `requires_fork` gates the ones that are meaningless on a stock wheel.
#
# B is the arm reviewers ask about and the easiest to skip: it runs the forked binary with
# both features switched off, and must match A. That is what demonstrates the patch is inert
# when disabled, i.e. that any difference in C/D/E comes from the features rather than from
# the build differing in some unrelated way.
@dataclass(frozen=True)
class Arm:
    key: str
    label: str
    env: dict[str, str]
    requires_fork: bool


ARMS: dict[str, Arm] = {
    "A": Arm("A", "stock baseline",
             {"TIMETABLE_MRV": "off", "TIMETABLE_FORK_TAGS": "0"}, False),
    "B": Arm("B", "fork, both features off (must match A)",
             {"TIMETABLE_MRV": "off", "TIMETABLE_FORK_TAGS": "0",
              "TIMETABLE_DIVISION_DAY_LNS": "0"}, True),
    "C": Arm("C", "static ordering only (Tier-0 ablation)",
             {"TIMETABLE_MRV": "static", "TIMETABLE_FORK_TAGS": "1",
              "TIMETABLE_DIVISION_DAY_LNS": "0"}, False),
    "D": Arm("D", "division_day_lns only",
             {"TIMETABLE_MRV": "off", "TIMETABLE_FORK_TAGS": "1",
              "TIMETABLE_DIVISION_DAY_LNS": "1"}, True),
    "E": Arm("E", "both features (headline)",
             {"TIMETABLE_MRV": "dynamic", "TIMETABLE_FORK_TAGS": "1",
              "TIMETABLE_DIVISION_DAY_LNS": "1"}, True),
}


def _run_trial(seed: int, time_limit: float) -> dict:
    """Solve once and emit one JSON line. Runs as `python -m research.fork_benchmark --trial`."""
    from engine.sample_data import load_reference_instance
    from engine.scoring import score
    from engine.solvers.cpsat import CPSATSolver, HAS_TIMETABLE_FORK

    problem = load_reference_instance()
    solution = CPSATSolver().solve(
        problem, time_limit_s=time_limit, extra_solver_params={"random_seed": seed})
    sc = score(solution, problem)
    return {
        "seed": seed,
        "status": solution.status,
        "wall_clock": round(solution.wall_clock_seconds, 3),
        "objective": solution.objective_value,
        "penalty": sc.soft_cost,
        "hard_violations": sc.hard_violations,
        "assignments": len(solution.assignments),
        "fork_build": HAS_TIMETABLE_FORK,
    }


def _summarise(rows: list[dict], field: str) -> str:
    """Median and full range. Never a bare mean: one OOM-slowed run would dominate it."""
    vals = [r[field] for r in rows if isinstance(r.get(field), (int, float))]
    if not vals:
        return "n/a"
    if len(vals) == 1:
        return f"{vals[0]:.1f} (n=1)"
    return (f"{statistics.median(vals):.1f} "
            f"[{min(vals):.1f}–{max(vals):.1f}]")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=5, help="trials per arm (default 5)")
    ap.add_argument("--time-limit", type=float, default=60.0, help="seconds per solve")
    ap.add_argument("--arms", nargs="*", default=list(ARMS), metavar="ARM",
                    help=f"subset of {' '.join(ARMS)} (default: all)")
    ap.add_argument("--out", default="research/fork_benchmark_results.json")
    ap.add_argument("--trial", type=int, help=argparse.SUPPRESS)  # internal worker mode
    args = ap.parse_args()

    if args.trial is not None:
        print(json.dumps(_run_trial(args.trial, args.time_limit)))
        return 0

    from engine.solvers.cpsat import HAS_TIMETABLE_FORK
    print(f"interpreter : {sys.executable}")
    print(f"build       : {'FORK' if HAS_TIMETABLE_FORK else 'STOCK'}")
    print(f"seeds/arm   : {args.seeds}   time limit: {args.time_limit}s\n")

    results: dict[str, list[dict]] = {}
    for key in args.arms:
        arm = ARMS.get(key)
        if arm is None:
            print(f"  skip {key}: unknown arm")
            continue
        if arm.requires_fork and not HAS_TIMETABLE_FORK:
            # Not a soft warning: running it anyway would silently produce stock numbers
            # under a fork label, which is the single easiest way to corrupt this experiment.
            print(f"  SKIP {key} ({arm.label}): requires the forked wheel")
            continue

        print(f"arm {key}: {arm.label}")
        rows: list[dict] = []
        for seed in range(args.seeds):
            env = {**os.environ, **arm.env}
            proc = subprocess.run(
                [sys.executable, "-m", "research.fork_benchmark",
                 "--trial", str(seed), "--time-limit", str(args.time_limit)],
                env=env, capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"    seed {seed}: FAILED rc={proc.returncode} {proc.stderr.strip()[-200:]}")
                continue
            row = json.loads(proc.stdout.strip().splitlines()[-1])
            rows.append(row)
            print(f"    seed {seed}: {row['status']:<9} "
                  f"{row['wall_clock']:>7.2f}s  penalty={row['penalty']}  "
                  f"hard={row['hard_violations']}")
        results[key] = rows
        if rows:
            print(f"    -> wall {_summarise(rows, 'wall_clock')}   "
                  f"penalty {_summarise(rows, 'penalty')}\n")

    print("\n" + "=" * 72)
    print(f"{'arm':<4} {'n':>3}  {'wall clock (median [range])':<28} {'penalty':<24}")
    print("-" * 72)
    for key, rows in results.items():
        print(f"{key:<4} {len(rows):>3}  {_summarise(rows, 'wall_clock'):<28} "
              f"{_summarise(rows, 'penalty'):<24}")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"arms": {k: asdict(ARMS[k]) for k in results}, "results": results},
                  fh, indent=2)
    print(f"\nwrote {args.out}")

    if "A" in results and "B" in results and results["A"] and results["B"]:
        a = statistics.median(r["penalty"] for r in results["A"] if r["penalty"] is not None)
        b = statistics.median(r["penalty"] for r in results["B"] if r["penalty"] is not None)
        note = "match" if a == b else f"DIFFER (A={a}, B={b}) -- investigate before trusting C/D/E"
        print(f"\nArm A vs B (patch-inert check): {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
