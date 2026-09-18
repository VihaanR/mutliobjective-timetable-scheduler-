# CHANGES_DONE — Source-level modification of Google OR-Tools for university timetabling

This document records what was changed in the OR-Tools CP-SAT solver, why each change could not be
achieved through the public API, and how the modified solver is driven from this project.

**Upstream base commit:** `98c165af62df62b3056c2ee0fca66b24e79097cb` (google/or-tools, branch
`stable`). This must be pinned: every file touched is a solver internal, not public API, and these
files move between releases.

**Total change:** 9 files, **245 insertions, 2 modifications, 0 deletions.**

| File | + | − | Role |
|---|---:|---:|---|
| `ortools/sat/cp_model_utils.h` | 30 | 0 | Declares the domain-tag parser |
| `ortools/sat/cp_model_utils.cc` | 28 | 0 | Implements it |
| `ortools/sat/cp_model_lns.h` | 49 | 0 | `DivisionDayNeighborhoodGenerator` class |
| `ortools/sat/cp_model_lns.cc` | 45 | 0 | Its constructor and `Generate()` |
| `ortools/sat/cp_model_solver.cc` | 17 | 0 | Registers the new subsolver |
| `ortools/sat/cp_model.proto` | 13 | 0 | New enum `CHOOSE_MIN_UNFIXED_IN_GROUP` |
| `ortools/sat/cp_model_search.cc` | 52 | **1** | The new branching rule |
| `ortools/sat/python/cp_model.py` | 7 | 0 | Re-exports the enum to Python |
| `ortools/sat/cp_model_checker.cc` | 4 | **1** | Whitelists the enum in model validation |

The two modified lines are a lambda capture in `cp_model_search.cc` (§3.3) and a condition
in `cp_model_checker.cc` (§3.8). Everything else is
pure addition — no upstream code is deleted, and no upstream behaviour changes unless a model
opts in.

The complete change is vendored as a patch at `third_party/or-tools-fork/timetabling.patch`.

---

## 1. The problem: CP-SAT cannot see application structure

A `CpModelProto` is a flat list of variables, constraints and an objective. It has no field that
says *"these 40 Boolean variables are the candidate placements of one course"* or *"these 300
variables all belong to Division D1's Monday."* That knowledge exists only in the model-building
layer — in our case `engine/solvers/cpsat.py`.

This matters because both optimisations we want are statements **about groups of variables**:

1. *Branch on the course with the fewest placements left* — a property of a group, not of any
   single variable.
2. *Re-optimise one whole division-day at a time* — a fragment defined by a group.

Neither is expressible in the proto, so neither is reachable by setting parameters. §2 and §3.1
give the specific proofs.

### 1.1 The mechanism: tags in variable names

We pass structure down through the variable name. This requires one parameter to be set, and
getting it wrong is silent:

> **`SatParameters.ignore_names` defaults to `true`.** CP-SAT's first action is to copy the user
> model into its presolve context via `ModelCopy::ImportVariablesAndMaybeIgnoreNames()`, which
> drops every variable name unless this parameter is `false`. The solver core then never sees a
> single tag, both features find zero groups, and the fork behaves *exactly* like stock — with
> no error, no warning, and a perfectly valid solution.

Measured, not inferred: dumping both models for a tagged instance gave **96** `@G=` names in the
user model and **0** in the presolved model. Flipping the parameter takes CP-SAT's subsolver list
from 9 entries to 10, with `division_day_lns` among them.

With `ignore_names = false` set, names then survive the rest of presolve: it copies a variable's
name whenever it creates a replacement variable (`presolve_context.cc:1118-1120`), so the tags
reach the solver core intact. `engine/solvers/cpsat.py` sets the parameter whenever tags are
enabled, since keeping names costs memory in CP-SAT's internal copy and buys nothing otherwise.

The model emits names shaped like:

```
x_<req>_<slot>_<room>@R=<requirement id>@G=<division>#<day>
                     └── requirement ──┘└── division-day ──┘
```

Each `@KEY=` value runs to the next `@` or to the end of the name. Text before the first `@` is
free-form, so the existing human-readable debug names are preserved unchanged.

