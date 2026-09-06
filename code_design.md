# Code Design — Multiobjective Timetable Scheduler

Working notes on the architecture, what's been built so far, and how "accommodate all years"
is meant to work going forward. This file is a running design record, not user-facing docs.

## 1. Where this project started

`mutliobjective-timetable-scheduler-` began as a single bespoke CP-SAT model
(`data.py`/`model.py`/`solver.py`/`pareto.py`) hardcoded to one dataset: DJSCE CSE-DS, 2nd year
(SY), Sem IV. Its constraints (`twice`, `sibling_sync`, `day_edges_only` subject flags) are
hand-tuned to that exact structure — not a generic timetabling engine. It has a genuinely good
3-axis Pareto multi-objective sweep (faculty/student/resource scores, epsilon-constraint) that a
sibling project (`timetable/`) didn't have.

The sibling `timetable/` project has the opposite shape: a fully generic engine (any
divisions/courses/faculty/rooms; Greedy/MIP/GA/CP-SAT solvers, one shared `scoring.py`) plus a
mature DB-backed dashboard platform (FastAPI + SQLite, entity CRUD, auth, background jobs,
calendar OCR) — but no Pareto sweep wired into its API (though the underlying CP-SAT model already
had the epsilon-constraint machinery built and sitting unused in a research script).

The plan (see the earlier session's approved plan at
`C:\Users\Vihaan\.claude\plans\witty-cuddling-cocoa.md`): port the generic engine + dashboard into
this repo, keep the bespoke SY pipeline exactly as it was (additive, nothing broken), wire the
Pareto sweep onto the generic engine, and build a real timetable-image OCR importer.

## 2. What's built so far (Phase A)

- `engine/` — a straight port of `timetable/timetable/*` (models, scoring, sample_data, pipeline,
  disruption, export, io_json, view, solvers/{greedy,mip,ga,cpsat}). Only change: internal imports
  rewritten `timetable.X` → `engine.X`. This is a fully generic timetabling engine: any set of
  divisions/courses/faculty/rooms, scored by one `scoring.py`, solved by four interchangeable
  solvers.
- `webapp/{db.py, models_db.py, auth.py, problem_builder.py, jobs.py, routers/, static/}` — ported
  from `timetable/webapp/*`, same import rewrite. This is the DB-backed dashboard: `Branch` →
  `Division` → `Allocation` (division×course→faculty), global `Faculty`/`Room`/`SlotTemplate`,
  auth (faculty/student login), background job runner for solves, calendar upload+review.
- `webapp/server.py` — merged by hand: the pre-existing bespoke SY pipeline's startup solve and
  its 4 read-only endpoints (`/api/timetable/*`, `/api/stats`, legacy `/api/pareto`) are preserved
  byte-for-byte in behavior; the ported platform's routers are additionally mounted. The old
  `index.html` viewer moved from `/` to `/sy-reference` so the ported `dashboard.html` could take
  over `/` (the platform's own intended home page). No API path collisions between the two halves.
- `requirements.txt` — new, covers both dependency sets (all already installed in this
  environment: fastapi, sqlmodel, ortools, pypdf, pillow, anthropic, etc.).
- `tests/engine/` — the ported project's full test suite (models/scoring/pipeline/solvers/
  disruption/API/export/breaks), so the ported code carries its own regression coverage.

**Verification status:** engine and webapp packages both import cleanly; `webapp.server:app`
assembles 83 routes with no collisions. Full `tests/engine/` suite run in progress as of this
writing (background) — two known-flaky tests (`test_breaks.py::test_cpsat_breaks_vary_across_days`
and `::test_pipeline_respects_continuous_teaching_cap`) fail identically in the *original,
unmodified* `timetable` project on this machine, confirmed to be pre-existing time-budget
flakiness, not a porting regression.

## 3. How "accommodate all years" actually works

This was the open design question, and it turned out the platform's existing `Branch` entity
already solves it — no schema changes needed:

- **One `Branch` row per year/program-term.** E.g. `CSE-DS-SY` (2nd year, Sem IV), `CSE-DS-TY-D1`
  (3rd year, Sem V), `CSE-DS-BTECH-D2` (Final year, Sem VII). Each branch owns its own
  `Division`/`Course`/`Allocation` rows.
- **`Faculty`, `Room`, and `SlotTemplate` are global**, shared across every branch — a professor
  who teaches both SY and TY is the same `Faculty` row in both; the department's classrooms/labs
  and weekly period grid are one shared institutional resource, not duplicated per year.
