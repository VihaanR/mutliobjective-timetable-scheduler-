"""
solver.py — Solve the CP-SAT model and print the full solution report.
"""
import sys, io
import json, time, os
from datetime import datetime
# Force UTF-8 output on Windows to avoid cp1252 encode errors
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from ortools.sat.python import cp_model


# ---------------------------------------------------------------------------
# Helper — pretty fixed-width cell
# ---------------------------------------------------------------------------
CELL = 14

def _cell(s):
    return str(s)[:CELL].ljust(CELL)


def _hline(cols):
    return '+' + ('+'.join(['-' * (CELL + 2)] * cols)) + '+'


def _row(cells):
    return '|' + '|'.join(f' {_cell(c)} ' for c in cells) + '|'


# ---------------------------------------------------------------------------
# Post-solution extraction helpers
# ---------------------------------------------------------------------------

def _get_val(solver, var):
    return solver.Value(var)


def _theory_grid(solver, tv, div, day, data):
    """Return slot→(subj_name, teacher) for a division on a day."""
    grid = {}
    for subj in range(data.NUM_THEORY_SUBJ):
        for slot in data.ACADEMIC_SLOTS:
            if _get_val(solver, tv[(div, subj, day, slot)]):
                t = data.THEORY_SUBJ[subj]['teachers'][div]
                grid[slot] = (data.THEORY_SUBJ[subj]['name'], t)
    return grid


def _lab_grid(solver, lv, lr, batch, day, data):
    """Return slot→(lab_name, teacher, room) for a batch on a day."""
    grid = {}
    for ls in range(data.NUM_LAB_SUBJ):
        for ss in data.LAB_START_SLOTS:
            if _get_val(solver, lv[(batch, ls, day, ss)]):
                teacher = data.LAB_SUBJ[ls]['teachers'][batch]
                room = next(
                    (data.LABS_LIST[r] for r in range(data.NUM_LABS)
                     if _get_val(solver, lr[(batch, ls, day, ss, r)])),
                    '?'
                )
                lab_name = data.LAB_SUBJ[ls]['name']
                grid[ss] = (lab_name, teacher, room)
                grid[ss+1] = (f'{lab_name}(cont)', teacher, room)
    return grid


# ---------------------------------------------------------------------------
# Hard-constraint violation checker
# ---------------------------------------------------------------------------

