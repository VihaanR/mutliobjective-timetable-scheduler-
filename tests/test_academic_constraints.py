"""Unit tests for declarative academic constraints YAML, router endpoints, and solver hooks."""
import pytest
from fastapi.testclient import TestClient

from webapp.server import app
from webapp.routers.constraints import (
    load_academic_constraints_yaml,
    save_academic_constraints_yaml,
    apply_academic_constraints,
    evaluate_academic_violations,
    get_constraints_config_path,
)
from engine.sample_data import generate_sample_instance
from engine.solvers.cpsat import CPSATSolver
from engine.scoring import score

client = TestClient(app)


def test_yaml_config_loads_properly():
    """Verify academic_constraints.yaml exists, loads, and contains all required sections."""
    cfg = load_academic_constraints_yaml(force_reload=True)
    assert isinstance(cfg, dict)
    assert "global_rules" in cfg
    assert "faculty_rules" in cfg
    assert "year_rules" in cfg

    # Verify faculty rules
    faculty_cfg = cfg["faculty_rules"]
    assert "shift_distribution" in faculty_cfg
    assert faculty_cfg["shift_distribution"]["exempt_faculty"] == ["KS"]
    assert "visiting_faculty" in faculty_cfg
    assert "TC" in faculty_cfg["visiting_faculty"]
    assert "SF" in faculty_cfg["visiting_faculty"]
    assert "ND" in faculty_cfg["visiting_faculty"]

    # Verify year rules
    year_cfg = cfg["year_rules"]
    assert "sy" in year_cfg
    assert "ty" in year_cfg
    assert "btech" in year_cfg
    assert year_cfg["sy"]["shift"]["name"] == "afternoon_evening"
    assert year_cfg["ty"]["shift"]["mandatory_start_period"] == 0
    assert year_cfg["btech"]["active_weekdays"] == [0, 1, 2, 3]


def test_constraints_rest_api():
    """Verify all /api/constraints endpoints respond accurately."""
    # 1. GET /api/constraints/config
    res = client.get("/api/constraints/config")
    assert res.status_code == 200
    data = res.json()
    assert "faculty_rules" in data

    # 2. GET /api/constraints/yaml
    res_yaml = client.get("/api/constraints/yaml")
    assert res_yaml.status_code == 200
    yaml_text = res_yaml.json()["yaml"]
    assert "allowed_weekdays" in yaml_text

    # 3. GET /api/constraints/faculty
    res_fac = client.get("/api/constraints/faculty")
    assert res_fac.status_code == 200
    assert "visiting_faculty" in res_fac.json()

    # 4. GET /api/constraints/year/2 (SY), 3 (TY), 4 (BTech)
    res_sy = client.get("/api/constraints/year/2")
    assert res_sy.status_code == 200
    assert res_sy.json()["key"] == "sy"

    res_ty = client.get("/api/constraints/year/3")
    assert res_ty.status_code == 200
    assert res_ty.json()["key"] == "ty"

    res_bt = client.get("/api/constraints/year/4")
    assert res_bt.status_code == 200
    assert res_bt.json()["key"] == "btech"

    # Test invalid year
    res_invalid = client.get("/api/constraints/year/99")
    assert res_invalid.status_code == 400


def test_cpsat_solver_with_academic_constraints_hook():
    """Test that CP-SAT solver runs smoothly with the academic constraints hook integrated."""
    problem = generate_sample_instance(scale="small", seed=42)
    solution = CPSATSolver().solve(problem, time_limit_s=10.0)
    assert solution is not None
    assert len(solution.assignments) > 0

    # Verify scoring integration
    score_res = score(solution, problem)
    assert isinstance(score_res.hard_violations, int)
    assert isinstance(score_res.soft_cost, float)