- **Solves are scoped by `branch_ids`.** `problem_builder.build_problem_dict(session, branch_ids)`
  filters `Course`/`Division` to the requested branches while always emitting the full global
  faculty/room/slot set — so "generate this year's timetable" and "generate the whole
  institution's timetable at once" are the same code path, just a different filter.
- **Adding a new year is: drop a reference JSON in `data/reference/`, register it in
  `webapp/seed.py`'s `KNOWN_DATASETS`, hit `POST /api/seed/{name}`.** That's the whole mechanism —
  demonstrated below by adding two more years on top of the original SY dataset.

### The one real gap this surfaced: cross-branch division-name collisions

Division ids/names (`D1`, `D2`, `D3`) are only unique *within* a branch, by original design. A
whole-institution solve (`branch_ids=None`) that includes two branches which both happen to use
`D1` would collide in the engine's id-keyed lookups — `problem_builder.readiness()` already
detects this defensively and reports it as a readiness issue rather than silently corrupting data
(verified in §4 below: seeding SY + TY-D1 + BTech-D2 together correctly flags `D1`/`D2` as
colliding). Per-branch solves are unaffected. If a genuine simultaneous whole-institution solve
across years is ever needed, the fix is to make `Division.name` branch-qualified (e.g. `TY-D1`
instead of `D1`) at seed time — not attempted here since it wasn't asked for and every current
solve is per-branch.

### `seed.py`'s generalization (this session)

The ported `seed.py` only supported a single hardcoded branch (refused if *any* branch already
existed). Rewrote it so multiple datasets coexist:

- `_upsert_faculty` / `_upsert_room` / `_ensure_slot_template` — look up by code/`(day,period)`
  first; only insert if genuinely new. This is what lets two years share the same professor or
  the same classroom without duplicate rows.
- `_seed_dataset(session, data, branch_code, force)` — the reusable core (extracted from the old
  single-purpose `seed_reference` handler).
- `POST /api/seed/{dataset}` — generic entry point, `KNOWN_DATASETS` maps a short name to
  `(json filename stem, branch code)`. `POST /api/seed/reference` kept as its own route for
  backward compatibility (same dataset, same behavior as before this session).
- `GET /api/seed/datasets` — lists what's available, so a dashboard picker doesn't need the
  mapping hardcoded twice.
- `force=true` now wipes only *that* branch's own courses/divisions/allocations — never other
  branches, never the shared faculty/room/slot tables (the old behavior wiped every entity table
  in the DB, which only made sense when there could only ever be one branch).

## 4. Data entered this session

The user sent 4 photographed DJSCE timetables (term: July–December 2026). Transcribed by direct
(manual, non-automated) reading — see `data/ocr_drafts/djsce_all_years_july_dec_2026.md` for the
full per-sheet transcription with legends, before it was turned into seedable data.

| Sheet | Included? | Reference JSON | Branch code |
|---|---|---|---|
| TY (D1), Sem V | Yes | `data/reference/djsce_ty_d1_sem5_jul_dec_2026.json` | `CSE-DS-TY-D1` |
| BTech (D2), Sem VII | Yes | `data/reference/djsce_btech_d2_sem7_jul_dec_2026.json` | `CSE-DS-BTECH-D2` |
| BTech (D1), Sem VII | **No — excluded per instruction.** Almost entirely blank (2 subjects, ~2 hours/week); consistent with Final Year D1 being largely on OJT this term rather than following a normal timetable. | — | — |
| Division D3, semester unconfirmed | JSON built and validated, but **not registered in `KNOWN_DATASETS`/not seeded yet.** The photo's header (class/semester) was on a page not captured — only batch labels (D31/D32) and the subject legend were visible. Subject mix suggests SY Sem III or FY Sem II, but this is a guess, not a read. Confirm the actual class/semester before this goes live. | `data/reference/djsce_d3_unconfirmed_sem_jul_dec_2026.json` (ready, held back) | — |

Both included datasets were validated end-to-end:
- `engine.io_json.problem_from_dict()` parses each with zero validation issues.
- Seeded into a temp DB via `_seed_dataset()`: 3 branches (including the original SY reference),
  5 divisions, 29 courses, 27 faculty rows (deduped — e.g. `AS`/Adil Shaikh appears on both the
  TY-D1 and BTech-D2 sheets and correctly resolves to one shared `Faculty` row), 11 rooms, 50 slots.
- Per-branch `readiness()` is clean (`issues=[]`) for all three; whole-institution `readiness()`
  correctly reports the `D1`/`D2` name collision described in §3, rather than solving on
  corrupted data.

