# Modifying OR-Tools at the Source Level for This Timetabling Engine

Grounded against `google/or-tools` `stable` branch (pip package `ortools==9.9.3963`, currently
installed; upstream latest tag as of writing is `v9.15`) and this repo's actual solver code:
`engine/solvers/cpsat.py` and `engine/solvers/mip.py`. Every file/line/class name below was
pulled directly from the OR-Tools GitHub repo via `gh api`, not recalled from memory — verify
against your checked-out commit before citing in the paper, since these are internal
(non-`ortools/*.h` public-API) files that upstream is allowed to refactor between releases.

This doc has two purposes: (1) an honest map of what can already be tuned via the public
Python API / `sat_parameters.proto` with **zero forking** (Tier 0), and (2) concrete,
file-and-function-level patch proposals for the cases where a real fork is warranted (Tier 1).
A research paper that forks OR-Tools without first ruling out Tier 0 will get "why didn't you
just set a parameter" reviews — Section 2 exists so you can pre-empt that.

---

## 0. Status — what is implemented right now

Two of the Section-4 proposals are written and sitting in `../or-tools-reference/`
(a sparse checkout of `google/or-tools` @ `98c165a`, `stable`). `git diff` there produces the
patch series, also exported to `or_tools_timetabling_fork.patch`.

**Verification status — read this before quoting any of it in the paper:**

| Claim | Status |
|---|---|
| The new algorithms are correct | **verified** — `or_tools_fork_algo_test.cc`, 4558 real variable names, all checks pass |
| Python side still solves correctly | **verified** — `OPTIMAL`, 0 hard violations, full pytest suite |
| No build-file changes needed | **verified** — `cp_model_lns` and `cp_model_search` already dep on `cp_model_utils` in `BUILD.bazel` |
| The patch compiles inside OR-Tools | **NOT verified** — no MSVC/CMake on this machine |
| It improves solve time or quality | **NOT measured** — requires a build first |

The last two are the gap. Everything else is evidence you can cite.

| Proposal | Status | Files touched |
|---|---|---|
| 4.1 Division-day LNS neighborhood | **written** | `cp_model_lns.h`, `cp_model_lns.cc`, `cp_model_solver.cc` |
| 4.3 MRV branching (`CHOOSE_MIN_UNFIXED_IN_GROUP`) | **written** | `cp_model.proto`, `cp_model_search.cc` |
| shared: domain-tag parser | **written** | `cp_model_utils.h`, `cp_model_utils.cc` |
| 4.2 / 4.4 / 4.5 | not started (future work) | — |

Total: **7 files, ~200 added lines, 1 modified line, nothing deleted.**

### How the solver learns our problem structure

Both changes need something CP-SAT cannot represent: *which Boolean variables describe the same
course*, and *which belong to the same division-day*. We pass it down through the only channel
that survives presolve intact — the variable name. `presolve_context.cc:1118-1120` copies a
variable's name whenever it creates a replacement, which is what makes this viable.

`engine/solvers/cpsat.py` now emits names shaped like:

```
x_D1_CS101_TH_0_12_R101@R=D1_CS101_TH_0@G=D1#3
                       └── requirement ──┘└ division#day ┘
```

Each `@KEY=` value runs to the next `@`. Everything before the first `@` is free-form, so the
old human-readable debug names are unchanged, and **stock OR-Tools ignores variable names
entirely — so this line is inert on an unmodified build.** That property matters for the paper:
the same Python code runs against both stock and forked solvers, so the benchmark compares
solvers, not two different models.

Verified on the sample instance: 4250 tagged placement variables across 10 division-day groups
and 76 requirement groups; 308 untagged auxiliary variables. Solve still returns `OPTIMAL` with
0 hard violations.

### One non-obvious correctness point worth a paragraph in the paper

`RelaxGivenVariables()` **fixes** every variable you do not explicitly name. Our model's
auxiliary variables (`occ_`, `gap_`, `before_`, `after_`, `day_load_` — the 308 untagged ones)
are functionally derived from the placement variables via `lin_max`/linear constraints. Relaxing
a division-day's placements while freezing its occupancy indicators would pin the exact
occupancy pattern the generator is trying to change: the fragment stays *feasible* but becomes
essentially *unimprovable*, and the generator would look like it does nothing.

