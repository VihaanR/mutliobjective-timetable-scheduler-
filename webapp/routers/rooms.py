"""Room CRUD (global — shared across branches) + Room & Lab Availability Finder."""
from __future__ import annotations

import re
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from webapp.auth import require_faculty
from webapp.db import get_session
from webapp.models_db import Room, RoomCreate, RoomUpdate, TimetableRun
from webapp.routers._crud import apply_update, get_or_404, unique_or_400

router = APIRouter(prefix="/api", tags=["rooms"])

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_MAP = {
    "monday": 0, "mon": 0, "0": 0,
    "tuesday": 1, "tue": 1, "1": 1,
    "wednesday": 2, "wed": 2, "2": 2,
    "thursday": 3, "thu": 3, "3": 3,
    "friday": 4, "fri": 4, "4": 4,
    "saturday": 5, "sat": 5, "5": 5,
    "sunday": 6, "sun": 6, "6": 6,
}


# ---------------------------------------------------------------- Room Availability Helpers & Models
class RoomAvailabilityQuery(BaseModel):
    date: Optional[str] = None
    day: Optional[str] = None
    timeslot: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    run_id: Optional[int] = None


def parse_time_str(t: str) -> int | None:
    if not t:
        return None
    t = str(t).strip().upper()
    # Strip enclosing parentheses or brackets if present
    t = re.sub(r"^[(\[\{]\s*", "", t)
    t = re.sub(r"\s*[)\]\}]$", "", t)
    t = t.strip()

    m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", t)
    if not m:
        m = re.match(r"^(\d{1,2})\s*(AM|PM)?$", t)
        if not m:
            return None
        hh, mm, ampm = int(m.group(1)), 0, m.group(2)
    else:
        hh, mm, ampm = int(m.group(1)), int(m.group(2)), m.group(3)

    if ampm == "PM" and hh < 12:
        hh += 12
    elif ampm == "AM" and hh == 12:
        hh = 0
    elif ampm is None:
        # In college operating hours context (8:00 AM to 7:00 PM), 1..7 means 13:00..19:00
        if 1 <= hh <= 7:
            hh += 12

    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    total_mins = hh * 60 + mm
    # Strict college operating hours: 8:00 AM (480 mins) to 7:00 PM (1140 mins)
    if total_mins < 480 or total_mins > 1140:
        return None
    return total_mins


def parse_timeslot_range(slot_str: str) -> tuple[int | None, int | None, str, str]:
    """Parse timeslot string with hyphen or separator."""
    s = str(slot_str).strip()
    parts = []
    if " -- " in s:
        parts = s.split(" -- ", 1)
    elif "--" in s:
        parts = s.split("--", 1)
    elif " - " in s:
        parts = s.split(" - ", 1)
    elif "-" in s and not s.startswith("-"):
        parts = s.split("-", 1)
    elif " to " in s.lower():
        parts = re.split(r"\s+to\s+", s, flags=re.IGNORECASE)

    if len(parts) == 2:
        st_str, et_str = parts[0].strip(), parts[1].strip()
        return parse_time_str(st_str), parse_time_str(et_str), st_str, et_str
    return None, None, slot_str, ""


def format_minutes_display(mins: int) -> str:
    hh = mins // 60
    mm = mins % 60
    ampm = "AM" if hh < 12 else "PM"
    disp_h = hh if 1 <= hh <= 12 else (hh - 12 if hh > 12 else 12)
    return f"{disp_h:02d}:{mm:02d} {ampm}"


# ---------------------------------------------------------------- List / Create / Availability Routes
@router.get("/rooms")
def list_rooms(session: Session = Depends(get_session), _=Depends(require_faculty)):
    rows = session.exec(select(Room)).all()
    return sorted(rows, key=lambda r: (r.room_type, r.name))


