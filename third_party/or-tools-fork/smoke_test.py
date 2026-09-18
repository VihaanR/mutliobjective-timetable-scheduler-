"""Functional test that the timetabling fork actually engages, not merely that it imports.

Run against any interpreter with a candidate wheel installed:

    python third_party/or-tools-fork/smoke_test.py

Deliberately self-contained -- no project imports -- so it tests the wheel and nothing else.

Why this exists: checking that `cp_model.CHOOSE_MIN_UNFIXED_IN_GROUP` imports is not enough.
Two separate bugs produced a wheel that imported perfectly and still did nothing:

  1. cp_model_checker.cc whitelists strategies by value, so a model using the new one was
     rejected with MODEL_INVALID ("Unknown or unsupported variable_selection_strategy: 5").
  2. SatParameters.ignore_names defaults to true, so CP-SAT stripped the "@R="/"@G=" tags
     while copying the user model into presolve. The subsolver found zero groups, never
     registered, and the fork behaved exactly like stock -- silently, with a valid answer.

Each check below corresponds to a failure actually observed.
"""

import sys

from ortools.sat.python import cp_model

GROUPS, ITEMS, SLOTS = 8, 8, 12

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' -- ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def build_model() -> tuple[cp_model.CpModel, list]:
    """A small assignment problem whose variables carry the project's tag scheme."""
    model = cp_model.CpModel()
    x = {}
    for g in range(GROUPS):
        for i in range(ITEMS):
            for s in range(SLOTS):
                x[g, i, s] = model.NewBoolVar(f"x_{g}_{i}_{s}@R={g * ITEMS + i}@G={g}")
            model.AddExactlyOne(x[g, i, s] for s in range(SLOTS))
    for g in range(GROUPS):
        for s in range(SLOTS):
            model.AddAtMostOne(x[g, i, s] for i in range(ITEMS))
    # An objective is required for CP-SAT to run its LNS portfolio at all.
    model.Minimize(
        sum(s * x[g, i, s] for g in range(GROUPS) for i in range(ITEMS) for s in range(SLOTS))
    )
    return model, list(x.values())


def solve(model, *, use_strategy_vars=None, ignore_names=True, seconds=20):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = 8
    solver.parameters.ignore_names = ignore_names
    solver.parameters.log_search_progress = True
    log: list[str] = []
    solver.log_callback = log.append
    if use_strategy_vars is not None:
        model.AddDecisionStrategy(
            use_strategy_vars, cp_model.CHOOSE_MIN_UNFIXED_IN_GROUP, cp_model.SELECT_MAX_VALUE
        )
    status = solver.Solve(model)
    return solver, status, "\n".join(log)


print("1. the wheel exposes the fork")
strategy = getattr(cp_model, "CHOOSE_MIN_UNFIXED_IN_GROUP", None)
check(strategy is not None, "CHOOSE_MIN_UNFIXED_IN_GROUP is exported to Python")
if strategy is None:
    print("\nSTOCK wheel -- nothing further to test.")
    sys.exit(1)

print("\n2. the subsolver registers and runs (needs ignore_names=false)")
model, _ = build_model()
solver, status, log = solve(model, ignore_names=False)
registered = "division_day_lns" in log
check(
    registered,
    "division_day_lns appears in CP-SAT's subsolver list",
    "" if registered else "the tags were stripped or the subsolver did not register",
)
subsolver_line = next((l for l in log.splitlines() if "interleaved subsolvers" in l), "")
if subsolver_line:
    print(f"       {subsolver_line.strip()[:200]}")

print("\n3. a model using the new strategy is accepted by the validator")
model2, all_vars = build_model()
solver2, status2, _ = solve(model2, use_strategy_vars=all_vars, ignore_names=False)
name2 = solver2.StatusName(status2)
check(
    status2 != cp_model.MODEL_INVALID,
    "model with CHOOSE_MIN_UNFIXED_IN_GROUP is not rejected",
    f"status={name2} info={solver2.ResponseProto().solution_info}"
    if status2 == cp_model.MODEL_INVALID
    else f"status={name2}",
)
check(
    status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE),
    "that model actually solves",
    f"status={name2}",
)

print("\n4. the fork stays inert on an untagged model")
plain = cp_model.CpModel()
v = [plain.NewBoolVar(f"y_{i}") for i in range(20)]
plain.AddExactlyOne(v)
plain.Minimize(sum(i * v[i] for i in range(20)))
_, status3, log3 = solve(plain, seconds=5, ignore_names=False)
check(
    "division_day_lns" not in log3,
    "division_day_lns does not register without @G= tags",
)

print()
if failures:
    print(f"SMOKE TEST FAILED: {len(failures)} check(s) -- {'; '.join(failures)}")
    sys.exit(1)
print("SMOKE TEST PASSED: the fork is present, registers, validates and solves.")
