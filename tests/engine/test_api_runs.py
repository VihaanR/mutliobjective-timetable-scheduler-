"""API tests for the P2 generate job + runs router (design.md §5.3, CLAUDE.md §11).

Background jobs run in Starlette's threadpool via BackgroundTasks. Under TestClient, background
tasks execute synchronously right after the response body is produced but before `.post()` returns
control to the caller — so by the time `POST /api/generate` comes back, the job has already
finished. No polling loop is needed in these tests; a single `GET /api/runs/{id}` right after is
enough to observe the final status.
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine

from webapp.auth import hash_password
from webapp.db import set_engine, init_db, get_engine
from webapp.jobs import sweep_stale_running
from webapp.models_db import Faculty, TimetableRun
from webapp.server import app


@pytest.fixture
def client(tmp_path):
    """A TestClient already logged in as a teacher (Auth, design.md §11) - see the identical note
    in tests/test_api_entities.py's fixture."""
    db_file = tmp_path / "test_platform.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    set_engine(engine)
    init_db()
    with Session(engine) as session:
        session.add(Faculty(code="TESTFAC", name="Test Teacher"))
        session.commit()
    with TestClient(app) as c:
        c.post("/api/auth/bootstrap",
               json={"faculty_code": "TESTFAC", "email": "test@test.local", "password": "testpass123"})
        yield c
    engine.dispose()


def _seed(client):
    r = client.post("/api/seed/reference")
    assert r.status_code == 200, r.text
    return r.json()


def test_generate_greedy_runs_to_done(client):
    _seed(client)
    r = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    got = client.get(f"/api/runs/{run_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "done"
    assert body["grids"]  # non-empty dict/list
    assert isinstance(body["hard"], int)


def test_generate_on_empty_db_rejected(client):
    r = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, list) and len(detail) > 0


def test_readiness_endpoint(client):
    empty = client.get("/api/readiness")
    assert empty.status_code == 200
    body = empty.json()
    assert body["ready"] is False
    assert isinstance(body["issues"], list) and len(body["issues"]) > 0

    _seed(client)
    ready = client.get("/api/readiness")
    assert ready.json()["ready"] is True


def test_faculty_my_timetable_requires_login():
    """Anonymous access is rejected before any DB/run concerns even come into play."""
    with TestClient(app) as anon:
        r = anon.get("/api/faculty/me/timetable")
    assert r.status_code == 401


def test_faculty_my_timetable_404_before_any_run(client):
    _seed(client)
    r = client.get("/api/faculty/me/timetable")
    assert r.status_code == 404
    assert "no generated timetable" in r.json()["detail"]


def test_faculty_my_timetable_merges_multiple_divisions(client):
    """MD teaches across all three seeded divisions (D1/D2/D3) in the reference dataset - the
    headline claim of this feature is that their personal grid shows every division's sessions
    merged into one view, not just one division like the student equivalent."""
    _seed(client)

    faculty_list = client.get("/api/faculty").json()
    md = next(f for f in faculty_list if f["code"] == "MD")
    with Session(get_engine()) as session:
        row = session.get(Faculty, md["id"])
        row.email = "md@djsce.edu.in"
        row.password_hash = hash_password("mdpass123")
        session.add(row)
        session.commit()

    r = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3})
    assert r.status_code == 200, r.text

    with TestClient(app) as md_client:
        login = md_client.post("/api/auth/login",
                               json={"role": "faculty", "email": "md@djsce.edu.in", "password": "mdpass123"})
        assert login.status_code == 200, login.text

        got = md_client.get("/api/faculty/me/timetable")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["faculty_name"] == md["name"]

        all_entries = [e for entries in body["cells"].values() for e in entries]
        assert all_entries, "MD should have at least one scheduled session"
        # every entry belongs to MD, never leaking another faculty's sessions
        assert all(e.get("faculty") == "MD" for e in all_entries if not e.get("is_break"))
        # the headline claim: sessions from more than one division are merged into this one view
        divisions_seen = {e["division_id"] for e in all_entries if e.get("division_id")}
        assert len(divisions_seen) > 1, f"expected multiple divisions, got {divisions_seen}"


def test_generate_single_flight_conflict(client):
    _seed(client)
    with Session(get_engine()) as session:
        session.add(TimetableRun(status="running", solver="greedy", time_limit=3, problem_snapshot={}))
        session.commit()

    r = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3})
    assert r.status_code == 409


def test_sweep_clears_orphaned_queued_run_and_unblocks_generate(client):
    """A process that dies between `POST /api/runs` inserting a `queued` row and the background
    task flipping it to `running` leaves that row permanently `queued` — nothing in a fresh
    process will ever pick it up. Before the fix, `sweep_stale_running` only looked at `running`
    rows, so this `queued` row would keep `has_active_run` true forever and every subsequent
    `POST /api/runs` would 409 with no recovery path. Assert the sweep clears it and generate
    is unblocked afterward."""
    with Session(get_engine()) as session:
        run = TimetableRun(status="queued", solver="greedy", time_limit=1, problem_snapshot={})
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    with Session(get_engine()) as session:
        swept = sweep_stale_running(session)
        assert swept == 1

    with Session(get_engine()) as session:
        run = session.get(TimetableRun, run_id)
        assert run.status == "failed"
        assert run.error == "orphaned by restart"

    _seed(client)
    r = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3})
    assert r.status_code != 409, r.text


