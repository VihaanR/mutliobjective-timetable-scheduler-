"""Comprehensive test suite for Milestone 1: Security & Auth-First Institutional Gateway.
Tests:
- Gateway dispatcher (GET /) 307 redirects for unauthenticated, student, faculty, and admin.
- Route guards for /platform and /student.
- Dev credentials fallback for both NM sample faculty and admin@djsce.ac.in.
- Dual-cookie generation (tt_session & access_token) and deletion on logout.
- Multi-source token resolution: access_token cookie, tt_session cookie, and Bearer authorization header.
- Zero-redirect delivery for /login and public /static assets.
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

from webapp.auth import (
    ACCESS_TOKEN_COOKIE, DEV_ADMIN_EMAIL, DEV_ADMIN_PASSWORD, DEV_ADMIN_FACULTY_CODE,
    DEV_SAMPLE_EMAIL, DEV_SAMPLE_PASSWORD, SESSION_COOKIE,
    create_session_token, get_secret, hash_password,
)
from webapp.db import init_db, set_engine
from webapp.models_db import Branch, Division, Faculty, Student
from webapp.server import app


@pytest.fixture
def auth_env(tmp_path):
    """Initializes a fresh isolated SQLite database with student and faculty rows for auth testing."""
    db_file = tmp_path / "test_auth_env.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    set_engine(engine)
    init_db()

    with Session(engine) as session:
        # Create a branch and division for student testing
        branch = Branch(code="COMP", name="Computer Engineering")
        session.add(branch)
        session.commit()
        session.refresh(branch)

        division = Division(branch_id=branch.id, name="Div A", year=2)
        session.add(division)
        session.commit()
        session.refresh(division)

        # Normal faculty row
        faculty = Faculty(
            code="FAC1",
            name="Regular Faculty",
            email="fac1@djsce.ac.in",
            password_hash=hash_password("FacPass123"),
        )
        session.add(faculty)

        # Normal student row
        student = Student(
            division_id=division.id,
            roll_no="60004210001",
            name="Test Student",
            email="student@djsce.ac.in",
            password_hash=hash_password("StudentPass123"),
        )
        session.add(student)
        session.commit()
        session.refresh(faculty)
        session.refresh(student)

        secret = get_secret(session)
        faculty_token = create_session_token({"role": "faculty", "id": faculty.id}, secret)
        admin_token = create_session_token({"role": "admin", "id": faculty.id}, secret)
        student_token = create_session_token({"role": "student", "id": student.id}, secret)

    yield {
        "engine": engine,
        "faculty_token": faculty_token,
        "admin_token": admin_token,
        "student_token": student_token,
    }
    engine.dispose()


@pytest.fixture
def client(auth_env):
    """Yields a TestClient instance connected to the isolated auth_env SQLite database."""
    with TestClient(app) as c:
        yield c


def test_unauthenticated_root_redirects_307_to_login(auth_env):
    with TestClient(app) as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/login"


def test_student_root_redirects_307_to_student(auth_env):
    token = auth_env["student_token"]
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/student"


def test_faculty_root_redirects_307_to_platform(auth_env):
    token = auth_env["faculty_token"]
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/platform"


def test_admin_root_redirects_307_to_platform(auth_env):
    token = auth_env["admin_token"]
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/platform"


def test_platform_page_guards(auth_env):
    with TestClient(app) as client:
        # Unauthenticated -> /login
        r_anon = client.get("/platform", follow_redirects=False)
        assert r_anon.status_code == 307
        assert r_anon.headers["location"] == "/login"

        # Student -> /student
        client.cookies.set(SESSION_COOKIE, auth_env["student_token"])
        r_stud = client.get("/platform", follow_redirects=False)
        assert r_stud.status_code == 307
        assert r_stud.headers["location"] == "/student"

        # Faculty -> 200 platform.html
        client.cookies.set(SESSION_COOKIE, auth_env["faculty_token"])
        r_fac = client.get("/platform", follow_redirects=False)
        assert r_fac.status_code == 200

        # Admin -> 200 platform.html
        client.cookies.set(SESSION_COOKIE, auth_env["admin_token"])
        r_adm = client.get("/platform", follow_redirects=False)
        assert r_adm.status_code == 200


def test_student_page_guards(auth_env):
    with TestClient(app) as client:
        # Unauthenticated -> /login
        r_anon = client.get("/student", follow_redirects=False)
        assert r_anon.status_code == 307
        assert r_anon.headers["location"] == "/login"

        # Faculty -> /platform
        client.cookies.set(SESSION_COOKIE, auth_env["faculty_token"])
        r_fac = client.get("/student", follow_redirects=False)
        assert r_fac.status_code == 307
        assert r_fac.headers["location"] == "/platform"

        # Admin -> /platform
        client.cookies.set(SESSION_COOKIE, auth_env["admin_token"])
        r_adm = client.get("/student", follow_redirects=False)
        assert r_adm.status_code == 307
        assert r_adm.headers["location"] == "/platform"

        # Student -> 200 student.html
        client.cookies.set(SESSION_COOKIE, auth_env["student_token"])
        r_stud = client.get("/student", follow_redirects=False)
        assert r_stud.status_code == 200


def test_login_page_renders_stateless(auth_env):
    with TestClient(app) as client:
        r = client.get("/login", follow_redirects=False)
        assert r.status_code == 200
        assert "Dwarkadas J. Sanghvi College of Engineering" in r.text
        assert "Auto-fill Dev Credentials" in r.text


def test_static_assets_accessible_anonymously(auth_env):
    with TestClient(app) as client:
        r_css = client.get("/static/style.css")
        assert r_css.status_code == 200
        r_js = client.get("/static/icons.js")
        assert r_js.status_code == 200


def test_dev_faculty_login(auth_env):
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={
            "role": "faculty",
            "email": DEV_SAMPLE_EMAIL,
            "password": DEV_SAMPLE_PASSWORD,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["role"] == "faculty"
        assert data["token_type"] == "bearer"
        assert "access_token" in data
        assert SESSION_COOKIE in r.cookies
        assert ACCESS_TOKEN_COOKIE in r.cookies


def test_dev_admin_login_auto_upsert(auth_env):
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={
            "role": "faculty",
            "email": DEV_ADMIN_EMAIL,
            "password": DEV_ADMIN_PASSWORD,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["role"] == "admin"
        assert data["token_type"] == "bearer"
        assert "access_token" in data
        assert data["name"] == "Institutional Administrator"
        assert SESSION_COOKIE in r.cookies
        assert ACCESS_TOKEN_COOKIE in r.cookies

        # Confirm admin row in DB
        with Session(auth_env["engine"]) as session:
            admin_row = session.exec(select(Faculty).where(Faculty.code == "ADMIN")).first()
            assert admin_row is not None
            assert admin_row.email == DEV_ADMIN_EMAIL


def test_auth_via_access_token_cookie_alone(auth_env):
    token = auth_env["faculty_token"]
    with TestClient(app) as client:
        client.cookies.set(ACCESS_TOKEN_COOKIE, token)
        # Should authenticate and redirect to /platform
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/platform"


def test_auth_via_authorization_bearer_header(auth_env):
    token = auth_env["admin_token"]
    with TestClient(app) as client:
        r = client.get("/", headers={"Authorization": f"Bearer {token}"}, follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/platform"


def test_auth_via_authorization_raw_header(auth_env):
    token = auth_env["student_token"]
    with TestClient(app) as client:
        r = client.get("/", headers={"Authorization": token}, follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/student"


def test_invalid_or_expired_token_redirects_to_login(auth_env):
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, "invalid.token.structure")
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/login"


def test_logout_deletes_both_cookies(auth_env):
    token = auth_env["faculty_token"]
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE, token)
        client.cookies.set(ACCESS_TOKEN_COOKIE, token)
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        # Check set-cookie headers expire or clear both
        set_cookies = r.headers.get_list("set-cookie")
        cookie_names = [sc.split("=")[0].strip() for sc in set_cookies]
        assert SESSION_COOKIE in cookie_names
        assert ACCESS_TOKEN_COOKIE in cookie_names


def test_me_endpoint_for_all_roles(auth_env):
    with TestClient(app) as client:
        # Faculty
        client.cookies.set(SESSION_COOKIE, auth_env["faculty_token"])
        r_fac = client.get("/api/auth/me")
        assert r_fac.status_code == 200
        assert r_fac.json()["role"] == "faculty"

        # Admin
        client.cookies.set(SESSION_COOKIE, auth_env["admin_token"])
        r_adm = client.get("/api/auth/me")
        assert r_adm.status_code == 200
        assert r_adm.json()["role"] == "admin"

        # Student
        client.cookies.set(SESSION_COOKIE, auth_env["student_token"])
        r_stud = client.get("/api/auth/me")
        assert r_stud.status_code == 200
        assert r_stud.json()["role"] == "student"


def test_dev_credentials_rejected_when_dev_fallback_disabled(client, monkeypatch):
    """Verifies that when ALLOW_DEV_FALLBACK is False:
    1. Dev admin credentials (admin@djsce.ac.in / devpass123) are rejected with HTTP 401.
    2. Dev sample teacher credentials (nilesh.marathe@djsce.edu.in / Timetable@123) are rejected with HTTP 401.
    3. Real database records for seeded faculty and students continue to authenticate normally (HTTP 200).
    """
    monkeypatch.setattr("webapp.auth.ALLOW_DEV_FALLBACK", False)

    # 1. Dev admin credentials rejected under role='admin'
    r_admin = client.post("/api/auth/login", json={
        "role": "admin",
        "email": DEV_ADMIN_EMAIL,
        "password": DEV_ADMIN_PASSWORD,
    })
    assert r_admin.status_code == 401
    assert "invalid" in r_admin.json().get("detail", "")

    # Dev admin credentials rejected under role='faculty'
    r_admin_fac_role = client.post("/api/auth/login", json={
        "role": "faculty",
        "email": DEV_ADMIN_EMAIL,
        "password": DEV_ADMIN_PASSWORD,
    })
    assert r_admin_fac_role.status_code == 401

    # 2. Dev sample teacher credentials rejected
    r_nm = client.post("/api/auth/login", json={
        "role": "faculty",
        "email": DEV_SAMPLE_EMAIL,
        "password": DEV_SAMPLE_PASSWORD,
    })
    assert r_nm.status_code == 401
    assert "invalid" in r_nm.json().get("detail", "")

    # 3. Real seeded faculty in DB continues to authenticate normally
    r_fac = client.post("/api/auth/login", json={
        "role": "faculty",
        "email": "fac1@djsce.ac.in",
        "password": "FacPass123",
    })
    assert r_fac.status_code == 200
    data_fac = r_fac.json()
    assert data_fac["role"] == "faculty"
    assert data_fac["token_type"] == "bearer"
    assert "access_token" in data_fac
    assert SESSION_COOKIE in r_fac.cookies
    assert ACCESS_TOKEN_COOKIE in r_fac.cookies

    # 4. Real seeded student in DB continues to authenticate normally
    r_stud = client.post("/api/auth/login", json={
        "role": "student",
        "email": "student@djsce.ac.in",
        "password": "StudentPass123",
    })
    assert r_stud.status_code == 200
    data_stud = r_stud.json()
    assert data_stud["role"] == "student"
    assert data_stud["token_type"] == "bearer"
    assert "access_token" in data_stud
    assert SESSION_COOKIE in r_stud.cookies
    assert ACCESS_TOKEN_COOKIE in r_stud.cookies


def test_dev_admin_not_upserted_when_dev_fallback_disabled(client, auth_env, monkeypatch):
    """Verifies that an unauthenticated dev admin login attempt does NOT create or auto-upsert
    an ADMIN row in the database when ALLOW_DEV_FALLBACK is False."""
    monkeypatch.setattr("webapp.auth.ALLOW_DEV_FALLBACK", False)

    r = client.post("/api/auth/login", json={
        "role": "admin",
        "email": DEV_ADMIN_EMAIL,
        "password": DEV_ADMIN_PASSWORD,
    })
    assert r.status_code == 401

    with Session(auth_env["engine"]) as session:
        admin_row = session.exec(select(Faculty).where(Faculty.code == DEV_ADMIN_FACULTY_CODE)).first()
        assert admin_row is None, "Dev admin faculty record must NOT be upserted when fallback is disabled"


def test_dev_fallback_toggle_restores_sample_credentials(client, monkeypatch):
    """Verifies that toggling ALLOW_DEV_FALLBACK dynamically enables or disables dev credentials."""
    # Disabled -> 401
    monkeypatch.setattr("webapp.auth.ALLOW_DEV_FALLBACK", False)
    r_off = client.post("/api/auth/login", json={
        "role": "faculty",
        "email": DEV_SAMPLE_EMAIL,
        "password": DEV_SAMPLE_PASSWORD,
    })
    assert r_off.status_code == 401

    # Enabled -> 200
    monkeypatch.setattr("webapp.auth.ALLOW_DEV_FALLBACK", True)
    r_on = client.post("/api/auth/login", json={
        "role": "faculty",
        "email": DEV_SAMPLE_EMAIL,
        "password": DEV_SAMPLE_PASSWORD,
    })
    assert r_on.status_code == 200
    assert r_on.json()["role"] == "faculty"

    # Enabled admin -> 200
    r_admin_on = client.post("/api/auth/login", json={
        "role": "admin",
        "email": DEV_ADMIN_EMAIL,
        "password": DEV_ADMIN_PASSWORD,
    })
    assert r_admin_on.status_code == 200
    assert r_admin_on.json()["role"] == "admin"


def test_dev_sample_faculty_cannot_escalate_to_admin(client):
    """Ensures sample faculty cannot claim admin privileges by passing role='admin'."""
    r = client.post("/api/auth/login", json={
        "role": "admin",
        "email": DEV_SAMPLE_EMAIL,
        "password": DEV_SAMPLE_PASSWORD,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["role"] == "faculty"
    assert data["name"] == "Prof. Nilesh Marathe"


def test_dev_fallback_disabled_rejects_even_if_row_exists_in_db(client, auth_env, monkeypatch):
    """Ensures dev credentials cannot authenticate via normal DB lookup when fallback is disabled."""
    with Session(auth_env["engine"]) as session:
        admin_row = Faculty(
            code=DEV_ADMIN_FACULTY_CODE,
            name="Institutional Administrator",
            email=DEV_ADMIN_EMAIL,
            password_hash=hash_password(DEV_ADMIN_PASSWORD),
        )
        session.add(admin_row)
        session.commit()

    monkeypatch.setattr("webapp.auth.ALLOW_DEV_FALLBACK", False)
    r = client.post("/api/auth/login", json={
        "role": "admin",
        "email": DEV_ADMIN_EMAIL,
        "password": DEV_ADMIN_PASSWORD,
    })
    assert r.status_code == 401
    assert "invalid" in r.json().get("detail", "")

