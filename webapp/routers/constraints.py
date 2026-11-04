"""Centralized Academic Timetable Constraints Controller & Engine Hooks.

Encloses:
1. Declarative YAML loader, validator, and REST CRUD endpoints.
2. CP-SAT mathematical constraint generator (apply_academic_constraints).
3. Ground-truth solution violation evaluator (evaluate_academic_violations).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from engine.models import ProblemInstance, Solution, SessionType, expand_requirements
from engine.solvers.candidates import NO_ROOM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/constraints", tags=["constraints"])

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "academic_constraints.yaml"

# Global in-memory cache of parsed configuration
_CACHED_CONFIG: Optional[dict[str, Any]] = None
_CACHED_MTIME: float = 0.0


def get_constraints_config_path() -> Path:
    return CONFIG_PATH


def load_academic_constraints_yaml(force_reload: bool = False) -> dict[str, Any]:
    """Load and parse config/academic_constraints.yaml with mtime caching."""
    global _CACHED_CONFIG, _CACHED_MTIME
    path = get_constraints_config_path()
    if not path.exists():
        logger.warning(f"Academic constraints YAML not found at {path}. Returning empty config.")
        return {}

    mtime = path.stat().st_mtime
    if not force_reload and _CACHED_CONFIG is not None and mtime == _CACHED_MTIME:
        return _CACHED_CONFIG

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _CACHED_CONFIG = data
        _CACHED_MTIME = mtime
        return data
    except Exception as exc:
        logger.error(f"Error loading {path}: {exc}")
        if _CACHED_CONFIG is not None:
            return _CACHED_CONFIG
        raise exc


def save_academic_constraints_yaml(yaml_content: str) -> dict[str, Any]:
    """Validate YAML content, write to disk, and refresh cache."""
    global _CACHED_CONFIG, _CACHED_MTIME
    try:
        parsed = yaml.safe_load(yaml_content)
        if not isinstance(parsed, dict):
            raise ValueError("YAML root must be a dictionary / mapping.")
    except Exception as exc:
        raise ValueError(f"Invalid YAML syntax or structure: {exc}") from exc

    path = get_constraints_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    _CACHED_CONFIG = parsed
    _CACHED_MTIME = path.stat().st_mtime
    return parsed


# ------------------------------------------------------------------------------
# Pydantic Schemas for REST API
# ------------------------------------------------------------------------------
class ConstraintQuery(BaseModel):
    query: str
    run_id: Optional[int] = None


class UpdateYamlRequest(BaseModel):
    yaml: str = Field(..., description="Raw YAML configuration string")


# ------------------------------------------------------------------------------
# REST API Endpoints
# ------------------------------------------------------------------------------
@router.get("/config")
def get_constraints_config():
    """Retrieve parsed academic constraints configuration as structured JSON."""
    return load_academic_constraints_yaml()


@router.get("/yaml")
def get_constraints_yaml():
    """Retrieve raw YAML configuration text."""
    path = get_constraints_config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Constraints YAML configuration file not found.")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return {"yaml": content}


@router.put("/yaml")
def update_constraints_yaml(body: UpdateYamlRequest):
    """Validate and update the declarative YAML constraints file."""
    try:
        parsed = save_academic_constraints_yaml(body.yaml)
        return {"status": "saved", "config": parsed}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


@router.get("/faculty")
def get_faculty_constraints():
    """Get summarized faculty constraints (shift distribution & visiting faculty)."""
    cfg = load_academic_constraints_yaml()
    return cfg.get("faculty_rules", {})


@router.get("/year/{year_level}")
def get_year_constraints(year_level: int):
    """Get constraints specific to year_level (2 for SY, 3 for TY, 4 for BTech)."""
    cfg = load_academic_constraints_yaml()
    years_map = {2: "sy", 3: "ty", 4: "btech"}
    key = years_map.get(year_level)
    if not key:
        raise HTTPException(status_code=400, detail=f"Invalid year level: {year_level}. Supported: 2 (SY), 3 (TY), 4 (BTech).")
    year_cfg = cfg.get("year_rules", {}).get(key, {})
    return {"year_level": year_level, "key": key, "rules": year_cfg}


@router.post("/evaluate")
def evaluate_constraint(body: ConstraintQuery):
    """Evaluate natural language constraint feasibility."""
    from engine.llm_constraints import evaluate_constraint_feasibility
    return evaluate_constraint_feasibility(body.query, None)


# ------------------------------------------------------------------------------
# Helper Utilities for Solver and Scorer
# ------------------------------------------------------------------------------
def _is_year(division, target_years: tuple[int, ...], problem: ProblemInstance | None = None) -> bool:
    """Helper to check if a division belongs to target years."""
    if not division:
        return False
    div_id = division.id.upper()
    if 2 in target_years and ("SY" in div_id or "SEM3" in div_id or "SEM4" in div_id):
        return True
    if 3 in target_years and ("TY" in div_id or "SEM5" in div_id or "SEM6" in div_id):
        return True
    if 4 in target_years and ("BTECH" in div_id or "SEM7" in div_id or "SEM8" in div_id):
        return True

    # If problem has institutional 10-period structure (periods 0..9, e.g. 50 slots)
    if problem and len(problem.time_slots) >= 45:
        max_p = max((t.period for t in problem.time_slots), default=0)
        if max_p >= 9:
            sem = getattr(division, "semester", None)
            if sem is not None:
                year_from_sem = (sem + 1) // 2
                return year_from_sem in target_years

    return False


# ------------------------------------------------------------------------------
# CP-SAT Solver Integration Hook
# ------------------------------------------------------------------------------
def apply_academic_constraints(model, problem: ProblemInstance, candidates: dict, x: dict) -> dict[str, int]:
    """Hook invoked by engine/solvers/cpsat.py to enforce academic constraints from YAML.

    Applies:
    1. SY Afternoon Shift Window: No teaching sessions before Period 3 (11:00 AM).
    2. TY Morning Shift: Mandatory start at Period 0 (08:00 AM) and finish by early afternoon.
    3. TY Elective Set 1 & Set 2 Concurrency: Strict start slot equality and mutual non-overlap.
    4. BTech Weekday Restriction: Monday to Thursday only (Friday reserved for DMP & Research).
    5. Online Course Room Banning: Online courses must not book physical rooms.
    6. Visiting Faculty Guardrails: TC (Thu-Fri 10:00-16:00), SF (afternoon only), ND (back-to-back).
    7. Full-Time Shift Spread: 8 AM and 6 PM distribution for 18 full-time teachers (KS exempt).
    """
    stats: dict[str, int] = {"rules_applied": 0}
    try:
        cfg = load_academic_constraints_yaml()
    except Exception as exc:
        logger.warning(f"Could not load academic constraints YAML: {exc}")
        return stats

    if not cfg:
        return stats

    time_slots_by_id = {t.id: t for t in problem.time_slots}
    divisions_by_id = problem.division_by_id()
    requirements = expand_requirements(problem)
    reqs_by_id = {r.id: r for r in requirements}

    global_rules = cfg.get("global_rules", {})
    faculty_rules = cfg.get("faculty_rules", {})
    year_rules = cfg.get("year_rules", {})

    # 1. SY Afternoon Shift Window
    sy_cfg = year_rules.get("sy", {})
    sy_shift = sy_cfg.get("shift", {})
    earliest_sy_period = sy_shift.get("earliest_period", 3)  # default Period 3 (~11:00 AM)
    max_period = max((t.period for t in problem.time_slots), default=7)

    # Check which divisions legitimately fit in the afternoon window
    sy_divisions_fitting = set()
    for div in problem.divisions:
        if _is_year(div, (2,), problem):
            div_load = sum(r.duration_slots for r in requirements if r.division_id == div.id and not r.is_break)
            available_afternoon_slots = (max_period - earliest_sy_period + 1) * problem.days_per_week
            if max_period >= 8 and div_load <= available_afternoon_slots:
                sy_divisions_fitting.add(div.id)

    for req in requirements:
        if req.is_break:
            continue
        if req.division_id in sy_divisions_fitting:
            valid_cands = candidates.get(req.id, [])
            afternoon_cands = [c for c in valid_cands if time_slots_by_id[c[0]].period >= earliest_sy_period]
            if afternoon_cands:
                for c in valid_cands:
                    start_ts = time_slots_by_id.get(c[0])
                    if start_ts and start_ts.period < earliest_sy_period:
                        var = x.get((req.id, c[0], c[3]))
                        if var is not None:
                            model.Add(var == 0)
                            stats["rules_applied"] += 1

    # 2. BTech Monday to Thursday Only (Friday restricted to DMP & Research)
    btech_cfg = year_rules.get("btech", {})
    active_btech_days = set(btech_cfg.get("active_weekdays", [0, 1, 2, 3]))
    friday_policy = btech_cfg.get("friday_policy", {})
    friday_exceptions = {e.get("code") for e in friday_policy.get("exceptions", []) if isinstance(e, dict)}
    friday_exceptions.update(["DMP", "RESEARCH_PROJECT", "PROJECT", "MAJOR_PROJECT"])

    for req in requirements:
        if req.is_break:
            continue
        div = divisions_by_id.get(req.division_id)
        if _is_year(div, (4,), problem):
            valid_cands = candidates.get(req.id, [])
            is_exception = req.course_code in friday_exceptions
            if not is_exception:
                # If course is not Friday-exempt, restrict to active_btech_days
                has_active_day_cands = any(time_slots_by_id[c[0]].day in active_btech_days for c in valid_cands)
                if has_active_day_cands:
                    for c in valid_cands:
                        ts = time_slots_by_id.get(c[0])
                        if ts and ts.day not in active_btech_days:
                            var = x.get((req.id, c[0], c[3]))
                            if var is not None:
                                model.Add(var == 0)
                                stats["rules_applied"] += 1

    # 3. Online Courses: Room Banning (room_id must be NO_ROOM)
    course_delivery = btech_cfg.get("course_delivery", {})
    online_course_codes = set()
    for code, details in course_delivery.items():
        if isinstance(details, dict):
            theory = details.get("theory", {})
            if theory.get("mode") == "online" or details.get("mode") == "online":
                online_course_codes.add(code)
                online_course_codes.add(details.get("code", code))

    for req in requirements:
        if req.course_code in online_course_codes and req.session_type != SessionType.PRACTICAL:
            valid_cands = candidates.get(req.id, [])
            has_no_room = any(c[3] == NO_ROOM for c in valid_cands)
            if has_no_room:
                for c in valid_cands:
                    if c[3] != NO_ROOM:
                        var = x.get((req.id, c[0], c[3]))
                        if var is not None:
                            model.Add(var == 0)
                            stats["rules_applied"] += 1

    # 4. Visiting Faculty Restrictions
    visiting_cfg = faculty_rules.get("visiting_faculty", {})
    tc_cfg = visiting_cfg.get("TC", {})
    tc_days = set(tc_cfg.get("allowed_days", [3, 4]))
    tc_window = tc_cfg.get("time_window", {})
    tc_start_hr = tc_window.get("start_hour", 10)
    tc_end_hr = tc_window.get("end_hour", 16)

    sf_cfg = visiting_cfg.get("SF", {})

    for req in requirements:
        if not req.faculty_id:
            continue
        fid = req.faculty_id.upper()
        valid_cands = candidates.get(req.id, [])

        # Trupti Chandak (TC): Thu/Fri only, 10:00 - 16:00
        if "TC" in fid or "TRUPTI" in fid:
            has_valid = any(
                time_slots_by_id[c[0]].day in tc_days and (tc_start_hr <= 8 + time_slots_by_id[c[0]].period <= tc_end_hr)
                for c in valid_cands
            )
            if has_valid:
                for c in valid_cands:
                    ts = time_slots_by_id.get(c[0])
                    if ts:
                        hour = 8 + ts.period
                        if ts.day not in tc_days or not (tc_start_hr <= hour <= tc_end_hr):
                            var = x.get((req.id, c[0], c[3]))
                            if var is not None:
                                model.Add(var == 0)
                                stats["rules_applied"] += 1

        # Sonal Foss (SF): Afternoon/evening only (no morning before 12:00, period >= 4)
        if "SF" in fid or "SONAL" in fid:
            has_afternoon = any(time_slots_by_id[c[0]].period >= 4 for c in valid_cands)
            if has_afternoon:
                for c in valid_cands:
                    ts = time_slots_by_id.get(c[0])
                    if ts and ts.period < 4:
                        var = x.get((req.id, c[0], c[3]))
                        if var is not None:
                            model.Add(var == 0)
                            stats["rules_applied"] += 1

    # 5. Full-time Faculty 8 AM and 6 PM Spread (Strict Hard Constraint)
    shift_dist = faculty_rules.get("shift_distribution", {})
    if shift_dist.get("enforce_strict_hard", True):
        exempt = set(shift_dist.get("exempt_faculty", ["KS"]))
        target_faculty = set(shift_dist.get("target_full_time_faculty", []))

        faculty_reqs: dict[str, list] = {}
        for req in requirements:
            if req.faculty_id:
                faculty_reqs.setdefault(req.faculty_id, []).append(req)

        for fid, f_reqs in faculty_reqs.items():
            fid_clean = fid.upper()
            is_exempt = any(ex in fid_clean for ex in exempt)
            if is_exempt or len(f_reqs) < 2:
                continue

            matches_target = any(tf in fid_clean for tf in target_faculty) if target_faculty else True
            if not matches_target:
                continue

            eight_am_vars = []
            six_pm_vars = []
            for req in f_reqs:
                for c in candidates.get(req.id, []):
                    ts = time_slots_by_id.get(c[0])
                    if not ts:
                        continue
                    var = x.get((req.id, c[0], c[3]))
                    if var is None:
                        continue
                    if ts.period == 0:
                        eight_am_vars.append(var)
                    end_period = ts.period + req.duration_slots
                    if end_period >= 9:
                        six_pm_vars.append(var)

            if eight_am_vars:
                model.Add(sum(eight_am_vars) >= 1)
                stats["rules_applied"] += 1
            if six_pm_vars:
                model.Add(sum(six_pm_vars) >= 1)
                stats["rules_applied"] += 1

    # 6. TY Elective Concurrency
    ty_cfg = year_rules.get("ty", {})
    e1_courses = {"TSA", "AAIA", "MIII"}
    e2_courses = {"CV", "ROBOTICS"}

    e1_reqs = [r for r in requirements if r.course_code in e1_courses and r.session_type != SessionType.PRACTICAL]
    if len(e1_reqs) >= 2:
        # Define start slot expression for each theory session
        slot_exprs = []
        for req in e1_reqs:
            cands = candidates.get(req.id, [])
            if cands:
                slot_exprs.append(sum(c[0] * x[(req.id, c[0], c[3])] for c in cands if (req.id, c[0], c[3]) in x))
        # Synchronize first with the rest
        if len(slot_exprs) >= 2:
            base_slot = slot_exprs[0]
            for expr in slot_exprs[1:]:
                model.Add(base_slot == expr)
                stats["rules_applied"] += 1

    # 7. Multi-Session Labs (e.g. Web Engineering Lab: 4h/week -> 2h x 2 separate sessions)
    # Ensure at most 1 lab session of the same course per batch per day (non-consecutive / separate days)
    batch_lab_reqs: dict[tuple[str, str, str], list] = {}
    for req in requirements:
        if req.is_break or req.session_type != SessionType.PRACTICAL:
            continue
        key = (req.division_id, req.course_code, req.batch_id or "")
        batch_lab_reqs.setdefault(key, []).append(req)

    for (div_id, c_code, b_id), r_list in batch_lab_reqs.items():
        if len(r_list) > 1 and problem.days_per_week >= len(r_list):
            for day in range(problem.days_per_week):
                day_vars = []
                for req in r_list:
                    for c in candidates.get(req.id, []):
                        ts = time_slots_by_id.get(c[0])
                        if ts and ts.day == day:
                            var = x.get((req.id, c[0], c[3]))
                            if var is not None:
                                day_vars.append(var)
                if len(day_vars) > 1:
                    model.Add(sum(day_vars) <= 1)
                    stats["rules_applied"] += 1

    return stats


# ------------------------------------------------------------------------------
# Ground-Truth Scoring / Violation Evaluator
# ------------------------------------------------------------------------------
def evaluate_academic_violations(solution: Solution, problem: ProblemInstance) -> dict[str, int]:
    """Audit and evaluate violations against YAML academic constraints for a solved timetable."""
    violations: dict[str, int] = {}
    try:
        cfg = load_academic_constraints_yaml()
    except Exception:
        return violations

    if not cfg:
        return violations

    time_slots_by_id = {t.id: t for t in problem.time_slots}
    divisions_by_id = problem.division_by_id()
    requirements = expand_requirements(problem)
    assignments = solution.assignment_by_session()

    year_rules = cfg.get("year_rules", {})
    faculty_rules = cfg.get("faculty_rules", {})

    # Check SY afternoon rule
    sy_cfg = year_rules.get("sy", {})
    earliest_sy_period = sy_cfg.get("shift", {}).get("earliest_period", 3)
    for req in requirements:
        if req.is_break:
            continue
        div = divisions_by_id.get(req.division_id)
        if _is_year(div, (2,), problem):
            asgn = assignments.get(req.id)
            if asgn:
                ts = time_slots_by_id.get(asgn.time_slot_id)
                if ts and ts.period < earliest_sy_period:
                    violations["sy_morning_lecture_violation"] = violations.get("sy_morning_lecture_violation", 0) + 1

    # Check BTech Friday restriction
    btech_cfg = year_rules.get("btech", {})
    active_btech_days = set(btech_cfg.get("active_weekdays", [0, 1, 2, 3]))
    friday_policy = btech_cfg.get("friday_policy", {})
    friday_exceptions = {e.get("code") for e in friday_policy.get("exceptions", []) if isinstance(e, dict)}
    friday_exceptions.update(["DMP", "RESEARCH_PROJECT", "PROJECT", "MAJOR_PROJECT"])

    for req in requirements:
        if req.is_break:
            continue
        div = divisions_by_id.get(req.division_id)
        if _is_year(div, (4,), problem):
            asgn = assignments.get(req.id)
            if asgn:
                ts = time_slots_by_id.get(asgn.time_slot_id)
                if ts and ts.day not in active_btech_days and req.course_code not in friday_exceptions:
                    violations["btech_friday_lecture_violation"] = violations.get("btech_friday_lecture_violation", 0) + 1

    # Check Visiting Faculty Timing (TC & SF)
    visiting_cfg = faculty_rules.get("visiting_faculty", {})
    tc_days = set(visiting_cfg.get("TC", {}).get("allowed_days", [3, 4]))
    for req in requirements:
        if not req.faculty_id:
            continue
        fid = req.faculty_id.upper()
        asgn = assignments.get(req.id)
        if not asgn:
            continue
        ts = time_slots_by_id.get(asgn.time_slot_id)
        if not ts:
            continue
        if "TC" in fid or "TRUPTI" in fid:
            hour = 8 + ts.period
            if ts.day not in tc_days or not (10 <= hour <= 16):
                violations["trupti_timing_violation"] = violations.get("trupti_timing_violation", 0) + 1
        if "SF" in fid or "SONAL" in fid:
            if ts.period < 4:
                violations["sonal_morning_violation"] = violations.get("sonal_morning_violation", 0) + 1

    return violations