The decisive property: **stock OR-Tools ignores variable names entirely.** The tags are inert on
an unmodified build. A stock-vs-fork benchmark therefore compares two *solvers* on a
byte-identical *model*, which is what makes the comparison meaningful.

---

## 2. The domain-tag parser — `cp_model_utils.{h,cc}`

Both features need the same primitive, so it lives in one place.

### 2.1 `ExtractDomainTag`

```cpp
absl::string_view ExtractDomainTag(absl::string_view var_name,
                                   absl::string_view tag) {
  const absl::string_view::size_type start = var_name.find(tag);
  if (start == absl::string_view::npos) return absl::string_view();
  const absl::string_view::size_type value_start = start + tag.size();
  const absl::string_view::size_type end = var_name.find('@', value_start);
  if (end == absl::string_view::npos) return var_name.substr(value_start);
  return var_name.substr(value_start, end - value_start);
}
```

**Why `absl::string_view` throughout.** The function allocates nothing. Every return value is a
view into the proto's own name string, which outlives the call. This is called once per variable
during setup — 4,250 times on our model — and again inside the LNS constructor; returning
`std::string` would mean thousands of heap allocations for data already in memory.

**Why a missing tag returns an empty view rather than an error.** A model that did not come from
the timetable builder has no tags at all. Every call site treats "empty" as "not part of any
group" and skips the variable, so the code is a no-op on unrelated models instead of a failure.

**Why the value terminates at the next `@`.** It makes the format extensible: a third tag can be
appended later without changing this parser or invalidating existing names.

### 2.2 `GroupVariablesByDomainTag`

```cpp
std::vector<std::vector<int>> GroupVariablesByDomainTag(
    const CpModelProto& cp_model, absl::string_view tag) {
  std::vector<std::vector<int>> groups;

  // Keys are views into the proto's own name strings, which outlive this call.
  absl::flat_hash_map<absl::string_view, int> group_index;
  for (int var = 0; var < cp_model.variables_size(); ++var) {
    const absl::string_view key =
        ExtractDomainTag(cp_model.variables(var).name(), tag);
    if (key.empty()) continue;
    const auto [it, inserted] =
        group_index.insert({key, static_cast<int>(groups.size())});
    if (inserted) groups.push_back({});
    groups[it->second].push_back(var);
  }
  return groups;
}
```

Three deliberate choices:

- **`absl::flat_hash_map` keyed on `string_view`.** Open-addressing, no per-key allocation, keys
  borrowing the proto's storage. This is the idiomatic Abseil map used throughout CP-SAT.
- **The `insert` + structured-binding idiom** performs lookup-and-insert in a **single hash
  probe**. The naive `if (!map.count(k)) map[k] = ...;` form probes twice.
- **Groups are returned in first-seen order, not hash order.** This is the important one. LNS and
  branching must be reproducible run to run; the iteration order of a hash map is not a stable
  contract. Ordering by first appearance makes the grouping a deterministic function of the model.

### 2.3 Why this file

`cp_model_utils` was chosen as the home because **both** `cp_model_lns` and `cp_model_search`
already `#include` it and already declare `:cp_model_utils` in `BUILD.bazel`. The patch therefore
adds **zero new build-dependency edges** — verified before the code was written, not after.

A smaller-looking alternative — putting the helper in `cp_model_lns.h` — would have forced
`cp_model_search` to depend on the LNS library, coupling two subsystems that are currently
independent. Minimising the *dependency* diff matters more than minimising the *line* diff when
the goal is a patch that survives rebasing onto a future upstream release.

---

## 3. Feature 1 — `CHOOSE_MIN_UNFIXED_IN_GROUP` branching

### 3.1 Why parameters cannot do this

`DecisionStrategyProto::VariableSelectionStrategy` has exactly five values upstream:

```proto
CHOOSE_FIRST = 0;
CHOOSE_LOWEST_MIN = 1;
CHOOSE_HIGHEST_MAX = 2;
CHOOSE_MIN_DOMAIN_SIZE = 3;
CHOOSE_MAX_DOMAIN_SIZE = 4;
```