def check_hard_violations(solver, tv, oe1, oe2, lv, lr, div_day_hours, data):
    violations = []

    # HC1 – Weekly lecture counts
    for div in range(data.NUM_DIVS):
        for subj in range(data.NUM_THEORY_SUBJ):
            count = sum(_get_val(solver, tv[(div, subj, d, s)])
                        for d in range(data.NUM_DAYS) for s in data.ACADEMIC_SLOTS)
            expected = data.THEORY_SUBJ[subj]['weekly']
            if count != expected:
                violations.append(
                    f"HC1 – {data.DIVISIONS[div]} {data.THEORY_SUBJ[subj]['name']}: "
                    f"got {count}, need {expected}")

    # HC2 – Fluctuation
    for div in range(data.NUM_DIVS):
        counts = {_get_val(solver, div_day_hours[(div, d)]) for d in range(data.NUM_DAYS)}
        if len(counts) == 1:
            violations.append(f"HC2 – {data.DIVISIONS[div]}: all days have same hour count ({list(counts)[0]})")

    # HC3/HC4 – Break slot (implicit, always enforced by variable structure)

    # HC7 – Lab room double-booking
    for day in range(data.NUM_DAYS):
        for slot in data.ACADEMIC_SLOTS:
            for r in range(data.NUM_LABS):
                users = [
                    (b, ls, ss)
                    for b in range(data.NUM_BATCHES)
                    for ls in range(data.NUM_LAB_SUBJ)
                    for ss in data.LAB_START_SLOTS
                    if (ss == slot or ss + 1 == slot)
                    and _get_val(solver, lr[(b, ls, day, ss, r)])
                ]
                if len(users) > 1:
                    violations.append(
                        f"HC7 – Lab {data.LABS_LIST[r]} double-booked "
                        f"{data.DAYS[day]} {data.SLOT_NAMES[slot]}: {users}")

    # HC8 – Teacher clash (all labs now treated uniformly — sibling-sync pairs
    # have different teachers so no special exemption needed)
    for teacher in sorted({t for s in data.THEORY_SUBJ for t in s['teachers'].values()} |
                          {t for s in data.LAB_SUBJ for t in s['teachers'].values()}):
        for day in range(data.NUM_DAYS):
            for slot in data.ACADEMIC_SLOTS:
                cnt = 0
                for div in range(data.NUM_DIVS):
                    for subj in range(data.NUM_THEORY_SUBJ):
                        if data.THEORY_SUBJ[subj]['teachers'].get(div) == teacher:
                            cnt += _get_val(solver, tv[(div, subj, day, slot)])
                for b in range(data.NUM_BATCHES):
                    for ls in range(data.NUM_LAB_SUBJ):
                        if data.LAB_SUBJ[ls]['teachers'].get(b) == teacher:
                            for ss in data.LAB_START_SLOTS:
                                if ss == slot or ss + 1 == slot:
                                    cnt += _get_val(solver, lv[(b, ls, day, ss)])
                if cnt > 1:
                    violations.append(
                        f"HC8 – Teacher {teacher} clash: "
                        f"{data.DAYS[day]} {data.SLOT_NAMES[slot]} (count={cnt})")

    # HC16/HC17 – OE pattern
    oe1_days = [d for d in range(data.NUM_DAYS) if _get_val(solver, oe1[d])]
    oe2_days = [d for d in range(data.NUM_DAYS) if _get_val(solver, oe2[d])]
    if len(oe1_days) != 1:
        violations.append(f"HC16 – Expected 1 OE-1hr day, got {oe1_days}")
    if len(oe2_days) != 1:
        violations.append(f"HC16 – Expected 1 OE-2hr day, got {oe2_days}")
    if oe1_days and oe2_days and oe1_days[0] == oe2_days[0]:
        violations.append(f"HC16 – OE-1hr and OE-2hr on same day ({oe1_days[0]})")

    # HC17 – twice-weekly labs run exactly twice per week, on different days
    twice_weekly_idx = [i for i, s in enumerate(data.LAB_SUBJ) if s['twice']]
    for b in range(data.NUM_BATCHES):
        for ls in twice_weekly_idx:
            cnt = sum(_get_val(solver, lv[(b, ls, d, ss)])
                      for d in range(data.NUM_DAYS) for ss in data.LAB_START_SLOTS)
            if cnt != 2:
                violations.append(f"HC17 – {data.BATCHES[b]} {data.LAB_SUBJ[ls]['name']}: {cnt} sessions (need 2)")
            we_days = [d for d in range(data.NUM_DAYS)
                       if any(_get_val(solver, lv[(b, ls, d, ss)])
                              for ss in data.LAB_START_SLOTS)]
            if len(we_days) != len(set(we_days)):
                violations.append(f"HC17 – {data.BATCHES[b]} {data.LAB_SUBJ[ls]['name']} on same day twice")

    # HC18 – Other labs exactly once
    for b in range(data.NUM_BATCHES):
        for ls in range(data.NUM_LAB_SUBJ):
            if ls in twice_weekly_idx:
                continue
            cnt = sum(_get_val(solver, lv[(b, ls, d, ss)])
                      for d in range(data.NUM_DAYS) for ss in data.LAB_START_SLOTS)
            if cnt != 1:
                violations.append(
                    f"HC18 – {data.BATCHES[b]} {data.LAB_SUBJ[ls]['name']}: {cnt} sessions (need 1)")

    # HC14 – sibling-sync lab subjects must start simultaneously
    sibling_sync_idx = [i for i, s in enumerate(data.LAB_SUBJ) if s['sibling_sync']]
    for (b1, b2) in data.SIBLING_PAIRS:
        for ls in sibling_sync_idx:
            for day in range(data.NUM_DAYS):
                for ss in data.LAB_START_SLOTS:
                    v1 = _get_val(solver, lv[(b1, ls, day, ss)])
                    v2 = _get_val(solver, lv[(b2, ls, day, ss)])
                    if v1 != v2:
                        violations.append(
                            f"HC14 – {data.LAB_SUBJ[ls]['name']} {data.BATCHES[b1]}/{data.BATCHES[b2]} desync "
                            f"{data.DAYS[day]} {data.SLOT_NAMES[ss]}: {v1} vs {v2}")

    return violations


# ---------------------------------------------------------------------------
# Print division timetable grid
# ---------------------------------------------------------------------------

