# Agent Guide & Repository Architecture — Multiobjective Timetable Scheduler

> **Target Audience:** Autonomous AI coding agents and human contributors working on `mutliobjective-timetable-scheduler-`.
> **Primary Rule:** Read this document in full before proposing or applying code modifications. Maintain existing conventions, strictly preserve backwards-compatibility for legacy endpoints, keep the core engine decoupled from web concerns, and follow established verification procedures.

---

## 1. Executive Summary & Project Purpose

**Multiobjective Timetable Scheduler** is an enterprise-grade university timetabling platform designed for **DJ Sanghvi College of Engineering (DJSCE, Department of Computer Science & Engineering - Data Science)**.

The system automates the generation of complex, conflict-free academic schedules across multiple years, branches, divisions, faculty members, and specialized classrooms/laboratories. It incorporates multi-objective optimization to balance institutional hard constraints with pedagogical and faculty quality-of-life preferences.

### Dual-Architecture Reality

The codebase unifies two distinct architectural layers alongside a custom C++ solver fork:

1. **The Bespoke Reference Pipeline (Root Modules):**
   - Files: [`data.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/data.py), [`model.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/model.py), [`solver.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/solver.py), [`extract_schedule.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/extract_schedule.py), [`main.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/main.py).
   - Tailored specifically to the original Second Year (SY Sem IV) dataset. Contains hardcoded constraints (e.g. `sibling_sync`, `twice`, `day_edges_only`).
   - Must be preserved without regressions; served via legacy read-only endpoints on the FastAPI server and viewed at `/sy-reference`.

2. **The Generic Multi-Objective Engine & Platform (`engine/` & `webapp/`):**
   - A fully generalized scheduling engine capable of handling any academic structure (any branch, year, semester, division, course, faculty, room, and weekly slot template).
   - Powered by four interchangeable solvers (**Greedy**, **MIP**, **GA**, and **CP-SAT**) plus a 4-stage hybrid [`pipeline.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/engine/pipeline.py).
   - Backed by a SQLite database via SQLModel, a comprehensive REST API (FastAPI), background asynchronous task runners, real-time Pareto frontier sweeps, and an interactive HTML5 drag-and-drop manual timetable editor.

3. **Source-Modified Google OR-Tools CP-SAT Fork (`third_party/or-tools-fork/`):**
   - C++ level extensions to Google OR-Tools to overcome CP-SAT's lack of domain-level awareness for timetabling (adds `division_day_lns` and `CHOOSE_MIN_UNFIXED_IN_GROUP`).
   - Fully fail-safe: the engine runs identically on standard upstream OR-Tools via graceful runtime fallback if the modified wheel is not installed.

---

## 2. Comprehensive Directory & File Structure