The natural candidate is `CHOOSE_MIN_DOMAIN_SIZE` — minimum remaining values, the standard CSP
heuristic. **It is a provable dead end here.** Our placement variables are Booleans created by
`NewBoolVar`, so every one has domain `{0, 1}`, size 2. The heuristic scores all 4,250 of them
identically and degenerates to `CHOOSE_FIRST`.

The quantity we actually want — *how many placements remain open for this course* — is a property
of a **set** of variables. No per-variable scoring function can express it, which is why this
requires a new enum value rather than a parameter setting.

### 3.2 The proto addition

```proto
    CHOOSE_MAX_DOMAIN_SIZE = 4;
+
+    // Timetabling research fork. Branch on a variable belonging to the group
+    // with the fewest still-unfixed members, where groups are read from the
+    // "@R=" tags in variable names (see cp_model_utils.h).
+    CHOOSE_MIN_UNFIXED_IN_GROUP = 5;
```

Appending value `5` is wire-compatible: existing serialised models never contain it, and older
readers treat it as an unknown enum rather than misparsing.

**Adding the value to the proto is necessary but not sufficient to reach Python.**
`ortools/sat/python/cp_model.py` re-exports each enum value *by hand*:

```python
CHOOSE_MAX_DOMAIN_SIZE = (
    cmh.DecisionStrategyProto.VariableSelectionStrategy.CHOOSE_MAX_DOMAIN_SIZE
)
```

so the patch must add the matching line (§3.7). Without it `cp_model.CHOOSE_MIN_UNFIXED_IN_GROUP`
does not exist, and a correctly patched, correctly compiled build is indistinguishable from a
stock one — the project's fork detection returns `None` and silently falls back to plain CP-SAT.

### 3.3 The one modified line, and why it was unavoidable

```diff
-  return [&view, &parameters, random, strategies]() {
+  return [&view, &parameters, random, strategies, groups, var_to_group,
+          group_unfixed = std::vector<int>()]() mutable {
```

The upstream lambda is stateless, so it needs no `mutable`. Our heuristic must hold a scratch
buffer (`group_unfixed`) **across** invocations to avoid reallocating at every decision node, and
mutating a captured member requires `mutable`. Hence exactly one existing line changes.

`groups` and `var_to_group` are captured **by value**, deliberately. The comment directly above the
upstream lambda explains why the strategies vector is copied — "to keep the return function
validity independently of the life of the passed vector" — and the same reasoning applies to ours:
the returned closure outlives this function's stack frame.

### 3.4 Precompute, done once

```cpp
  std::vector<std::vector<int>> groups;
  std::vector<int> var_to_group;
  for (const DecisionStrategyProto& strategy : strategies) {
    if (strategy.variable_selection_strategy() !=
        DecisionStrategyProto::CHOOSE_MIN_UNFIXED_IN_GROUP) {
      continue;
    }
    groups = GroupVariablesByDomainTag(cp_model_proto, "@R=");
    var_to_group.assign(cp_model_proto.variables_size(), -1);
    for (int g = 0; g < static_cast<int>(groups.size()); ++g) {
      for (const int v : groups[g]) var_to_group[v] = g;
    }
    break;
  }
```

This runs **once per worker at strategy-construction time**, and only if some strategy actually
requests the new enum — a model that never uses it pays a single scan over a short vector.

The `var_to_group` inverse index turns the per-decision question "which group is variable *v* in?"
into an O(1) array read. Building it costs one pass; without it, every scored variable would need a
search through the group lists.

### 3.5 The per-node refresh

```cpp
      if (strategy.variable_selection_strategy() ==
          DecisionStrategyProto::CHOOSE_MIN_UNFIXED_IN_GROUP) {
        group_unfixed.assign(groups.size(), 0);
        for (int g = 0; g < static_cast<int>(groups.size()); ++g) {
          for (const int v : groups[g]) {
            if (!view.IsFixed(v)) ++group_unfixed[g];
          }
        }
      }
```

