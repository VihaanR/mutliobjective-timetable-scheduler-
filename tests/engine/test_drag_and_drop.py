from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select, create_engine
from engine.io_json import problem_from_dict, solution_from_dict
from engine.models import expand_requirements
from webapp.db import set_engine, init_db, get_engine
from webapp.models_db import Faculty, TimetableRun, ManualEdit
from webapp.server import app

@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "test_dd.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    set_engine(engine)
    init_db()
    with Session(engine) as s:
        s.add(Faculty(code="TESTFAC", name="Test Teacher"))
        s.commit()
    with TestClient(app) as c:
        c.post("/api/auth/bootstrap", json={"faculty_code": "TESTFAC", "email": "t@t.local", "password": "pass1234"})
        yield c
    engine.dispose()

def _seed(client):
    r = client.post("/api/seed/reference")
    assert r.status_code == 200, r.text

def _generate(client):
    r = client.post("/api/runs", json={"solver": "cpsat", "time_limit": 30})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    run = client.get(f"/api/runs/{run_id}").json()
    if run["status"] == "failed":
        pytest.skip(f"Solver failed to find a baseline solution: {run.get('error')}")
    assert run["status"] == "done", str(run)
    return run_id, run

def _first_movable(run_id):
    with Session(get_engine()) as session:
        run = session.get(TimetableRun, run_id)
    problem = problem_from_dict(run.problem_snapshot)
    solution = solution_from_dict(run.solution)
    reqs = {r.id: r for r in expand_requirements(problem)}
    by_s = solution.assignment_by_session()
    for rid, req in reqs.items():
        if req.is_break or req.fixed_time_slot_id is not None:
            continue
        a = by_s.get(rid)
        if a:
            return req, a, problem, solution
    pytest.fail("No movable session")

def _free_slot(problem, solution, div_id, req, exclude_sid):
    reqs = {r.id: r for r in expand_requirements(problem)}
    occ = set(); fac_at = {}; div_at = set()
    for a in solution.assignments:
        r2 = reqs.get(a.session_id)
        if r2 is None or r2.is_break:
            continue
        for k in range(r2.duration_slots):
            sid = a.time_slot_id + k
            occ.add((sid, a.room_id))
            if r2.faculty_id:
                fac_at.setdefault(sid, set()).add(r2.faculty_id)
            if r2.division_id == div_id:
                div_at.add(sid)
    rtype = getattr(req, "room_type", "classroom")
    for ts in problem.time_slots:
        if ts.id == exclude_sid or ts.id in div_at:
            continue
        if req.faculty_id and req.faculty_id in fac_at.get(ts.id, set()):
            continue
        for room in problem.rooms:
            if rtype not in ("none", "") and room.room_type != rtype:
                continue
            if (ts.id, room.id) in occ:
                continue
            return ts, room
    return None, None

def _move(client, run_id, req, ts, room):
    return client.post(f"/api/runs/{run_id}/move-session", json={
        "session_id": req.id,
        "target_day": ts.day,
        "target_period": ts.period,
        "target_room_id": room.id,
    })

def test_move_returns_updated_grids(client):
    _seed(client); run_id, _ = _generate(client)
    req, a, prob, sol = _first_movable(run_id)
    ts, room = _free_slot(prob, sol, req.division_id, req, a.time_slot_id)
    if ts is None:
        pytest.skip("No free slot")
    r = _move(client, run_id, req, ts, room)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["moved_session"] == req.id
    assert body["to_slot_id"] == ts.id
    assert body["to_room_id"] == room.id
    assert isinstance(body["hard"], int)
    assert body["grids"] and body["grids"]["divisions"]

def test_move_updates_stored_solution(client):
    _seed(client); run_id, _ = _generate(client)
    req, a, prob, sol = _first_movable(run_id)
    ts, room = _free_slot(prob, sol, req.division_id, req, a.time_slot_id)
    if ts is None:
        pytest.skip("No free slot")
    r = _move(client, run_id, req, ts, room)
    assert r.status_code == 200, r.text
    updated = client.get(f"/api/runs/{run_id}").json()
    assert updated["grids"] is not None

def test_manual_edit_row_persisted(client):
    _seed(client); run_id, _ = _generate(client)
    req, a, prob, sol = _first_movable(run_id)
    ts, room = _free_slot(prob, sol, req.division_id, req, a.time_slot_id)
    if ts is None:
        pytest.skip("No free slot")
    r = _move(client, run_id, req, ts, room)
    assert r.status_code == 200, r.text
    with Session(get_engine()) as session:
        edits = session.exec(select(ManualEdit).where(ManualEdit.run_id == run_id)).all()
    assert len(edits) == 1
    e = edits[0]
    assert e.session_id == req.id
    assert e.to_slot_id == ts.id
    assert e.to_room_id == room.id
    assert e.from_slot_id == a.time_slot_id
    assert e.from_room_id == a.room_id