`DivisionDayNeighborhoodGenerator` therefore always relaxes every untagged variable alongside the
chosen groups (`always_relaxed_`). This costs almost nothing in search — once the frozen
division-days' placements are fixed, propagation re-derives those variables immediately — but
without it the whole change is silently inert. This is exactly the sort of trap that makes a
"we modified the solver" result irreproducible, and it is worth reporting.

### Reproducing the verification that exists

```bash
# 1. Algorithm-level verification (no OR-Tools build required)
g++ -std=c++17 -Wall -Wextra -O2 -o algo_test docs/or_tools_fork_algo_test.cc
./algo_test docs/or_tools_fork_test_varnames.txt      # -> ALL CHECKS PASSED

# 2. Python side unaffected
python -m pytest tests/ -q
```

`or_tools_fork_algo_test.cc` lifts the three new algorithms out of the patch verbatim (only
`absl::string_view` -> `std::string_view`) and runs them over the 4558 real variable names this
model emits. It independently reproduces the same group counts Python computes (10 / 76 / 4250 /
308), checks parsing edge cases, checks the neighborhood sampler covers every division-day and
never double-relaxes, and checks MRV actually selects the most-constrained requirement.

### What is still needed to finish the job

The build is blocked on toolchain, not on code. OR-Tools on Windows needs:

- **Visual Studio 2022** with the "Desktop development with C++" workload (MSVC). The MSYS2
  `g++` present on this machine is enough for the standalone algorithm test above, but not for
  OR-Tools itself.
- **CMake** >= 3.18.
- A **full** clone (the `or-tools-reference` checkout is sparse — 9 files). Either
  `git sparse-checkout disable` in it, or clone upstream fresh and apply
  `or_tools_timetabling_fork.patch`.
- ~20-30 GB free disk and 1-3 hours for the first build (it builds abseil, protobuf, re2, SCIP,
  CoinOR/CBC and friends from source).

Then: `cmake -S . -B build -DBUILD_PYTHON=ON -DBUILD_DEPS=ON`, build, and `pip install` the
resulting wheel into a *separate* venv so stock and forked solvers can be benchmarked
side by side.

### Turning the features on and off (this is your A/B switch)

`SubsolverNameFilter::Keep()` returns true unless a name is explicitly filtered
(`cp_model_search.cc:1200`), so **`division_day_lns` runs by default** on the forked build the
moment the model carries `@G=` tags — exactly like `rnd_var_lns` and the other built-in LNS
workers. You therefore benchmark by turning it *off*, not on:

```python
# Baseline arm: forked binary, new neighborhood disabled.
solver.parameters.ignore_subsolvers.append("division_day_lns")

# Treatment arm: change nothing -- it is already active.
```

Running both arms on the *same* forked binary is the cleaner experiment than forked-vs-stock: it
isolates the neighborhood itself from every other difference between two separately built
binaries.

Change 4.3 is opt-in, since it only applies where you attach a decision strategy:

```python
# In cpsat.py, after the model is built. Order the vars however you like; the
# heuristic re-ranks by live group size at each node regardless.
model.AddDecisionStrategy(
    ordered_vars,
    cp_model.CHOOSE_MIN_UNFIXED_IN_GROUP,   # available once the wheel is rebuilt
    cp_model.SELECT_MIN_VALUE)
```

---

## 1. What your engine actually asks CP-SAT to do

Your CP-SAT model (`engine/solvers/cpsat.py:57-415`, function `_build_model`) is a **pure
0/1 set-partitioning / pseudo-Boolean model**, not a scheduling or routing model in OR-Tools'
sense:

| Your code | OR-Tools Python call | Underlying `CpModelProto` field | Where it's implemented in C++ |
|---|---|---|---|
| one bool var per (requirement, start slot, room) — `cpsat.py:74-77` | `model.NewBoolVar` | `variables` | `cp_model.cc` |
| "exactly one candidate wins" — `cpsat.py:83` | `AddExactlyOne` | `ConstraintProto.exactly_one` (`cp_model.proto:359`) | `cp_model_expand.cc`, `clause.cc` |
| room/faculty/division/batch mutual exclusion — `cpsat.py:129-136` | `AddAtMostOne` | `ConstraintProto.at_most_one` (`cp_model.proto:349`) | `cp_model_expand.cc`, `clause.cc` |
| batch-pair / cross-division sync equality — `cpsat.py:154-171` | `model.Add(sum==sum)` | `LinearConstraintProto` | `linear_propagation.cc`, `linear_programming_constraint.cc` |
| faculty daily/weekly caps, consecutive-session cap, division daily band — `cpsat.py:174-253` | `model.Add(sum(...) <= k)` | `LinearConstraintProto` | same as above |
| "occupied at period p" reified booleans + gap detection — `cpsat.py:340-372` | `NewBoolVar` + `AddMaxEquality` | `ConstraintProto.lin_max` (`cp_model.proto:388-391`) | `integer_expr.cc` (`LinMaxConstraint`), enforcement via `enforcement.cc` |
| per-faculty day-load range (min/max) — `cpsat.py:397-403` | `AddMaxEquality` / `AddMinEquality` | `lin_max` (min via negation) | `integer_expr.cc` |
| warm start — `cpsat.py:496` | `AddHint` | `CpModelProto.solution_hint` | `cp_model_loader.cc` |
| Pareto sweep (AUGMECON2) — `cpsat.py:433-480` | rebuilds the whole model per point, adds slack + scaled objective | n/a (Python-level orchestration) | n/a |

**The important negative fact, confirmed by reading `cp_model_solver.cc:1804-2043`:** you never
create an `Interval`, `NoOverlap`, `Cumulative`, `Circuit`, `Routes`, or `NoOverlap2D` constraint
anywhere in `cpsat.py`. That single fact drives most of Section 5 below.

Your MIP solver (`engine/solvers/mip.py:19`) uses `pywraplp.Solver.CreateSolver("CBC")` — a
completely separate code path (`ortools/linear_solver` → CBC, which lives in
`ortools/third_party_solvers`/COIN-OR, not in `ortools/sat` at all). Nothing in this document's
CP-SAT sections applies to it; see Section 6 for the one MIP-relevant point (build trimming).

---

## 2. Tier 0 — already exposed, no fork required

Confirmed present in `ortools/sat/sat_parameters.proto` (line numbers as of the commit fetched):

- `num_search_workers` (:708), `num_full_subsolvers` (:715) — thread/portfolio size.
- `subsolvers`, `extra_subsolvers`, `ignore_subsolvers`, `filter_subsolvers` (:742-758) — you can
  **name which named subsolver configs run** (e.g. drop `"lp_search"`, add another
  `"quick_restart_no_lp"` worker) without touching C++. Comment at line 723 lists the built-in
  named configs (`default_lp`, `no_lp`, `max_lp`, `quick_restart_no_lp`, ...).
- `search_branching` (:1194) — `AUTOMATIC_SEARCH`, `FIXED_SEARCH`, `PORTFOLIO_SEARCH`,
  `LP_SEARCH`, `PSEUDO_COST_SEARCH`, `RANDOMIZED_SEARCH`, etc.
- `linearization_level` (:1652), `symmetry_level` (:1615), `cp_model_presolve` (:502),
  `use_lns` / `use_lns_only` (:1483-1486).
- `model.AddDecisionStrategy(vars, var_strategy, value_strategy)` (Python API, no proto flag
  needed) — you can already impose your own variable order (e.g. tightest-candidate-set first,
  mirroring classic MRV) **today**, in `cpsat.py`, with no fork. This should be your first
  experiment before writing any C++, and it's a legitimate ablation baseline for the paper.
- `solver.parameters.<name> = value` is already how `cpsat.py:499-503`
  (`_solve_and_decode`) injects `extra_solver_params` — so all of the above are one dict entry
  away in your existing code, not a new mechanism.