This is the heart of the feature and the reason it cannot be emulated statically. `view.IsFixed(v)`
is queried **at the current node**, so the ranking reflects the live propagation state: as CP-SAT
fixes placements, courses become more constrained and rise in priority automatically. A Python-side
ordering computed before the solve is frozen at model-build time and cannot react to propagation —
that gap is precisely what this feature closes, and `TIMETABLE_MRV=static` (§5.3) exists to measure
it.

Two performance properties:

- `assign()` on an already-sized vector reuses its capacity — **no allocation after the first
  node**. This is why the buffer is a closure member rather than a local.
- Counting once into `group_unfixed` and then reading it during the variable scan keeps the node
  cost **linear**. Scoring each variable by re-counting its group on demand would be quadratic in
  the group size.

Each worker constructs its own strategy from its own `Model*`, so this mutable state is thread-local
by construction — it is never shared across CP-SAT's parallel workers.

### 3.6 The scoring case

```cpp
          case DecisionStrategyProto::CHOOSE_MIN_UNFIXED_IN_GROUP: {
            const int group = var < static_cast<int>(var_to_group.size())
                                  ? var_to_group[var]
                                  : -1;
            value = group < 0 ? std::numeric_limits<int64_t>::max() - 1
                              : group_unfixed[group];
            break;
          }
```

Untagged variables are scored `int64_max - 1`: they sort **last** but remain selectable, so a
partially tagged model still makes progress rather than stalling. The `- 1` keeps them strictly
below the `int64_max` sentinel that means "no candidate chosen yet", so an untagged variable can
still win when it is the only option left.

The bounds check against `var_to_group.size()` guards the case where presolve introduced variables
after the index was built.

One upstream detail that had to be checked rather than assumed: the existing early-exit in the scan
loop is gated on `CHOOSE_FIRST` only, so the new strategy correctly scans all variables before
deciding.


### 3.7 Exporting the enum to Python — `ortools/sat/python/cp_model.py`

```python
CHOOSE_MIN_UNFIXED_IN_GROUP = (
    cmh.DecisionStrategyProto.VariableSelectionStrategy.CHOOSE_MIN_UNFIXED_IN_GROUP
)
```

Seven lines including the comment, and the whole feature is unreachable from Python without them.
This is the failure mode the CI verification step exists to catch: the wheel built cleanly, the
C++ was correct, and the only symptom was that the fork appeared to be stock.


### 3.8 Passing model validation — `ortools/sat/cp_model_checker.cc`

`ValidateSearchStrategies()` whitelists strategies **by value**, so an unrecognised one is
rejected before the solver ever starts:

```cpp
    if (vss != DecisionStrategyProto::CHOOSE_FIRST &&
        ...
        vss != DecisionStrategyProto::CHOOSE_MAX_DOMAIN_SIZE &&
        vss != DecisionStrategyProto::CHOOSE_MIN_UNFIXED_IN_GROUP) {
      return absl::StrCat(
          "Unknown or unsupported variable_selection_strategy: ", vss);
```

Without this clause a fully patched, correctly compiled wheel returns `MODEL_INVALID` with
`Unknown or unsupported variable_selection_strategy: 5` the moment a model uses the strategy.

**Three files must agree for one enum value to work**: the proto defines it, the checker admits
it, and `cp_model.py` exports it. Missing any one produces a different, quiet failure —
`AttributeError`, `MODEL_INVALID`, or a silent fall back to stock. An exhaustive grep for the
adjacent enum value is the cheap way to find every site:

```bash
grep -rn "CHOOSE_MAX_DOMAIN_SIZE" --include=*.cc --include=*.h --include=*.py ortools/
```

That returns exactly three non-test sites: the checker, the search strategy switch, and the
FlatZinc translator — the last of which only *emits* strategies and is excluded from this build
(`BUILD_FLATZINC=OFF`).

---

## 4. Feature 2 — the `division_day_lns` neighbourhood

### 4.1 Why the built-in generators are structurally blind

CP-SAT's Large Neighbourhood Search repeatedly freezes most of the incumbent solution and
re-optimises a fragment. Upstream picks that fragment either **at random**
(`RelaxRandomVariablesGenerator`, `RelaxRandomConstraintsGenerator`) or by **walking the
variable/constraint graph** (`graph_var_lns`, `graph_arc_lns`, `graph_cst_lns`, `graph_dec_lns`).