```text
v:/Projects/IPD/mutliobjective-timetable-scheduler-/
├── AGENTS.md                                # This document (primary reference for AI agents)
├── README.md                                # User & developer getting started guide
├── CHANGES_DONE.md                          # Technical whitepaper on OR-Tools C++ modifications
├── requirements.txt                         # Python dependencies (fastapi, sqlmodel, ortools, etc.)
├── pytest.ini                               # Pytest configuration
│
├── main.py                                  # CLI runner for bespoke SY Sem 4 CP-SAT model
├── data.py                                  # Bespoke SY Sem 4 DataBundle loader & hardcoded constraints
├── model.py                                 # Bespoke CP-SAT model building & constraint formulations
├── solver.py                                # Bespoke CP-SAT solver execution & Pareto frontier routines
├── extract_schedule.py                      # Bespoke timetable extraction & grid formatting
├── run_odd_semesters.py                     # Diagnostic script for multi-year odd semester generation
│
├── engine/                                  # GENERIC MULTI-OBJECTIVE ENGINE (Branch-agnostic)
│   ├── __init__.py                          # Package initialization
│   ├── models.py                            # Pydantic data schemas: Problem, Solution, Course, Division, etc.
│   ├── scoring.py                           # Single source of truth for hard constraint & soft objective scoring
│   ├── adaptive.py                          # Algorithmic Adaptive CP-SAT weight controller & persistence tracking
│   ├── pipeline.py                          # 4-stage hybrid solver: Greedy -> MIP -> GA -> CP-SAT
│   ├── sample_data.py                       # Synthetic problem generators for testing and benchmarks
│   ├── disruption.py                        # Rescheduling engine under unexpected faculty/room disruptions
│   ├── export.py                            # Timetable exports to Excel (openpyxl) and PDF formats
│   ├── io_json.py                           # JSON serialization, deserialization, and schema validation
│   ├── view.py                              # Terminal ASCII and structured grid formatters
│   ├── llm_constraints.py                   # Experimental natural language constraint translation
│   ├── ml_predictors.py                     # Solve-time and feasibility heuristics predictors
│   └── solvers/                             # Solvers implementing engine.solvers.base.Solver
│       ├── __init__.py                      # Solver registry: SOLVERS = {greedy, mip, ga, cpsat}
│       ├── base.py                          # Abstract base class Solver
│       ├── candidates.py                    # Candidate placement generation & domain reduction
│       ├── cpsat.py                         # Constraint Programming solver (OR-Tools fork aware)
│       ├── greedy.py                        # Fast constructive heuristic solver
│       ├── mip.py                           # Mixed-Integer Programming solver
│       └── ga.py                            # Genetic Algorithm solver
│
├── webapp/                                  # WEB PLATFORM & REST API (FastAPI + SQLModel)
│   ├── server.py                            # FastAPI application entrypoint; mounts all routers & static assets
│   ├── db.py                                # Engine, session generator, and additive SQLite migrations
│   ├── models_db.py                         # SQLModel DB tables (Branch, Division, Course, Faculty, Room, etc.)
│   ├── problem_builder.py                   # DB -> engine.models.Problem builder with branch qualification
│   ├── grid_meta.py                         # Post-solve grid annotator (injects department, year, semester metadata)
│   ├── seed.py                              # Dataset seeder (reference datasets, de-duplicating faculty/rooms)
│   ├── jobs.py                              # Background worker thread pool for long-running solves
│   ├── auth.py                              # JWT-based authentication for Admin, Faculty, and Students
│   ├── extract_calendar.py                  # Vision-LLM academic calendar parser (Claude API)
│   │
│   ├── routers/                             # FastAPI endpoint modules
│   │   ├── _crud.py                         # Generic helper for CRUD endpoints
│   │   ├── auth.py                          # /api/auth (login, token refresh, current user)
│   │   ├── branches.py                      # /api/branches (Branch & Division management)
│   │   ├── courses.py                       # /api/courses (Course definitions)
│   │   ├── faculty.py                       # /api/faculty & /api/faculty/me/timetable (Personalized schedules)
│   │   ├── allocations.py                   # /api/allocations (Division x Course -> Faculty assignments)
│   │   ├── rooms.py                         # /api/rooms & /api/rooms/availability (Room matrix & conflicts)
│   │   ├── slots.py                         # /api/slots (Slot template definitions)
│   │   ├── constraints.py                   # /api/constraints (Constraint toggles and weights)
│   │   ├── runs.py                          # /api/runs (Generate, status, solutions, drag-and-drop move-session)
│   │   ├── calendar.py                      # /api/calendar (Academic calendar events & OCR upload)
│   │   └── students.py                      # /api/students (Student portals & elective enrollment)
│   │
│   └── static/                              # Front-end UI (Vanilla HTML5 / Modern CSS / Vanilla JS)
│       ├── dashboard.html                   # Administrative CRUD & resource management dashboard
│       ├── platform.html                    # Main Timetable Solver, Comparison & Interactive Grid Editor
│       ├── platform.js                      # UI logic: solve forms, live Pareto SVG charts, Drag & Drop handlers
│       ├── style.css                        # Design system & Drag-and-Drop visual cues
│       ├── styles.css                       # Secondary platform style sheet
│       ├── index.html                       # Legacy SY Sem IV reference visualization page
│       ├── faculty_timetable.html           # Dedicated faculty personalized schedule viewer
│       ├── student.html                     # Student elective view
│       └── login.html                       # Authentication page
│
├── data/                                    # DATASETS & TRANSCRIPTIONS
│   └── reference/                           # Canonical verified JSON datasets for seeding
│       ├── djsce_cse_ds_sy_sem4.json        # SY Sem IV (2nd Year)
│       ├── djsce_sy_sem3_jul_dec_2026.json  # SY Sem III (2nd Year)
│       ├── djsce_ty_d1_sem5_jul_dec_2026.json # TY Sem V Division D1 (3rd Year)
│       └── djsce_btech_d2_sem7_jul_dec_2026.json # BTech Sem VII Division D2 (4th Year)
│
├── tests/                                   # TEST SUITE
│   ├── test_data_bundle.py                  # Tests for bespoke SY data structures
│   ├── test_model.py                        # Tests for bespoke CP-SAT model constraints
│   ├── test_solver.py                       # Tests for bespoke solver execution
│   ├── test_extract_schedule.py             # Tests for bespoke schedule formatters
│   └── engine/                              # Exhaustive test suite for the generic engine & webapp
│       ├── conftest.py                      # Shared test fixtures & in-memory test databases
│       ├── test_models.py                   # Core Pydantic schema validation tests
│       ├── test_scoring.py                  # Scoring & objective verification tests
│       ├── test_solvers.py                  # Verification across Greedy, MIP, GA, and CP-SAT solvers
│       ├── test_pipeline.py                 # Multi-stage hybrid pipeline tests
│       ├── test_drag_and_drop.py            # Comprehensive tests for manual session adjustments & validation
│       ├── test_api_runs.py                 # API tests for timetable generation & lifecycle
│       ├── test_api_entities.py             # CRUD router tests for faculty, rooms, courses, branches
│       ├── test_api_room_availability.py    # Tests for room conflict and occupancy matrix endpoints
│       ├── test_api_calendar.py             # Academic calendar & OCR endpoint tests
│       ├── test_problem_builder.py          # Problem builder & branch qualification tests
│       ├── test_breaks.py                   # Mid-day break constraint & distribution tests
│       ├── test_adaptive.py                 # Comprehensive unit & integration tests for Adaptive CP-SAT
│       ├── test_disruption.py               # Disruption recovery & localized rescheduling tests
│       ├── test_export.py                   # Excel and PDF export tests
│       ├── test_platform_page.py            # Platform UI routing and page rendering tests
│       └── test_reference_data.py           # Verification that reference JSON datasets pass validation
│
├── third_party/or-tools-fork/               # CUSTOM C++ OR-TOOLS FORK
│   ├── timetabling.patch                    # 245-line patch against google/or-tools@98c165af
│   ├── verify_fork.py                       # CLI script to verify if runtime has fork or stock OR-Tools
│   ├── smoke_test.py                        # Functional 5/5 check for custom C++ subsolvers
│   ├── algo_test.cc                         # Standalone C++ test for custom search algorithms
│   └── NOTICE                               # Apache-2.0 license and attribution
│
├── research/                                # BENCHMARKING & EXPERIMENT HARNESS
│   ├── fork_benchmark.py                    # Multi-arm, multi-seed comparative benchmark script
│   ├── adaptive_cpsat_benchmark.py          # Priority vs Adaptive CP-SAT comparison benchmark
│   └── adaptive_benchmark_results.json      # Benchmark trajectory and comparison results
│
└── ADAPTIVE_CPSAT.md                        # In-depth technical specification of Adaptive CP-SAT engine
```

