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
every existing caller), but `solve_pareto_point()` exposes the categories individually so a
research-track epsilon-constraint sweep (design.md Sec 15.3) can bound some of them and optimize
another, reusing this exact model -- see `research/pareto_sweep.py`.
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

FACULTY_BALANCE_WEIGHT = 30  # penalty per hour of (max weekly load - min weekly load) spread

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

# The four objective categories a Pareto sweep can bound/optimize independently.
OBJECTIVE_CATEGORIES = ("rooms", "labs", "students", "faculty")

# solve()'s default combined objective -- deliberately excludes "faculty". Unlike the other three
# (pre-existing, just regrouped), "faculty" is a brand-new term (Sec 15.3), and folding it into
# every caller's default objective measurably slowed convergence on the reference dataset (a
# borderline-budget test, tests/test_breaks.py::test_cpsat_breaks_vary_across_days at 25s, started
# timing out with zero assignments once it was included). solve()'s default stays byte-identical
# to pre-Sec-15.3 behavior for every existing caller (pipeline, benchmarks, webapp); "faculty" is
# available only via solve_pareto_point(), which the research-track sweep budgets time for.
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

    # each division's day must START at the first period (08:00) -- no empty leading slot
    first_slot_id_by_day = {day: ds[0].id for day, ds in days.items() if ds}
    first_slot_terms: dict = {}
    for req in requirements:
        if req.is_break:
            continue
        for (start_id, _occ, day, room_id) in candidates[req.id]:
            if start_id == first_slot_id_by_day.get(day):
                first_slot_terms.setdefault((req.division_id, day), []).append(x[(req.id, start_id, room_id)])
    for (division_id, day), terms in first_slot_terms.items():
        if day in relaxed:
            continue
        model.Add(sum(terms) >= 1)

    # no all-theory day: each day must have >=1 practical/skill session, for divisions that offer one
    practical_or_skill_terms: dict = {}
    divisions_with_practical: set[str] = set()
    for req in requirements:
        if req.is_break:
            continue
        course = courses_by_code.get(req.course_code)
        if not course:
            continue
        if req.session_type == SessionType.PRACTICAL or course.category == CourseCategory.SKILL:
            divisions_with_practical.add(req.division_id)
            for (start_id, _occ, day, room_id) in candidates[req.id]:
                practical_or_skill_terms.setdefault((req.division_id, day), []).append(
                    x[(req.id, start_id, room_id)])
    for division in problem.divisions:
        if division.id not in divisions_with_practical:
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
                    room_obj_terms.append(int(waste * 10) * var)  # scaled to stay integral
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

    # native gap-minimization: penalize periods that sit strictly between two occupied
    # periods on the same (division, day) but are themselves free. A break IS occupied for
    # this purpose (it's a real scheduled event) -- excluding it would incentivize the solver
    # to shove the break to the day's edge just to dodge the gap penalty, instead of a
    # natural mid-day placement.
    GAP_WEIGHT = 50
    DAY_SPAN_WEIGHT = 40   # per late-tail period occupied; ~scoring.py day_span * CP-SAT scale
    for division in problem.divisions:
        for day, day_slots in days.items():
            occupied_by_period: dict[int, list] = {ts.period: [] for ts in day_slots}
            for req in requirements:
                if req.division_id != division.id:
                    continue
                for (start_id, occ_ids, d, room_id) in candidates[req.id]:
                    if d != day:
                        continue
                    var = x[(req.id, start_id, room_id)]
                    for sid in occ_ids:
                        ts = next((t for t in day_slots if t.id == sid), None)
                        if ts:
                            occupied_by_period[ts.period].append(var)

            occupied_bool = {}
            for period, terms in occupied_by_period.items():
                b = model.NewBoolVar(f"occ_{division.id}_{day}_{period}")
                if terms:
                    model.AddMaxEquality(b, terms)
                else:
                    model.Add(b == 0)
                occupied_bool[period] = b

            # day-span penalty: discourage occupying the late "tail" periods (beyond a compact
            # 6h+break span from the day's start) so days end earlier and vary in length instead
            # of every division stretching across the whole template -- mirrors scoring.py's
            # day_span term.
            if day_slots:
                first_period = day_slots[0].period
                for period, b in occupied_bool.items():
                    if period - first_period >= COMPACT_DAY_SPAN:
                        student_obj_terms.append(DAY_SPAN_WEIGHT * b)

            periods_sorted = sorted(occupied_bool.keys())
            for i, period in enumerate(periods_sorted):
                if i == 0 or i == len(periods_sorted) - 1:
                    continue
                before = model.NewBoolVar(f"before_{division.id}_{day}_{period}")
                after = model.NewBoolVar(f"after_{division.id}_{day}_{period}")
                model.AddMaxEquality(before, [occupied_bool[p] for p in periods_sorted[:i]])
                model.AddMaxEquality(after, [occupied_bool[p] for p in periods_sorted[i + 1:]])
                gap = model.NewBoolVar(f"gap_{division.id}_{day}_{period}")
                model.Add(gap <= before)
                model.Add(gap <= after)
                model.Add(gap <= 1 - occupied_bool[period])
                model.Add(gap >= before + after - occupied_bool[period] - 1)
                student_obj_terms.append(GAP_WEIGHT * gap)

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
    }

    return _BuiltModel(
        model=model, x=x, requirements=requirements, candidates=candidates,
        objective_categories=objective_categories,
    )