Neither can know that a timetable decomposes into division-days. This matters because **nearly
every soft cost the objective pays — idle gaps, day span, break placement — is computed *within* a
single division-day.** A random fragment scattered across ten divisions and five days leaves every
gap penalty pinned by the frozen variables around it: the sub-solver can shuffle placements but
cannot collapse a gap, because the neighbours that define that gap are frozen.

Relaxing one *whole* division-day hands the sub-solver a fragment whose internal structure it can
actually improve.

### 4.2 `Generate()`

```cpp
Neighborhood DivisionDayNeighborhoodGenerator::Generate(
    const CpSolverResponse& initial_solution, SolveData& data,
    absl::BitGenRef random) {
  if (groups_.empty()) return helper_.NoNeighborhood();

  const int num_groups = static_cast<int>(groups_.size());
  const int num_to_relax = std::max(
      1, static_cast<int>(std::round(data.difficulty * num_groups)));
  if (num_to_relax >= num_groups) return helper_.FullNeighborhood();

  // Partial Fisher-Yates: we only need the first `num_to_relax` of a shuffle,
  // so stop there instead of ordering all the groups.
  std::vector<int> order(num_groups);
  std::iota(order.begin(), order.end(), 0);
  for (int i = 0; i < num_to_relax; ++i) {
    std::swap(order[i], order[absl::Uniform(random, i, num_groups)]);
  }

  std::vector<int> relaxed_variables = always_relaxed_;
  for (int i = 0; i < num_to_relax; ++i) {
    const std::vector<int>& group = groups_[order[i]];
    relaxed_variables.insert(relaxed_variables.end(), group.begin(),
                             group.end());
  }
  return helper_.RelaxGivenVariables(initial_solution, relaxed_variables);
}
```

**Honouring the difficulty contract.** `data.difficulty` is in `[0, 1]` and is tuned by the LNS
framework based on how quickly the sub-solver closes the fragments we return. We map it onto *how
many whole division-days to unfreeze*, with a floor of **one** — a fragment smaller than a single
division-day cannot improve the within-day objective terms this generator exists to attack. Respecting
this contract is what lets the generator participate in CP-SAT's adaptive difficulty loop rather than
fighting it.

**Partial Fisher-Yates.** Only the first `num_to_relax` entries of the shuffle are ever read, so the
loop stops there: O(k) rather than O(n) per call. `absl::Uniform` draws from the framework-supplied
`BitGenRef`, keeping the stream reproducible under a fixed seed.

**The two early exits** use the framework's own vocabulary — `NoNeighborhood()` when there is nothing
to work on, `FullNeighborhood()` when the requested difficulty covers everything — so the LNS
scheduler accounts for them exactly as it does for the built-in generators.

### 4.3 `always_relaxed_` — a correctness requirement, not an optimisation

```cpp
DivisionDayNeighborhoodGenerator::DivisionDayNeighborhoodGenerator(
    NeighborhoodGeneratorHelper const* helper, absl::string_view name,
    std::vector<std::vector<int>> groups)
    : NeighborhoodGenerator(name, helper), groups_(std::move(groups)) {
  const CpModelProto& model_proto = helper->ModelProto();
  for (int var = 0; var < model_proto.variables_size(); ++var) {
    if (ExtractDomainTag(model_proto.variables(var).name(), "@G=").empty()) {
      always_relaxed_.push_back(var);
    }
  }
}
```

`RelaxGivenVariables()` relaxes exactly what it is given and **freezes everything else**. Our model
contains 308 auxiliary variables carrying no `@G=` tag — per-day occupancy indicators, gap and span
literals, workload counters. These are *functionally determined* by the placement variables through
`lin_max` and linear constraints.

Freezing them at their incumbent values while relaxing the placements underneath them would pin the
very occupancy pattern the generator is trying to change, producing a neighbourhood that is feasible
but essentially **unimprovable** — the generator would appear to run while achieving nothing.

Relaxing them costs almost nothing in search: once the frozen division-days' placements are fixed,
propagation re-derives them immediately.

