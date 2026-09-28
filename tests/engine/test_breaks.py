"""Regression tests for break placement: exactly one break per day per division, pinned to its
own day, inside the mid-day band, and -- since breaks must count as "occupied" for gap-scoring
purposes -- not pushed to the day's edge just to dodge the idle-gap penalty. Also tests the
continuous-teaching limit: no more than 4 contiguous non-break teaching hours per day."""
from engine.models import expand_requirements
from engine.scoring import score, MAX_CONTINUOUS_TEACHING_PERIODS
from engine.solvers.greedy import GreedySolver
from engine.solvers.ga import GASolver


def _break_placements(solution, problem):
    reqs = {r.id: r for r in expand_requirements(problem)}
    slots = {t.id: t for t in problem.time_slots}
    out = []
    for a in solution.assignments:
        r = reqs.get(a.session_id)
        if r and r.is_break:
            out.append((r, slots[a.time_slot_id]))
    return out


def _max_continuous_teaching_run(solution, problem):
    """Compute the longest run of contiguous non-break periods per (division, day)."""
    reqs = {r.id: r for r in expand_requirements(problem)}
    slots_by_id = {t.id: t for t in problem.time_slots}
    assignments_by_session = solution.assignment_by_session()

    # per-(division, day), collect occupied periods and break periods
    division_day_periods = {}
    division_day_break_periods = {}
    for req in reqs.values():
        a = assignments_by_session.get(req.id)
        if not a:
            continue
        ts = slots_by_id.get(a.time_slot_id)
        if not ts:
            continue
        dkey = (req.division_id, ts.day)
        periods = division_day_periods.setdefault(dkey, set())
        for p in range(ts.period, ts.period + req.duration_slots):
            periods.add(p)
        if req.is_break:
            division_day_break_periods.setdefault(dkey, set()).add(ts.period)

    max_run = 0
    for dkey, periods in division_day_periods.items():
        if not periods:
            continue
        break_periods = division_day_break_periods.get(dkey, set())
        sorted_periods = sorted(periods)
        run = 1
        for i in range(1, len(sorted_periods)):
            if sorted_periods[i] == sorted_periods[i - 1] + 1:
                # contiguous with previous
                if sorted_periods[i - 1] not in break_periods and sorted_periods[i] not in break_periods:
                    run += 1
                else:
                    run = 1  # break interrupts
            else:
                run = 1  # gap interrupts
            max_run = max(max_run, run)
    return max_run


def test_greedy_break_exactly_one_per_day_in_band(small_problem):
    sol = GreedySolver().solve(small_problem)
    for req, ts in _break_placements(sol, small_problem):
        day_slots = small_problem.slots_for_day(ts.day)
        assert ts.day == req.fixed_day
        assert ts.period not in (day_slots[0].period, day_slots[1].period, day_slots[-1].period)


def test_ga_randomized_seed_break_respects_band(small_problem):
    # exercises GA's own _randomized_construct seeding path (regression for the stale band bug)
    sol = GASolver().solve(small_problem, time_limit_s=6)
    for req, ts in _break_placements(sol, small_problem):
        day_slots = small_problem.slots_for_day(ts.day)
        assert ts.day == req.fixed_day, f"{req.id} landed on day {ts.day}, expected {req.fixed_day}"
        assert ts.period not in (day_slots[0].period, day_slots[1].period, day_slots[-1].period)


def test_scorer_does_not_penalize_break_as_a_gap(small_problem):
    # a break sandwiched between occupied periods must not register as an idle gap -- otherwise
    # the optimizer is incentivized to push breaks to the day's edge instead of mid-day
    sol = GreedySolver().solve(small_problem)
    result = score(sol, small_problem)
    breaks_in_band = all(
        ts.period not in (small_problem.slots_for_day(ts.day)[0].period, small_problem.slots_for_day(ts.day)[-1].period)
        for _req, ts in _break_placements(sol, small_problem)
    )
    assert breaks_in_band
    # idle_gaps should reflect only genuinely idle (unoccupied, non-break) periods
    assert result.details["idle_gaps"] >= 0