Two things flagged for the user to confirm before treating this data as authoritative (also noted
inline in the JSON files' `_transcription_note` fields):
1. The subject code `GDS` means two different courses depending on sheet (BTech-D1: "Green Data
   Science"; BTech-D2: "Geospatial Data Science") — likely a source-document inconsistency.
2. Division D3's class/semester header wasn't in the photo (see table above).

## 5. Phase B — done: generic Pareto sweep + branch selector + individual-solver UI

Built and committed (`01c6063`):
- `engine/pareto_sweep.py` — pure `sweep()`/`sweep_pair()` functions reusing
  `CPSATSolver.solve_pareto_point()` (AUGMECON2 epsilon-constraint), no CSV/plotting side effects.
- `webapp/routers/pareto.py` — `POST /api/pareto` (background job via `ParetoRun` table, mirrors
  `TimetableRun`'s queued→running→done/failed pattern), `GET /api/pareto/{id}`,
  `GET /api/pareto/runs`. Deliberately not `GET /api/pareto` bare — that path is the existing
  bespoke SY-only 3-axis endpoint served directly from `server.py`; kept untouched and separate.
- `platform.html`/`platform.js` — a Pareto panel (pair checkboxes, time budget, poll, results table
  + inline-SVG frontier scatter, no chart library).

**Two real bugs surfaced by end-to-end testing, both fixed:**

1. **No branch selector existed anywhere in the UI.** Every Generate/Compare/Pareto request
   implicitly solved `branch_ids=None` (whole institution), which only ever "worked" by accident
   when exactly one branch existed. Once TY/BTech were seeded alongside SY, the shared division
   names (`D1`/`D2`/`D3` across all three) collide in a whole-institution solve — confirmed live:
   `GET /api/readiness` in that state returns `ready:false` naming all three collisions, while
   `GET /api/readiness?branch_ids=2` (TY alone) returns `ready:true`. Added a real branch
   multi-select to `platform.html`, threaded through readiness/generate/compare/pareto payloads,
   defaulting to "all branches" on load (so a freshly-seeded dataset isn't silently excluded) but
   narrowable to one. **This was the actual fix for "it is still SY only."**
2. **MIP and GA were hidden from the UI.** The backend has always supported all four solvers
   individually (`engine.solvers.SOLVERS = {greedy, mip, ga, cpsat}`, `POST /api/runs` and
   `POST /api/compare` both already accepted any of them) — only the dropdown/checkboxes in
   `platform.html` restricted the choice to greedy/cpsat/pipeline. That restriction was the
   *source* `timetable` project's own deliberate call ("benchmark narrative, not end-user
   choices" — CLAUDE.md §6); it directly contradicts this project's founding goal (individual
   Greedy/MIP/GA/CP-SAT comparison), so it was removed. `pipeline.py` is a separate, additional
   4-stage hybrid (Greedy→MIP→GA→CP-SAT chained together) — not a replacement for using any of
   the four on their own, which remains fully available.

Live-verified against a running server (not just unit tests): seeded all 3 branches, bootstrapped
a faculty login, confirmed per-branch readiness is clean while whole-institution readiness
correctly reports the collision, and ran a branch-scoped `POST /api/runs` (greedy, TY branch)
that produced a distinctly different result from SY — proving the selector changes what's actually
solved, not just what's displayed.

## 6. Open question: how OJT works across three divisions

**Unresolved — needs an answer from the department before Sem VII output can be trusted.**

The photographed BTech (D1) Sem VII sheet is almost entirely blank (~2 subjects, ~2 hours/week),
which reads as D1 being on **OJT (On-the-Job Training)** that term rather than following a normal
teaching schedule. D1 is therefore not modelled at all: the `CSE-DS-BTECH-SEM7` branch schedules
only D2 and D3.

What we don't know:
- Does D1 alone go on OJT each term, or do the three divisions **rotate** through it?
- Do OJT students still need *some* campus sessions timetabled (review meetings, project reviews,
  a weekly seminar), or genuinely none?
- If divisions rotate, the branch's division set changes term-to-term, which affects shared
  faculty/room demand across the whole institution — the current Sem VII load is understated by
  roughly a third.

Until answered, any Sem VII timetable covers **two of three divisions** and its faculty/room load
must not be read as true Final Year demand. This is surfaced in the UI rather than left in a
comment: `Branch.notice` (fed from the dataset JSON's `branch_notice` key by `seed.py`) renders a
red alert on `/platform` whenever that branch is selected, shown *before* Generate — a
clean-looking timetable is exactly when an unmodelled assumption gets forgotten.

The mechanism is generic, not a special case for this branch: any dataset can declare a
`branch_notice` and it will be surfaced the same way.

## 7. Phase C — not started

Real timetable-image OCR automation. `webapp/extract_calendar.py` already has the proven pattern
(Claude-vision call + JSON schema, gated on `ANTHROPIC_API_KEY`, drafts land unconfirmed for human
review). Plan: `webapp/extract_timetable.py` on the same pattern, new
`TimetableUpload`/`TimetableDraftRow` tables, `webapp/routers/timetable_ocr.py` with a confirm
step that fans out into `Branch`/`Division`/`Course`/`Faculty`/`Allocation` (unlike the calendar
module's confirm, which just flips one boolean). §4 above is what this *automates* once built —
this session did it by hand instead (manually reading the photographed timetables), which is what
"for now I'll send you the timetables and you OCR it yourself" asked for.

## 8. Cross-year branch identity on the teaching timetable + "Generate All Years"

Two gaps this closed: (1) a solved grid carried only an opaque `Division.id` ("D1") with no
department/year/semester attached, and (2) every solve was scoped to exactly one branch, so a
teacher who teaches SY, TY *and* Final Year could never see all three at once — `/api/faculty/me/
timetable` reads the single latest "done" run, and a single-branch run can only ever contain that
one branch's sessions.

**Id collision, and why it blocked a whole-institution solve before now.** The engine keys
divisions by `Division.id` and courses by `Course.code` in flat, global dicts — unique only
*within* a branch. Every DJSCE year reuses D1/D2/D3, and SY Sem III's `OE` collides with SY Sem
IV's `OE`. `readiness()` used to hard-block this (`"division name 'D1' is used by more than one
included branch"`), which is exactly correct for an *unqualified* solve but meant "solve everything
at once" was never actually reachable.

**Fix: branch-qualified engine ids, used only when they're needed.**
`webapp/problem_builder.qualify(branch_code, name)` → `"CSE-DS-SY-SEM3::D1"`. `spans_multiple_
branches()` decides whether a given `branch_ids` selection needs qualification; a single-branch
solve is untouched — same plain `"D1"`, same collision guard, same everything, so nothing about the
existing single-branch flow (adjust/disruption, exports, compare) changed shape.
`unqualify()` strips it back off for display (`"CSE-DS-SY-SEM3::D1"` → `"D1"`).

**Labelling:** `webapp/problem_builder.build_division_meta()` maps every emitted division id to its
branch identity (department/year/semester/division name), stored on `TimetableRun.division_meta` at
generation time. `webapp/grid_meta.annotate_grids()` stamps that onto the solved grid post-hoc —
every division AND every per-cell session entry gets `class_label` ("SY Sem IV · D1"),
`year_label`, `semester`, `department`, `course_code` (the unqualified course, since `course` itself
is qualified on a multi-branch run), etc. Kept as a post-processing pass specifically so the engine
stays branch-unaware — `engine/` has no concept of "branch" and shouldn't need one.

**`POST /api/runs` with `branch_ids: null`** now actually solves the whole institution (every
seeded branch, shared rooms/faculty) instead of being permanently blocked by the collision guard.
The platform page's "Generate All Years Timetable" button (`platform.html`/`.js`) is exactly this —
bypasses the Branch/Year/Semester picker, submits `branch_ids: null`. `TimetableRun.branch_ids`
records which branches a run actually covered (`[]` conventionally means "all of them" isn't
recorded that way — the router resolves `null` to every branch id that existed *at generate time*
and stores that list, so a run stays self-describing even if branches are added/removed later).

**Verified end-to-end** (2026-08-21, greedy solver, 4 seeded branches — SY Sem III/IV, TY Sem V,
BTech Sem VII): a `branch_ids: null` run produced 11 divisions across all four branches with
correctly qualified ids and `class_label`s ("SY Sem IV · D1", "TY Sem V · D1", "BTech Sem VII ·
D2/D3", …); faculty `NM` (Dr. Nilesh Marathe)'s `/api/faculty/me/timetable` merged sessions from
three different years in one response (`classes: ["BTech Sem VII · D2", "BTech Sem VII · D3", "SY
Sem IV · D1", "TY Sem V · D1"]`, `spans_multiple_branches: true`) — exactly the "one teacher's
lectures across all years" view this was built for. A parallel single-branch run confirmed
unqualified ids (`"D1"`, not `"CSE-DS-SY-SEM4::D1"`) are unchanged.

An additive SQLite migration (`webapp/db._apply_additive_migrations`) adds `TimetableRun.
branch_ids`/`division_meta` to a pre-existing `platform.db` on startup, since `create_all` never
alters an existing table — this repo's DB has been through several schema-additive sessions already
and there is no migration framework, so each addition follows this same manual `ALTER TABLE`
pattern.