The list is computed **once in the constructor**, not per call, and `groups` is taken by value and
`std::move`d into the member so the vectors are never copied.

### 4.4 Self-disabling on unrelated models

```cpp
  bool ReadyToGenerate() const override {
    return !groups_.empty() && NeighborhoodGenerator::ReadyToGenerate();
  }
```

A model with no `@G=` tags yields no groups, so the generator reports itself not-ready rather than
burning an LNS worker. It composes with — rather than replaces — the base-class condition.

### 4.5 Registration

```cpp
    {
      std::vector<std::vector<int>> division_day_groups =
          GroupVariablesByDomainTag(shared->model_proto, "@G=");
      if (division_day_groups.size() > 1 &&
          name_filter.Keep("division_day_lns")) {
        reentrant_interleaved_subsolvers.push_back(std::make_unique<LnsSolver>(
            std::make_unique<DivisionDayNeighborhoodGenerator>(
                helper, name_filter.LastName(),
                std::move(division_day_groups)),
            lns_params_base, lns_params_stalling, helper, shared));
      }
    }
```

This mirrors exactly how upstream gates its scheduling, packing and routing generators: register only
when the model actually contains the structure the generator exploits, so the change costs nothing on
every other model. `> 1` because a single group means the "neighbourhood" would be the entire model.

`reentrant_interleaved_subsolvers` places it in the same pool as the other LNS workers, so it competes
for time under CP-SAT's existing scheduler with no special privileges. The whole block is braced so
`division_day_groups` does not leak into the surrounding scope.

**Activation semantics matter here.** `SubsolverNameFilter::Keep()` returns true by default, so the
generator **runs automatically** once compiled in — it is not opt-in via `extra_subsolvers`.
Conversely it is switched *off* by naming it in `ignore_subsolvers`, which is what makes the baseline
arm of the A/B possible on a single binary.

---

## 5. Project integration — `engine/solvers/cpsat.py` (+118 / −2)

### 5.1 Fork detection

```python
_FORK_MRV_STRATEGY = getattr(cp_model, "CHOOSE_MIN_UNFIXED_IN_GROUP", None)
HAS_TIMETABLE_FORK = _FORK_MRV_STRATEGY is not None
```

Both features ship in the same patch, so the presence of the new enum also certifies that
`division_day_lns` exists as a subsolver. This lets the project run correctly on a stock
`pip install ortools` **and** on the fork with no code edits — on stock it silently falls back to
plain CP-SAT.

### 5.2 Tag emission

```python
            name = f"x_{req.id}_{start_id}_{room_id}"
            if tags_on:
                r = req_tag.setdefault(req.id, len(req_tag))
                g = group_tag.setdefault((req.division_id, day), len(group_tag))
                name = f"{name}@R={r}@G={g}"
            x[(req.id, start_id, room_id)] = model.NewBoolVar(name)
```

Compact **integer** tags rather than full ids: the C++ side groups by the tag's *value*, so any
unique token works, and short names keep presolve's repeated copying of them cheap. Tags default off
on a stock build, where nothing reads them.

### 5.3 Experiment switches

All read from the environment at call time, so a benchmark can sweep arms without editing code.

| Variable | Values | Meaning |
|---|---|---|
| `TIMETABLE_MRV` | `auto`, `dynamic`, `static`, `off` | branching order |
| `TIMETABLE_DIVISION_DAY_LNS` | `1`, `0` | the new neighbourhood |
| `TIMETABLE_FORK_TAGS` | `1`, `0` | force tags on/off |

`static` is the **Tier-0 ablation**: one fixed order decided before search starts, the best a stock
build can do. It separates "gain from merely ordering variables" from "gain from re-ranking them
live", which is the fork's actual claim — and it pre-empts the obvious reviewer question, *"why not
just tune the parameters?"*

---

## 6. Verification status

**Verified:**

- The patch applies cleanly to upstream `98c165af` — 9 files, 245 insertions, 2 deletions.
- The algorithms are correct. `third_party/or-tools-fork/algo_test.cc` lifts both out of the patch
  verbatim (only `absl::string_view` → `std::string_view`), compiles with plain
  `g++ -std=c++17 -Wall -Wextra -O2`, and runs against 4,558 real variable names from this project's
  model: **10 `@G=` groups, 76 `@R=` groups, 4,250 tagged, 308 untagged — all checks passed**,
  independently reproducing the counts Python computes on the same model.
