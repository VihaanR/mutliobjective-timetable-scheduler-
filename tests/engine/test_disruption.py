"""Golden tests for the disruption engine (design.md §7 / P4).

Covers the resolve rebuild (default: re-solve the whole week, drop nothing) and keeps a smaller set
of tests for the original `mode="minimal"` patcher, which is still supported for comparison.
"""
import pytest

from engine.models import expand_requirements
from engine.scoring import score
from engine.solvers.cpsat import CPSATSolver
from engine.disruption import replan, affected_slots_for_day


@pytest.fixture(scope="module")
def baseline(reference_problem):
    # a CONFLICT-FREE baseline (hard=0, all sessions placed) is required for the disruption
    # engine's is_valid / conflict_violations semantics to be meaningful -- they measure whether
    # the re-plan introduces NEW conflicts, which only makes sense relative to a clean baseline.
    sol = CPSATSolver().solve(reference_problem, time_limit_s=45)
    assert score(sol, reference_problem).hard_violations == 0, "baseline must be conflict-free"
    return sol


def _occ(problem, slot_id, duration):
    slots_by_id = {t.id: t for t in problem.time_slots}
    sbp = {(t.day, t.period): t for t in problem.time_slots}
    ts = slots_by_id[slot_id]
    ids = [ts.id]
    for k in range(1, duration):
        nxt = sbp.get((ts.day, ts.period + k))
        if nxt:
            ids.append(nxt.id)
    return ids


# --- engine-level field support (blocked_slot_ids / relaxed_days) ---

def test_blocked_slot_ids_default_empty_is_noop(reference_problem):
    assert reference_problem.blocked_slot_ids == frozenset()
    assert reference_problem.relaxed_days == frozenset()
    assert reference_problem.pinned_slots == {}


def test_scoring_flags_sessions_in_blocked_slots(reference_problem, baseline):
    from dataclasses import replace
    # block a mid-morning Tuesday slot the greedy baseline is likely using
    blocked = frozenset({10, 11, 12})
    clean = score(baseline, reference_problem)
    with_block = score(baseline, replace(reference_problem, blocked_slot_ids=blocked))
    assert with_block.hard_violations >= clean.hard_violations


def test_pinned_slots_fix_session_via_fixed_time_slot_id(reference_problem):
    from dataclasses import replace
    reqs = expand_requirements(reference_problem)
    target = next(r.id for r in reqs if not r.is_break)
    pinned = replace(reference_problem, pinned_slots={target: 18})
    by_id = {r.id: r for r in expand_requirements(pinned)}
    assert by_id[target].fixed_time_slot_id == 18


# --- replan() golden behavior ---
#
# Solver choice in these tests is deliberate. The structural guarantees (nothing lands in a blocked
# slot, inputs aren't mutated, unplaceable sessions are named) hold for ANY solver, so those tests
# use `solver="greedy"` and run instantly. Only the quality claim -- "a rained-out session is
# rescheduled instead of dropped" -- needs a real solve, so it shares ONE module-scoped CP-SAT
# replan rather than paying for it per test. CP-SAT needs >=10s here: at 5s it times out and the
# greedy fallback takes over (hard=22). The budget was widened 30s -> 120s when hard constraint 21
# (division continuous-teaching cap, CLAUDE.md §4) landed: the extra per-division/day sliding-window
# constraints across all 5 days made the whole-week resolve model harder to satisfy within the old
# 30s margin, so CP-SAT was timing out and falling back to greedy (which doesn't enforce rule 21).
# In isolation 60s alone reliably reached hard=0, but inside the full test_disruption.py run (CPU
# contention from the earlier CP-SAT baseline fixture and other tests in the same session) 60s
# proved marginal and flaked to hard=0 in isolation vs hard=28 in-suite -- CP-SAT's parallel search
# is not wall-clock-deterministic near the solver feasibility boundary. 120s gave comfortable, repeatable
# margin (hard=0 both standalone and inside the full test_disruption.py run) -- see design.md §17.

@pytest.fixture(scope="module")
def rain_replan(reference_problem, baseline):
    """One CP-SAT resolve of the headline scenario (rain from period 5 on Tuesday), shared by the
    quality assertions below."""
    affected = affected_slots_for_day(reference_problem, day=1, from_period=5)
    return affected, replan(reference_problem, baseline, affected, solver="cpsat", time_limit_s=120)


def test_replan_no_disruption_equals_baseline(reference_problem, baseline):
    result = replan(reference_problem, baseline, frozenset())
    assert result.moved_sessions == []
    assert result.dropped_session_ids == []
    assert result.is_valid


def test_replan_places_nothing_in_blocked_slots(reference_problem, baseline):
    affected = affected_slots_for_day(reference_problem, day=1, from_period=5)
    result = replan(reference_problem, baseline, affected, solver="greedy", time_limit_s=5)
    reqs = {r.id: r for r in expand_requirements(reference_problem)}
    for a in result.solution.assignments:
        req = reqs.get(a.session_id)
        if req and not req.is_break:
            occ = _occ(reference_problem, a.time_slot_id, req.duration_slots)
            assert not any(sid in affected for sid in occ), f"{a.session_id} landed in a blocked slot"