**What I verified is *not* a source-level win (a common false assumption):** the LNS
neighborhood generators for scheduling/packing/routing
(`RandomIntervalSchedulingNeighborhoodGenerator`, `RoutingRandomNeighborhoodGenerator`, etc.,
registered in `cp_model_solver.cc:1955-2043`) are each gated at runtime by
`helper->TypeToConstraints(ConstraintProto::kNoOverlap)` / `kCumulative` / `kCircuit` / `kRoutes`
being non-empty (`cp_model_solver.cc:1932-1934, 2027-2028`). Since your model has zero of those
constraint types, **this code already never runs for you** — stripping it from a fork changes
binary size and compile time only, not your solve time. Don't claim a runtime speedup from
removing it; that claim would not survive review.

---

## 3. CP-SAT's actual pipeline (for the paper's background section)

```
Python cp_model.CpModel()          ortools/sat/python/cp_model.py (pybind11 wrapper over
        │                          cp_model_helper.cc / wrappers.cc — NOT SWIG despite the
        ▼                          legacy filename ortools/sat/swig_helper.cc)
  CpModelProto (protobuf)           ortools/sat/cp_model.proto
        │
        ▼
  Presolve                          cp_model_presolve.cc (context in presolve_context.cc,
        │                           expansion of high-level constraints into primitives in
        │                           cp_model_expand.cc, symmetry detection in
        │                           cp_model_symmetries.cc gated by symmetry_level)
        ▼
  Linear relaxation + cuts          linear_relaxation.cc, cuts.cc, linear_programming_
        │                           constraint.cc (Glop-backed LP relaxation, active when
        │                           linearization_level > 0)
        ▼
  Portfolio of subsolvers           subsolver.h/.cc (SubSolver interface), wired up in
    - full-problem workers            cp_model_solver.cc (FullProblemSolver instances per
    - LNS workers                     named parameter set from GetNamedParameters)
    - feasibility jump / pump        cp_model_lns.cc (NeighborhoodGenerator + LnsSolver),
        │                            work_assignment.cc (thread/task distribution)
        ▼
  SAT core + lazy integer encoding  sat_solver.cc, integer.cc/integer_expr.cc, clause.cc,
    (CDCL + cut-generation +          enforcement.cc (reified/enforcement-literal handling),
     bound propagation)               restart.cc, sat_decision.cc (VSIDS-style branching)
        ▼
  Postsolve → CpSolverResponse      cp_model_postsolve.cc
```