---

## 3. Core Architecture & Design Philosophy

### 3.1 Entity Model & Separation of Concerns

The project maintains a strict boundary between the database entities and the mathematical solving engine:

```
[SQLModel Database (webapp/models_db.py)]
  ├── Branch (e.g. CSE-DS-SY-SEM4, CSE-DS-TY-SEM5)
  ├── Division (D1, D2, D3 per branch)
  ├── Course (Theory & Lab subjects)
  ├── Allocation (Division × Course -> Faculty)
  ├── Faculty (Global, shared across all branches)
  ├── Room (Global, shared classrooms & labs)
  └── SlotTemplate (Global 5-day / 10-period grid)
                   │
                   ▼ (problem_builder.py)
[Pydantic Mathematical Model (engine/models.py)]
  └── Problem (Flat, immutable dict of requirements, domains, resources)
                   │
                   ▼ (engine/solvers/* or engine/pipeline.py)
[Solved Timetable (engine/models.py)]
  └── Solution (Grid dict: division_grids, faculty_grids, room_grids, unplaced, metrics)
                   │
                   ▼ (grid_meta.py)
[Annotated Result in Database (webapp/models_db.py)]
  └── TimetableRun (Stores problem_snapshot, solution JSON, division_meta, Pareto score)
```

**Crucial Invariant:** The `engine/` package is completely **branch-unaware**. It does not know what a "Branch", "Semester", or "Department" is. All scoping, division ID qualification, and metadata stamping take place strictly in `webapp/problem_builder.py` and `webapp/grid_meta.py`.

