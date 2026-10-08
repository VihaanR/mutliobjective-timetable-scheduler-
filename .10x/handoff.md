# Handoff Summary — Multiobjective Timetable Scheduler

## Active State
- **Active Branch**: `Vihaan` (synced with `fork/Vihaan`).
- **Web App**: Running on `http://127.0.0.1:8750` via FastAPI (`webapp.server:app`).
- **Test Suite**: 145/145 unit and integration tests passing (`pytest tests/engine/`).

## Recent Achievements
1. **Contiguous Student Schedules & Zero Multi-Hour Gaps**:
   - Linear McCormick formulation for consecutive gap pairs ($cgap_{div, day, p} \ge gap_p + gap_{p+1} - 1$) in `engine/solvers/cpsat.py`.
   - Dynamic adaptive tracking of `idle_gaps` (50.0) and `consecutive_gaps` (100.0) in `engine/adaptive.py`.
   - Verified on All Odd Semesters (8 divisions, 311 sessions) with 0 hard violations and 0 days with multi-hour gaps in TY and BTech.
2. **UI Polishing**:
   - Modern Period 1–10 pill badges and start/end hour styling.
   - Cross-day period row hover tracking (`.period-row:hover`).
   - Drag-and-drop async state safety in `webapp/static/platform.js`.
