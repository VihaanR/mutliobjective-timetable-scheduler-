# HANDOFF.md — Multiobjective Timetable Scheduler
**Last updated:** 2026-11-07 | Even Semester 2026-27 (SY Sem IV, TY Sem VI, BTech Sem VIII)

> This document is the authoritative pickup guide for a new agent/session continuing this project.
> Read `AGENTS.md` first for the full architectural overview, then come back here for current state.

---

## 1. Project At a Glance

| Item | Value |
|------|-------|
| Repo root | `v:\Projects\IPD\mutliobjective-timetable-scheduler-\` |
| Server | `python -m uvicorn webapp.server:app --host 127.0.0.1 --port 8750` |
| DB | `webapp/data/platform.db` (SQLite) |
| Admin login | `admin@djsce.ac.in` / `devpass123` |
| Faculty sample | `nilesh.marathe@djsce.edu.in` / `Timetable@123` |
| Active semester scope | Even Semester 2026-27 (Sem IV, VI, VIII) |

---

## 2. Database State (authoritative as of 2026-11-07)

### Branches (key ones)
| Branch ID | Label | Divisions |
|-----------|-------|-----------|
| 5 | SY Sem IV (CSE-DS-SY-SEM4) | D1(id=12), D2(13), D3(14) |
| 18 | BTech Sem VIII (CSE-DS-BTECH-SEM8) | D1(id=46), D2(47) |
| 19 | TY Sem VI (CSE-DS-TY-SEM6) | D1, D2, D3 |

> Branches 6, 7, 8 are legacy/odd-semester entries. Do NOT touch them.

### Course counts (confirmed clean)
| Branch | Courses | Allocations |
|--------|---------|-------------|
| 5 (SY) | 8 | 24 |
| 18 (BTech) | 5 | 9 |
| 19 (TY) | 9 | 27 |

### Critical course & faculty corrections applied
- **Solver Console Time Budget Expansion & Custom Input**:
  - Replaced the 90s cap with extended presets: `30s Quick`, `45s`, `90s`, `2m Standard (120s)`, `3m Deep (180s)`, `5m Max (300s)`.
  - Added an interactive **Custom** time budget option allowing administrators to specify any custom budget from 5 to 1,800 seconds (up to 30 minutes).
  - Synchronized `unifiedTimeLimit` options on the Command Center to include 90s, 120s, 180s, and 300s.
- **Visiting Faculty Roster Cleanup (Excel Load Distribution Rows 137–144)**:
  - Removed incorrect hardcoded visiting flags for permanent faculty (e.g. `MD` Prof. Mangal Dandekar, `SR` Sunita Ram).
  - Synchronized the 3 authentic visiting faculty members designated under the Excel master load distribution:
    1. `TC`: **Ms. Trupti Chandak** (DS)
    2. `SF`: **Ms. Sonal Foss** (QPM)
    3. `ND`: **Dr. Niti Desai** (TSA)
  - Synchronized all 25 faculty names directly against the Department load matrix.
- `Data_Ethics` (Branch 18, id=151): `theory_per_week=3`, `practical_per_week=0`
  - Reason: original xlsx has practical column filled for faculty, but the course itself is theory-only. Setting practical=2 caused BTech to be INFEASIBLE.
- Legacy courses id=43 (PBC) and id=44 (EFM) were **deleted** from Branch 5 along with their allocations.
  - These caused SY Sem IV to be INFEASIBLE (10 allocs/division with duplicate subjects, breaking `AddAtMostOne` constraint).

### Authoritative allocation sync
- File: [`webapp/allocation_sync.py`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/allocation_sync.py)
- Endpoint: `POST /api/allocations/sync-master-xlsx`
- Source xlsx: `data/Load_Distribution_(5)_3-11-2026.xlsx`
- Run after any DB reset to restore all 60 allocations (24+9+27).

---

## 3. Solver Status

### BTech Sem VIII (Branch 18)
- **Status: OPTIMAL — 0 hard clashes**
- Solves in < 2 seconds
- 2 divisions: D1 (online/OJT-heavy), D2 (offline). Friday reserved for Project/OJT.

### SY Sem IV (Branch 5)
- **Status: FEASIBLE expected — 0 hard clashes**
- 3 divisions: D1, D2, D3
- Was INFEASIBLE due to legacy courses; now cleaned.

### TY Sem VI (Branch 19)
- **Status: FEASIBLE expected after practical-sessions fix (run 110 in progress)**
- **Root cause identified 2026-11-07:** `problem_builder.py` was mapping `practical_per_week` (contact hours) directly to `practical_sessions_per_week` (number of lab blocks). Since each lab block is 2 consecutive slots, `practical_per_week=2` = **1 lab session** (not 2). With 7 practical courses × 2 sessions × 2 batches = 28 practical reqs × 2 slots each = 56 slots + 18 theory = 74 slots needed but only 50 slots available → INFEASIBLE.
- **Fix applied:** `webapp/problem_builder.py` line 185: `practical_sessions_per_week = max(1, practical_per_week // 2) if practical_per_week else 0`
- After fix: 7 courses × 1 session × 2 batches = 14 practical reqs, 14×2 + 18 = 46 slot-hours → comfortably FEASIBLE within 50 slots.

### Last run
- Run 108: **FAILED** ("cpsat failed to find any feasible candidate timetables", wall_clock=183.9s)
  - Was run against polluted DB (legacy courses still present). Now obsolete.
- **TODO: Trigger a fresh run** (see Section 5).

---

## 4. Completed Features

### UI / UX
- **Topbar streamlining**: Removed `.convener-pill` and `.semester-badge` from [`platform.html`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/static/platform.html). User name now links to `/profile` as an avatar pill.
- **User Profile page** (`/profile`): [`profile.html`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/static/profile.html) — institutional card with credentials, role badge, department, faculty code, max load, assigned workload breakdown table (theory/lab contact hours), password change form (`#changePasswordForm`).
- **Multi-semester Faculty Timetable** (`/my-timetable`): [`faculty_timetable.html`](file:///v:/Projects/IPD/mutliobjective-timetable-scheduler-/webapp/static/faculty_timetable.html) — cohort filter pills (All/Sem4/Sem6/Sem8), admin faculty picker, color-coded semester badge chips.
- **Subject & Batch Allocation Studio** (in platform Academic Data Studio): `#dataPanelAllocations`, sync button `#btnSyncMasterXlsx`, cohort pills, search `#allocSearchInput`, studio table `#dsAllocationTable`.

### API Enhancements
- `GET /api/auth/me`: Returns `email`, `code`, `department`, `max_load_hours_per_week`, `workload` (theory/lab hours per semester).
- `POST /api/auth/change-password`: Password change endpoint.
- `GET /api/faculty/me/timetable`: Returns `semester`-stamped cells, `all_faculties` list for admin, `is_admin` flag.
- `POST /api/allocations/sync-master-xlsx`: Syncs allocations from master xlsx.

### Solver Engine
- CP-SAT morning-start constraint softened (high-penalty soft preference instead of hard ban).
- BTech divisions detected via `semester=8` on Division objects → skips morning constraint.
- Multi-branch solve (`qualify_ids=True`) prefixes division IDs with branch code.

---

## 5. Immediate Next Steps

### PRIORITY 1 — Trigger fresh multi-branch solve
Run 108 is failed. Need a new run against the clean DB:
```python
import requests
s = requests.Session()
s.post('http://127.0.0.1:8750/api/auth/login', json={'role':'admin','email':'admin@djsce.ac.in','password':'devpass123'})
# Option A: All 3 branches at once (may timeout for TY)
r = s.post('http://127.0.0.1:8750/api/runs', json={'branch_ids':[5,19,18],'solver':'cpsat','opt_mode':'adaptive','time_limit':120.0})
print(r.json())
# Option B: TY standalone first (60s limit to test soft-morning fix)
# r = s.post('http://127.0.0.1:8750/api/runs', json={'branch_ids':[19],'solver':'cpsat','opt_mode':'adaptive','time_limit':60.0})
```

If TY (branch 19) times out again:
1. Check `engine/solvers/cpsat.py` lines ~358–400 — confirm the morning-start constraint is `model.Add(...).OnlyEnforceIf(morning_start_bool.Not())` style soft penalty, NOT `model.Add()` hard.
2. Inspect if SF (faculty code `SF`) is correctly mapped to afternoon-only slots in the DB via `FacultyAvailability` or if the constraint is in YAML.

### PRIORITY 2 — Git commit all changes
```
cd v:\Projects\IPD\mutliobjective-timetable-scheduler-
git add -A
git commit -m "feat: even-sem fine-tune, allocation studio, profile page, multi-sem timetable, clean DB"
```

### PRIORITY 3 — Verify timetable grid after fresh solve
After a successful run:
1. Admin login → platform → Timetable View → check grids populate for SY/TY/BTech.
2. `/my-timetable` → verify semester-tagged session chips appear correctly.
3. Download PDF — confirm year-wise sections in `engine/export.py`.

### PRIORITY 4 — PDF export year-wise validation
Inspect `engine/export.py` to ensure PDF groups sessions by year (SY/TY/BTech separately). The uploaded reference PDFs (`Final_Year_SEM_VIII.pdf`, `Third_Year_SEM_V.pdf`, `Second_Year_SEM_III.pdf`) show the target format. Endpoint: `GET /api/runs/{id}/export/pdf`.

---

## 6. File Change Map (all unstaged as of 2026-11-07)

### Modified files
| File | Key change |
|------|-----------|
| `config/academic_constraints.yaml` | Declarative constraint adjustments for even semester |
| `engine/export.py` | Year-wise PDF grouping |
| `engine/models.py` | Schema additions |
| `engine/scoring.py` | Hard/soft constraint scoring updates |
| `engine/solvers/cpsat.py` | Morning-start softened; BTech exemption; multi-branch qualify |
| `webapp/auth.py` | Auth helpers |
| `webapp/db.py` | Additive migration updates |
| `webapp/jobs.py` | Background worker refinements |
| `webapp/models_db.py` | DB model additions |
| `webapp/problem_builder.py` | Branch qualification, semester tagging |
| `webapp/routers/allocations.py` | `POST /api/allocations/sync-master-xlsx` |
| `webapp/routers/auth.py` | `/api/auth/me` enriched, `change-password` added |
| `webapp/routers/faculty.py` | Semester-stamped timetable, admin picker |
| `webapp/routers/runs.py` | Run management |
| `webapp/routers/constraints.py` | Constraint endpoint fixes |
| `webapp/seed.py` | Dataset seeder updates |
| `webapp/server.py` | `/profile` route added |
| `webapp/static/platform.html` | Topbar, allocation studio panel |
| `webapp/static/platform.js` | Avatar initials, studio JS functions |
| `webapp/static/faculty_timetable.html` | Sem pills, admin picker |
| `webapp/static/style.css` | Allocation studio styles |
| `webapp/static/styles.css` | General style updates |
| `webapp/static/dashboard.html` | Dashboard updates |
| `webapp/static/login.html` | Login page |
| `webapp/static/index.html` | Landing page |
| `webapp/static/student.html` | Student view |

### New (untracked) files
| File | Purpose |
|------|---------|
| `webapp/allocation_sync.py` | Authoritative allocation sync from master xlsx |
| `webapp/static/profile.html` | User profile page |
| `webapp/static/icons.js` | SVG icon helpers |
| `data/reference/djsce_btech_sem8_even_2027.json` | BTech reference data |
| `data/reference/djsce_ty_sem6_even_2027.json` | TY reference data |
| `engine/assets/` | Engine asset files |
| `tests/test_platform_features.py` | Playwright E2E tests |
| `tests/engine/test_api_auth.py` | API auth tests |
| `graphify-out/` | Knowledge graph output (can be gitignored) |

---

## 7. Known Gotchas for Next Session

| Gotcha | Detail |
|--------|--------|
| Login payload | Must include `role`: `{"role":"admin","email":"...","password":"..."}` |
| CourseCategory enum | Valid values: `"Open-Elective"`, `"Skill-Based"`, `"Ability-Enhancement"` (NOT `"OpenElective"`) |
| Allocation studio tab | Selector is `#subtabAllocationsBtn` (NOT button text "Subject & Batch Allocations") |
| Faculty picker ID | `#adminFacultyWrap` (container), `#facultyPicker` (select element) — NOT `#adminFacultyPicker` |
| Password form ID | `#changePasswordForm` — NOT `#changePwForm` |
| `get_session()` | It's a generator — use `next(get_session())` NOT `with get_session() as session` |
| `expand_requirements` | The cpsat solver function is `expand_requirements(problem)` NOT `_build_requirements` |
| `ProblemInstance.requirements` | Attribute does NOT exist. Use `expand_requirements(problem)` |
| Active run guard | If a run is stuck in 'running', new runs get 409. Mark stuck run as failed in DB: `UPDATE timetablerun SET status='failed' WHERE id=<id>` |
| `has_active_run` | Only 1 run can be active per branch at a time |
| BTech divisions | EXACTLY 2 (D1 online/OJT, D2 offline). Do NOT add a D3 for BTech. |
| `semester` on Division | BTech divisions have `semester=8` — this triggers the morning-constraint skip in cpsat.py |
| Course column names | DB uses `theory_per_week`, `practical_per_week`, `tutorial_per_week` (NOT `weekly_lectures`) |
| Port | Server on **8750** (NOT 8000, 5173, or 8080) |
| Scratch files | Use `C:\Users\Vihaan\.gemini\antigravity\brain\1da6c795-8e96-4b24-99a4-eed61ec2b6b1\scratch\` — NOT inside the repo |

---

## 8. Constraints & Commitments

- Zero emojis in UI and PDFs
- Adaptive CP-SAT exclusively — no GA or greedy fallback for primary solve
- 8am morning rule: **soft preference with high weight** (not hard) — user confirmed "it just needs to be in the morning"
- BTech: exactly 2 divisions, Friday reserved for Project/OJT
- Permanent faculty: 1 morning slot + 1 slot till 6pm rules as soft constraints with high weight
- Faculty SF (Ms. Sonal Fosa): afternoon-only (cannot teach before 12:00 PM) — handled via availability constraint, NOT by hardcoding
- Keep all existing comments and docstrings
- Do not perform unrelated refactors or dependency changes
- Work strictly inside `v:\Projects\IPD\mutliobjective-timetable-scheduler-\`

---

## 9. Playwright Test Selectors (confirmed working)

```python
# Login
page.fill('#email', 'admin@djsce.ac.in')
page.fill('#password', 'devpass123')
page.click('#btnSubmit')

# Allocation Studio tab
page.click('#subtabAllocationsBtn')

# Faculty picker (admin-only, /my-timetable)
page.locator('#facultyPicker')

# Profile page cards
profile_cards = page.locator('.profile-card')
assert profile_cards.count() >= 2  # NOT .is_visible() on a single element

# Change password form
page.locator('#changePasswordForm')

# Topbar avatar/user link
page.locator('#topbarUserLink')
```

---

## 10. Quick Verification Script

```python
import requests
s = requests.Session()
# 1. Login
assert s.post('http://127.0.0.1:8750/api/auth/login', json={'role':'admin','email':'admin@djsce.ac.in','password':'devpass123'}).status_code == 200
# 2. Check /me
me = s.get('http://127.0.0.1:8750/api/auth/me').json()
print('User:', me['email'], '| Role:', me['role'])
# 3. Check allocations
allocs = s.get('http://127.0.0.1:8750/api/allocations/?branch_id=5').json()
print('SY allocs:', len(allocs))  # expect 24
allocs18 = s.get('http://127.0.0.1:8750/api/allocations/?branch_id=18').json()
print('BTech allocs:', len(allocs18))  # expect 9
allocs19 = s.get('http://127.0.0.1:8750/api/allocations/?branch_id=19').json()
print('TY allocs:', len(allocs19))  # expect 27
# 4. Check latest run
runs = s.get('http://127.0.0.1:8750/api/runs?limit=5').json()
print('Latest runs:', [(r['id'], r['status']) for r in runs])
```