def _print_div_timetable(solver, tv, oe1, oe2, lv, lr, is_break, div, data):
    oe1_day = next((d for d in range(data.NUM_DAYS) if _get_val(solver, oe1[d])), None)
    oe2_day = next((d for d in range(data.NUM_DAYS) if _get_val(solver, oe2[d])), None)

    div_name = data.DIVISIONS[div]
    classroom = data.CLASSROOMS[div]
    batches = data.DIV_TO_BATCHES[div]

    print(f"\n{'='*80}")
    print(f"  TIMETABLE — Division {div_name}   (Classroom: {classroom})")
    print(f"{'='*80}")

    cols = 1 + data.NUM_DAYS
    print(_hline(cols))
    print(_row(['Slot'] + data.DAYS))
    print(_hline(cols))

    for slot in range(data.NUM_SLOTS):
        sname = data.SLOT_NAMES[slot]

        row_cells = [sname]
        for day in range(data.NUM_DAYS):
            cell_text = ''
            # Break check
            if _get_val(solver, is_break[(div, day, slot)]):
                cell_text = '--- BREAK ---'
            # OE check
            elif day == oe2_day and slot in (0, 1):
                cell_text = 'OE(2hr)'
            elif day == oe1_day and slot == 0:
                cell_text = 'OE(1hr)'
            else:
                # Theory?
                for subj in range(data.NUM_THEORY_SUBJ):
                    if _get_val(solver, tv[(div, subj, day, slot)]):
                        teacher = data.THEORY_SUBJ[subj]['teachers'][div]
                        cell_text = f"{data.THEORY_SUBJ[subj]['name']}({teacher})"
                        break
                # Lab (use first batch as representative)?
                if not cell_text:
                    b_rep = batches[0]
                    for ls in range(data.NUM_LAB_SUBJ):
                        for ss in data.LAB_START_SLOTS:
                            if (ss == slot or ss + 1 == slot) and \
                               _get_val(solver, lv[(b_rep, ls, day, ss)]):
                                room = next(
                                    (data.LABS_LIST[r] for r in range(data.NUM_LABS)
                                     if _get_val(solver, lr[(b_rep, ls, day, ss, r)])), '?')
                                lname = data.LAB_SUBJ[ls]['name']
                                suffix = '' if ss == slot else '(c)'
                                cell_text = f"{lname}{suffix}"

            row_cells.append(cell_text if cell_text else '.')
        print(_row(row_cells))

    print(_hline(cols))


# ---------------------------------------------------------------------------
# Print batch lab timetable
# ---------------------------------------------------------------------------

def _print_batch_timetables(solver, lv, lr, data):
    print(f"\n{'='*80}")
    print("  BATCH LAB TIMETABLES")
    print(f"{'='*80}")
    for b in range(data.NUM_BATCHES):
        print(f"\n  Batch {data.BATCHES[b]}:")
        print(f"  {'Day':<12} {'Slot':<6} {'Lab':<12} {'Teacher':<8} {'Room'}")
        print(f"  {'-'*55}")
        for day in range(data.NUM_DAYS):
            for ls in range(data.NUM_LAB_SUBJ):
                for ss in data.LAB_START_SLOTS:
                    if _get_val(solver, lv[(b, ls, day, ss)]):
                        teacher = data.LAB_SUBJ[ls]['teachers'][b]
                        room = next(
                            (data.LABS_LIST[r] for r in range(data.NUM_LABS)
                             if _get_val(solver, lr[(b, ls, day, ss, r)])), '?')
                        print(f"  {data.DAYS[day]:<12} {data.SLOT_NAMES[ss]:<6} "
                              f"{data.LAB_SUBJ[ls]['name']:<12} {teacher:<8} {room}")


# ---------------------------------------------------------------------------
# Timetable Grid Extraction & Evolution Callback
# ---------------------------------------------------------------------------