def test_unknown_session_id_400(client):
    _seed(client); run_id, _ = _generate(client)
    r = client.post(f"/api/runs/{run_id}/move-session",
                    json={"session_id": "NONEXISTENT", "target_day": 0, "target_period": 0, "target_room_id": "R1"})
    assert r.status_code == 400

def test_break_session_rejected(client):
    _seed(client); run_id, _ = _generate(client)
    with Session(get_engine()) as session:
        run = session.get(TimetableRun, run_id)
    prob = problem_from_dict(run.problem_snapshot)
    brk = next(r for r in expand_requirements(prob) if r.is_break)
    r = client.post(f"/api/runs/{run_id}/move-session",
                    json={"session_id": brk.id, "target_day": 0, "target_period": 0, "target_room_id": "R1"})
    assert r.status_code == 400
    assert "break" in r.json()["detail"].lower()

def test_nonexistent_slot_400(client):
    _seed(client); run_id, _ = _generate(client)
    req, _, _, _ = _first_movable(run_id)
    r = client.post(f"/api/runs/{run_id}/move-session",
                    json={"session_id": req.id, "target_day": 99, "target_period": 99, "target_room_id": "R1"})
    assert r.status_code == 400
    assert "No time slot" in r.json()["detail"]

def test_nonexistent_room_400(client):
    _seed(client); run_id, _ = _generate(client)
    req, _, prob, _ = _first_movable(run_id)
    slot = prob.time_slots[0]
    r = client.post(f"/api/runs/{run_id}/move-session",
                    json={"session_id": req.id, "target_day": slot.day, "target_period": slot.period, "target_room_id": "GHOST_ROOM"})
    assert r.status_code == 400
    assert "not found" in r.json()["detail"].lower()

def test_wrong_room_type_rejected(client):
    _seed(client); run_id, _ = _generate(client)
    with Session(get_engine()) as session:
        run = session.get(TimetableRun, run_id)
    prob = problem_from_dict(run.problem_snapshot)
    reqs = expand_requirements(prob)
    theory_req = next((r for r in reqs if r.session_type.value == "Theory" and not r.is_break), None)
    labs = [r for r in prob.rooms if r.room_type == "lab"]
    if theory_req is None or not labs:
        pytest.skip("Need theory + lab")
    slot = prob.time_slots[0]
    r = client.post(f"/api/runs/{run_id}/move-session",
                    json={"session_id": theory_req.id, "target_day": slot.day, "target_period": slot.period, "target_room_id": labs[0].id})
    assert r.status_code == 400

def test_run_not_found_404(client):
    r = client.post("/api/runs/999999/move-session",
                    json={"session_id": "X", "target_day": 0, "target_period": 0, "target_room_id": "R1"})
    assert r.status_code == 404

def test_run_not_done_409(client):
    with Session(get_engine()) as session:
        run = TimetableRun(status="queued", solver="greedy", time_limit=3, problem_snapshot={})
        session.add(run); session.commit(); session.refresh(run)
        run_id = run.id
    r = client.post(f"/api/runs/{run_id}/move-session",
                    json={"session_id": "X", "target_day": 0, "target_period": 0, "target_room_id": "R1"})
    assert r.status_code == 409

def test_run_done_no_solution_409(client):
    _seed(client)
    with Session(get_engine()) as session:
        run = TimetableRun(status="done", solver="greedy", time_limit=3, problem_snapshot={}, solution=None)
        session.add(run); session.commit(); session.refresh(run)
        run_id = run.id
    r = client.post(f"/api/runs/{run_id}/move-session",
                    json={"session_id": "X", "target_day": 0, "target_period": 0, "target_room_id": "R1"})
    assert r.status_code == 409

def test_anonymous_rejected():
    with TestClient(app) as anon:
        r = anon.post("/api/runs/1/move-session",
                      json={"session_id": "X", "target_day": 0, "target_period": 0, "target_room_id": "R1"})
    assert r.status_code == 401

@pytest.mark.parametrize("dataset", ["reference", "sy-sem3", "ty-sem5", "btech-sem7"])
def test_move_works_for_each_semester(client, dataset):
    """Drag-and-drop works for every seeded semester branch."""
    r = client.post(f"/api/seed/{dataset}")
    if r.status_code == 404:
        pytest.skip(f"Dataset {dataset!r} not registered")
    assert r.status_code in (200, 409), f"Seed {r.status_code}: {r.text}"
    run_id, _ = _generate(client)
    req, a, prob, sol = _first_movable(run_id)
    ts, room = _free_slot(prob, sol, req.division_id, req, a.time_slot_id)
    if ts is None:
        pytest.skip(f"No free slot for {dataset!r}")
    r = _move(client, run_id, req, ts, room)
    assert r.status_code == 200, f"{dataset!r}: {r.text}"
    assert r.json()["status"] == "ok"
    assert r.json()["grids"] is not None