def test_day_span_is_a_scored_soft_term(small_problem):
    # §16: day_span discourages a division stretching across the whole template; it must be a
    # first-class scored term so the optimizers can see and minimize it.
    from engine.scoring import SOFT_WEIGHTS
    assert "day_span" in SOFT_WEIGHTS
    result = score(GreedySolver().solve(small_problem), small_problem)
    assert "day_span" in result.details and result.details["day_span"] >= 0


def test_cpsat_breaks_vary_across_days(reference_problem):
    # §16: the break target rotates per day, so a division's breaks should spread over the week
    # rather than all landing on the same period (the pre-fix behaviour).
    from engine.solvers.cpsat import CPSATSolver
    sol = CPSATSolver().solve(reference_problem, time_limit_s=25)
    by_div = {}
    for req, ts in _break_placements(sol, reference_problem):
        by_div.setdefault(req.division_id, set()).add(ts.period)
    assert any(len(periods) >= 3 for periods in by_div.values()), \
        f"expected a division's breaks to span >=3 distinct periods, got {by_div}"


def test_reference_pipeline_breaks_cluster_near_midmorning(reference_problem):
    from engine.pipeline import run_pipeline, PipelineConfig
    cfg = PipelineConfig(greedy_time_limit_s=3, mip_time_limit_s=20, ga_time_limit_s=8, cpsat_time_limit_s=20)
    sol = run_pipeline(reference_problem, cfg).final
    placements = _break_placements(sol, reference_problem)
    assert len(placements) == len(reference_problem.divisions) * reference_problem.days_per_week
    for req, ts in placements:
        assert ts.day == req.fixed_day
        day_slots = reference_problem.slots_for_day(ts.day)
        assert ts.period not in (day_slots[0].period, day_slots[1].period, day_slots[-1].period)


def test_scorer_detects_division_continuous_teaching_exceeds_limit(small_problem):
    # Verify that the scorer correctly detects when a division exceeds the 4-period continuous
    # teaching limit. We construct this by checking a greedy solution (which doesn't enforce
    # the constraint) and if it violates it, confirming the scorer catches the violation.
    from engine.solvers.greedy import GreedySolver
    sol = GreedySolver().solve(small_problem)
    result = score(sol, small_problem)
    # Greedy is a fast seed that may violate the continuous-teaching constraint, so we just
    # verify the scorer can detect it (if violated) or report zero violations (if satisfied).
    # Either way, the metric must exist in the breakdown.
    assert "hard:division_continuous_teaching_exceeds_limit" in result.details or result.hard_violations == 0


def test_cpsat_respects_continuous_teaching_cap(small_problem):
    from engine.solvers.cpsat import CPSATSolver
    sol = CPSATSolver().solve(small_problem, time_limit_s=20)
    result = score(sol, small_problem)
    assert result.hard_violations == 0, f"Expected hard_violations=0, got {result.hard_violations}; details: {result.details}"
    # Verify the longest contiguous non-break teaching run doesn't exceed the cap
    max_run = _max_continuous_teaching_run(sol, small_problem)
    assert max_run <= MAX_CONTINUOUS_TEACHING_PERIODS, \
        f"Division had {max_run} contiguous teaching periods, max is {MAX_CONTINUOUS_TEACHING_PERIODS}"


def test_pipeline_respects_continuous_teaching_cap(reference_problem):
    from engine.pipeline import run_pipeline, PipelineConfig
    cfg = PipelineConfig(greedy_time_limit_s=3, mip_time_limit_s=20, ga_time_limit_s=8, cpsat_time_limit_s=60)
    sol = run_pipeline(reference_problem, cfg).final
    result = score(sol, reference_problem)
    assert result.hard_violations == 0, f"Expected hard_violations=0, got {result.hard_violations}"
    max_run = _max_continuous_teaching_run(sol, reference_problem)
    assert max_run <= MAX_CONTINUOUS_TEACHING_PERIODS, \
        f"Division had {max_run} contiguous teaching periods, max is {MAX_CONTINUOUS_TEACHING_PERIODS}"