def extract_division_timetable(solver, vars_dict, data):
    """Extract division timetable grids from current solver variable values."""
    tv = vars_dict['tv']
    oe1 = vars_dict['oe1']
    oe2 = vars_dict['oe2']
    lv = vars_dict['lv']
    lr = vars_dict['lr']
    is_break = vars_dict['is_break']

    oe1_day = next((d for d in range(data.NUM_DAYS) if _get_val(solver, oe1[d])), None)
    oe2_day = next((d for d in range(data.NUM_DAYS) if _get_val(solver, oe2[d])), None)

    divisions_data = []
    for div in range(data.NUM_DIVS):
        div_name = data.DIVISIONS[div]
        classroom = data.CLASSROOMS[div]
        batches = data.DIV_TO_BATCHES[div]
        cells = {}

        for day in range(data.NUM_DAYS):
            for slot in range(data.NUM_SLOTS):
                slot_key = f"{day}_{slot}"
                entries = []

                # Break check
                if _get_val(solver, is_break[(div, day, slot)]):
                    entries.append({
                        "course": "BREAK",
                        "type": "Break",
                        "faculty": "",
                        "faculty_name": "",
                        "room": "",
                        "room_name": "",
                        "batch": "",
                        "is_break": True,
                        "division_id": div_name,
                    })
                # OE check
                elif day == oe2_day and slot in (0, 1):
                    entries.append({
                        "course": "OE",
                        "type": "Theory",
                        "faculty": "OE",
                        "faculty_name": "Open Elective",
                        "room": classroom,
                        "room_name": classroom,
                        "batch": "",
                        "is_break": False,
                        "division_id": div_name,
                    })
                elif day == oe1_day and slot == 0:
                    entries.append({
                        "course": "OE",
                        "type": "Theory",
                        "faculty": "OE",
                        "faculty_name": "Open Elective",
                        "room": classroom,
                        "room_name": classroom,
                        "batch": "",
                        "is_break": False,
                        "division_id": div_name,
                    })
                else:
                    found_theory = False
                    for subj in range(data.NUM_THEORY_SUBJ):
                        if _get_val(solver, tv[(div, subj, day, slot)]):
                            teacher = data.THEORY_SUBJ[subj]['teachers'][div]
                            subj_name = data.THEORY_SUBJ[subj]['name']
                            entries.append({
                                "course": subj_name,
                                "type": "Theory",
                                "faculty": teacher,
                                "faculty_name": teacher,
                                "room": classroom,
                                "room_name": classroom,
                                "batch": "",
                                "is_break": False,
                                "division_id": div_name,
                            })
                            found_theory = True
                            break

                    if not found_theory:
                        for b_idx in batches:
                            b_name = data.BATCHES[b_idx]
                            for ls in range(data.NUM_LAB_SUBJ):
                                for ss in data.LAB_START_SLOTS:
                                    if (ss == slot or ss + 1 == slot) and \
                                       _get_val(solver, lv[(b_idx, ls, day, ss)]):
                                        teacher = data.LAB_SUBJ[ls]['teachers'][b_idx]
                                        room = next(
                                            (data.LABS_LIST[r] for r in range(data.NUM_LABS)
                                             if _get_val(solver, lr[(b_idx, ls, day, ss, r)])), '?'
                                        )
                                        lname = data.LAB_SUBJ[ls]['name']
                                        entries.append({
                                            "course": lname,
                                            "type": "Practical",
                                            "faculty": teacher,
                                            "faculty_name": teacher,
                                            "room": room,
                                            "room_name": room,
                                            "batch": b_name,
                                            "is_break": False,
                                            "division_id": div_name,
                                        })

                if entries:
                    cells[slot_key] = entries

        divisions_data.append({
            "id": div_name,
            "name": div_name,
            "classroom": classroom,
            "cells": cells,
        })

    return {
        "days": data.DAYS,
        "periods": [{"period": s, "start": data.SLOT_NAMES[s], "end": ""} for s in range(data.NUM_SLOTS)],
        "divisions": divisions_data,
    }


class SolutionCallback(cp_model.CpSolverSolutionCallback):
    """CP-SAT SolutionCallback capturing every improving solution and timetable evolution."""
    def __init__(self, vars_dict, data):
        super().__init__()
        self.vars_dict = vars_dict
        self.data = data
        self.start_time = time.time()
        self.step = 0
        self.best_penalty = None
        self.evolution_steps = []

    def on_solution_callback(self):
        tot_var = self.vars_dict.get('total_penalty')
        tot = int(self.Value(tot_var)) if tot_var is not None else int(self.ObjectiveValue())

        # Only capture improving solutions (or first feasible solution)
        if self.best_penalty is not None and tot >= self.best_penalty:
            return

        self.best_penalty = tot
        self.step += 1
        elapsed = round(time.time() - self.start_time, 2)
        timestamp = datetime.now().isoformat()

        try:
            fac = int(self.Value(self.vars_dict['faculty_score']))
        except Exception:
            fac = 0
        try:
            stu = int(self.Value(self.vars_dict['student_score']))
        except Exception:
            stu = 0
        try:
            res = int(self.Value(self.vars_dict['resource_score']))
        except Exception:
            res = 0

        timetable_data = extract_division_timetable(self, self.vars_dict, self.data)

        snapshot = {
            "step": self.step,
            "faculty_score": fac,
            "student_score": stu,
            "resource_score": res,
            "total_penalty": tot,
            "elapsed_time": elapsed,
            "timestamp": timestamp,
            "timetable_data": timetable_data,
        }
        self.evolution_steps.append(snapshot)
        print(f"Step {self.step}: total_penalty={tot} (faculty={fac}, student={stu}, resource={res}) at {elapsed}s")


# ---------------------------------------------------------------------------
# Main report function
# ---------------------------------------------------------------------------

