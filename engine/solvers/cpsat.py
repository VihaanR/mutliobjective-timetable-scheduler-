"""CP-SAT solver -- Danish's approach.

Uses `ortools.sat.python.cp_model`. Shares the same candidate-pruning and hard-constraint set as
the MIP solver (via `timetable.solvers.candidates`) so the two are judged on equal footing, but
adds a genuinely richer objective CP-SAT can express natively that MIP's linear-sum formulation
cannot: per-(division, day) gap-minimization via reified "occupied at period p" booleans and
AddMaxEquality-based before/after indicators. This -- alongside AddNoOverlap-style reasoning being
unnecessary here since occupancy is already tracked per slot -- is the concrete mechanism behind
CP-SAT's quality edge in the team's own benchmark (best soft-cost, longest runtime).

The objective is built as four named categories (`rooms`, `labs`, `students`, `faculty`) instead
of one flat sum. `solve()` still minimizes their combined total (unchanged end-to-end behavior for
every existing caller).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from ortools.sat.python import cp_model

from engine.models import (
    Assignment, CourseCategory, ProblemInstance, Solution, SessionType, expand_requirements,
)
from engine.scoring import COMPACT_DAY_SPAN, MAX_CONTINUOUS_TEACHING_PERIODS  # keep in sync with scorer
from engine.solvers.base import SolverBase
from engine.solvers.candidates import (
    NO_ROOM, batch_group_members, build_candidates, slots_by_day, sync_group_members,
)

FACULTY_BALANCE_WEIGHT = 5  # calibrated penalty per hour of (max weekly load - min weekly load) spread

# ---- timetabling fork of OR-Tools (third_party/or-tools-fork/) --------------------------------
# Everything below degrades cleanly on a stock `pip install ortools`: the "@R="/"@G=" tags in
# variable names are simply ignored, CHOOSE_MIN_UNFIXED_IN_GROUP does not exist (so the MRV
# strategy is skipped), and "division_day_lns" is not a registered subsolver (so naming it in
# ignore_subsolvers is a no-op). That is deliberate -- the *same* model is emitted either way, so
# a stock-vs-fork benchmark compares solvers rather than two different models.
_FORK_MRV_STRATEGY = getattr(cp_model, "CHOOSE_MIN_UNFIXED_IN_GROUP", None)
# Both fork features ship in the same patch, so the presence of the new branching enum is also
# our signal that "division_day_lns" exists as a subsolver. That second check matters: OR-Tools
# *validates* subsolver names and fails the whole solve with MODEL_INVALID
# ("subsolver 'division_day_lns' is not valid") if an unknown one is named on a stock build.
HAS_TIMETABLE_FORK = _FORK_MRV_STRATEGY is not None
HAS_FORK_MRV = HAS_TIMETABLE_FORK  # back-compat alias

_FALSEY = {"0", "false", "no", "off", ""}


def _tags_enabled() -> bool:
    """Whether to emit "@R="/"@G=" structure tags in variable names.

    Defaults to ON only on a forked build. Tags are pure overhead on stock OR-Tools -- nothing
    reads them -- and that overhead is not free: an earlier, verbose tag format doubled the
    average variable name (23 -> 46 chars, ~98 KB extra across 4250 variables) and pushed
    tests/engine/test_breaks.py::test_cpsat_breaks_vary_across_days, which this module's own
    docstring already flags as borderline, past its time budget. Names are copied repeatedly
    through presolve, so their size is not incidental.

    Set TIMETABLE_FORK_TAGS=1 to force tags on for a stock-vs-fork benchmark, so both arms solve
    a byte-identical model.
    """
    raw = os.environ.get("TIMETABLE_FORK_TAGS")
    if raw is None:
        return HAS_TIMETABLE_FORK
    return raw.strip().lower() not in _FALSEY


def _mrv_mode() -> str:
    """How to order branching decisions. Read from $TIMETABLE_MRV at call time so a benchmark
    can sweep modes without editing code.

      auto    (default) dynamic on a forked build, off on a stock one
      dynamic CP-SAT re-ranks requirements by remaining candidates at every node (fork only)
      static  the best a stock build can do: one fixed order decided before search starts
      off     no decision strategy at all -- CP-SAT's own automatic search

    "static" is the Tier-0 ablation baseline: it shows how much of any gain came from merely
    ordering the variables versus from re-ranking them live, which is the fork's actual claim.
    """
    mode = os.environ.get("TIMETABLE_MRV", "auto").strip().lower()
    if mode not in {"auto", "dynamic", "static", "off"}:
        mode = "auto"
    if mode == "auto":
        mode = "dynamic" if HAS_FORK_MRV else "off"
    if mode == "dynamic" and not HAS_FORK_MRV:
        mode = "off"  # asked for a fork-only feature on a stock build
    return mode


def _add_mrv_decision_strategy(model, requirements, candidates, x, *, dynamic: bool) -> int:
    """Branch on the requirement that has the fewest placements still open.

    Requirements are emitted tightest-first. On a forked build the solver re-ranks them live;
    on a stock build the order is frozen at model-build time, which is the whole limitation the
    fork removes. Returns how many variables were placed under the strategy.
    """
    ordered = sorted((r for r in requirements if candidates.get(r.id)),
                     key=lambda r: len(candidates[r.id]))
    vars_in_order = [x[(req.id, start_id, room_id)]
                     for req in ordered
                     for (start_id, _occ, _day, room_id) in candidates[req.id]]
    if not vars_in_order:
        return 0
    model.AddDecisionStrategy(
        vars_in_order,
        _FORK_MRV_STRATEGY if dynamic else cp_model.CHOOSE_FIRST,
        cp_model.SELECT_MAX_VALUE,  # try "yes, place it here" before "no"
    )
    return len(vars_in_order)

# Objective categories retained for solver scoring and reporting.
OBJECTIVE_CATEGORIES = ("rooms", "labs", "students", "faculty", "resource")

# solve()'s default combined objective -- deliberately excludes "faculty". Unlike the other three
# (pre-existing, just regrouped), "faculty" is a brand-new term (Sec 15.3), and folding it into
# every caller's default objective measurably slowed convergence on the reference dataset (a
# borderline-budget test, tests/test_breaks.py::test_cpsat_breaks_vary_across_days at 25s, started
# timing out with zero assignments once it was included). solve()'s default stays byte-identical
# to pre-Sec-15.3 behavior for every existing caller (pipeline, benchmarks, webapp); "faculty" is
# kept separate from the default objective to preserve its established solve-time behavior.
DEFAULT_SOLVE_CATEGORIES = ("rooms", "labs", "students")


@dataclass
class _BuiltModel:
    model: cp_model.CpModel
    x: dict[tuple[str, int, str], cp_model.IntVar]
    requirements: list
    candidates: dict
    objective_categories: dict[str, list]


def _build_model(problem: ProblemInstance) -> _BuiltModel:
    """Build the shared CP-SAT model: every hard constraint, plus the objective terms grouped
    into four named categories. Does not set an objective or solve -- callers do that (`solve()`
    minimizes the combined total; `solve_pareto_point()` bounds some categories and optimizes
    one)."""
    model = cp_model.CpModel()

    requirements = expand_requirements(problem)
    candidates = build_candidates(problem, requirements)
    days = slots_by_day(problem)
    faculty_by_id = problem.faculty_by_id()
    divisions_by_id = problem.division_by_id()
    courses_by_code = problem.course_by_code()
    rooms_by_id = problem.room_by_id()
    batch_groups = batch_group_members(requirements)
    sync_groups = sync_group_members(requirements)

    x: dict[tuple[str, int, str], cp_model.IntVar] = {}
    # Compact integer tags, not the full ids: the C++ side groups by the tag's *value*, so any
    # unique token works, and short ones keep the names (and presolve's copying of them) cheap.
    tags_on = _tags_enabled()
    req_tag: dict[str, int] = {}
    group_tag: dict[tuple[str, int], int] = {}
    for req in requirements:
        for (start_id, _occ, day, room_id) in candidates[req.id]:
            # The name optionally carries domain-structure tags our OR-Tools fork reads back out
            # (third_party/or-tools-fork/): "@R=" groups all candidate placements of one
            # requirement (consumed by CHOOSE_MIN_UNFIXED_IN_GROUP branching), "@G=" groups a
            # whole division-day (consumed by the division_day_lns neighborhood). A tag value
            # runs to the next "@". Stock OR-Tools ignores variable names entirely.
            name = f"x_{req.id}_{start_id}_{room_id}"
            if tags_on:
                r = req_tag.setdefault(req.id, len(req_tag))
                g = group_tag.setdefault((req.division_id, day), len(group_tag))
                name = f"{name}@R={r}@G={g}"
            x[(req.id, start_id, room_id)] = model.NewBoolVar(name)

    for req in requirements:
        cands = candidates[req.id]
        if not cands:
            continue
        model.AddExactlyOne(x[(req.id, s, r)] for (s, _, _, r) in cands)

    mrv_mode = _mrv_mode()
    if mrv_mode in ("dynamic", "static"):
        _add_mrv_decision_strategy(model, requirements, candidates, x,
                                   dynamic=(mrv_mode == "dynamic"))

    def _accumulate(bucket: dict, key, var):
        bucket.setdefault(key, []).append(var)

    room_terms: dict = {}
    faculty_terms: dict = {}
    whole_div_terms: dict = {}
    batch_terms: dict = {}
    for req in requirements:
        division = divisions_by_id.get(req.division_id)
        batches = division.batch_pair() if division else ()
        for (start_id, occ_ids, day, room_id) in candidates[req.id]:
            var = x[(req.id, start_id, room_id)]
            if room_id != NO_ROOM:
                for sid in occ_ids:
                    _accumulate(room_terms, (room_id, sid), var)
            if req.faculty_id:
                for sid in occ_ids:
                    _accumulate(faculty_terms, (req.faculty_id, sid), var)
            for sid in occ_ids:
                if req.batch_id is None:
                    _accumulate(whole_div_terms, (req.division_id, sid), var)
                    for b in batches:
                        _accumulate(batch_terms, (req.division_id, b, sid), var)
                else:
                    _accumulate(batch_terms, (req.division_id, req.batch_id, sid), var)

    # teaching-only occupancy (excludes breaks) per (division_id, slot_id), for the
    # continuous-teaching limit (constraint 21). Batch-pair labs are counted once via the
    # first-seen half (the batch-pair same-slot equality constraint guarantees the other half
    # is 1 at exactly the same slot).
    division_teaching_terms: dict = {}
    seen_teaching_groups: set[str] = set()
    for req in requirements:
        if req.is_break:
            continue
        if req.batch_group_id:
            if req.batch_group_id in seen_teaching_groups:
                continue
            seen_teaching_groups.add(req.batch_group_id)
        for (start_id, occ_ids, day, room_id) in candidates[req.id]:
            var = x[(req.id, start_id, room_id)]
            for sid in occ_ids:
                _accumulate(division_teaching_terms, (req.division_id, sid), var)

    for vars_ in room_terms.values():
        model.AddAtMostOne(vars_)
    for vars_ in faculty_terms.values():
        model.AddAtMostOne(vars_)
    for vars_ in whole_div_terms.values():
        model.AddAtMostOne(vars_)
    for vars_ in batch_terms.values():
        model.AddAtMostOne(vars_)

    # subject at most once per day per division (batch-pair counted once via representative)
    seen_groups: set[str] = set()
    day_subject_terms: dict = {}
    for req in requirements:
        if req.is_break:
            continue
        if req.batch_group_id:
            if req.batch_group_id in seen_groups:
                continue
            seen_groups.add(req.batch_group_id)
        for (start_id, _occ, day, room_id) in candidates[req.id]:
            _accumulate(day_subject_terms, (req.division_id, req.course_code, day), x[(req.id, start_id, room_id)])
    for vars_ in day_subject_terms.values():
        model.AddAtMostOne(vars_)

    # batch-pair: same slot (different room already guaranteed by candidate pruning)
    for group_id, members in batch_groups.items():
        anchor, others = members[0], members[1:]
        for other in others:
            slot_ids = {s for (s, _, _, _) in candidates[anchor.id]} | {s for (s, _, _, _) in candidates[other.id]}
            for slot_id in slot_ids:
                anchor_terms = [x[(anchor.id, s, r)] for (s, _, _, r) in candidates[anchor.id] if s == slot_id]
                other_terms = [x[(other.id, s, r)] for (s, _, _, r) in candidates[other.id] if s == slot_id]
                model.Add(sum(anchor_terms) == sum(other_terms))

    # cross-division sync (open elective): same slot across all linked divisions
    for group_id, members in sync_groups.items():
        anchor, others = members[0], members[1:]
        for other in others:
            slot_ids = {s for (s, _, _, _) in candidates[anchor.id]} | {s for (s, _, _, _) in candidates[other.id]}
            for slot_id in slot_ids:
                anchor_terms = [x[(anchor.id, s, r)] for (s, _, _, r) in candidates[anchor.id] if s == slot_id]
                other_terms = [x[(other.id, s, r)] for (s, _, _, r) in candidates[other.id] if s == slot_id]
                model.Add(sum(anchor_terms) == sum(other_terms))

    # faculty daily (<=6h, matching the softened NEP institutional-norms reading) / weekly load cap
    faculty_day_terms: dict = {}
    faculty_week_terms: dict = {}
    for req in requirements:
        if not req.faculty_id:
            continue
        for (start_id, _occ, day, room_id) in candidates[req.id]:
            var = x[(req.id, start_id, room_id)]
            faculty_day_terms.setdefault((req.faculty_id, day), []).append((var, req.duration_slots))
            faculty_week_terms.setdefault(req.faculty_id, []).append((var, req.duration_slots))
    for terms in faculty_day_terms.values():
        model.Add(sum(v * d for v, d in terms) <= 6)
    for fid, terms in faculty_week_terms.items():
        fac = faculty_by_id.get(fid)
        cap = fac.max_load_hours_per_week if fac else 20
        # Faculty allocations are fixed inputs -- ensure the weekly cap is at least the total allocated hours
        total_allocated = sum(d for (_, d) in terms)
        cap = max(cap, total_allocated)
        model.Add(sum(v * d for v, d in terms) <= cap)

    # no more than max_consecutive_sessions in a row for any faculty
    for fid in {r.faculty_id for r in requirements if r.faculty_id}:
        fac = faculty_by_id.get(fid)
        cap = fac.max_consecutive_sessions if fac else 2
        for day, day_slots in days.items():
            for start in range(len(day_slots) - cap):
                window = day_slots[start:start + cap + 1]
                window_terms = []
                for ts in window:
                    window_terms.extend(faculty_terms.get((fid, ts.id), []))
                if window_terms:
                    model.Add(sum(window_terms) <= cap)

    # division daily load in [6, 8] hours (batch pairs counted once)
    seen_groups2: set[str] = set()
    div_day_terms: dict = {}
    for req in requirements:
        if req.is_break:
            continue
        if req.batch_group_id:
            if req.batch_group_id in seen_groups2:
                continue
            seen_groups2.add(req.batch_group_id)
        for (start_id, _occ, day, room_id) in candidates[req.id]:
            var = x[(req.id, start_id, room_id)]
            div_day_terms.setdefault((req.division_id, day), []).append((var, req.duration_slots))
    # a relaxed (disrupted) day is exempt from the day-shaped hard rules -- see scoring.py
    relaxed = problem.relaxed_days
    for (division_id, day), terms in div_day_terms.items():
        if day in relaxed:
            continue
        expr = sum(v * d for v, d in terms)
        model.Add(expr >= 6)
        model.Add(expr <= 8)

    # no more than MAX_CONTINUOUS_TEACHING_PERIODS (4 hours) of contiguous non-break teaching
    # periods per division per day (hard constraint 21): for every window of (cap+1) consecutive
    # periods in a day, at most `cap` may be busy with teaching sessions
    for division_id in {r.division_id for r in requirements}:
        for day, day_slots in days.items():
            if day in relaxed:
                continue
            cap = MAX_CONTINUOUS_TEACHING_PERIODS
            for start in range(len(day_slots) - cap):
                window = day_slots[start:start + cap + 1]
                window_terms = []
                for ts in window:
                    window_terms.extend(division_teaching_terms.get((division_id, ts.id), []))
                if window_terms:
                    model.Add(sum(window_terms) <= cap)

    # all occupancy (teaching sessions + breaks) per (division_id, slot_id)
    # Batch-pair labs are counted once via the first-seen half
    division_all_terms: dict = {}
    seen_all_groups: set[str] = set()
    for req in requirements:
        if req.batch_group_id:
            if req.batch_group_id in seen_all_groups:
                continue
            seen_all_groups.add(req.batch_group_id)
        for (start_id, occ_ids, day, room_id) in candidates[req.id]:
            var = x[(req.id, start_id, room_id)]
            for sid in occ_ids:
                _accumulate(division_all_terms, (req.division_id, sid), var)

    # each division's day must form a strictly CONTIGUOUS block (ZERO IDLE GAPS)
    for division in problem.divisions:
        for day, day_slots in days.items():
            if day in relaxed or not day_slots:
                continue
            div_occ_vars = []
            for ts in day_slots:
                terms = division_all_terms.get((division.id, ts.id), [])
                if terms:
                    occ = model.NewBoolVar(f"div_occ_{division.id}_{day}_{ts.period}")
                    model.Add(occ == sum(terms))
                    div_occ_vars.append(occ)
                else:
                    occ = model.NewBoolVar(f"div_occ_{division.id}_{day}_{ts.period}")
                    model.Add(occ == 0)
                    div_occ_vars.append(occ)

            if div_occ_vars:
                # Must start at the first period (08:00)
                model.Add(div_occ_vars[0] == 1)
                # Contiguous: no gap / hole allowed anywhere in the student's day
                for p in range(len(div_occ_vars) - 1):
                    model.Add(div_occ_vars[p + 1] <= div_occ_vars[p])

    # no all-theory day: each day must have >=1 practical/skill session, for divisions that offer enough sessions
    practical_or_skill_terms: dict = {}
    div_practical_count: dict[str, int] = {}
    seen_prac_groups: set[str] = set()
    for req in requirements:
        if req.is_break:
            continue
        course = courses_by_code.get(req.course_code)
        if not course:
            continue
        if req.session_type == SessionType.PRACTICAL or course.category == CourseCategory.SKILL:
            if req.batch_group_id:
                if req.batch_group_id not in seen_prac_groups:
                    seen_prac_groups.add(req.batch_group_id)
                    div_practical_count[req.division_id] = div_practical_count.get(req.division_id, 0) + 1
            else:
                div_practical_count[req.division_id] = div_practical_count.get(req.division_id, 0) + 1
            for (start_id, _occ, day, room_id) in candidates[req.id]:
                practical_or_skill_terms.setdefault((req.division_id, day), []).append(
                    x[(req.id, start_id, room_id)])
    unrelaxed_days_count = len([d for d in days if d not in relaxed])
    for division in problem.divisions:
        if div_practical_count.get(division.id, 0) < unrelaxed_days_count:
            continue
        for day in days:
            if day in relaxed:
                continue
            terms = practical_or_skill_terms.get((division.id, day), [])
            if terms:
                model.Add(sum(terms) >= 1)

    # ---- objective, grouped into four named categories ----
    # "rooms": room-capacity waste. "labs": late-lab-slot placement penalty. "students": break
    # placement + day-span + gap-minimization (division/student schedule quality). "faculty":
    # weekly workload balance (new -- previously faculty load was only a hard cap, never
    # minimized). See design.md Sec 15.3 / research/pareto_sweep.py for why these four exist as
    # separately addressable expressions rather than one flat sum.
    room_obj_terms: list = []
    lab_obj_terms: list = []
    student_obj_terms: list = []
    for req in requirements:
        division = divisions_by_id.get(req.division_id)
        occupants = (division.student_count // 2) if (req.batch_id and division) else (division.student_count if division else 0)
        for (start_id, _occ, day, room_id) in candidates[req.id]:
            var = x[(req.id, start_id, room_id)]
            if room_id != NO_ROOM:
                room = rooms_by_id[room_id]
                waste = max(0, room.capacity - occupants)
                if waste:
                    room_obj_terms.append(int(waste * 1) * var)  # calibrated integral waste weight
            if req.session_type == SessionType.PRACTICAL:
                day_slots = days[day]
                ts = next(t for t in day_slots if t.id == start_id)
                if len(day_slots) >= 2 and ts.period in {day_slots[-1].period, day_slots[-2].period}:
                    lab_obj_terms.append(200 * var)
            if req.is_break:
                # vary the target across days (rotate through the legal mid-day band by day
                # index) so breaks spread over the week instead of all landing on one period --
                # mirrors scoring.py's break_not_midmorning term
                day_slots = days[day]
                ts = next(t for t in day_slots if t.id == start_id)
                band = [t.period for t in day_slots[2:-1]]
                if band:
                    target_period = band[day % len(band)]
                else:
                    target_period = day_slots[0].period + max(1, round(len(day_slots) * 0.35))
                dist = abs(ts.period - target_period)
                if dist:
                    student_obj_terms.append(15 * dist * var)

    # native gap-minimization / compact campus stay: penalize late periods beyond compact 6-hour span.
    # Scaled with weight 1 to keep student_score in the well-calibrated 200-500 range across 3 years.
    GAP_WEIGHT = 1
    for division in problem.divisions:
        for day, day_slots in days.items():
            if day in relaxed or not day_slots:
                continue
            first_period = day_slots[0].period
            for req in requirements:
                if req.division_id != division.id:
                    continue
                for (start_id, occ_ids, d, room_id) in candidates[req.id]:
                    if d != day:
                        continue
                    var = x[(req.id, start_id, room_id)]
                    end_p = max(ts.period for sid in occ_ids for ts in day_slots if ts.id == sid)
                    span_from_first = end_p - first_period
                    excess_span = max(0, span_from_first - 6)
                    if excess_span > 0:
                        student_obj_terms.append(int(GAP_WEIGHT * excess_span) * var)

    # "faculty": per-faculty day-to-day load balance (max daily load - min daily load across the
    # week, summed over faculty). NOTE: faculty->course assignment is fixed input, not a solver
    # decision (CLAUDE.md Sec 3) -- so a given faculty's *weekly total* hours cannot be changed by
    # scheduling at all, and a cross-faculty weekly-total balance term would be a constant, not a
    # real objective (verified empirically: an earlier version of this term used weekly totals and
    # its achieved value never moved regardless of what was optimized). What the solver DOES
    # control is which *day* each of a faculty's sessions lands on, so a per-faculty day-load
    # range is the genuinely schedule-dependent fairness proxy available here -- distinct from,
    # but in the same spirit as, scoring.py's `teacher_workload_spread` (variance of daily hours).
    faculty_obj_terms: list = []
    for fid in faculty_week_terms:
        day_cap = 6  # matches the <=6h/day hard constraint above
        day_load_vars = []
        for day in days:
            terms = faculty_day_terms.get((fid, day), [])
            load_var = model.NewIntVar(0, day_cap, f"day_load_{fid}_{day}")
            if terms:
                model.Add(load_var == sum(v * d for v, d in terms))
            else:
                model.Add(load_var == 0)
            day_load_vars.append(load_var)
        if len(day_load_vars) < 2:
            continue
        max_day = model.NewIntVar(0, day_cap, f"max_day_load_{fid}")
        min_day = model.NewIntVar(0, day_cap, f"min_day_load_{fid}")
        model.AddMaxEquality(max_day, day_load_vars)
        model.AddMinEquality(min_day, day_load_vars)
        day_range = model.NewIntVar(0, day_cap, f"day_load_range_{fid}")
        model.Add(day_range == max_day - min_day)
        faculty_obj_terms.append(FACULTY_BALANCE_WEIGHT * day_range)

    objective_categories = {
        "rooms": room_obj_terms,
        "labs": lab_obj_terms,
        "students": student_obj_terms,
        "faculty": faculty_obj_terms,
        "resource": room_obj_terms + lab_obj_terms,
    }

    return _BuiltModel(
        model=model, x=x, requirements=requirements, candidates=candidates,
        objective_categories=objective_categories,
    )


class CPSATSolver(SolverBase):
    name = "cpsat"

    def solve(self, problem: ProblemInstance, time_limit_s: float = 300,
               warm_start: Solution | None = None,
               extra_solver_params: dict | None = None,
               absent_classes: dict[str, list[str]] | None = None) -> Solution:
        start_time = time.time()
        built = _build_model(problem)
        all_terms = [t for cat in DEFAULT_SOLVE_CATEGORIES for t in built.objective_categories[cat]]
        if all_terms:
            built.model.Minimize(sum(all_terms))
        solution, _category_values = _solve_and_decode(
            built, problem, time_limit_s, warm_start, extra_solver_params, start_time)
        return solution



class _IntermediateCallback(cp_model.CpSolverSolutionCallback):
    def __init__(self, built: _BuiltModel, problem: ProblemInstance, callback_fn=None):
        super().__init__()
        self.built = built
        self.problem = problem
        self.callback_fn = callback_fn
        self.solution_count = 0

    def on_solution_callback(self):
        self.solution_count += 1
        if not self.callback_fn:
            return
        try:
            assignments: list[Assignment] = []
            for req in self.built.requirements:
                for (start_id, _occ, _day, room_id) in self.built.candidates[req.id]:
                    if self.Value(self.built.x[(req.id, start_id, room_id)]) == 1:
                        assignments.append(Assignment(session_id=req.id, time_slot_id=start_id, room_id=room_id))
                        break
            sol = Solution(
                assignments=assignments,
                solver_name="cpsat",
                wall_clock_seconds=self.WallTime(),
                objective_value=self.ObjectiveValue(),
                status="FEASIBLE",
            )
            cat_vals = {}
            for category, terms in self.built.objective_categories.items():
                if not terms:
                    cat_vals[category] = 0
                else:
                    try:
                        cat_vals[category] = int(sum(self.Value(t) for t in terms))
                    except Exception:
                        cat_vals[category] = 0
            self.callback_fn(sol, cat_vals, self.solution_count, self.WallTime())
        except Exception:
            pass


def _solve_and_decode(built: _BuiltModel, problem: ProblemInstance, time_limit_s: float,
                       warm_start: Solution | None, extra_solver_params: dict | None,
                       start_time: float, solution_callback=None) -> tuple[Solution, dict[str, int | None]]:
    model, x, requirements, candidates = built.model, built.x, built.requirements, built.candidates

    if warm_start is not None:
        assigned = warm_start.assignment_by_session()
        for req in requirements:
            a = assigned.get(req.id)
            if a is None:
                continue
            key = (req.id, a.time_slot_id, a.room_id)
            if key in x:
                model.AddHint(x[key], 1)  # this ortools build's AddHint takes one (var, value) at a time

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8

    # CP-SAT discards variable names when it copies the user model into its presolve context
    # (ModelCopy::ImportVariablesAndMaybeIgnoreNames), because SatParameters.ignore_names
    # defaults to TRUE. Both fork features read their structure out of those names, so without
    # this line the tags are stripped before the solver ever sees them: division_day_lns finds
    # zero groups, never registers, and the whole fork silently behaves exactly like stock.
    #
    # Verified by dumping the models: the user model carried 96 "@G=" names and the presolved
    # model carried 0, and flipping this parameter takes the subsolver list from 9 entries to 10
    # with "division_day_lns" among them.
    #
    # Only set when tags are on -- keeping names costs memory in CP-SAT's internal copy, and a
    # run with tags disabled has nothing to gain from it.
    if _tags_enabled():
        solver.parameters.ignore_names = False

    # The forked build registers "division_day_lns" and runs it by default (SubsolverNameFilter
    # keeps any name that is not explicitly filtered). Setting TIMETABLE_DIVISION_DAY_LNS=0 is
    # therefore the *baseline* arm of the A/B: same binary, neighborhood switched off.
    #
    # Gated on HAS_TIMETABLE_FORK because naming an unknown subsolver is NOT harmless: on a stock
    # build OR-Tools rejects the parameters outright with MODEL_INVALID, which this module used
    # to report as "TIMEOUT" with zero assignments -- i.e. a config error wearing the costume of
    # a hard scheduling problem. Verified, not theoretical.
    if HAS_TIMETABLE_FORK and \
            os.environ.get("TIMETABLE_DIVISION_DAY_LNS", "1").strip().lower() in _FALSEY:
        solver.parameters.ignore_subsolvers.append("division_day_lns")
    if extra_solver_params:
        for key, value in extra_solver_params.items():
            setattr(solver.parameters, key, value)
    cb = _IntermediateCallback(built, problem, solution_callback) if solution_callback else None
    status = solver.Solve(model, cb) if cb else solver.Solve(model)
    elapsed = time.time() - start_time

    status_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        # Distinct from TIMEOUT on purpose: MODEL_INVALID means the model or the solver
        # parameters were rejected outright, which is a bug to fix, not a budget to raise.
        # Folding it into "TIMEOUT" hides configuration errors as slow solves.
        cp_model.MODEL_INVALID: "MODEL_INVALID",
    }
    status_name = status_map.get(status, "TIMEOUT")

    assignments: list[Assignment] = []
    category_values: dict[str, int | None] = {}
    if status_name in ("OPTIMAL", "FEASIBLE"):
        for req in requirements:
            for (start_id, _occ, _day, room_id) in candidates[req.id]:
                if solver.Value(x[(req.id, start_id, room_id)]) == 1:
                    assignments.append(Assignment(session_id=req.id, time_slot_id=start_id, room_id=room_id))
                    break
        for category, terms in built.objective_categories.items():
            category_values[category] = int(solver.Value(sum(terms))) if terms else 0
    else:
        category_values = {category: None for category in built.objective_categories}

    solution = Solution(
        assignments=assignments, solver_name="cpsat", wall_clock_seconds=elapsed,
        objective_value=solver.ObjectiveValue() if status_name in ("OPTIMAL", "FEASIBLE") else None,
        status=status_name,
    )
    return solution, category_values