@router.post("/rooms", status_code=201)
def create_room(body: RoomCreate, session: Session = Depends(get_session), _=Depends(require_faculty)):
    if body.room_type not in ("classroom", "lab"):
        raise HTTPException(status_code=400, detail="room_type must be 'classroom' or 'lab'")
    unique_or_400(session, Room, "code", body.code)
    room = Room.model_validate(body)
    session.add(room)
    session.commit()
    session.refresh(room)
    return room


@router.post("/rooms/availability")
def check_availability_post(body: RoomAvailabilityQuery, session: Session = Depends(get_session)):
    st = body.start_time
    et = body.end_time
    if body.timeslot:
        _, _, s_raw, e_raw = parse_timeslot_range(body.timeslot)
        st, et = s_raw, e_raw
    if not st and not et:
        st, et = "08:00", "10:00"
    return _compute_room_availability(body.date, body.day, st or "", et or "", body.run_id, session)


@router.get("/rooms/availability")
def check_availability_get(
    date: Optional[str] = None,
    day: Optional[str] = None,
    timeslot: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    run_id: Optional[int] = None,
    session: Session = Depends(get_session),
):
    st = start_time
    et = end_time
    if timeslot:
        _, _, s_raw, e_raw = parse_timeslot_range(timeslot)
        st, et = s_raw, e_raw
    if not st and not et:
        st, et = "08:00", "10:00"
    return _compute_room_availability(date, day, st or "", et or "", run_id, session)