Your model only exercises the middle two-thirds of this: proto → presolve → LP relaxation
(mostly from your `AddAtMostOne`/linear cap constraints) → the Boolean/integer SAT core. The
scheduling-specific propagators (`cumulative.cc`, `disjunctive.cc`, `timetable.cc` — note:
OR-Tools' internal file `ortools/sat/timetable.cc` implements the *cumulative-resource
timetabling propagation algorithm*, an unrelated CP scheduling technique, not your university
timetable — don't conflate the two in the paper) never activate for you.

---

## 4. Tier 1 — proposals that genuinely require a fork

Each entry: motivation tied to your actual code, exact target, sketch, and how to measure it.

### 4.1 Domain-specific LNS neighborhoods (medium effort, good ROI)

**Motivation.** The generic LNS generators OR-Tools ships (`RelaxRandomVariablesGenerator`,
`VariableGraphNeighborhoodGenerator`, `ArcGraphNeighborhoodGenerator`, ...) relax variables based
on the constraint-graph topology or pure randomness. Your decision variables have rich domain
structure the generic generators can't see: `x[(req, start, room)]` naturally groups by
`(division_id, day)`, by `faculty_id`, and by `room_id` — exactly the axes your objective
(`cpsat.py:279-410`) is scored on.

**Target.** `ortools/sat/cp_model_lns.h`/`.cc` (subclass `NeighborhoodGenerator`, pattern after
`VariableGraphNeighborhoodGenerator` at `cp_model_lns.h:582-591`), registered in
`cp_model_solver.cc` alongside the existing `reentrant_interleaved_subsolvers.push_back(...)`
block (`cp_model_solver.cc:1900-1934`).

**Sketch.** A `DivisionDayRelaxationNeighborhoodGenerator` needs to know which proto variable
index belongs to which `(division_id, day)`. You don't need a new proto field for this: your own
code already names every variable `f"x_{req.id}_{start_id}_{room_id}"`
(`cpsat.py:77`), and `start_id`/`room_id` are enough to recover `day` and `division_id` by
re-joining against the `candidates`/`requirements` structures serialized alongside the model (or,
cleaner, by walking `CpModelProto.variables[i].name` and parsing it back — the same trick
`cp_model_solver_test.cc` uses to introspect named variables). Generate one neighborhood per
`(division_id, day)`, relaxing (unfixing) exactly that division's session/room choices for that
day, fixing everything else to the incumbent solution — this directly targets the
gap/day-span objective terms at `cpsat.py:318-372`. Add a `FacultyWeekRelaxationGenerator`
symmetrically for the faculty-balance term at `cpsat.py:383-403`.

**Measure.** Best-objective-at-timeout curve (your existing `benchmark`/pareto-sweep harness)
with vs. without the new generator registered, at fixed `num_search_workers` and fixed wall
time, on your reference dataset (`data/reference/*.json`).

**Effort/risk.** Medium — pure C++ addition, no proto/presolve changes, low upstream-drift
surface. This is the strongest "systems contribution" candidate for the paper because it's a
direct, defensible novelty claim (domain-informed LNS beats generic LNS on this problem class).

### 4.2 A native `NoInteriorGap` global constraint (high effort, highest novelty)

**Motivation.** `cpsat.py:340-372` builds, per `(division, day)`, one `occ_{period}` reified bool
per period (via `AddMaxEquality`), then for every interior period a `before`/`after`/`gap` triple
of reified bools and four linear constraints — O(periods²) auxiliary variables and constraints
per division per day, purely to express "penalize free periods sandwiched between busy ones."
This is exactly the kind of pattern OR-Tools' own global constraints (`AllDifferent`, `Circuit`,
`Cumulative`) exist to replace with a single compact propagator.

**Target.** New constraint type: extend `ConstraintProto` (`cp_model.proto`) with a
`NoInteriorGapConstraintProto` (list of occupancy literals in period order + a gap-count target
var), new propagator in `cp_constraints.cc` (pattern after `AllDifferentConstraint`), loader hook
in `cp_model_loader.cc`, a pass-through/no-op presolve case in `cp_model_presolve.cc`'s giant
constraint-type switch, and a linear-relaxation contribution in `linear_relaxation.cc` (a valid
cut is: for any 3 consecutive periods, `gap_count >= occ[i-1] + occ[i+1] - occ[i] - 1`, which is
literally the four inequalities you currently materialize by hand — the propagator can derive and
maintain this incrementally instead of the model carrying it as static linear rows).

**Measure.** Model size before/after (var count, constraint count — cheap to log from
`CpModelProto` directly), presolve time, and end-to-end solve time at fixed time budget, as
division/day count scales up (synthetic larger instances). This is the chapter to lead with if
the paper's angle is "solver internals," since it's a real propagator, not parameter tuning.

**Effort/risk.** High. Touches proto (recompile all language bindings), presolve, loader, and
relaxation. Budget for this to be the single largest engineering item; keep it isolated in your
fork as its own commit/patch so it can be dropped if it destabilizes other constraint types.

### 4.3 Structure-aware default search for pure set-partitioning CP-SAT models (medium effort)

**Motivation.** `AUTOMATIC_SEARCH` (`cp_model_search.cc`, dispatch starts around
`ConstructHeuristicSearchStrategy` at `cp_model_search.cc:355`) picks a heuristic by inspecting
coarse model shape (has LP? has scheduling constraints? pure SAT?). It has no notion of "this
model is a disjoint union of `AddExactlyOne` groups" — which is precisely what a requirement's
candidate set is (`cpsat.py:79-83`). Classic CSP timetabling literature's MRV (minimum-remaining-
values) heuristic — branch first on the requirement with fewest remaining feasible
(slot, room) candidates — is a natural fit and is *not* what generic VSIDS-style SAT branching
(`sat_decision.cc`) or automatic search does.

