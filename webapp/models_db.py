"""SQLModel table definitions (design.md §4.2).

P1 covers the editable entity tables that feed the engine snapshot: branch -> division ->
allocation, plus global faculty / room / slot_template. Calendar, run, and adjustment tables are
added in their own phases (P2/P3/P4). Enum-valued columns (program, category, room_type) are stored
as plain strings and validated against the engine enums at the router layer, keeping the schema
portable.

Each entity follows the SQLModel pattern: a `*Base` with the shared fields, a `table=True` model
adding the primary key + FKs, a `*Create` for POST bodies, and a `*Update` (all-optional) for PUT.
The base doubles as the response schema.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


# --------------------------------------------------------------------------- branch
class BranchBase(SQLModel):
    code: str = Field(index=True)
    name: str
    semester_label: str = ""
    # A "branch" row is really one (department, year, semester) triple -- the code string
    # ("CSE-DS-TY-SEM5") encodes all three, which is unreadable in a picker and forces the UI to
    # parse identifiers. Stored as separate fields so the UI can offer three plain dropdowns and
    # resolve them back to exactly one row.
    department: str = ""        # "CSE-DS"
    department_name: str = ""   # "Computer Science & Engineering (Data Science)"
    year_label: str = ""        # "SY" | "TY" | "BTech"
    year_name: str = ""         # "Second Year"
    semester: int = 0           # 4


class Branch(BranchBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # Weekdays (0=Mon..4=Fri) this branch does not run normal teaching on -- e.g. Final Year
    # reserves Friday for Final Project / Research Work. Exempts the day from the day-shaped
    # hard rules (labs-every-day, 6-8h daily load) instead of pretending it is a teaching day,
    # which would make such a branch unsolvable. Fed to ProblemInstance.relaxed_days.
    relaxed_days: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    # Caveat surfaced prominently in the UI whenever this branch is selected -- for known gaps
    # in the modelling that a generated timetable would otherwise hide (e.g. a division missing
    # because it is on OJT). Empty means no caveat. Deliberately a warning banner, not a code
    # comment: a plausible-looking timetable is exactly when an unmodelled assumption is most
    # likely to be forgotten.
    notice: str = ""


class BranchCreate(BranchBase):
    pass


class BranchUpdate(SQLModel):
    code: Optional[str] = None
    name: Optional[str] = None
    semester_label: Optional[str] = None


# --------------------------------------------------------------------------- division
class DivisionBase(SQLModel):
    name: str                       # "D1"
    program: str = "FYUP"           # ProgramType value
    semester: int = 1
    student_count: int = 60
    batch1_name: str = ""           # default {name}1 applied in problem_builder if blank
    batch2_name: str = ""


class Division(DivisionBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # branch_id lives on the table + response, but is supplied by the URL path, not the POST body
    branch_id: int = Field(foreign_key="branch.id", index=True)


class DivisionCreate(DivisionBase):
    pass  # branch_id comes from the path (POST /api/branches/{id}/divisions)


class DivisionUpdate(SQLModel):
    name: Optional[str] = None
    program: Optional[str] = None
    semester: Optional[int] = None
    student_count: Optional[int] = None
    batch1_name: Optional[str] = None
    batch2_name: Optional[str] = None


# --------------------------------------------------------------------------- faculty (global)
class FacultyBase(SQLModel):
    code: str = Field(index=True)   # "AR"
    name: str
    max_load_hours_per_week: int = 20
    max_consecutive_sessions: int = 2
    email: Optional[str] = None     # login identity (Auth, design.md §11) - nullable until a
                                     # teacher sets credentials for this row


class Faculty(FacultyBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    unavailable_slot_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    # NEVER on FacultyBase (which doubles as the read/response schema) - only ever read/written by
    # webapp/auth.py and webapp/routers/auth.py. Every route returning a Faculty row MUST use
    # response_model=FacultyPublic below so this never serializes.
    password_hash: Optional[str] = None


class FacultyCreate(FacultyBase):
    unavailable_slot_ids: list[int] = []
    password: Optional[str] = None   # plaintext in; hashed by the router, never stored as-is


class FacultyUpdate(SQLModel):
    code: Optional[str] = None
    name: Optional[str] = None
    max_load_hours_per_week: Optional[int] = None
    max_consecutive_sessions: Optional[int] = None
    unavailable_slot_ids: Optional[list[int]] = None
    email: Optional[str] = None
    password: Optional[str] = None   # if provided, re-hash and replace; omitted = unchanged


class FacultyPublic(FacultyBase):
    id: int
    unavailable_slot_ids: list[int] = []


# --------------------------------------------------------------------------- course (branch-scoped)
class CourseBase(SQLModel):
    branch_id: int = Field(foreign_key="branch.id", index=True)
    code: str = Field(index=True)
    title: str
    credits: int = 3
    category: str = "Major"         # CourseCategory value
    theory_per_week: int = 0
    practical_per_week: int = 0
    tutorial_per_week: int = 0
    is_heavy: bool = False


class Course(CourseBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class CourseCreate(CourseBase):
    pass


class CourseUpdate(SQLModel):
    code: Optional[str] = None
    title: Optional[str] = None
    credits: Optional[int] = None
    category: Optional[str] = None
    theory_per_week: Optional[int] = None
    practical_per_week: Optional[int] = None
    tutorial_per_week: Optional[int] = None
    is_heavy: Optional[bool] = None


# --------------------------------------------------------------------------- allocation
class AllocationBase(SQLModel):
    division_id: int = Field(foreign_key="division.id", index=True)
    course_id: int = Field(foreign_key="course.id", index=True)
    faculty_id: Optional[int] = Field(default=None, foreign_key="faculty.id")
    batch1_faculty_id: Optional[int] = Field(default=None, foreign_key="faculty.id")
    batch2_faculty_id: Optional[int] = Field(default=None, foreign_key="faculty.id")


class Allocation(AllocationBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class AllocationCreate(AllocationBase):
    pass


class AllocationUpdate(SQLModel):
    faculty_id: Optional[int] = None
    batch1_faculty_id: Optional[int] = None
    batch2_faculty_id: Optional[int] = None


# --------------------------------------------------------------------------- room (global)
class RoomBase(SQLModel):
    code: str = Field(index=True)
    name: str
    capacity: int = 60
    room_type: str = "classroom"    # "classroom" | "lab"


class Room(RoomBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class RoomCreate(RoomBase):
    pass


class RoomUpdate(SQLModel):
    code: Optional[str] = None
    name: Optional[str] = None
    capacity: Optional[int] = None
    room_type: Optional[str] = None


# --------------------------------------------------------------------------- slot_template (global weekly grid)
class SlotTemplateBase(SQLModel):
    day: int                        # 0=Mon .. 4=Fri
    period: int                     # 0-based within the day
    start: str
    end: str


class SlotTemplate(SlotTemplateBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class SlotTemplateCreate(SlotTemplateBase):
    pass


# --------------------------------------------------------------------------- timetable_run (P2)
class TimetableRun(SQLModel, table=True):
    """A generate job record (design.md §4.2). `problem_snapshot` makes the run reproducible
    (and is what `disruption.replan` will consume once P4's persistence lands); `solution`/
    `grids`/`stage_reports` are populated by `webapp.jobs.run_generation` once the background
    solve finishes. `status` moves queued -> running -> done/failed."""

    id: Optional[int] = Field(default=None, primary_key=True)
    label: str = ""
    solver: str
    time_limit: float
    status: str = "queued"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    problem_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # Which branches this run actually covered. An EMPTY list means the whole institution (every
    # branch) -- the same convention `problem_builder.build_problem_dict(branch_ids=None)` uses.
    # Recorded because a run is otherwise indistinguishable from any other once stored, and the
    # per-teacher view has to answer "what is the newest run for EACH branch?" (webapp/routers/
    # faculty.py) rather than just "what is the newest run?".
    branch_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    # engine division id -> the branch identity behind it ({branch_id, branch_code, department,
    # year_label, year_name, semester, division_name}). The engine only knows an opaque division
    # id string, so without this a stored grid cannot say which year/semester a session belongs to.
    division_meta: dict = Field(default_factory=dict, sa_column=Column(JSON))
    solution: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    grids: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    stage_reports: Optional[list] = Field(default=None, sa_column=Column(JSON))
    hard: Optional[int] = None
    soft: Optional[float] = None
    wall_clock: Optional[float] = None    # total solve wall-clock seconds (pipeline total, or solver's)
    error: Optional[str] = None


# --------------------------------------------------------------------------- pareto_run
# Job record for the generic epsilon-constraint Pareto sweep (engine/pareto_sweep.py), mirroring
# TimetableRun's queued -> running -> done/failed shape. Kept as its own table (not reusing
# TimetableRun) because a sweep result is a list of frontier points per objective pair, not a
# single timetable solution.
class ParetoRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    label: str = ""
    branch_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    time_limit_s: float = 30.0
    sweep_points: int = 5
    status: str = "queued"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    points: Optional[dict] = Field(default=None, sa_column=Column(JSON))  # {pair_label: [point, ...]}
    error: Optional[str] = None


# --------------------------------------------------------------------------- term (P3)
# Dates are stored as ISO-8601 strings ("YYYY-MM-DD"), matching SlotTemplate's start/end
# convention above (plain strings, not SQLAlchemy Date columns) - keeps the schema portable and
# side-steps driver-specific date-adapter behavior for a field the engine never touches directly
# (the engine has no date axis - CLAUDE.md §12/design.md §12).
class TermBase(SQLModel):
    name: str                       # "Sem IV Jan-May 2026"
    start_date: str                 # ISO date "YYYY-MM-DD"
    end_date: str                   # ISO date "YYYY-MM-DD"


class Term(TermBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class TermCreate(TermBase):
    pass


class TermUpdate(SQLModel):
    name: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


# --------------------------------------------------------------------------- calendar_upload (P3)
# The raw artifact record - design.md §4.2: "never modified or deleted by parsing". No Update/
# Create-from-JSON schema: rows are only ever inserted by the multipart upload handler in
# webapp/routers/calendar.py, and are otherwise read-only.
class CalendarUploadBase(SQLModel):
    filename: str
    mime: str                       # detected from magic bytes, not the client's claimed type
    path: str                       # absolute path under webapp/uploads/ (or TIMETABLE_UPLOADS_PATH)
    sha256: str


class CalendarUpload(CalendarUploadBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)


# --------------------------------------------------------------------------- calendar_event (P3)
# Trust rule (CLAUDE.md §12/design.md §6): machine-extracted data is never trusted. Only
# webapp/routers/calendar.py's confirm endpoint may ever set confirmed=True; the extraction path
# and the manual-create path both always insert confirmed=False, enforced in the router, not here.
class CalendarEventBase(SQLModel):
    term_id: int = Field(foreign_key="term.id", index=True)
    date: str                       # ISO date "YYYY-MM-DD"
    name: str
    kind: str = "holiday"           # "holiday" | "exam" | "event"
    source: str = "manual"          # "manual" | "extracted"
    confirmed: bool = False


class CalendarEvent(CalendarEventBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)


class CalendarEventCreate(CalendarEventBase):
    pass


class CalendarEventUpdate(SQLModel):
    # No `source` or `confirmed` here by design: provenance is fixed at creation, and confirmation
    # only ever happens through PUT /api/calendar/events/{id}/confirm (the trust rule's one door).
    term_id: Optional[int] = None
    date: Optional[str] = None
    name: Optional[str] = None
    kind: Optional[str] = None


# --------------------------------------------------------------------------- student (Auth)
# Individual student roster + login, added alongside Faculty credentials (design.md §11 Auth).
# division_id is the DB Division row's int PK - NOT the engine's string division id ("D1", stored
# on Division.name). Resolving "my division's timetable" means looking up that row's `.name`.
class StudentBase(SQLModel):
    roll_no: str = Field(index=True, unique=True)
    name: str
    division_id: int = Field(foreign_key="division.id", index=True)
    email: str = Field(unique=True, index=True)


class Student(StudentBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # NEVER on StudentBase - see the identical FacultyPublic/password_hash note above.
    password_hash: str


class StudentCreate(StudentBase):
    password: str   # plaintext in; hashed by the router, never stored as-is


class StudentUpdate(SQLModel):
    roll_no: Optional[str] = None
    name: Optional[str] = None
    division_id: Optional[int] = None
    email: Optional[str] = None
    password: Optional[str] = None   # if provided, re-hash and replace; omitted = unchanged


class StudentPublic(StudentBase):
    id: int


# --------------------------------------------------------------------------- app secret (Auth)
# One-row table holding the HMAC signing secret for session tokens (webapp/auth.py). Generated
# once via secrets.token_hex(32) and reused after - stored in the DB rather than a file so it
# survives restarts without adding a third allowed write location beyond platform.db/uploads
# (CLAUDE.md §15).
class AppSecret(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    value: str


# --------------------------------------------------------------------------- manual edit (drag-and-drop)
# A dated overlay on a run's solution, shaped like design.md's never-built `adjustment` table -
# the master run/solution is never mutated (same philosophy as the disruption engine). `status`
# supports the same active/reverted undo pattern already fully specified for `adjustment`.
class ManualEdit(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="timetablerun.id", index=True)
    session_id: str
    from_slot_id: Optional[int] = None
    from_room_id: Optional[str] = None
    to_slot_id: int
    to_room_id: str
    edited_by_faculty_id: Optional[int] = None   # DB Faculty.id (int PK) - not the engine code
    status: str = "active"                        # "active" | "reverted"
    created_at: datetime = Field(default_factory=datetime.utcnow)
