# Timetabling fork of OR-Tools

Source-level modifications to Google OR-Tools' CP-SAT solver, specialising it for university
timetabling. The rationale, the architecture walkthrough and the evaluation plan live in
[`../../docs/or_tools_source_modification_guide.md`](../../docs/or_tools_source_modification_guide.md);
this directory holds the artifacts.

We vendor the **patch**, not a copy of the OR-Tools tree — 331 lines instead of megabytes of
third-party source, and it stays honest about exactly what is ours.

| File | What it is |
|---|---|
| `timetabling.patch` | The change itself: 7 files, 234 insertions, 1 modification, 0 deletions |
| `apply_fork.sh` | Clones upstream at the pinned commit and applies the patch |
| `algo_test.cc` | Standalone verification of the new algorithms (no OR-Tools build needed) |
| `test_varnames.txt` | Fixture: 4558 real variable names emitted by `engine/solvers/cpsat.py` |

**Upstream base commit: `98c165af62df62b3056c2ee0fca66b24e79097cb`** (branch `stable`).
Pin this in the paper — the files touched are OR-Tools internals, not public API, so they move
between releases.

## What the patch changes

1. **`division_day_lns`** — a new LNS neighborhood that re-optimises one whole division-day at a
   time, instead of the random / graph-walk fragments the built-in generators produce.
   (`cp_model_lns.h`, `cp_model_lns.cc`, `cp_model_solver.cc`)
2. **`CHOOSE_MIN_UNFIXED_IN_GROUP`** — a new branching strategy: pick the course with the fewest
   placements still open, re-ranked live at every node. (`cp_model.proto`, `cp_model_search.cc`)
3. **Domain-tag parser** — the shared mechanism both features use to recover application
   structure from variable names. (`cp_model_utils.h`, `cp_model_utils.cc`)

Neither feature can be reproduced by setting existing parameters; see the guide, §2 and §4.3.

## How the solver learns our structure

`engine/solvers/cpsat.py` names every placement variable:

```
x_D1_CS101_TH_0_12_R101@R=D1_CS101_TH_0@G=D1#3
                       └── requirement ─┘└division#day┘
```

Each `@KEY=` value runs to the next `@`; text before the first `@` is free-form. Presolve copies
variable names when it rewrites variables (`presolve_context.cc:1118-1120`), which is what makes
this survive into the solver. **Stock OR-Tools ignores variable names entirely**, so the tagging
is inert on an unmodified build and both arms of a benchmark solve an identical model.

## Verification that exists today

```bash
# Algorithms — compiles and runs with plain g++, no OR-Tools build required
g++ -std=c++17 -Wall -Wextra -O2 -o algo_test algo_test.cc
./algo_test test_varnames.txt          # -> ALL CHECKS PASSED

# Patch is valid against the pinned base
git -C <your or-tools clone> apply --check third_party/or-tools-fork/timetabling.patch
```

`algo_test.cc` lifts the new algorithms out of the patch verbatim (only `absl::string_view` ->
`std::string_view`) and independently reproduces the group counts Python computes on the same
model: 10 division-day groups, 76 requirement groups, 4250 tagged and 308 untagged variables.

**Not yet verified: that the patch compiles inside OR-Tools, or that it improves solve time or
quality.** Both need a real build. Do not claim either in the paper until then.

## Installing (collaborators start here)

This directory holds a *patch*, not a library. Running the fork needs a compiled C++
extension, so pick one of three paths.

**1. Install a prebuilt wheel (fastest, ~20 seconds).** Grab the wheel for your OS from
the repository's Releases page, then:

```bash
python -m venv .venv-fork && . .venv-fork/bin/activate   # Windows: .venv-fork\Scripts\activate
pip install <path-or-url-to>/ortools-*.whl
pip install -r requirements.txt
python third_party/or-tools-fork/verify_fork.py
```

Use a separate virtualenv. The stock `ortools` wheel must stay installed somewhere,
because it is the baseline arm of the experiment below.