# ---------------------------------------------------------------- Parameterized Room Routes
@router.get("/rooms/{room_id}")
def get_room(room_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    return get_or_404(session, Room, room_id)


@router.patch("/rooms/{room_id}")
def update_room(room_id: int, body: RoomUpdate, session: Session = Depends(get_session), _=Depends(require_faculty)):
    room = get_or_404(session, Room, room_id)
    if body.room_type is not None and body.room_type not in ("classroom", "lab"):
        raise HTTPException(status_code=400, detail="room_type must be 'classroom' or 'lab'")
    if body.code is not None and body.code != room.code:
        unique_or_400(session, Room, "code", body.code, exclude_id=room_id)
    apply_update(room, body)
    session.commit()
    session.refresh(room)
    return room


@router.delete("/rooms/{room_id}")
def delete_room(room_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    room = get_or_404(session, Room, room_id)
    session.delete(room)
    session.commit()
    return {"ok": True}


# ---------------------------------------------------------------- Availability Computation Engine
def _compute_room_availability(
    date_str: Optional[str],
    day_str: Optional[str],
    start_time_str: str,
    end_time_str: str,
    run_id: Optional[int],
    session: Session,
) -> dict:
    from datetime import datetime

    start_mins = parse_time_str(start_time_str)
    end_mins = parse_time_str(end_time_str)

    if start_mins is None or end_mins is None or start_mins >= end_mins:
        return {
            "valid": False,
            "error": "invalid timeslot",
            "message": "Invalid timeslot",
            "provided_start": start_time_str,
            "provided_end": end_time_str,
            "available_classrooms": [],
            "available_labs": [],
            "occupied_rooms": [],
        }

    day_idx = 0
    normalized_day = "Monday"
    formatted_date = None

    if date_str:
        d_clean = str(date_str).strip()
        parsed_dt = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                parsed_dt = datetime.strptime(d_clean, fmt)
                break
            except ValueError:
                continue
        if parsed_dt:
            day_idx = parsed_dt.weekday()  # Mon=0..Sun=6
            normalized_day = DAY_NAMES[day_idx]
            formatted_date = parsed_dt.strftime("%d %b %Y")
    elif day_str:
        day_key = str(day_str).strip().lower()
        day_idx = DAY_MAP.get(day_key, 0)
        normalized_day = DAY_NAMES[day_idx]

    runs_to_scan = []
    if run_id:
        r_single = session.get(TimetableRun, run_id)
        if r_single:
            runs_to_scan.append(r_single)
    else:
        # Aggregate across all completed timetable runs so all generated years/branches are accounted for
        runs_to_scan = session.exec(
            select(TimetableRun).where(TimetableRun.status == "done").order_by(TimetableRun.created_at.desc())
        ).all()

    all_rooms = session.exec(select(Room)).all()
    room_by_code = {r.code.lower(): r for r in all_rooms}
    room_by_id = {r.id: r for r in all_rooms}
    room_by_str_id = {str(r.id): r for r in all_rooms}
    room_by_name = {r.name.lower(): r for r in all_rooms}

    # Determine period slot mapping from runs if available, else standard 1-hr periods
    period_intervals = []
    found_periods = False
    for r_obj in runs_to_scan:
        if r_obj.grids and "periods" in r_obj.grids:
            for p_meta in r_obj.grids["periods"]:
                p_num = p_meta.get("period", 0)
                p_start_m = parse_time_str(p_meta.get("start", f"{8+p_num:02d}:00")) or (480 + p_num * 60)
                p_end_m = parse_time_str(p_meta.get("end", f"{9+p_num:02d}:00")) or (p_start_m + 60)
                period_intervals.append((p_num, p_start_m, p_end_m, p_meta.get("start", ""), p_meta.get("end", "")))
            found_periods = True
            break

    if not found_periods:
        for p in range(11):  # 08:00 to 19:00 (periods 0..10)
            p_start_m = 480 + p * 60
            p_end_m = p_start_m + 60
            if p_start_m >= 1140:
                break
            period_intervals.append((
                p, p_start_m, p_end_m,
                f"{p_start_m//60:02d}:{p_start_m%60:02d}",
                f"{p_end_m//60:02d}:{p_end_m%60:02d}",
            ))

    # Overlapping periods with queried [start_mins, end_mins]
    overlapping_periods = []
    for p_num, p_s, p_e, p_s_str, p_e_str in period_intervals:
        if max(p_s, start_mins) < min(p_e, end_mins):
            overlapping_periods.append({
                "period": p_num,
                "start": p_s_str,
                "end": p_e_str,
                "start_mins": p_s,
                "end_mins": p_e,
            })

    # Group occupied entries from run.grids across all completed timetable runs
    room_occupancies: dict[int, list[dict]] = {r.id: [] for r in all_rooms}
    for run in runs_to_scan:
        if not run.grids:
            continue

        # 1. Scan room-specific grids
        for room_list in (run.grids.get("classrooms", []), run.grids.get("labs", []), run.grids.get("rooms", [])):
            for r_entry in room_list:
                r_id_raw = str(r_entry.get("id") or "").strip().lower()
                r_name_raw = str(r_entry.get("name") or "").strip().lower()
                matched = room_by_code.get(r_id_raw) or room_by_str_id.get(r_id_raw) or room_by_name.get(r_name_raw)
                if not matched:
                    continue
                target_room_id = matched.id
                cells = r_entry.get("cells", {})
                for op in overlapping_periods:
                    p = op["period"]
                    cell_key = f"{day_idx}_{p}"
                    sessions_in_cell = cells.get(cell_key, [])
                    for s in sessions_in_cell:
                        if not s.get("is_break"):
                            already = any(
                                occ["period"] == p and occ["course"] == (s.get("course") or s.get("course_code")) and occ.get("run_id") == run.id
                                for occ in room_occupancies[target_room_id]
                            )
                            if not already:
                                room_occupancies[target_room_id].append({
                                    "period": p,
                                    "period_time": f"{op['start']} - {op['end']}",
                                    "course": s.get("course") or s.get("course_code") or "Academic Session",
                                    "faculty": s.get("faculty_name") or s.get("faculty") or "Faculty",
                                    "class_label": s.get("class_label") or s.get("division_name") or s.get("batch") or (f"Run #{run.id}" if run.label else ""),
                                    "type": s.get("type") or "Lecture",
                                    "run_id": run.id,
                                })

        # 2. Also scan division grids
        for div_entry in run.grids.get("divisions", []):
            div_label = div_entry.get("id") or div_entry.get("name") or ""
            div_cells = div_entry.get("cells", {})
            for op in overlapping_periods:
                p = op["period"]
                cell_key = f"{day_idx}_{p}"
                for s in div_cells.get(cell_key, []):
                    if s.get("is_break"):
                        continue
                    r_code_raw = str(s.get("room") or s.get("room_name") or "").strip().lower()
                    if not r_code_raw:
                        continue
                    matched = room_by_code.get(r_code_raw) or room_by_name.get(r_code_raw) or room_by_str_id.get(r_code_raw)
                    if not matched:
                        continue
                    target_room_id = matched.id
                    already = any(
                        occ["period"] == p and occ["course"] == (s.get("course") or s.get("course_code")) and occ.get("run_id") == run.id
                        for occ in room_occupancies[target_room_id]
                    )
                    if not already:
                        room_occupancies[target_room_id].append({
                            "period": p,
                            "period_time": f"{op['start']} - {op['end']}",
                            "course": s.get("course") or s.get("course_code") or "Academic Session",
                            "faculty": s.get("faculty_name") or s.get("faculty") or "Faculty",
                            "class_label": s.get("class_label") or div_label or (f"Run #{run.id}" if run.label else ""),
                            "type": s.get("type") or "Lecture",
                            "run_id": run.id,
                        })

    available_classrooms = []
    available_labs = []
    occupied_classrooms = []
    occupied_labs = []

    for room in sorted(all_rooms, key=lambda r: (r.room_type, r.name)):
        occupancy = room_occupancies.get(room.id, [])
        is_free = len(occupancy) == 0
        room_dict = {
            "id": room.id,
            "code": room.code,
            "name": room.name,
            "capacity": room.capacity,
            "room_type": room.room_type,
            "building": "Lab Wing" if room.room_type == "lab" else "Main Wing",
            "floor": "Floor 2" if room.room_type == "lab" else "Floor 1",
            "is_available": is_free,
            "occupancy": occupancy,
        }
        if room.room_type == "lab":
            if is_free:
                available_labs.append(room_dict)
            else:
                occupied_labs.append(room_dict)
        else:
            if is_free:
                available_classrooms.append(room_dict)
            else:
                occupied_classrooms.append(room_dict)

    return {
        "valid": True,
        "date": formatted_date or date_str,
        "day": normalized_day,
        "day_index": day_idx,
        "start_time": start_time_str,
        "end_time": end_time_str,
        "start_time_formatted": format_minutes_display(start_mins),
        "end_time_formatted": format_minutes_display(end_mins),
        "time_range_display": f"{format_minutes_display(start_mins)} - {format_minutes_display(end_mins)}",
        "overlapping_periods": [op["period"] for op in overlapping_periods],
        "overlapping_periods_detail": overlapping_periods,
        "available_classrooms": available_classrooms,
        "available_labs": available_labs,
        "occupied_classrooms": occupied_classrooms,
        "occupied_labs": occupied_labs,
        "summary": {
            "total_classrooms": len(available_classrooms) + len(occupied_classrooms),
            "available_classrooms_count": len(available_classrooms),
            "occupied_classrooms_count": len(occupied_classrooms),
            "total_labs": len(available_labs) + len(occupied_labs),
            "available_labs_count": len(available_labs),
            "occupied_labs_count": len(occupied_labs),
            "total_rooms": len(all_rooms),
            "total_available_count": len(available_classrooms) + len(available_labs),
            "total_occupied_count": len(occupied_classrooms) + len(occupied_labs),
        },
        "timetable_runs_scanned": len(runs_to_scan),
        "timetable_run_id": runs_to_scan[0].id if runs_to_scan else None,
        "timetable_run_label": ", ".join(r.label for r in runs_to_scan if r.label) if runs_to_scan else "No Timetable Runs",
    }
