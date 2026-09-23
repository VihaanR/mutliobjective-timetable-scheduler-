"""Pure dominance filter for AUGMECON2-style Pareto frontiers.

No solver imports. Minimisation on both objectives. O(n²) — acceptable because
n ≤ ~10 per pair in practice (spec §3.1).

Design contract
---------------
``pareto_filter`` flags points in-place and returns the same list (same order,
same objects) so the API remains an honest record of every solve. Points are
*flagged*, never removed.

Eligibility rules (§3.1):
- A point is **eligible** iff both objective values are not ``None`` **and**
  ``hard_violations == 0``.  Ineligible points are marked ``dominated = True``
  and never dominate anything.
- Among eligible points with identical ``(bound_value, minimize_value)``, the
  first in input order is the representative; the rest are marked dominated.
- A representative is ``dominated = True`` iff some other eligible point
  dominates it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.pareto_sweep import FrontierPoint


def dominates(a: "FrontierPoint", b: "FrontierPoint") -> bool:
    """Return True iff *a* weakly dominates *b* on both objectives and is strictly
    better on at least one (minimisation on both axes).

    Callers are responsible for passing only eligible points; this function
    performs no eligibility check itself.
    """
    # a dominates b: a ≤ b on both, and a < b on at least one
    return (
        a.bound_value <= b.bound_value
        and a.minimize_value <= b.minimize_value
        and (a.bound_value < b.bound_value or a.minimize_value < b.minimize_value)
    )


def pareto_filter(points: list["FrontierPoint"]) -> list["FrontierPoint"]:
    """Flag dominated / ineligible points in *points* and return the same list.

    Each ``FrontierPoint.dominated`` field is set according to the rules in
    §3.1.  The input list and its order are never changed; only the
    ``dominated`` attribute on each point is written.

    Args:
        points: A list of ``FrontierPoint`` objects (any current ``dominated``
                value is overwritten).

    Returns:
        The same *points* list (same objects, same order) with ``dominated``
        freshly computed for every element.
    """
    # Step 1: reset all to non-dominated; identify eligible points
    eligible_indices: list[int] = []
    for i, p in enumerate(points):
        p.dominated = False  # start clean; may be overwritten below
        if p.bound_value is None or p.minimize_value is None or p.hard_violations != 0:
            p.dominated = True  # ineligible
        else:
            eligible_indices.append(i)

    # Step 2: collapse duplicates — among eligible points with the same
    # (bound_value, minimize_value), keep only the first as the representative.
    seen: dict[tuple, int] = {}  # (bound_value, minimize_value) → first eligible index
    representative_indices: list[int] = []
    for i in eligible_indices:
        key = (points[i].bound_value, points[i].minimize_value)
        if key in seen:
            points[i].dominated = True  # duplicate; not the representative
        else:
            seen[key] = i
            representative_indices.append(i)

    # Step 3: among representatives, flag those dominated by another representative.
    # O(n²) with n = len(representative_indices) ≤ ~10, so this is fine.
    for i in representative_indices:
        for j in representative_indices:
            if i == j:
                continue
            if dominates(points[j], points[i]):
                points[i].dominated = True
                break  # no need to keep checking once dominated

    return points