### 3.2 Branch Qualification & Cross-Year Solving ("Generate All Years")

In Indian engineering colleges like DJSCE, division names (`D1`, `D2`) are reused across every academic year. A teacher (e.g. Dr. Nilesh Marathe) teaches classes across Second Year, Third Year, and Final Year simultaneously.

1. **Division ID Collision Prevention:**
   - In a single-branch solve, divisions remain plain strings (`"D1"`).
   - In a multi-branch or whole-institution solve (`branch_ids=None`), divisions and courses are namespaced using `problem_builder.qualify(branch_code, name)`:
     - Example: `"CSE-DS-SY-SEM3::D1"` and `"CSE-DS-TY-SEM5::D1"`.
   - `unqualify()` strips these prefixes when formatting for end-user display.
2. **Post-Hoc Annotation:**
   - `webapp/grid_meta.annotate_grids()` stamps `class_label` (e.g. `"SY Sem IV · D1"`), `year_label`, `semester`, and `department` onto each cell in the solution grid.
3. **Unified Faculty Timetable:**
   - Endpoint: `GET /api/faculty/me/timetable`.
   - Reads the latest comprehensive timetable run and seamlessly aggregates sessions across all academic years into a single unified weekly schedule for that professor.

### 3.3 Custom Google OR-Tools C++ Fork

Standard CP-SAT views problems as flat variables and constraints. It cannot natively group variables by semantic concepts like "all periods in Division D1's Monday".

To solve this, a lightweight patch was applied to OR-Tools C++ internals:
- **`ExtractDomainTag` (`cp_model_utils.cc`):** Extracts `@KEY=VALUE` metadata from variable names.
- **`division_day_lns` (`cp_model_lns.cc`):** A Large Neighborhood Search subsolver that destroys and repairs one entire division-day at a time.
- **`CHOOSE_MIN_UNFIXED_IN_GROUP` (`cp_model_search.cc`):** A dynamic branching rule that branches on courses with the fewest remaining candidate placements.
- **Presolve Survival:** `SatParameters.ignore_names` must be set to `False` in `engine/solvers/cpsat.py` so variable names survive presolve into the solver core.
- **Fail-Safe Fallback:** If running against a standard PyPI OR-Tools package, `engine/solvers/cpsat.py` detects the absence of the custom enum, omits `@` tags, and solves cleanly using vanilla CP-SAT.

### 3.4 Algorithmic Adaptive CP-SAT Objective Layer (`engine/adaptive.py`)

Building on top of the custom C++ solver fork, the platform provides an algorithmic, deterministic adaptive multi-objective controller:
- **Optimization Modes:**
  - `baseline`: Preserves original stock objective weighting and legacy solver behavior.
  - `priority`: Uses fixed, user-configured soft constraint weights without iteration.
  - `adaptive`: Iterative feedback-driven reweighting across sequential CP-SAT solves.
- **Dynamic Pressure & Persistence:** Soft constraint violations are normalized by instance scale factors and tracked alongside consecutive violation persistence ($P_i = \alpha \hat{v}_i^{(t)} + \beta \hat{v}_i^{(t-1)} + \gamma c_i$).
- **Controlled Decay & Bounded Scaling:** Unsatisfied constraints receive bounded weight boosts ($w \le 50.0$), while satisfied constraints decay back toward their base weights ($w \ge w^{(0)}$) via a decay factor ($\delta = 0.95$).
- **Warm-Start Hints (`AddHint`):** Variables from the best feasible incumbent are fed as hints into subsequent CP-SAT models, yielding a ~2x solve speedup per iteration and enabling rapid iterative search.
- **Best Solution Retention:** Evaluates each iteration using strict lexicographic priority (hard feasibility first, followed by soft objective quality); never overwrites a superior solution.

