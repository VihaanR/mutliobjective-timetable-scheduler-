# AUGMECON2 Fix Documentation

## Overview

This document explains the changes made to implement the AUGMECON2 epsilon-constraint formulation correctly, as specified in the research paper. The changes address the issues with epsilon values and slack normalization. This document also explains the changes made to implement the absent classes feature correctly, as specified in the research paper.

## Issues Addressed

### 1. Epsilon Values in the 5-6 Lakh Range

**Root Cause:**
- Scale mismatch between objective categories used in the payoff table.
- The penalty weights in each category were very different in magnitude.

**Solution:**
- Implemented the paper's exact AUGMECON2 formulation (Eq. 8):
  ```
  min Z_f(x) + μ * (s_s/r_s + s_r/r_r)
  s.t. Z_s(x) + s_s = ε_s, s_s ≥ 0
       Z_r(x) + s_r = ε_r, s_r ≥ 0
  ```
- Where `μ = 10⁻³` and `r_s`, `r_r` are the ranges of each bounded objective from the payoff table.

### 2. Tight/Loose Bound Affected by Scale Mismatch

**Root Cause:**
- The slack variable was not normalized by its range.

**Solution:**
- Added `ranges` parameter to `solve_pareto_point()` to pass per-category ranges.
- Implemented slack normalization in the objective:
  ```
  model.Minimize(1000 * minimize_expr + sum(c * s for c, s in zip(slack_coeffs, slack_terms)))
  ```
- Where `c = max(1, 1000 // r_k)` is the normalized slack coefficient.

**Absent Classes Feature:**
- Added `absent_classes` parameter to `solve_pareto_point()` to pass the list of absent classes.
- Implemented absent classes handling in the objective:
  ```
  model.Minimize(1000 * minimize_expr + sum(c * s for c, s in zip(slack_coeffs, slack_terms)) + sum(a * absent for a, absent in zip(absent_coeffs, absent_terms)))
  ```
- Where `a = max(1, 1000 // a_k)` is the normalized absent coefficient.

**Root Cause:**
- The slack variable was not normalized by its range.

**Solution:**
- Added `ranges` parameter to `solve_pareto_point()` to pass per-category ranges.
- Implemented slack normalization in the objective:
  ```
  model.Minimize(1000 * minimize_expr + sum(c * s for c, s in zip(slack_coeffs, slack_terms)))
  ```
- Where `c = max(1, 1000 // r_k)` is the normalized slack coefficient.

## Implementation Details

### Files Modified

1. `engine/solvers/cpsat.py`
   - Added `ranges` parameter to `solve_pareto_point()`
   - Implemented paper's Eq. 8 in integer form
   - Added slack normalization

2. `engine/pareto_sweep.py`
   - Modified `_lexicographic_payoff()` to return `range_val`
   - Updated `sweep_pair_stream()` to pass ranges to `session.solve()`

3. `tests/engine/test_cpsat_pareto.py`
   - Added tests for sane epsilon range and normalized slack

### Key Changes

1. **ParetoSession.solve()**
   - Added `ranges` parameter
   - Forwarded to `solve_pareto_point()`

2. **solve_pareto_point()**
   - Added `ranges` parameter
   - Implemented paper's Eq. 8
   - Added slack normalization

3. **_lexicographic_payoff()**
   - Returned `range_val`

4. **sweep_pair_stream()**
   - Passed ranges to `session.solve()`

## Verification

The changes were verified with:
- Unit tests for epsilon range and slack normalization
- Integration tests with the existing test suite
- Manual verification of the payoff table and epsilon grid generation

## Conclusion

The implementation now correctly follows the AUGMECON2 formulation from the research paper, addressing the issues with epsilon values and slack normalization.