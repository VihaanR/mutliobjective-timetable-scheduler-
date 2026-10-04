"""API tests for Room & Lab Availability Finder with Date and Two Time Inputs."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine

from webapp.db import set_engine, init_db
from webapp.models_db import Faculty, Room
from webapp.server import app


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "test_room_avail.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    set_engine(engine)
    init_db()
    with Session(engine) as session:
        session.add(Faculty(code="TESTFAC", name="Test Teacher"))
        session.add(Room(code="CR51", name="Classroom 51", capacity=70, room_type="classroom"))
        session.add(Room(code="CR52", name="Classroom 52", capacity=70, room_type="classroom"))
        session.add(Room(code="LAB1", name="Computing Lab 1", capacity=30, room_type="lab"))
        session.add(Room(code="LAB2", name="Computing Lab 2", capacity=30, room_type="lab"))
        session.commit()
    with TestClient(app) as c:
        yield c
    engine.dispose()


def test_room_availability_date_and_two_times(client):
    # Valid date and two time inputs (8:00 AM to 10:00 AM)
    r = client.post("/api/rooms/availability", json={
        "date": "2026-10-05",  # 2026-10-05 is a Monday
        "start_time": "8:00 AM",
        "end_time": "10:00 AM",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is True
    assert data["day"] == "Monday"
    assert data["summary"]["available_classrooms_count"] == 2
    assert data["summary"]["available_labs_count"] == 2

    # 24-hr format (08:00 to 19:00)
    r2 = client.post("/api/rooms/availability", json={
        "date": "2026-10-07",  # Wednesday
        "start_time": "08:00",
        "end_time": "19:00",
    })
    assert r2.status_code == 200
    assert r2.json()["valid"] is True
    assert r2.json()["day"] == "Wednesday"


def test_room_availability_invalid_ranges(client):
    # Before 8:00 AM
    r1 = client.post("/api/rooms/availability", json={
        "date": "2026-10-05",
        "start_time": "7:00 AM",
        "end_time": "9:00 AM",
    })
    assert r1.status_code == 200
    data1 = r1.json()
    assert data1["valid"] is False
    assert data1["error"] == "invalid timeslot"

    # After 7:00 PM (19:00)
    r2 = client.post("/api/rooms/availability", json={
        "date": "2026-10-05",
        "start_time": "5:00 PM",
        "end_time": "8:00 PM",
    })
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["valid"] is False
    assert data2["error"] == "invalid timeslot"

    # Start time after end time
    r3 = client.post("/api/rooms/availability", json={
        "date": "2026-10-05",
        "start_time": "3:00 PM",
        "end_time": "1:00 PM",
    })
    assert r3.status_code == 200
    data3 = r3.json()
    assert data3["valid"] is False
    assert data3["error"] == "invalid timeslot"