### 3.5 Contiguous Student Day Optimization & Consecutive Idle Gap Minimization

To eliminate fragmented student schedules where students face multi-hour empty gaps between lectures and practicals:
1. **McCormick Linear Lower Bound for Consecutive Idle Gaps (`engine/solvers/cpsat.py`):**
   - Rather than treating a 3-hour hole identically to three isolated 1-hour gaps on separate days, the formulation models consecutive gap pairs:
     $$\forall p \in [0, P-2]: \quad cgap_{div, day, p} \ge gap_{p} + gap_{p+1} - 1 \quad (cgap \in \{0, 1\})$$
   - Objective term: `150 * sum(idle_gaps) + 800 * sum(consecutive_gaps)`.
   - A single 1-hour gap incurs 150 penalty; a 2-hour gap incurs $150 \times 2 + 800 = 1100$; a 3-hour hole incurs $150 \times 3 + 1600 = 2050$.
   - This pure linear McCormick bound introduces zero reification (`OnlyEnforceIf`) overhead, preserving fast CP-SAT presolve.
2. **Adaptive Re-weighting Integration (`engine/adaptive.py`):**
   - Added `idle_gaps` (base weight 50.0, matching institutional `SOFT_WEIGHTS`) and `consecutive_gaps` (base weight 100.0) to `DEFAULT_ADAPTIVE_BASE_WEIGHTS`.
   - Added normalized scale denominators to `compute_normalization_denominators()`, ensuring iterative adaptation actively tracks and penalizes idle gaps instead of dropping them during warm-start iterations.
   - Enforced an iteration time floor (`iter_time_limit = min(remaining, max(25.0, ...))`), giving warm-start solver stages adequate search time to optimize and shave off gaps.
3. **UI Highlighting & Drag-and-Drop Hardening (`webapp/static/`):**
   - Prominent Period row badges (`Period 1` – `Period 10`) with start/end time styling.
   - Full-row hover illumination (`.period-row:hover`) tracking the current hour across all weekdays.
   - Synchronous drag-data capture in `ondrop` in `webapp/static/platform.js` fixing the async `_dragData = null` race condition.

---

## 4. Manual Timetable Adjustments & Drag-and-Drop Editor

### 4.1 Purpose & Workflow
Timetable administrators frequently require human-in-the-loop fine-tuning after an automatic solve (e.g., swapping lecture slots, moving a lab to a specific room, or adjusting for special events).

The manual adjustment engine allows users to interactively drag sessions across time slots and rooms directly on the `/platform` web interface.