**2. Let CI build it.** Push a `fork-v*` tag, or run the *build-ortools-fork* workflow
from the Actions tab. It patches upstream, builds Linux and Windows wheels, asserts the
fork enum is present, and attaches the wheels to a Release.

**3. Build locally** — see *Building* below. Expect roughly an hour.

Whichever path, `verify_fork.py` is the check that matters. It exits non-zero on a stock
interpreter, so it is safe to put in front of a benchmark run. The project is fail-safe by
design: without the fork it silently runs plain CP-SAT rather than erroring, so an
unverified "fork" run can quietly be a stock run.

## Building

Requires a toolchain this project does not ship: **Visual Studio 2022** with the "Desktop
development with C++" workload (MSVC) and **CMake >= 3.18** on Windows, or GCC/Clang + CMake on
Linux. The MSYS2 `g++` that runs `algo_test.cc` is *not* sufficient for OR-Tools itself.

```bash
./apply_fork.sh /path/to/build/dir      # clone upstream @ pinned commit + apply patch
cd /path/to/build/dir/or-tools
cmake -S . -B build -DBUILD_PYTHON=ON -DBUILD_DEPS=ON
cmake --build build --config Release -j
```

The first build compiles abseil, protobuf, re2, SCIP and CoinOR from source and takes a while.
Install the resulting wheel into a **separate** virtualenv so stock and forked solvers can be
benchmarked side by side.

## Running the experiment

`engine/solvers/cpsat.py` auto-detects which build it is running on and exposes both features
through environment variables, so no code edits are needed to sweep the arms:

| Variable | Values | Meaning |
|---|---|---|
| `TIMETABLE_MRV` | `auto` (default), `dynamic`, `static`, `off` | branching order. `auto` = `dynamic` on a forked build, `off` on stock. `static` is the Tier-0 ablation: one fixed order, the best a stock build can do |
| `TIMETABLE_DIVISION_DAY_LNS` | `1` (default), `0` | the new neighborhood. It runs by default on a forked build, so `0` is the **baseline** arm |

The four arms worth reporting:

```bash
# 1. stock baseline
TIMETABLE_MRV=off  python -m <your benchmark>
# 2. best achievable with knobs alone (no fork) -- pre-empts "why not just tune parameters?"
TIMETABLE_MRV=static python -m <your benchmark>
# 3. fork, neighborhood only
TIMETABLE_MRV=off  TIMETABLE_DIVISION_DAY_LNS=1 python -m <your benchmark>
# 4. fork, both features
TIMETABLE_MRV=dynamic TIMETABLE_DIVISION_DAY_LNS=1 python -m <your benchmark>
```

Arm 2 is the one that makes the paper defensible: it measures how much of any improvement needed
*new code* rather than *new settings*.

## ⚠ Measured: single runs are not evidence on this workload

`solve()` uses `num_search_workers = 8`, and CP-SAT's parallel portfolio is **not deterministic**
by default — workers race, so the same input can land on different solutions run to run.

We measured this. `tests/engine/test_breaks.py::test_cpsat_breaks_vary_across_days`, run five
times against an *unmodified* model:

```
run 1 PASS   run 2 PASS   run 3 PASS   run 4 FAIL   run 5 FAIL
-> 3/5 on a borderline instance, with nothing changed between runs
```

`tests/engine/test_solvers.py::test_mip_finds_zero_hard_violation_solution` is similarly
borderline (one observed run finished at 57.6 s against a 60 s budget, another passed).

Consequences for the evaluation:

- **Never report an n=1 comparison.** A baseline that flips ~40% of the time will happily
  manufacture a large "improvement" (or regression) from pure noise. During this work a single
  run briefly looked like a clear regression caused by the tags; five runs showed the model was
  byte-identical and the test simply flaky.
- Run every arm **many times** and report a distribution — median and spread, not one number.
- Fix `solver.parameters.random_seed` and consider `num_search_workers = 1` (or
  `interleave_search = true`) for the runs you want reproducible, at the cost of realism.
- Prefer instances that are *not* right at the time budget, or raise the budget until the
  baseline passes reliably; otherwise you are measuring the budget, not the solver.