**Target.** Add a new `SearchHeuristic`/`VariableSelectionStrategy` case in `cp_model_search.cc`
(near `ConstructUserSearchStrategy`, `cp_model_search.cc:210-274`) that detects the
all-`exactly_one`-partition structure and orders variable groups by shrinking domain size.

**Cheaper first step (Tier 0, do this before touching C++):** this exact heuristic is
expressible today via `model.AddDecisionStrategy()` from Python — order your requirement groups
by `len(candidates[req.id])` ascending and pass that order in. Only escalate to a C++ patch if
you need it to interact with CP-SAT's internal propagation-aware dynamic reordering (which the
Python-level static strategy can't do, since it fixes the order once).

**Measure.** Nodes explored / time to first feasible / time to proven-optimal vs.
`AUTOMATIC_SEARCH` and vs. the Tier-0 static-order variant, isolating what the *dynamic* C++
version buys over the *static* Python version — that delta is the actual research finding.

### 4.4 Native multi-objective sweep (medium-high effort, direct win for `pareto_sweep.py`)

**Motivation.** `solve_pareto_point()` (`cpsat.py:433-480`) rebuilds `_build_model(problem)` from
scratch for *every* point on the Pareto frontier (`research/pareto_sweep.py` drives the outer
loop). Each rebuild re-runs presolve and re-derives cuts from zero, throwing away everything the
solver learned about this exact model at the previous epsilon bound.

**Target.** A new entry point alongside `SolveCpModel` in `cp_model_solver.h`/`.cc`, e.g.
`SolveCpModelParetoFront(...)`, that keeps the `SharedResponseManager`/presolved model alive
across successive calls where only the `bounds` slack constraints change, re-using learned
clauses and the LP basis as warm starts for the next epsilon point (AUGMECON2, same algorithm you
already implement at the Python level in `cpsat.py:437-480` — this only moves the *loop*
in-process).

**Measure.** Wall-clock for a full N-point Pareto sweep, fork vs. current rebuild-per-point
baseline, at fixed frontier resolution.

**Risk.** This is the riskiest proposal to scope correctly — verify how much CP-SAT's presolve
output is actually reusable across a tightened bound before committing to it; if the answer is
"not much," the honest paper finding is "why this doesn't help," which is still a valid result.

### 4.5 Presolve pass for interchangeable-resource symmetry breaking (medium effort)

**Motivation.** Rooms of identical `room_type` and `capacity` are interchangeable in your model —
swapping which physical lab two labs of the same type/capacity get assigned to at the same slot
produces an equally-good, symmetric solution. Generic graph-automorphism symmetry detection
(`cp_model_symmetries.cc`, gated by `symmetry_level`, `sat_parameters.proto:1615`) has to
*discover* this from the constraint graph; a domain-aware pass can assert it directly and far
more cheaply (lexicographic ordering constraint over same-(type,capacity) room groups per slot).

**Target.** New presolve pass added to the pipeline in `cp_model_presolve.cc`, keyed off grouping
`room_id`s by `(room_type, capacity)` — information available in your Python layer
(`problem.rooms`) but not intrinsically visible to generic proto-level symmetry detection.

**Cheaper alternative worth trying first (Tier 0):** emit the lexicographic-ordering constraints
directly from `cpsat.py` at model-build time (a handful of `model.Add(...)` calls over same-group
room indices) — no fork needed. Only worth a C++ presolve pass if you want it to compose
correctly with everything else presolve already does to the model, or if you're arguing the
general technique (not just this instance) as a paper contribution.

---

## 5. What I'd explicitly recommend *against* forking for

- **Removing routing/packing/scheduling code from `ortools/sat/`** to "speed up your solve" —
  per Section 2, that code doesn't run for your model class at all; you'd only save binary size
  and build time, and you'd need to keep re-applying the removal patch across upstream merges.
- **Building a custom CBC without COIN-OR** — you use `pywraplp.Solver.CreateSolver("CBC")`
  (`mip.py:19`) directly, so `USE_COINOR` must stay `ON` in any custom CMake build
  (`CMakeLists.txt:222`); this is the one place your MIP solver, not CP-SAT, constrains the build.
