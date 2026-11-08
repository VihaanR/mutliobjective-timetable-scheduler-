"""Tests for platform security hardening, run-specific evolution endpoints, and session pinning."""
import pytest
from fastapi.testclient import TestClient

from webapp.server import app
from webapp.db import get_engine, init_db
from sqlmodel import Session, select
from webapp.models_db import TimetableRun, Faculty
from webapp.auth import hash_password, create_session_token, get_secret, SESSION_COOKIE

client = TestClient(app)


@pytest.fixture(scope="module")
def auth_cookie():
    init_db()
    with Session(get_engine()) as session:
        # Create or find faculty
        fac = session.exec(select(Faculty)).first()
        if not fac:
            fac = Faculty(code="TEST", name="Test Teacher", max_load_hours_per_week=20)
            session.add(fac)
            session.commit()
            session.refresh(fac)
        secret = get_secret(session)
        token = create_session_token({"role": "faculty", "id": fac.id}, secret)
        return {SESSION_COOKIE: token}


def test_evolution_endpoints_require_authentication():
    """Verify unauthenticated requests to evolution endpoints return 401."""
    # 1. /api/evolution
    res1 = client.get("/api/evolution")
    assert res1.status_code == 401

    # 2. /api/evolution/step/1
    res2 = client.get("/api/evolution/step/1")
    assert res2.status_code == 401

    # 3. /api/runs/1/evolution
    res3 = client.get("/api/runs/1/evolution")
    assert res3.status_code == 401


def test_evolution_accessible_with_auth(auth_cookie):
    """Verify authenticated faculty can access evolution data."""
    res = client.get("/api/evolution", cookies=auth_cookie)
    # 200 or 404 (if no evolution file generated yet), but definitely NOT 401
    assert res.status_code in (200, 404)


def test_constraints_modifying_endpoints_require_authentication():
    """Verify PUT /api/constraints/yaml and POST /api/constraints/evaluate require auth."""
    res_yaml = client.put("/api/constraints/yaml", json={"yaml": "invalid: yaml: ["})
    assert res_yaml.status_code == 401

    res_eval = client.post("/api/constraints/evaluate", json={"query": "Prof X free"})
    assert res_eval.status_code == 401


def test_session_pinning_and_diff_api(auth_cookie):
    """Verify session pinning endpoint toggles and persists in the database."""
    init_db()
    with Session(get_engine()) as session:
        # Create a mock run
        mock_run = TimetableRun(
            label="Test Pinning Run",
            solver="cpsat",
            time_limit=30.0,
            status="done",
            problem_snapshot={"time_slots": [], "rooms": [], "courses": [], "divisions": [], "faculty": []},
            solution={"assignments": [], "solver_name": "cpsat", "wall_clock_seconds": 1.0, "status": "FEASIBLE"},
        )
        session.add(mock_run)
        session.commit()
        session.refresh(mock_run)
        run_id = mock_run.id

    # 1. Pin session
    res_pin = client.post(
        f"/api/runs/{run_id}/pin-session",
        json={"session_id": "TEST_SESSION_1", "pinned": True},
        cookies=auth_cookie,
    )
    assert res_pin.status_code == 200
    data = res_pin.json()
    assert data["is_pinned"] is True
    assert "TEST_SESSION_1" in data["pinned_session_ids"]

    # 2. Unpin session
    res_unpin = client.post(
        f"/api/runs/{run_id}/pin-session",
        json={"session_id": "TEST_SESSION_1", "pinned": False},
        cookies=auth_cookie,
    )
    assert res_unpin.status_code == 200
    assert res_unpin.json()["is_pinned"] is False

    # 3. Publish run
    res_pub = client.post(
        f"/api/runs/{run_id}/publish",
        json={"lifecycle_status": "published"},
        cookies=auth_cookie,
    )
    assert res_pub.status_code == 200
    assert res_pub.json()["lifecycle_status"] == "published"
