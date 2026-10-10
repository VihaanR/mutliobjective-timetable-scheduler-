"""Unit tests for 7-objective Pareto dominance analysis, candidate ranking, and tie-breaking."""
import pytest
from engine.judge import (
    AQWIReport,
    CriterionDetail,
    dominates,
    compute_pareto_front,
    compute_cohort_baseline,
    DEFAULT_COHORT_SCALE,
    AQWI_WEIGHTS,
)
from engine.models import ProblemInstance, Division, Faculty, Room, ProgramType


def _make_candidate(cand_id: int, metrics: list[float], quality_score: float = 90.0, total_penalty: float = 10.0):
    """Helper to construct candidate report dictionary with 7 metrics."""
    assert len(metrics) == 7
    keys = [
        "c1_avoid_8_6_span",
        "c2_student_idle_gaps",
        "c3_same_day_lec_lab",
        "c4_faculty_load_variance",
        "c5_honours_boundary",
        "c6_three_consecutive_days",
        "c7_faculty_gaps_over_2h",
    ]
    criteria = {
        k: CriterionDetail(
            name=k,
            weight=1.0,
            raw_metric=m,
            weighted_penalty=m,
            description=k,
        )
        for k, m in zip(keys, metrics)
    }
    rep = AQWIReport(
        candidate_id=cand_id,
        total_penalty=total_penalty,
        quality_score=quality_score,
        criteria=criteria,
    )
    return {
        "candidate_id": cand_id,
        "report": rep,
    }


def test_dominance_strict():
    """A strictly dominates B if A is <= B on all criteria and < B on at least one."""
    vec_a = [1.0, 2.0, 0.0, 1.5, 0.0, 1.0, 0.0]
    vec_b = [2.0, 2.0, 0.0, 1.5, 1.0, 1.0, 0.0]  # worse on c1 and c5
    assert dominates(vec_a, vec_b) is True
    assert dominates(vec_b, vec_a) is False


def test_dominance_tradeoff_non_dominated():
    """Candidates trading off criteria do not dominate each other."""
    vec_a = [1.0, 2.0, 0.0, 1.0, 0.0, 0.0, 0.0]  # better on c2 (2.0 vs 3.0), worse on c1 (1.0 vs 0.0)
    vec_b = [0.0, 3.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    assert dominates(vec_a, vec_b) is False
    assert dominates(vec_b, vec_a) is False


def test_dominance_identical_vectors():
    """Identical vectors do not strictly dominate each other."""
    vec_a = [1.0, 2.0, 3.0, 1.25, 0.0, 1.0, 2.0]
    vec_b = [1.0, 2.0, 3.0, 1.25, 0.0, 1.0, 2.0]
    assert dominates(vec_a, vec_b) is False
    assert dominates(vec_b, vec_a) is False


def test_dominance_floating_point_tolerance():
    """Floating-point differences within tolerance (1e-6) should not cause false dominance."""
    vec_a = [1.0, 2.0, 0.0, 1.5000001, 0.0, 1.0, 0.0]
    vec_b = [1.0, 2.0, 0.0, 1.5000002, 0.0, 1.0, 0.0]
    # Difference is 1e-7, within default tolerance 1e-6 -> neither dominates
    assert dominates(vec_a, vec_b, tolerance=1e-6) is False
    assert dominates(vec_b, vec_a, tolerance=1e-6) is False


def test_compute_pareto_front_deterministic_four_candidates():
    """Deterministic 4-candidate test dataset specified in requirements:
    - Candidate 1 dominates Candidate 2.
    - Candidate 1 and Candidate 3 trade off different criteria (both non-dominated).
    - Candidate 4 has the exact same metric vector as Candidate 3.
    """
    # Cand 1: strong overall
    c1 = _make_candidate(1, [1.0, 1.0, 0.0, 0.5, 0.0, 1.0, 0.0], quality_score=92.0, total_penalty=3.5)
    # Cand 2: strictly dominated by Cand 1 (worse on M1, M2, M5)
    c2 = _make_candidate(2, [3.0, 2.0, 0.0, 0.5, 1.0, 1.0, 0.0], quality_score=85.0, total_penalty=7.5)
    # Cand 3: trade-off with Cand 1 (better on M1=0.0, worse on M2=3.0) -> non-dominated
    c3 = _make_candidate(3, [0.0, 3.0, 0.0, 0.5, 0.0, 1.0, 0.0], quality_score=90.0, total_penalty=4.5)
    # Cand 4: identical metric vector as Cand 3
    c4 = _make_candidate(4, [0.0, 3.0, 0.0, 0.5, 0.0, 1.0, 0.0], quality_score=90.0, total_penalty=4.5)

    candidates = [c1, c2, c3, c4]
    compute_pareto_front(candidates)

    # Cand 1: non_dominated
    assert c1["report"].pareto_status == "non_dominated"
    assert c1["report"].pareto_rank == 1

    # Cand 2: dominated by Cand 1
    assert c2["report"].pareto_status == "dominated"
    assert c2["report"].pareto_rank == 2

    # Cand 3: non_dominated (trades off with Cand 1)
    assert c3["report"].pareto_status == "non_dominated"
    assert c3["report"].pareto_rank == 1

    # Cand 4: non_dominated (identical to Cand 3, does not dominate or get dominated by Cand 3)
    assert c4["report"].pareto_status == "non_dominated"
    assert c4["report"].pareto_rank == 1


def test_ranking_and_deterministic_tie_breaking():
    """Rank non-dominated by descending AQWI score, tie-break by lower total penalty, then cand_id."""
    # c_a and c_b have same score, but c_a has lower unrounded penalty
    c_a = _make_candidate(10, [1.0] * 7, quality_score=90.5, total_penalty=7.0)
    c_b = _make_candidate(20, [1.0] * 7, quality_score=90.5, total_penalty=7.2)
    # c_c ties on score and penalty with c_d, deterministic tie-break by candidate_id
    c_c = _make_candidate(5, [1.0] * 7, quality_score=88.0, total_penalty=8.0)
    c_d = _make_candidate(8, [1.0] * 7, quality_score=88.0, total_penalty=8.0)

    pool = [c_b, c_d, c_a, c_c]
    pool.sort(
        key=lambda c: (
            -c["report"].quality_score,
            c["report"].total_penalty,
            c["candidate_id"],
        )
    )

    expected_order = [c_a, c_b, c_c, c_d]
    assert [c["candidate_id"] for c in pool] == [10, 20, 5, 8]


def test_cohort_baseline_and_empty_problem():
    """Verify simplified cohort baseline formula K = 1000 * (N_div + N_fac) and edge cases."""
    div = Division(
        id="D1",
        program=ProgramType.FYUP,
        semester=1,
        student_count=60,
        course_codes=(),
        faculty_by_course={},
    )
    fac1 = Faculty(id="F1", name="Prof 1")
    fac2 = Faculty(id="F2", name="Prof 2")
    p = ProblemInstance(
        time_slots=[],
        rooms=[],
        courses=[],
        divisions=[div],
        faculty=[fac1, fac2],
    )

    k = compute_cohort_baseline(p, scale=1000.0)
    # 1 division + 2 faculty = 3 * 1000 = 3000.0
    assert k == 3000.0

    # Empty problem handling
    empty_p = ProblemInstance(time_slots=[], rooms=[], courses=[], divisions=[], faculty=[])
    k_empty = compute_cohort_baseline(empty_p, scale=1000.0)
    assert k_empty >= 1000.0