- The patch adds no new build-dependency edges.

**Not yet verified:** that the patch compiles inside the full OR-Tools tree, and that it improves
solve time or solution quality. Both require a completed build. **Neither should be claimed until
then.**

### 6.1 Measurement protocol

CP-SAT runs with `num_search_workers = 8` and its parallel portfolio is **not deterministic** by
default. Measured on this project: `tests/engine/test_breaks.py::test_cpsat_breaks_vary_across_days`,
run five times against an **unmodified** model, gave PASS / PASS / PASS / FAIL / FAIL.

A baseline that flips roughly 40% of the time will happily manufacture a large "improvement" — or a
large regression — from pure noise. **No n=1 comparison from this system is evidence.** Every arm must
be run many times, with a distribution reported: median and spread, not a single number.

The arms:

| Arm | Configuration | Purpose |
|---|---|---|
| A | stock wheel | baseline |
| B | fork wheel, tags off, MRV off | must match A — proves the patch is inert when disabled |
| C | fork wheel, `TIMETABLE_MRV=static` | Tier-0: gain from ordering alone |
| D | fork wheel, LNS only | neighbourhood in isolation |
| E | fork wheel, both features | the headline measurement |

Arm B is the one reviewers ask about and the easiest to skip.

### 6.2 How to report the result, including a null one

Decide this before looking at the numbers. Choosing a framing afterwards is how honest projects
drift into overclaiming.

**If arm E beats arm A.** Report the median and spread of both, state the number of seeds, and
attribute the gain only as far as the ablations support it. Arm C (static ordering) and arm D
(neighbourhood alone) exist to decompose it: if C already captures most of the gain, the
contribution is *variable ordering*, which is largely reachable without a fork, and the paper
should say so. The fork's distinctive claim is the gap between C and E — live re-ranking against
frozen ordering.

**If arm E matches arm A.** This is a legitimate result and it should be reported as one. The
defensible framing is: *two structure-aware specialisations were implemented at the solver's
source level; on an instance of this size and shape neither produced a statistically
distinguishable improvement over the stock portfolio.* That is a real finding about CP-SAT —
its default portfolio is strong enough that hand-written domain neighbourhoods do not
automatically beat it — and it is far more valuable than a two-run speedup that nobody can
reproduce.

A null result stays publishable **only if arm B is clean**. If B matches A, the patch is proven
inert when disabled, so the null is a genuine measurement rather than a broken build. If B
differs from A, nothing else in the table can be trusted. That is the entire reason arm B exists.

**What not to do.** Do not report the best of several runs. Do not drop seeds that came out
badly. Do not quote a single-run speedup. On this workload an unmodified model was measured
flipping PASS/PASS/PASS/FAIL/FAIL, so a cherry-picked pair of runs can manufacture almost any
conclusion in either direction.

**Scope the claim to what was measured.** One instance, one machine, one time budget. The honest
sentence is "on the DJSCE CSE-DS reference instance", not "for university timetabling".

### 6.3 What is fair to claim already

Independent of any timing result, the following are established and defensible:

- Two specialisations were implemented at solver source level, in 9 files, adding 245 lines while
  modifying two existing lines and deleting none.
- Neither is reachable through the public API, with the reason stated precisely: `NewBoolVar`
  gives every placement variable domain size 2, so `CHOOSE_MIN_DOMAIN_SIZE` cannot distinguish
  them, and no upstream neighbourhood generator can express a division-day fragment.
- The design routes application structure through variable names, which survive presolve, so the
  same model text is emitted to stock and forked solvers alike — making the comparison valid.
- The patch is inert by construction on any model lacking the tags, and the code paths that
  enable it are gated on their presence.

These are claims about *design and engineering*, and they do not depend on the benchmark outcome.

The cheapest confirmation that a run is actually using the fork is that `division_day_lns` appears in
CP-SAT's subsolver list in the solve log; on a stock build it is absent.