def test_generate_unknown_solver_rejected(client):
    _seed(client)
    r = client.post("/api/runs", json={"solver": "banana", "time_limit": 3})
    assert r.status_code == 400


def test_list_runs_after_generate(client):
    _seed(client)
    r = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3})
    run_id = r.json()["run_id"]

    runs = client.get("/api/runs").json()
    assert any(entry["id"] == run_id for entry in runs)


def test_compare_mode_returns_multiple_solver_results(client):
    _seed(client)
    r = client.post(
        "/api/compare",
        json={"time_limit": 3, "solvers": ["greedy", "cpsat"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["results"] and len(body["results"]) == 2
    assert body["best_solver"] in {"greedy", "cpsat"}
    assert isinstance(body["best_index"], int)
    assert body["results"][0]["grids"]


def test_compare_mode_rejects_invalid_solver(client):
    _seed(client)
    r = client.post(
        "/api/compare",
        json={"time_limit": 3, "solvers": ["greedy", "banana"]},
    )
    assert r.status_code == 400


def test_compare_mode_pipeline_solver_runs(client):
    """`pipeline` is in compare's DEFAULT solver list, so it must actually execute — regression for
    run_pipeline/PipelineConfig being used in the compare path without being imported (NameError)."""
    _seed(client)
    r = client.post("/api/compare", json={"time_limit": 2, "solvers": ["pipeline"]})
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["solver"] == "pipeline"
    assert result["stage_reports"]      # pipeline reports its per-stage track
    assert result["grids"]


def test_compare_mode_dedupes_and_defaults(client):
    _seed(client)
    dupes = client.post("/api/compare", json={"time_limit": 2, "solvers": ["greedy", "greedy"]})
    assert dupes.status_code == 200, dupes.text
    assert dupes.json()["solvers"] == ["greedy"]      # duplicates collapsed
    assert client.post("/api/compare", json={"time_limit": 2, "solvers": []}).status_code == 200


def test_export_xlsx_of_done_run(client):
    _seed(client)
    run_id = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3}).json()["run_id"]

    r = client.get(f"/api/runs/{run_id}/export.xlsx")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert len(r.content) > 0


def test_export_pdf_of_done_run(client):
    _seed(client)
    run_id = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3}).json()["run_id"]

    r = client.get(f"/api/runs/{run_id}/export.pdf")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert len(r.content) > 0


def test_export_missing_run_404(client):
    r = client.get("/api/runs/999999/export.xlsx")
    assert r.status_code == 404

    r = client.get("/api/runs/999999/export.pdf")
    assert r.status_code == 404


def test_export_not_done_run_409(client):
    with Session(get_engine()) as session:
        run = TimetableRun(status="queued", solver="greedy", time_limit=3, problem_snapshot={})
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    r = client.get(f"/api/runs/{run_id}/export.xlsx")
    assert r.status_code == 409

    r = client.get(f"/api/runs/{run_id}/export.pdf")
    assert r.status_code == 409


def test_adjust_run_returns_overlay(client):
    _seed(client)
    run_id = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3}).json()["run_id"]
    # rain from period 5 on Tuesday (day 1). `solver: greedy` keeps the test fast — the adjust
    # endpoint now runs a FULL-WEEK re-solve, so it would otherwise default to a 60s CP-SAT solve.
    r = client.post(f"/api/runs/{run_id}/adjust",
                    json={"day": 1, "from_period": 5, "reason": "rain",
                          "solver": "greedy", "time_limit_s": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disrupted_day"] == "Tuesday"
    assert body["grids"] and body["grids"]["divisions"]
    assert isinstance(body["moved"], list)
    assert len(body["affected_slot_ids"]) > 0  # a from-period cut blocks the tail of the day
    assert body["solver"] == "greedy"
    assert body["relaxed_days"] == [1]         # only the disrupted day, by default
    # anything the re-solve could not place is reported by name, never as a bare count
    assert len(body["unplaced_sessions"]) == body["dropped_count"]
    assert all(e["label"] for e in body["unplaced_sessions"])


def test_adjust_run_accepts_extra_relaxed_days(client):
    _seed(client)
    run_id = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3}).json()["run_id"]
    r = client.post(f"/api/runs/{run_id}/adjust",
                    json={"day": 3, "solver": "greedy", "time_limit_s": 5,
                          "extra_relaxed_days": [4]})
    assert r.status_code == 200, r.text
    assert r.json()["relaxed_days"] == [3, 4]


def test_adjust_run_validates_day_and_status(client):
    _seed(client)
    run_id = client.post("/api/runs", json={"solver": "greedy", "time_limit": 3}).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/adjust", json={"day": 9}).status_code == 400   # bad day
    assert client.post("/api/runs/9999/adjust", json={"day": 0}).status_code == 404          # missing run
    # unknown solver / out-of-range relaxed day are rejected before any solving happens
    assert client.post(f"/api/runs/{run_id}/adjust",
                       json={"day": 0, "solver": "nope"}).status_code == 400
    assert client.post(f"/api/runs/{run_id}/adjust",
                       json={"day": 0, "solver": "greedy", "extra_relaxed_days": [9]}).status_code == 400
    with Session(get_engine()) as s:
        s.add(TimetableRun(id=555, status="queued", solver="greedy", time_limit=3, problem_snapshot={}))
        s.commit()
    assert client.post("/api/runs/555/adjust", json={"day": 0}).status_code == 409            # not done
