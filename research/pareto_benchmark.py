"""Old-vs-new Pareto sweep benchmark (spec: docs/superpowers/specs/2026-09-23-pareto-filter-epsilon-design.md).

- `old`: the sweep as it was before the AUGMECON2 upgrade, reproduced here verbatim in behaviour --
  non-lexicographic payoff table, epsilon tight->loose, a fresh `_build_model()` per solve via
  `solve_pareto_point()`, no bypass, no early exit. The dominance filter is applied afterwards so
  both arms are scored on the same frontier definition.
- `new`: `engine.pareto_sweep.sweep()` -- lexicographic payoff, completed bound-feasible hints,
  loose->tight with bypass and early exit, one reused `ParetoSession` per pair.

Reports per pair: wall-clock, CP-SAT solves issued, and non-dominated frontier size, as a median
and range over repeated trials. CP-SAT runs 8 non-deterministic workers, so no single trial is
evidence (CHANGES_DONE.md §6.2) -- and a "no speed-up" result is recorded as such.

    .venv-fork/Scripts/python -m research.pareto_benchmark --trials 10 --time-limit 30 \\
        --out research/pareto_benchmark.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

from engine.pareto import pareto_filter
from engine.pareto_sweep import DEFAULT_PAIRS, FrontierPoint, _epsilon_grid, sweep
from engine.scoring import score
import engine.solvers.cpsat as cpsat_module
from engine.solvers.cpsat import CPSATSolver

_SOLVE_COUNT = 0
_real_solve_and_decode = cpsat_module._solve_and_decode


def _counting_solve_and_decode(*args, **kwargs):
    # every CP-SAT Solve() in both arms goes through here, including payoff solves of a pair that
    # later fails -- counting it directly beats inferring the count from returned points
    global _SOLVE_COUNT
    _SOLVE_COUNT += 1
    return _real_solve_and_decode(*args, **kwargs)


cpsat_module._solve_and_decode = _counting_solve_and_decode


def _old_sweep_pair(problem, bound: str, minimize: str, time_limit: float,
                    sweep_points: int, warm_start) -> list[FrontierPoint]:
    solver = CPSATSolver()
    _s, vb = solver.solve_pareto_point(problem, bounds={}, minimize=bound,
                                       time_limit_s=time_limit, warm_start=warm_start)
    _s, vm = solver.solve_pareto_point(problem, bounds={}, minimize=minimize,
                                       time_limit_s=time_limit, warm_start=warm_start)
    if vb[bound] is None or vm[bound] is None:
        return []
    points = []
    for epsilon in _epsilon_grid(vb[bound], vm[bound], sweep_points):
        t0 = time.time()
        sol, vals = solver.solve_pareto_point(problem, bounds={bound: epsilon}, minimize=minimize,
                                              time_limit_s=time_limit, warm_start=warm_start)
        points.append(FrontierPoint(
            pair=f"{bound}<=eps,min={minimize}", epsilon=epsilon, bound_category=bound,
            minimize_category=minimize, bound_value=vals[bound], minimize_value=vals[minimize],
            hard_violations=score(sol, problem).hard_violations if sol.assignments else -1,
            wall_s=time.time() - t0, optimal=sol.status == "OPTIMAL",
        ))
    return pareto_filter(points)


def _frontier_size(points: list[FrontierPoint]) -> int:
    return sum(1 for p in points if not p.dominated)


def _trial(problem, arm: str, pairs, time_limit: float, sweep_points: int) -> dict:
    from engine.solvers.greedy import GreedySolver

    global _SOLVE_COUNT
    out = {}
    for bound, minimize in pairs:
        label = f"{bound}<=eps,min={minimize}"
        _SOLVE_COUNT = 0
        t0 = time.time()
        # both arms pay for their greedy warm start inside the timed region (sweep() builds its own)
        if arm == "old":
            points = _old_sweep_pair(problem, bound, minimize, time_limit, sweep_points,
                                     GreedySolver().solve(problem))
        else:
            points = sweep(problem, pairs=[(bound, minimize)], time_limit_s=time_limit,
                           sweep_points=sweep_points)[label]
        out[label] = {"wall_s": time.time() - t0, "solves": _SOLVE_COUNT,
                      "frontier": _frontier_size(points)}
    return out


def _dist(values: list[float]) -> str:
    return f"{statistics.median(values):.1f} [{min(values):.1f}-{max(values):.1f}]"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=5, help="repeats per arm (default 5)")
    ap.add_argument("--time-limit", type=float, default=30.0, help="seconds per CP-SAT solve")
    ap.add_argument("--sweep-points", type=int, default=5)
    ap.add_argument("--arms", nargs="*", default=["old", "new"], choices=["old", "new"])
    ap.add_argument("--pairs", nargs="*", metavar="BOUND:MIN",
                    help="default: engine.pareto_sweep.DEFAULT_PAIRS")
    ap.add_argument("--instance", choices=["reference", "small"], default="reference")
    ap.add_argument("--out", default="research/pareto_benchmark.json")
    args = ap.parse_args()

    from engine.sample_data import generate_sample_instance, load_reference_instance
    problem = (load_reference_instance() if args.instance == "reference"
               else generate_sample_instance("small", seed=42))
    pairs = [tuple(p.split(":")) for p in args.pairs] if args.pairs else DEFAULT_PAIRS

    results: dict[str, list[dict]] = {arm: [] for arm in args.arms}
    for trial in range(args.trials):
        for arm in args.arms:  # interleaved, so machine drift hits both arms alike
            row = _trial(problem, arm, pairs, args.time_limit, args.sweep_points)
            results[arm].append(row)
            print(f"trial {trial} {arm:<3} " + "  ".join(
                f"{pair}: {r['wall_s']:.1f}s/{r['solves']} solves/{r['frontier']} pts"
                for pair, r in row.items()), flush=True)

    print(f"\n{args.trials} trials/arm, {args.time_limit}s per solve, instance={args.instance}")
    for pair in results[args.arms[0]][0]:
        print(f"\n{pair}")
        for arm in args.arms:
            rows = [r[pair] for r in results[arm]]
            print(f"  {arm:<3} wall {_dist([r['wall_s'] for r in rows])} s   "
                  f"solves {_dist([r['solves'] for r in rows])}   "
                  f"frontier pts {_dist([r['frontier'] for r in rows])}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"args": vars(args), "results": results}, f, indent=2)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