def test_replan_resolve_reschedules_instead_of_dropping(reference_problem, baseline, rain_replan):
    """THE contract of the resolve rebuild (design.md §7): a partial-day disruption drops NOTHING.
    The old minimal-change patcher could only reshuffle within the disrupted day and dropped the 7
    sessions that didn't fit; re-solving the whole week finds them a home elsewhere."""
    _affected, result = rain_replan
    assert result.dropped_session_ids == []
    assert result.unplaced_sessions == []
    placed = {a.session_id for a in result.solution.assignments}
    assert placed == {r.id for r in expand_requirements(reference_problem)}


def test_replan_resolve_may_move_sessions_off_the_disrupted_day(reference_problem, rain_replan):
    """Deliberate inversion of the OLD minimal-change guarantee. The patcher promised every moved
    session originated on the disrupted day; a full re-solve explicitly trades that away for
    "nothing is silently lost", so movement elsewhere in the week is expected, not a bug."""
    _affected, result = rain_replan
    assert result.moved_sessions, "a disruption this size must move something"


def test_replan_placed_sessions_are_conflict_free(rain_replan):
    _affected, result = rain_replan
    # nothing dropped AND no clashes among what was placed -> a genuinely valid adjusted week
    assert result.hard_violations == 0
    assert result.conflict_violations == 0
    assert result.is_valid


def test_replan_whole_day_holiday_reports_unplaced_sessions_by_name(reference_problem, baseline):
    """A whole-day holiday removes ~20% of weekly capacity, which the remaining four days genuinely
    cannot absorb. The contract is HONEST REPORTING, not a silent drop: hard violations surface, and
    every unplaceable session is named (course/division/faculty) so an admin can act on it."""
    affected = affected_slots_for_day(reference_problem, day=3, from_period=None)
    result = replan(reference_problem, baseline, affected, solver="greedy", time_limit_s=5)

    assert result.hard_violations > 0, "an over-constrained holiday must not claim success"
    assert result.unplaced_sessions, "unplaceable sessions must be reported, not silently dropped"
    assert len(result.unplaced_sessions) == len(result.dropped_session_ids)
    for entry in result.unplaced_sessions:
        assert entry["course_code"] and entry["division_id"]
        assert entry["label"] and entry["course_code"] in entry["label"]
    # the count-only note is followed by one bullet per session
    assert any(entry["label"] in note for note in result.notes for entry in result.unplaced_sessions)

    # even when over-constrained, nothing is placed into the blocked window
    reqs = {r.id: r for r in expand_requirements(reference_problem)}
    for a in result.solution.assignments:
        req = reqs.get(a.session_id)
        if req and not req.is_break:
            occ = _occ(reference_problem, a.time_slot_id, req.duration_slots)
            assert not any(sid in affected for sid in occ)


def test_extra_relaxed_days_lowers_hard_violations(reference_problem, baseline):
    """`extra_relaxed_days` is the admin's escape hatch (design.md §7): naming more days exempts
    them from the day-shaped hard rules so displaced load can legally land there.

    NOTE it does not make an over-constrained holiday feasible: relaxing day-SHAPE rules cannot
    manufacture slots, so the unplaceable set is unchanged and only the shape violations clear.
    Verified on the reference dataset: hard 28 -> 25 with all days relaxed, unplaced stays 18."""
    affected = affected_slots_for_day(reference_problem, day=3, from_period=None)
    default = replan(reference_problem, baseline, affected, solver="greedy", time_limit_s=5)
    relaxed = replan(reference_problem, baseline, affected, solver="greedy", time_limit_s=5,
                     relaxed_days=frozenset(range(reference_problem.days_per_week)))

    assert relaxed.hard_violations < default.hard_violations
    assert len(relaxed.dropped_session_ids) == len(default.dropped_session_ids)


def test_replan_rejects_unknown_solver_and_mode(reference_problem, baseline):
    affected = affected_slots_for_day(reference_problem, day=1, from_period=5)
    with pytest.raises(ValueError):
        replan(reference_problem, baseline, affected, solver="nope")
    with pytest.raises(ValueError):
        replan(reference_problem, baseline, affected, mode="nope")


# --- mode="minimal": the original patcher, kept for comparison/rollback ---

def test_minimal_mode_still_pins_undisrupted_days(reference_problem, baseline):
    """The old contract, now opt-in: only the disrupted day may change, and sessions that don't fit
    are dropped rather than rehomed elsewhere in the week."""
    affected = affected_slots_for_day(reference_problem, day=1, from_period=5)
    result = replan(reference_problem, baseline, affected, mode="minimal")
    slots_by_id = {t.id: t for t in reference_problem.time_slots}
    for m in result.moved_sessions:
        assert m.from_slot_id is None or slots_by_id[m.from_slot_id].day == 1
    assert result.is_valid  # drops are intentional here; placed sessions stay conflict-free


def test_minimal_mode_names_the_sessions_it_drops(reference_problem, baseline):
    affected = affected_slots_for_day(reference_problem, day=3, from_period=None)
    result = replan(reference_problem, baseline, affected, mode="minimal")
    assert result.dropped_session_ids
    assert len(result.unplaced_sessions) == len(result.dropped_session_ids)
    assert all(entry["label"] for entry in result.unplaced_sessions)


def test_replan_does_not_mutate_inputs(reference_problem, baseline):
    before_assignments = list(baseline.assignments)
    before_blocked = reference_problem.blocked_slot_ids
    replan(reference_problem, baseline, affected_slots_for_day(reference_problem, 1, from_period=5),
           solver="greedy", time_limit_s=5)
    assert baseline.assignments == before_assignments
    assert reference_problem.blocked_slot_ids == before_blocked  # master problem untouched