### 4.2 Endpoint: `POST /api/runs/{run_id}/move-session`
- **Payload Schema (`MoveSessionRequest` in [`webapp/routers/runs.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/routers/runs.py)):**
  ```python
  class MoveSessionRequest(BaseModel):
      division: str           # Division identifier (qualified or unqualified)
      source_day: int         # 0-indexed day
      source_period: int      # 0-indexed period
      target_day: int
      target_period: int
      target_room: Optional[str] = None
      force: bool = False     # If True, bypasses non-fatal validation warnings
  ```

### 4.3 Validation Pipeline
Before applying a move, the backend performs rigorous conflict detection:
1. **Source Session Existence:** Verifies that a valid session exists at `(source_day, source_period)` in the division's schedule.
2. **Break Period Immunity:** **Critical:** Break sessions (`is_break=True` or labelled `Lunch Break`) are never treated as conflicting teaching sessions and cannot be overwritten.
3. **Division Overlap:** Ensures the target division does not already have an active lecture or lab scheduled at `(target_day, target_period)`.
4. **Faculty Conflict:** Ensures the assigned faculty member is not already teaching another class in any division at `(target_day, target_period)`.
5. **Room Conflict & Capacity:**
   - Ensures the room is not already booked by another division at the target slot.
   - Verifies the room capacity satisfies the division/course student count requirements.

### 4.4 Transactional Mutation & Audit Log
1. **Grid Synchronization:** The backend transactionally updates both `division_grids` and `faculty_grids` in `run.solution`. If room allocation changes, `room_grids` is updated in lockstep.
2. **Audit Logging:** Every edit is recorded in the `ManualEdit` database table with timestamp, user ID, source slot, target slot, and diff.
3. **Recalculation:** Metrics and readiness states are updated, and the modified `run.solution` is persisted back to SQLite.

---

## 5. Progress History & Key Milestones

| Milestone | Status | Key Deliverables & Context |
|---|---|---|
| **Phase 1: Bespoke SY Model** | ✅ Completed | Hardcoded CP-SAT formulation for DJSCE CSE-DS SY Sem IV ([`data.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/data.py), [`model.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/model.py), [`solver.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/solver.py)). |
| **Phase 2: OR-Tools C++ Fork** | ✅ Completed | Patched OR-Tools with `division_day_lns` & `CHOOSE_MIN_UNFIXED_IN_GROUP`. Published wheels (`ortools-9.15.9999-cp311`). Comprehensive benchmark in [`CHANGES_DONE.md`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/CHANGES_DONE.md). |
| **Phase 3: Generic Engine & Webapp Integration** | ✅ Completed | Ported generic engine (`engine/`) and FastAPI web platform (`webapp/`). Integrated dual server architecture in [`webapp/server.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/server.py) with 83 unified routes and zero collisions. |
| **Phase 4: Multi-Year Expansion & OCR Datasets** | ✅ Completed | Manually transcribed and validated DJSCE timetables for July–Dec 2026: SY Sem 3, SY Sem 4, TY Sem 5, and BTech Sem 7. Stored in [`data/reference/`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/data/reference/). |
| **Phase 5: Cross-Branch Qualification & "Generate All Years"** | ✅ Completed | Solved cross-branch division name collisions via `problem_builder.qualify()`. Enabled institution-wide simultaneous solving (`branch_ids=None`) and unified multi-year faculty timetables. |
| **Phase 6: Room Availability & Pareto Optimization** | ✅ Completed | Added occupancy matrix endpoints ([`webapp/routers/rooms.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/routers/rooms.py)), live SSE streaming Pareto sweeps, and stop controls. |
| **Phase 7: Interactive Drag-and-Drop Timetable Editor** | ✅ Completed | Full implementation of `/api/runs/{run_id}/move-session`, HTML5 draggable session cards with visual snap targets, conflict toasts, and test suite ([`tests/engine/test_drag_and_drop.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/tests/engine/test_drag_and_drop.py)). |
| **Phase 8: Adaptive CP-SAT Objective Layer** | ✅ Completed | Implemented algorithmic weight controller ([`engine/adaptive.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/engine/adaptive.py)), warm-start CP-SAT iterative hints, optimization modes (`baseline`, `priority`, `adaptive`), unit tests ([`tests/engine/test_adaptive.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/tests/engine/test_adaptive.py)), and benchmark harness ([`research/adaptive_cpsat_benchmark.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/research/adaptive_cpsat_benchmark.py)). |
| **Phase 9: Scaled Multi-Year Solve Optimization** | ✅ Completed | Pruned lab batch room candidates and bound primary classrooms to reduce search variables from 73,090 to 19,465 ([`engine/solvers/candidates.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/engine/solvers/candidates.py)). Restored linear cold-start initial incumbent search in Adaptive CP-SAT ([`engine/solvers/cpsat.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/engine/solvers/cpsat.py)), solving All Odd Semesters (311 requirements) in 31.9s. |
| **Phase 10: Contiguous Schedules & Multi-Hour Gap Elimination** | ✅ Completed | Superlinear consecutive idle gap McCormick formulation in [`engine/solvers/cpsat.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/engine/solvers/cpsat.py); adaptive controller integration in [`engine/adaptive.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/engine/adaptive.py); UI Period/hour badges & row hover tracking in [`webapp/static/`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/static/); 100% test pass rate (145/145). |

---

## 6. Developer Workflows & Commands