class CPSATSolver(SolverBase):
    name = "cpsat"

    def solve(self, problem: ProblemInstance, time_limit_s: float = 300,
              warm_start: Solution | None = None,
              extra_solver_params: dict | None = None) -> Solution:
        start_time = time.time()
        built = _build_model(problem)
        all_terms = [t for cat in DEFAULT_SOLVE_CATEGORIES for t in built.objective_categories[cat]]
        if all_terms:
            built.model.Minimize(sum(all_terms))
        solution, _category_values = _solve_and_decode(
            built, problem, time_limit_s, warm_start, extra_solver_params, start_time)
        return solution

    def solve_pareto_point(self, problem: ProblemInstance, bounds: dict[str, int], minimize: str,
                            time_limit_s: float = 300, warm_start: Solution | None = None,
                            extra_solver_params: dict | None = None,
                            ) -> tuple[Solution, dict[str, int | None]]:
        """Epsilon-constraint (AUGMECON2-style) point on the Pareto frontier: bound each category
        in `bounds` at `<= epsilon`, then optimize `minimize` -- augmented with the bound
        constraints' slack so the result is a strictly (not merely weakly) Pareto-optimal point,
        per Mavrotas & Florios (2013). Reuses the exact same model `solve()` builds; see
        research/pareto_sweep.py for the sweep driver. Categories: "rooms", "labs", "students",
        "faculty" (`OBJECTIVE_CATEGORIES`).

        Unlike `solve()`, returns `(Solution, category_values)` -- the achieved integer value of
        every category's expression at the returned solution (`None` for all categories if no
        feasible solution was found), since a sweep needs to know exactly what each objective
        landed on, not just the aggregate `Solution.objective_value`."""
        start_time = time.time()
        built = _build_model(problem)
        model = built.model
        categories = built.objective_categories

        if minimize not in categories:
            raise ValueError(f"unknown objective category {minimize!r}; expected one of {OBJECTIVE_CATEGORIES}")
        for category in bounds:
            if category not in categories:
                raise ValueError(f"unknown objective category {category!r}; expected one of {OBJECTIVE_CATEGORIES}")

        slack_terms = []
        for category, epsilon in bounds.items():
            epsilon = int(epsilon)
            terms = categories[category]
            expr = sum(terms) if terms else 0
            slack = model.NewIntVar(0, max(epsilon, 0), f"slack_{category}")
            model.Add(expr + slack == epsilon)
            slack_terms.append(slack)

        minimize_terms = categories[minimize]
        minimize_expr = sum(minimize_terms) if minimize_terms else 0
        if slack_terms:
            # scale the primary objective well above the largest possible slack sum so the
            # augmentation term only breaks ties among equally-good `minimize` values (pushes
            # bounded objectives to their true optimum within their bound) rather than ever
            # overriding it -- the AUGMECON2 mechanism that avoids weakly-dominated frontier points.
            scale = sum(int(e) for e in bounds.values()) + 1
            model.Minimize(scale * minimize_expr - sum(slack_terms))
        else:
            model.Minimize(minimize_expr)

        return _solve_and_decode(built, problem, time_limit_s, warm_start, extra_solver_params, start_time)


def _solve_and_decode(built: _BuiltModel, problem: ProblemInstance, time_limit_s: float,
                       warm_start: Solution | None, extra_solver_params: dict | None,
                       start_time: float) -> tuple[Solution, dict[str, int | None]]:
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
    status = solver.Solve(model)
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
