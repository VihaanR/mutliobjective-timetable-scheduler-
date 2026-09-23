# Pareto filter + AUGMECON2 epsilon-constraint upgrade — design

**Date:** 2026-09-23 · **Status:** implemented (benchmark pending)
**Scope:** sub-project 1 of 4 from `docs/or_tools_source_modification_guide.md` follow-ups. Python
only; no OR-Tools rebuild. The native C++ sweep (guide §4.4) is a later, separate spec, justified
only if this work's benchmark shows per-point rebuild/presolve dominates sweep time.

## 1. Problem

`engine/pareto_sweep.py` produces pairwise (2-D) frontiers by bounding one objective category at
`<= ε` and minimising another via `CPSATSolver.solve_pareto_point()` (`engine/solvers/cpsat.py`).
Today it:

1. **Has no dominance filter.** Every grid point is returned, including dominated and duplicate
   points; the UI draws a line through all of them.
2. **Uses a non-lexicographic payoff table.** The loose end of the ε range is the bound category's
   value at an *unconstrained* optimum of the other objective, which can be weakly dominated, so
   the grid is wider than the true frontier.
3. **Implements augmentation but not the rest of AUGMECON2.** No bypass (skipping grid points that
   would return the same solution) and no early exit on infeasibility.
4. **Rebuilds the model per point.** `_build_model(problem)` runs from scratch for every ε.
5. **Does not record solve status.** A time-limited FEASIBLE point is indistinguishable from an
   OPTIMAL one, though only the latter is guaranteed Pareto-optimal.

## 2. Non-goals