### 6.1 Virtual Environment & Dependencies
> **Requirement:** Use **Python 3.11** specifically if using the prebuilt C++ fork wheel.

```bash
# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate
```

**Installing Dependencies:**
```bash
# IMPORTANT: If using the custom C++ fork, install the wheel FIRST
pip install ortools-9.15.9999-cp311-cp311-win_amd64.whl  # or linux equivalent
pip install -r requirements.txt
```

### 6.2 Verifying the Solver Build
```bash
# 1. Quick check (must report 'FORK build' or 'STOCK build')
python third_party/or-tools-fork/verify_fork.py

# 2. Functional smoke test (5/5 checks must pass on fork)
python third_party/or-tools-fork/smoke_test.py
```

### 6.3 Running the Application
```bash
python -m uvicorn webapp.server:app --host 127.0.0.1 --port 8750 --reload
```
- Admin / Faculty Portal: <http://127.0.0.1:8750/platform>
- Entity Management Dashboard: <http://127.0.0.1:8750/dashboard>
- Legacy SY Sem IV Visualizer: <http://127.0.0.1:8750/sy-reference>
- Default Dev Credentials: `nilesh.marathe@djsce.edu.in` / `Timetable@123`

### 6.4 Seeding Academic Datasets
```bash
# List available datasets
curl http://127.0.0.1:8750/api/seed/datasets

# Seed specific academic years
curl -X POST http://127.0.0.1:8750/api/seed/reference
curl -X POST http://127.0.0.1:8750/api/seed/sy-sem3
curl -X POST http://127.0.0.1:8750/api/seed/ty-sem5
curl -X POST http://127.0.0.1:8750/api/seed/btech-sem7
```

### 6.5 Running Tests
```bash
# Run the entire test suite
pytest tests/

# Run the drag-and-drop manual adjustment test suite
pytest tests/engine/test_drag_and_drop.py -v

# Run specific engine unit tests
pytest tests/engine/test_scoring.py
pytest tests/engine/test_solvers.py
pytest tests/engine/test_adaptive.py -v

# Run Priority vs Adaptive CP-SAT benchmark on reference instance
python -m research.adaptive_cpsat_benchmark --dataset reference --time-limit 45 --max-iterations 3
```

---

## 7. Critical Agent Constraints, Rules & Gotchas

1. **Preserve Legacy Endpoints Without Exception:**
   - Under no circumstances modify the route signatures or output contracts of `/api/timetable/*`, `/api/stats`, or `/api/pareto` (bare). These are bound to the original project requirements.
2. **Never Make `engine/` Branch-Aware:**
   - Do not pass `branch_id`, `branch_code`, or `department` into [`engine.models.Problem`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/engine/models.py) or [`engine.scoring`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/engine/scoring.py). All branch qualification must remain encapsulated in [`webapp/problem_builder.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/problem_builder.py) and [`webapp/grid_meta.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/grid_meta.py).
3. **Database Migrations are Strictly Manual & Additive:**
   - SQLite with SQLModel does not perform schema migrations automatically (`create_all` ignores existing tables).
   - Any new column or table must be accompanied by an additive `ALTER TABLE` statement in [`webapp/db.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/db.py) under `_apply_additive_migrations()`.
4. **Break Session Exclusion in Conflict Checks:**
   - Always verify that conflict algorithms skip sessions where `is_break=True` or `session.get("is_break")`. Lunch breaks and recess periods must never trigger double-booking or room occupancy errors.
5. **Handling CP-SAT Solver Timeouts in Tests:**
   - Dense datasets (such as `ty-sem5` with extensive lab requirements) can occasionally reach solver time limits on constrained CPU environments.
   - Tests that validate higher-level functionality (such as drag-and-drop, export, or UI routing) should gracefully handle solver timeouts or use faster solvers (`greedy` or `reference` seed) to prevent build pipeline hangs.
6. **No Spurious Artifacts in the Repository:**
   - Never commit ad-hoc `.log`, `.tmp`, or scratch files into the repository root. Always use external scratch directories or clean up temporary files immediately.

---

*Document maintained for Antigravity AI coding agents and engineering contributors.*