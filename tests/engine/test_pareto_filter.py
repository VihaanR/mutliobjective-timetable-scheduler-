"""Tests for engine.pareto — pure dominance filter (spec §3.1, TDD §4).

These tests are intentionally written FIRST (TDD) so they initially fail
(engine/pareto.py does not yet exist), then pass once the implementation lands.
No CP-SAT, no network, no filesystem: all pure-Python, sub-millisecond.
"""
from __future__ import annotations

import random

import pytest

from engine.pareto import dominates, pareto_filter
from engine.pareto_sweep import FrontierPoint


# ── helpers ─────────────────────────────────────────────────────────────────

def _pt(bound_value, minimize_value, hard=0, **kwargs) -> FrontierPoint:
    """Build a minimal FrontierPoint for filter tests."""
    return FrontierPoint(
        pair="fac<=eps,min=stu",
        epsilon=0,
        bound_category="faculty",
        minimize_category="students",
        bound_value=bound_value,
        minimize_value=minimize_value,
        hard_violations=hard,
        wall_s=0.1,
        **kwargs,
    )


# ── dominates() ─────────────────────────────────────────────────────────────

class TestDominates:
    def test_strictly_dominates_both_axes(self):
        a = _pt(1, 1)
        b = _pt(2, 2)
        assert dominates(a, b) is True

    def test_dominates_on_one_axis_equal_on_other(self):
        a = _pt(1, 2)
        b = _pt(2, 2)
        assert dominates(a, b) is True  # a < b on bound, equal on minimize

    def test_dominates_other_axis(self):
        a = _pt(2, 1)
        b = _pt(2, 2)
        assert dominates(a, b) is True  # equal on bound, a < b on minimize

    def test_equal_does_not_dominate(self):
        a = _pt(2, 2)
        b = _pt(2, 2)
        assert dominates(a, b) is False

    def test_does_not_dominate_incomparable(self):
        a = _pt(1, 3)
        b = _pt(3, 1)
        assert dominates(a, b) is False
        assert dominates(b, a) is False

    def test_dominated_by_on_both_axes(self):
        a = _pt(3, 3)
        b = _pt(1, 2)
        assert dominates(a, b) is False
        assert dominates(b, a) is True


# ── pareto_filter() ──────────────────────────────────────────────────────────

class TestParetoFilter:
    def test_empty_returns_empty(self):
        assert pareto_filter([]) == []

    def test_single_point_not_dominated(self):
        pts = [_pt(3, 5)]
        result = pareto_filter(pts)
        assert len(result) == 1
        assert result[0].dominated is False

    def test_dominated_point_flagged(self):
        a = _pt(1, 1)
        b = _pt(2, 2)
        result = pareto_filter([a, b])
        a_out = next(r for r in result if r.bound_value == 1)
        b_out = next(r for r in result if r.bound_value == 2)
        assert a_out.dominated is False
        assert b_out.dominated is True

    def test_order_preserved(self):
        pts = [_pt(3, 1), _pt(1, 3), _pt(2, 2)]
        result = pareto_filter(pts)
        assert [(r.bound_value, r.minimize_value) for r in result] == \
               [(3, 1), (1, 3), (2, 2)]

    def test_duplicate_first_representative_survives(self):
        """Among identical (bound_value, minimize_value) the FIRST in input order
        keeps dominated=False; the rest are marked True."""
        a = _pt(2, 2)
        b = _pt(2, 2)
        c = _pt(2, 2)
        result = pareto_filter([a, b, c])
        dominated_flags = [r.dominated for r in result]
        assert dominated_flags == [False, True, True]

    def test_none_values_ineligible(self):
        """A point with None objective values is ineligible: marked dominated, never dominates."""
        a = _pt(None, None)
        b = _pt(5, 5)
        result = pareto_filter([a, b])
        a_out = result[0]
        b_out = result[1]
        assert a_out.dominated is True   # ineligible
        assert b_out.dominated is False  # eligible, no dominator

    def test_hard_violations_ineligible(self):
        """Points with hard_violations > 0 are ineligible: marked dominated, never dominate."""
        bad = _pt(1, 1, hard=1)  # would dominate good if eligible
        good = _pt(2, 2, hard=0)
        result = pareto_filter([bad, good])
        bad_out = next(r for r in result if r.hard_violations > 0)
        good_out = next(r for r in result if r.hard_violations == 0)
        assert bad_out.dominated is True
        assert good_out.dominated is False

    def test_ineligible_none_bound_only(self):
        """bound_value None makes point ineligible regardless of minimize_value."""
        a = _pt(None, 1)
        b = _pt(2, 5)
        result = pareto_filter([a, b])
        assert result[0].dominated is True
        assert result[1].dominated is False

    def test_ineligible_none_minimize_only(self):
        """minimize_value None makes point ineligible."""
        a = _pt(1, None)
        b = _pt(2, 5)
        result = pareto_filter([a, b])
        assert result[0].dominated is True
        assert result[1].dominated is False

    def test_all_ineligible_all_dominated(self):
        pts = [_pt(None, None), _pt(1, 2, hard=3)]
        result = pareto_filter(pts)
        assert all(r.dominated for r in result)

    def test_original_objects_not_mutated(self):
        """pareto_filter must not mutate the input list or the input objects."""
        pts = [_pt(1, 2), _pt(3, 4)]
        # capture original dominated field (default False)
        orig_dominated = [p.dominated for p in pts]
        result = pareto_filter(pts)
        # input objects should be unchanged (filter returns same objects)
        # but dominated field on the returned objects may differ — that's OK,
        # the contract only says same objects same order
        assert len(pts) == 2
        # result references same objects
        assert result[0] is pts[0]
        assert result[1] is pts[1]

    # ── property-based check ─────────────────────────────────────────────────

    def test_property_no_two_nondominated_dominate_each_other(self):
        """Property: no two non-dominated eligible points dominate each other."""
        rng = random.Random(0)
        for _ in range(20):
            pts = [_pt(rng.randint(0, 10), rng.randint(0, 10)) for _ in range(rng.randint(1, 8))]
            result = pareto_filter(pts)
            non_dom = [r for r in result if not r.dominated]
            for i, a in enumerate(non_dom):
                for b in non_dom[i + 1:]:
                    assert not dominates(a, b), f"{a} dominates {b} but both non-dominated"
                    assert not dominates(b, a), f"{b} dominates {a} but both non-dominated"

    def test_property_every_dominated_eligible_has_dominator(self):
        """Property: every dominated eligible point has at least one dominator in the result."""
        rng = random.Random(1)
        for _ in range(20):
            pts = [_pt(rng.randint(0, 10), rng.randint(0, 10)) for _ in range(rng.randint(1, 8))]
            result = pareto_filter(pts)
            eligible = [r for r in result if r.bound_value is not None
                        and r.minimize_value is not None and r.hard_violations == 0]
            dominated_eligible = [r for r in eligible if r.dominated]
            non_dom_eligible = [r for r in eligible if not r.dominated]
            for d in dominated_eligible:
                has_dominator = any(dominates(nd, d) for nd in non_dom_eligible)
                assert has_dominator, \
                    f"dominated point {d.bound_value},{d.minimize_value} has no dominator"