- N-objective (>2) frontiers. Pairs stay 2-D; `DEFAULT_PAIRS` unchanged.
- Any C++ / OR-Tools fork change.
- Parallel solving of ε points (would break warm-start chaining and fight CP-SAT's 8 workers).
- Removing `solve_pareto_point()` — it stays, with unchanged signature and behaviour.

## 3. Design

### 3.1 `engine/pareto.py` — pure dominance filter (new)

No solver imports. Minimisation on both objectives.

- `dominates(a, b) -> bool`: `a` is ≤ `b` on both objectives and < on at least one.
- `pareto_filter(points) -> list[FrontierPoint]`: returns the same points (same order) with
  `dominated` set. Rules:
  - A point is **eligible** iff it has both objective values (not `None`) and
    `hard_violations == 0`. Ineligible points are marked `dominated = True` and never dominate
    anything.
  - Among eligible points with identical `(bound_value, minimize_value)`, the first in input order
    is the representative; the rest are marked `dominated = True`.
  - A representative is `dominated = True` iff some other eligible point dominates it.
- O(n²) is fine: n ≤ ~10 per pair.

Points are **flagged, never removed**, so the API remains an honest record of every solve.

### 3.2 `FrontierPoint` additions (`engine/pareto_sweep.py`)

Add, all with defaults so existing constructors keep working:

| field | type | meaning |
|---|---|---|
| `optimal` | `bool` | CP-SAT status was `OPTIMAL` (vs `FEASIBLE`/other) |
| `dominated` | `bool` | set by `pareto_filter` |
| `skipped_by_bypass` | `bool` | ε never solved; AUGMECON2 bypass proved it redundant |

A bypassed point is emitted (so the grid is fully accounted for) with the solved point's values
copied, `wall_s = 0`, `skipped_by_bypass = True`; `pareto_filter` then collapses it as a duplicate.

### 3.3 `ParetoSession` — build once, change only ε (`engine/solvers/cpsat.py`)

```python
session = ParetoSession(problem, bound_category, minimize_category)
sol, vals, status = session.solve(epsilon, time_limit_s, hint=prev_solution)
sol, vals, status = session.solve_unbounded(minimize=..., fix={category: value}, ...)
```

- Constructor calls `_build_model(problem)` **once**, adds an ε variable `eps` (domain later
  narrowed to a single value), a slack `slack ∈ [0, UB]`, and `bound_expr + slack == eps`, with
  the AUGMECON2 objective `scale * minimize_expr − slack`. `UB` and `scale` come from the
  payoff-table range, computed before the session is used for bounded solves.
- `solve(epsilon, ...)` sets `eps`'s domain to `[epsilon, epsilon]` directly in the model proto,
  clears and re-adds solution hints, solves, decodes. No rebuild.
- Payoff-table solves need the bound constraint inactive and a different objective; `ParetoSession`
  handles them by setting `eps`'s domain to its full `[0, UB]` range and swapping the objective.
  *(Implemented: proto-level domain and objective swapping works, so one model serves every
  solve of a pair. Fixing a category for the lexicographic stage is a domain change on an
  auxiliary `pareto_<category> == sum(terms)` variable, so no constraint is added after
  construction.)*
- Returns the CP-SAT status name alongside `(Solution, category_values)` so `optimal` can be set.
- `solve_pareto_point()` and `_solve_and_decode()` keep their current contracts; shared decoding
  logic is reused, not duplicated.

### 3.4 Sweep algorithm (`sweep_pair` rewrite)

1. **Lexicographic payoff table** (3 solves):
   - `tight_end`: min `bound`. Its lexicographic second stage (min `minimize` s.t.
     `bound == tight_end`) is *not* solved separately — it is exactly the grid's ε = `tight_end`
     point, which the augmented sweep solve reaches anyway. *(Amended during implementation: the
     original design solved it twice.)*
   - `loose_end`: min `minimize` → `m*`; then min `bound` s.t. `minimize == m*` → `loose_end`.
2. Grid: `_epsilon_grid(tight_end, loose_end, n)` as today; step `Δ = (loose − tight)/(n − 1)`.
3. Iterate ε from **loose to tight** (canonical AUGMECON2 order):
   - Hint every solve with a solution that already satisfies its bound: the previous point's
     solution when its bound value is ≤ ε, otherwise the tight-end payoff solution (bound value
     `tight_end`, feasible for every grid ε). `ParetoSession` completes that hint over every
     model variable before solving. *(Amended after the first reference-instance benchmark: a
     hint covering only placement variables is not an incumbent to CP-SAT, and solves near the
     frontier's edge timed out empty — 0 frontier points on 2 of 3 pairs. With the completed
     hint the same solve returned OPTIMAL.)*
   - **Infeasible (no solution)** → emit that point as infeasible and **stop**: every tighter ε is
     also infeasible. Only valid when the status is `INFEASIBLE` (proven); on `UNKNOWN`
     (time-limit, no solution) record the point and **continue**, since nothing was proven.
   - **Bypass**: achieved `bound_value = v ≤ ε`. Every remaining grid ε′ with `v ≤ ε′ < ε`
     yields the same optimum — only when this solve was `OPTIMAL`. Emit those as
     `skipped_by_bypass` and continue from the first grid point `< v`. For a `FEASIBLE`
     (non-optimal) solve, no bypass.
4. Run `pareto_filter` over the pair's points and return them.

`sweep()` keeps its signature and return type. The greedy warm start is still computed once.

### 3.5 API / UI

- `webapp/routers/pareto.py`: no route changes; points serialise with the three extra fields.
  Existing stored `ParetoRun.points` without the fields must still render (frontend defaults
  missing fields to `optimal=true, dominated=false, skipped_by_bypass=false`).
- `webapp/static/platform.js`:
  - scatter: non-dominated eligible points drawn solid and joined by the frontier line (sorted by
    `bound_value`); dominated points drawn hollow/grey, not joined.
  - table: add `optimal`, `dominated`, `bypass` columns.

### 3.6 Error handling

- Unknown category → `ValueError` (unchanged).
- Payoff-table solve with no solution → `RuntimeError` (unchanged; `sweep()` still maps it to an
  empty pair).
- Empty grid (tight == loose) → single point, filter trivially marks it non-dominated.

## 4. Testing (TDD)

- `tests/engine/test_pareto_filter.py` (pure, fast):
  dominance basics; strict-vs-weak (equal on one axis); duplicates collapse to first; `None`
  values and `hard_violations > 0` are ineligible and dominate nothing; property check over
  random point sets that no two non-dominated points dominate each other and every dominated
  eligible point has a dominator.
- `tests/engine/test_pareto_sweep.py` with a fake session (no CP-SAT):
  loose→tight order; bypass skips exactly the grid points in `[v, ε)` and only after `OPTIMAL`;
  early exit only on proven `INFEASIBLE`; `UNKNOWN` continues.
- `tests/engine/test_cpsat_pareto.py` additions (real CP-SAT, `small_problem`):
  `ParetoSession.solve(ε)` respects the bound; changing ε between solves does not rebuild
  (build called once — patched counter); frontier from the new sweep has the same non-dominated
  objective values as the rebuild-per-point path at a generous time limit.
- `research/pareto_benchmark.py`: old vs new `sweep()` wall-clock on the reference instance,
  multiple seeds, median + range — per `CHANGES_DONE.md` §6.2 (no n=1 claims).

## 5. Success criteria

1. No dominated or duplicate point is presented as part of any frontier line.
2. All existing tests pass; `solve_pareto_point()` behaviour unchanged.
3. New sweep's non-dominated frontier matches the old sweep's (filtered) on `small_problem`.
4. Benchmark reports per-pair solve counts and wall-clock, old vs new, as a distribution. The
   result, including "no speed-up", is recorded honestly; it is the evidence that decides whether
   the C++ native sweep follow-up is worth doing.