def solve_and_report(model, vars_dict, data, time_limit_s=90, save_evolution=True):
    solver  = cp_model.CpSolver()
    solver.parameters.log_search_progress = False
    solver.parameters.max_time_in_seconds = time_limit_s

    tv            = vars_dict['tv']
    oe1           = vars_dict['oe1']
    oe2           = vars_dict['oe2']
    lv            = vars_dict['lv']
    lr            = vars_dict['lr']
    div_day_hours = vars_dict['div_day_hours']

    cb = SolutionCallback(vars_dict, data)

    print("\nSolving … (this may take a few minutes for a large model)\n")
    status = solver.Solve(model, cb)
    status_name = solver.StatusName(status)

    vars_dict['evolution_steps'] = cb.evolution_steps

    if cb.evolution_steps:
        initial_p = cb.evolution_steps[0]["total_penalty"]
        final_p = cb.evolution_steps[-1]["total_penalty"]
        improvement = round((initial_p - final_p) / initial_p * 100, 1) if initial_p > 0 else 0.0
        print(f"Evolution captured: {len(cb.evolution_steps)} steps, initial penalty {initial_p}, final penalty {final_p}, improvement {improvement}%")

        if save_evolution:
            evo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
            os.makedirs(evo_dir, exist_ok=True)
            evo_file = os.path.join(evo_dir, "evolution_data.json")
            with open(evo_file, "w", encoding="utf-8") as f:
                json.dump(cb.evolution_steps, f, indent=2)
            print(f"Saved {len(cb.evolution_steps)} evolution steps to {evo_file}")

    print("\n" + "=" * 80)
    print("  === SOLUTION REPORT ===")
    print("=" * 80)
    print(f"  Status : {status_name}")
    print(f"  Wall time : {solver.WallTime():.2f} s")

    if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
        print(f"  Objective (total penalty) : {solver.ObjectiveValue():.0f}")

        # --- Multi-Objective Scores ---
        print("\n--- MULTI-OBJECTIVE SCORES ---")
        try:
            f_score = solver.Value(vars_dict['faculty_score'])
            s_score = solver.Value(vars_dict['student_score'])
            r_score = solver.Value(vars_dict['resource_score'])
            print(f"  Faculty Score  : {f_score}")
            print(f"  Student Score  : {s_score}")
            print(f"  Resource Score : {r_score}")
            print(f"  Total Penalty  : {f_score + s_score}")
        except KeyError:
            print("  [Legacy mode] Scores not extracted.")


        # --- Hard constraint check ---
        print("\n--- HARD CONSTRAINT VIOLATIONS (target: 0) ---")
        violations = check_hard_violations(solver, tv, oe1, oe2, lv, lr, div_day_hours, data)
        if violations:
            for v in violations:
                print(f"  [FAIL]  {v}")
        else:
            print("  [OK]  No hard violations detected.")
        print(f"  Total hard violations: {len(violations)}")

        # --- Daily hours summary ---
        print("\n--- DAILY ACADEMIC HOURS PER DIVISION ---")
        print(f"  {'':8}", end='')
        for d in data.DAYS:
            print(f"{d:>6}", end='')
        print()
        for div in range(data.NUM_DIVS):
            print(f"  {data.DIVISIONS[div]:8}", end='')
            for day in range(data.NUM_DAYS):
                h = solver.Value(div_day_hours[(div, day)])
                print(f"{h:>6}", end='')
            print()

        # --- OE schedule ---
        oe1_day = next((d for d in range(data.NUM_DAYS) if solver.Value(oe1[d])), None)
        oe2_day = next((d for d in range(data.NUM_DAYS) if solver.Value(oe2[d])), None)
        print(f"\n  OE 1-hr  : {data.DAYS[oe1_day] if oe1_day is not None else 'NONE'} @ H1")
        print(f"  OE 2-hr  : {data.DAYS[oe2_day] if oe2_day is not None else 'NONE'} @ H1-H2")

        # --- Division timetables ---
        is_break = vars_dict['is_break']
        for div in range(data.NUM_DIVS):
            _print_div_timetable(solver, tv, oe1, oe2, lv, lr, is_break, div, data)

        # --- Batch lab timetables ---
        _print_batch_timetables(solver, lv, lr, data)

    elif status == cp_model.INFEASIBLE:
        print("\n  *** MODEL IS INFEASIBLE ***")
        print("  The solver proved no feasible solution exists under the given constraints.")
        print("\n  Tip: Run with model.Validate() output (printed at end of main.py) for details.")

    else:
        print(f"\n  Solver returned status: {status_name}")

    print("\n" + "=" * 80)
    print("  END OF REPORT")
    print("=" * 80 + "\n")

    return solver, status, vars_dict