- **A brand-new SAT branching heuristic from scratch** — `sat_decision.cc`'s VSIDS-family
  branching is tuned against decades of SAT-competition instances; your instances are small,
  structured, and dominated by the `AddExactlyOne`/`AddAtMostOne` skeleton, so a *structural*
  heuristic (4.3) beats a *generic SAT-heuristic* rewrite for far less effort and is easier to
  justify in a paper as "we exploited known problem structure" rather than "we out-tuned Google's
  SAT team on general instances."

---

## 6. Build/deployment trimming (legitimate, but a binary-size story, not a speed story)

Confirmed via `CMakeLists.txt`: solver backends are individually toggleable —
`USE_BOP` (:218), `USE_COINOR` (:222, **keep ON** — see above), `USE_GLOP` (:254, keep ON; it's
CP-SAT's own LP relaxation backend, not optional for you), `USE_GLPK` (:260, OFF),
`USE_GUROBI`/`USE_HIGHS`/`USE_PDLP`/`USE_SCIP`/`USE_XPRESS`/`USE_CPLEX` (all safe to set OFF —
you use neither). `ortools/routing`, `ortools/graph`, `ortools/algorithms` have no equivalent
top-level CMake option in this file — dropping them means editing the source-file glob/subdirectory
list directly (verify against your checked-out `ortools/CMakeLists.txt`, since the exact
mechanism is not in the top-level file and may have moved between releases). Worth doing only if
the paper's angle includes deployment footprint (you already ship a FastAPI webapp —
`webapp/server.py` — so a slimmer wheel is a real, if minor, deliverable) — don't present it as a
performance optimization.

---

## 7. Fork/build/integration workflow

1. Fork `google/or-tools`, branch from the exact tag matching your currently-pinned pip version
   family if reproducibility matters for the paper (you're on `9.9.3963`; upstream stable is
   `v9.15` — decide once whether you're patching the old base you've already benchmarked against,
   or rebasing onto latest, and say so explicitly in the paper's methodology).
2. Build with `-DBUILD_PYTHON=ON` (off by default — `CMakeLists.txt:110`) to regenerate the
   pybind11 module (`ortools/sat/python/cp_model_helper.cc`, `wrappers.cc`) alongside any C++
   changes in `ortools/sat/`.
3. Install the built wheel into this project's venv (`C:\Python311\...\site-packages\ortools`,
   confirmed from your current `ortools.__file__`), ideally in an isolated venv first so you can
   A/B against stock `pip install ortools` on the exact same benchmark harness you already have
   (`tests/`, whatever drives `data/reference/*.json`).
4. Gate every patch behind your existing test suite (`tests/engine`, `tests/test_solver.py`,
   `tests/test_model.py`) as a correctness regression check *before* measuring performance —
   a faster solver that silently drops a hard constraint is not a result.
5. Keep patches as a `git format-patch` series against the exact upstream commit you branched
   from, one patch per Section-4 proposal — this is both good fork hygiene and exactly the
   artifact a paper's reproducibility appendix wants.

## 8. Suggested evaluation plan for the paper

For each Tier-1 change you implement, report against the stock-OR-Tools baseline on your
reference dataset(s) (`data/reference/`) and at least one synthetic scaled-up instance:

- Model size: variable count, constraint count (cheap: dump from `CpModelProto` directly).
- Presolve time, total solve time, time-to-first-feasible, final gap at your existing timeout.
- Solution quality via your own `engine/scoring.py` (hard violations, soft cost) — the yardstick
  you already use to compare CP-SAT/MIP/GA/greedy, so reuse it rather than inventing a new one.
- For 4.4: Pareto-front hypervolume and wall-clock for a full sweep, since that's the metric
  `research/pareto_sweep.py` already implicitly optimizes for.

Report Tier 0 experiments (AddDecisionStrategy ordering, parameter sweeps) as your baseline
ablations — they cost nothing to run and pre-empt the "did you even try the built-in knobs"
review comment.
