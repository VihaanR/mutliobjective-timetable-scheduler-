"""
model.py — CP-SAT model builder.
Creates all decision variables, encodes all 21 hard constraints (HC) and
9 soft penalty terms (SC), then returns the model and variable dictionaries.

Takes a DataBundle (see data.py) instead of importing module-level globals, so the same
builder works against the hardcoded default dataset or a dashboard/DB-driven one.
"""

from ortools.sat.python import cp_model


def build_model(data):
    model = cp_model.CpModel()

    NUM_DAYS = data.NUM_DAYS
    ACADEMIC_SLOTS = data.ACADEMIC_SLOTS
    LAB_START_SLOTS = data.LAB_START_SLOTS
    LAST_SLOT = data.LAST_SLOT
    NUM_DIVS = data.NUM_DIVS
    NUM_BATCHES = data.NUM_BATCHES
    DIV_TO_BATCHES = data.DIV_TO_BATCHES
    SIBLING_PAIRS = data.SIBLING_PAIRS
    NUM_LABS = data.NUM_LABS
    THEORY_SUBJ = data.THEORY_SUBJ
    NUM_THEORY_SUBJ = data.NUM_THEORY_SUBJ
    LAB_SUBJ = data.LAB_SUBJ
    NUM_LAB_SUBJ = data.NUM_LAB_SUBJ
    ALL_TEACHERS = data.ALL_TEACHERS
    THEORY_LAB_PAIRS = data.THEORY_LAB_PAIRS
    DIFFICULT_SUBJ_IDX = data.DIFFICULT_SUBJ_IDX
    MIN_HOURS_PER_DAY = data.MIN_HOURS_PER_DAY
    MAX_HOURS_PER_DAY = data.MAX_HOURS_PER_DAY
    W_FAC_GAP = data.W_FAC_GAP
    W_FAC_OVERLOAD = data.W_FAC_OVERLOAD
    W_FAC_H1 = data.W_FAC_H1
    W_FAC_H9 = data.W_FAC_H9
    W_FAC_CONSEC = data.W_FAC_CONSEC
    W_STU_CONSEC_DIFF = data.W_STU_CONSEC_DIFF
    W_STU_THEORY_H9 = data.W_STU_THEORY_H9
    W_STU_3DAYS_SAME = data.W_STU_3DAYS_SAME
    W_RES_CLASSROOM = data.W_RES_CLASSROOM
    W_RES_LAB = data.W_RES_LAB

    # =========================================================================
    # A.  DECISION VARIABLES
    # =========================================================================

    # --- Theory vars: tv[(div, subj, day, slot)] = 1 iff scheduled ---
    tv = {}
    for div in range(NUM_DIVS):
        for subj in range(NUM_THEORY_SUBJ):
            for day in range(NUM_DAYS):
                for slot in ACADEMIC_SLOTS:
                    tv[(div, subj, day, slot)] = model.NewBoolVar(
                        f'tv_d{div}_s{subj}_D{day}_h{slot}')

    # --- OE day-selection vars (teacher-free, simultaneous all divisions) ---
    oe1 = [model.NewBoolVar(f'oe1_D{d}') for d in range(NUM_DAYS)]  # 1-hr OE day
    oe2 = [model.NewBoolVar(f'oe2_D{d}') for d in range(NUM_DAYS)]  # 2-hr OE day

    # --- Lab vars: lv[(batch, lab_subj, day, start_slot)] = 1 iff scheduled ---
    lv = {}
    for b in range(NUM_BATCHES):
        for ls in range(NUM_LAB_SUBJ):
            for day in range(NUM_DAYS):
                for ss in LAB_START_SLOTS:
                    lv[(b, ls, day, ss)] = model.NewBoolVar(
                        f'lv_b{b}_ls{ls}_D{day}_ss{ss}')

    # --- Lab room vars: lr[(batch, lab_subj, day, start_slot, room)] = 1 iff assigned ---
    lr = {}
    for b in range(NUM_BATCHES):
        for ls in range(NUM_LAB_SUBJ):
            for day in range(NUM_DAYS):
                for ss in LAB_START_SLOTS:
                    for r in range(NUM_LABS):
                        lr[(b, ls, day, ss, r)] = model.NewBoolVar(
                            f'lr_b{b}_ls{ls}_D{day}_ss{ss}_r{r}')

    # =========================================================================
    # B.  HARD CONSTRAINTS
    # =========================================================================

    # --- Flexible Break vars ---
    is_break = {}
    is_on_campus = {}
    for div in range(NUM_DIVS):
        for day in range(NUM_DAYS):
            for s in range(9):
                is_break[(div, day, s)] = model.NewBoolVar(f'brk_{div}_{day}_{s}')
                is_on_campus[(div, day, s)] = model.NewBoolVar(f'camp_{div}_{day}_{s}')
            # Exactly one break per day
            model.AddExactlyOne(is_break[(div, day, s)] for s in range(9))

            # Break window (cannot be at slot 0 or slot 8)
            model.Add(is_break[(div, day, 0)] == 0)
            model.Add(is_break[(div, day, 8)] == 0)

    # ---- HC15 + HC16: OE pattern ----------------------------------------
    # Exactly one 1-hr day and one 2-hr day; must be different days.
    model.AddExactlyOne(oe1)
    model.AddExactlyOne(oe2)
    for d in range(NUM_DAYS):
        model.Add(oe1[d] + oe2[d] <= 1)   # different days for OE patterns

    # ---- HC17: twice-weekly lab subjects run exactly twice per batch per week,
    # on different days. HC18: every other lab subject exactly once. ----------
    twice_weekly_idx = [i for i, s in enumerate(LAB_SUBJ) if s['twice']]
    for b in range(NUM_BATCHES):
        for ls in twice_weekly_idx:
            model.Add(
                sum(lv[(b, ls, d, ss)]
                    for d in range(NUM_DAYS) for ss in LAB_START_SLOTS) == 2)
            for d in range(NUM_DAYS):
                model.Add(
                    sum(lv[(b, ls, d, ss)] for ss in LAB_START_SLOTS) <= 1)

    for b in range(NUM_BATCHES):
        for ls in range(NUM_LAB_SUBJ):
            if ls not in twice_weekly_idx:
                model.Add(
                    sum(lv[(b, ls, d, ss)]
                        for d in range(NUM_DAYS) for ss in LAB_START_SLOTS) == 1)

    # ---- Room assignment: if lab scheduled → exactly one room assigned ------
    for b in range(NUM_BATCHES):
        for ls in range(NUM_LAB_SUBJ):
            for day in range(NUM_DAYS):
                for ss in LAB_START_SLOTS:
                    model.Add(
                        sum(lr[(b, ls, day, ss, r)] for r in range(NUM_LABS))
                        == lv[(b, ls, day, ss)])

    # ---- HC7: No lab-room double-booking ------------------------------------
    # A lab occupies room r at slots ss and ss+1.
    for day in range(NUM_DAYS):
        for slot in ACADEMIC_SLOTS:
            for r in range(NUM_LABS):
                users = [
                    lr[(b, ls, day, ss, r)]
                    for b in range(NUM_BATCHES)
                    for ls in range(NUM_LAB_SUBJ)
                    for ss in LAB_START_SLOTS
                    if ss == slot or ss + 1 == slot
                ]
                if users:
                    model.Add(sum(users) <= 1)

    # ---- Flexible Break Lab consecutive check -------------------------------
    # Lab cannot start if either ss or ss+1 is a break for the batch's division
    for div in range(NUM_DIVS):
        for b in DIV_TO_BATCHES[div]:
            for ls in range(NUM_LAB_SUBJ):
                for day in range(NUM_DAYS):
                    for ss in LAB_START_SLOTS:
                        model.Add(is_break[(div, day, ss)] + is_break[(div, day, ss+1)] == 0).OnlyEnforceIf(lv[(b, ls, day, ss)])

    # ---- HC13/14: Sibling Batch Synchronization (No-Idle Hard Constraint) ------
    # If B1 has a lab at any slot, B2 must also have a lab at the exact same slot.
    for (b1, b2) in SIBLING_PAIRS:
        for day in range(NUM_DAYS):
            for ss in LAB_START_SLOTS:
                model.Add(
                    sum(lv[(b1, ls, day, ss)] for ls in range(NUM_LAB_SUBJ))
                    ==
                    sum(lv[(b2, ls, day, ss)] for ls in range(NUM_LAB_SUBJ))
                )

    # Also enforce HC14: sibling-sync lab subjects start simultaneously for both siblings
    sibling_sync_idx = [i for i, s in enumerate(LAB_SUBJ) if s['sibling_sync']]
    for (b1, b2) in SIBLING_PAIRS:
        for ls in sibling_sync_idx:
            for day in range(NUM_DAYS):
                for ss in LAB_START_SLOTS:
                    model.Add(
                        lv[(b1, ls, day, ss)]
                        == lv[(b2, ls, day, ss)])

    # ---- HC10: No batch clash -----------------------------------------------
    # A batch cannot be in two labs at the same slot.
    for b in range(NUM_BATCHES):
        for day in range(NUM_DAYS):
            for slot in ACADEMIC_SLOTS:
                batch_at_slot = [
                    lv[(b, ls, day, ss)]
                    for ls in range(NUM_LAB_SUBJ)
                    for ss in LAB_START_SLOTS
                    if ss == slot or ss + 1 == slot
                ]
                if batch_at_slot:
                    model.Add(sum(batch_at_slot) <= 1)

    # ---- HC11: Each theory subject at most once per day per division ---------
    for div in range(NUM_DIVS):
        for subj in range(NUM_THEORY_SUBJ):
            for day in range(NUM_DAYS):
                model.Add(
                    sum(tv[(div, subj, day, slot)] for slot in ACADEMIC_SLOTS) <= 1)

    # ---- HC1 (weekly counts): Required weekly theory lectures ---------------
    for div in range(NUM_DIVS):
        for subj in range(NUM_THEORY_SUBJ):
            model.Add(
                sum(tv[(div, subj, day, slot)]
                    for day in range(NUM_DAYS) for slot in ACADEMIC_SLOTS)
                == THEORY_SUBJ[subj]['weekly'])

    # ---- HC9 + HC6: No division clash / classroom double-booking -----------
    # (Classrooms are division-dedicated, so div-clash → room-clash equivalent.)
    # Also enforces HC15: OE at H1 (and H2 for 2-hr day) blocks theory there.
    for div in range(NUM_DIVS):
        for day in range(NUM_DAYS):
            for slot in ACADEMIC_SLOTS:
                theory_here = [tv[(div, subj, day, slot)]
                                for subj in range(NUM_THEORY_SUBJ)]
                if slot == 0:     # H1 – blocked on both OE days
                    model.Add(sum(theory_here) + oe1[day] + oe2[day] <= 1)
                elif slot == 1:   # H2 – blocked on 2-hr OE day only
                    model.Add(sum(theory_here) + oe2[day] <= 1)
                else:
                    model.Add(sum(theory_here) <= 1)

    # ---- OE blocks lab starts at H1 (both OE days) and H2 (OE-2hr day) ------
    for day in range(NUM_DAYS):
        for b in range(NUM_BATCHES):
            for ls in range(NUM_LAB_SUBJ):
                # OE1 (1-hr) occupies H1
                if 0 in LAB_START_SLOTS:
                    model.Add(lv[(b, ls, day, 0)] == 0).OnlyEnforceIf(oe1[day])
                # OE2 (2-hr) occupies H1 and H2
                if 0 in LAB_START_SLOTS:
                    model.Add(lv[(b, ls, day, 0)] == 0).OnlyEnforceIf(oe2[day])
                if 1 in LAB_START_SLOTS:
                    model.Add(lv[(b, ls, day, 1)] == 0).OnlyEnforceIf(oe2[day])

    # ---- Day-edges-only constraint (theory): subjects flagged day_edges_only must
    # sit at the start or end of the day, never mid-day -----------------------------
    day_edges_theory_idx = [i for i, s in enumerate(THEORY_SUBJ) if s.get('day_edges_only')]
    for subj in day_edges_theory_idx:
        for div in range(NUM_DIVS):
            for day in range(NUM_DAYS):
                # Block globally from mid-day H3, H4, H5, H6, H7 (indices 2, 3, 4, 5, 6)
                for s in [2, 3, 4, 5, 6]:
                    model.Add(tv[(div, subj, day, s)] == 0)

                # Block from H2 on OE-1hr days to prevent gap for non-flagged-subject students
                model.Add(tv[(div, subj, day, 1)] == 0).OnlyEnforceIf(oe1[day])

    # ---- Day-edges-only constraint (lab): same rule for flagged lab subjects -------
    day_edges_lab_idx = [i for i, s in enumerate(LAB_SUBJ) if s.get('day_edges_only')]
    for ls in day_edges_lab_idx:
        for b in range(NUM_BATCHES):
            for day in range(NUM_DAYS):
                # Block start slots in the mid-day: H3, H4, H5, H6 (indices 2, 3, 4, 5)
                # Allowed start slots: H1 (0), H2 (1) and H7 (6), H8 (7)
                for ss in [2, 3, 4, 5]:
                    if ss in LAB_START_SLOTS:
                        model.Add(lv[(b, ls, day, ss)] == 0)

                # Block from H2 on OE-1hr days to prevent gap for non-flagged-subject students
                if 1 in LAB_START_SLOTS:
                    model.Add(lv[(b, ls, day, 1)] == 0).OnlyEnforceIf(oe1[day])

    # ---- Division-in-lab indicator + no theory during lab -------------------
    # Theory is division-wide — if ANY batch of the division is in lab at slot s,
    # the whole division cannot run theory at that slot (students attending theory
    # must all be present; batch in lab cannot attend theory simultaneously).
    div_in_lab_at = {}   # (div, day, slot) -> BoolVar
    for div in range(NUM_DIVS):
        batches_of_div = DIV_TO_BATCHES[div]   # e.g. [0,1] for D1
        for day in range(NUM_DAYS):
            for slot in ACADEMIC_SLOTS:
                lab_vars = [
                    lv[(b, ls, day, ss)]
                    for b in batches_of_div
                    for ls in range(NUM_LAB_SUBJ)
                    for ss in LAB_START_SLOTS
                    if ss == slot or ss + 1 == slot
                ]
                if lab_vars:
                    dil = model.NewBoolVar(f'dil_{div}_{day}_{slot}')
                    model.AddMaxEquality(dil, lab_vars)   # 1 if ANY batch in lab
                    div_in_lab_at[(div, day, slot)] = dil
                    # No theory for this division during lab slots
                    for subj in range(NUM_THEORY_SUBJ):
                        model.Add(tv[(div, subj, day, slot)] + dil <= 1)

    # ---- HC8: Teacher clash (≤ 1 activity per teacher per day-slot) ---------
    for teacher in ALL_TEACHERS:
        for day in range(NUM_DAYS):
            for slot in ACADEMIC_SLOTS:
                t_vars = []
                # Theory contributions
                for div in range(NUM_DIVS):
                    for subj in range(NUM_THEORY_SUBJ):
                        if THEORY_SUBJ[subj]['teachers'].get(div) == teacher:
                            t_vars.append(tv[(div, subj, day, slot)])
                # Lab contributions — all labs treated uniformly (including sibling-sync labs)
                for b in range(NUM_BATCHES):
                    for ls in range(NUM_LAB_SUBJ):
                        if LAB_SUBJ[ls]['teachers'].get(b) == teacher:
                            for ss in LAB_START_SLOTS:
                                if ss == slot or ss + 1 == slot:
                                    t_vars.append(lv[(b, ls, day, ss)])
                if t_vars:
                    model.Add(sum(t_vars) <= 1)

    # ---- HC21: Theory and lab of same subject cannot overlap ----------------
    for (ts, ls) in THEORY_LAB_PAIRS:
        for div in range(NUM_DIVS):
            b_rep = DIV_TO_BATCHES[div][0]
            for day in range(NUM_DAYS):
                for slot in ACADEMIC_SLOTS:
                    lab_at_slot = [
                        lv[(b_rep, ls, day, ss)]
                        for ss in LAB_START_SLOTS
                        if ss == slot or ss + 1 == slot
                    ]
                    if lab_at_slot:
                        dil = model.NewBoolVar(f'tlc_{div}_{ts}_{day}_{slot}')
                        model.AddMaxEquality(dil, lab_at_slot)
                        model.Add(tv[(div, ts, day, slot)] + dil <= 1)

    # ---- HC1 (daily hours): Each division has MIN..MAX academic hours/day ---
    div_day_hours = {}
    for div in range(NUM_DIVS):
        batches_of_div = DIV_TO_BATCHES[div]   # both batches, e.g. [0,1]
        for day in range(NUM_DAYS):
            terms = []
            # Theory: 1 hr each
            for subj in range(NUM_THEORY_SUBJ):
                for slot in ACADEMIC_SLOTS:
                    terms.append(tv[(div, subj, day, slot)])
            # OE: oe1 day = 1 hr; oe2 day = 2 hrs (add twice)
            terms.append(oe1[day])
            terms.append(oe2[day])
            terms.append(oe2[day])
            # Lab hours: count UNION of both batches' lab blocks (not just b_rep).
            for ss in LAB_START_SLOTS:
                any_lab_at_ss = model.NewBoolVar(f'anylb_{div}_{day}_{ss}')
                lab_at_ss_vars = [
                    lv[(b, ls, day, ss)]
                    for b in batches_of_div
                    for ls in range(NUM_LAB_SUBJ)
                ]
                model.AddMaxEquality(any_lab_at_ss, lab_at_ss_vars)
                # 2 hrs per distinct lab block occupied by division
                terms.append(any_lab_at_ss)
                terms.append(any_lab_at_ss)
            h = model.NewIntVar(0, 16, f'hours_{div}_{day}')
            model.Add(h == sum(terms))
            model.Add(h >= MIN_HOURS_PER_DAY)
            model.Add(h <= MAX_HOURS_PER_DAY)
            div_day_hours[(div, day)] = h

    # ---- HC2: Daily hours must fluctuate (at least 2 different values) ------
    for div in range(NUM_DIVS):
        day_hvars = [div_day_hours[(div, d)] for d in range(NUM_DAYS)]
        min_h = model.NewIntVar(0, 10, f'minh_{div}')
        max_h = model.NewIntVar(0, 10, f'maxh_{div}')
        model.AddMinEquality(min_h, day_hvars)
        model.AddMaxEquality(max_h, day_hvars)
        model.Add(max_h - min_h >= 1)

    # ---- Constraint D (spec): Labs must run on every working day ------------
    for day in range(NUM_DAYS):
        model.Add(
            sum(lv[(b, ls, day, ss)]
                for b in range(NUM_BATCHES)
                for ls in range(NUM_LAB_SUBJ)
                for ss in LAB_START_SLOTS) >= 1)

    # =========================================================================
    # C.  MULTI-OBJECTIVE SCORING
    # =========================================================================
    faculty_penalties = []
    student_penalties = []
    resource_penalties = []

    # -------------------------------------------------------------------------
    # FACULTY SCORE
    # -------------------------------------------------------------------------
    for teacher in ALL_TEACHERS:
        ta = {}
        daily_load = []
        for day in range(NUM_DAYS):
            load_terms = []
            for slot in ACADEMIC_SLOTS:
                t_vars = []
                for div in range(NUM_DIVS):
                    for subj in range(NUM_THEORY_SUBJ):
                        if THEORY_SUBJ[subj]['teachers'].get(div) == teacher:
                            t_vars.append(tv[(div, subj, day, slot)])
                for b in range(NUM_BATCHES):
                    for ls in range(NUM_LAB_SUBJ):
                        if LAB_SUBJ[ls]['teachers'].get(b) == teacher:
                            for ss in LAB_START_SLOTS:
                                if ss == slot or ss + 1 == slot:
                                    t_vars.append(lv[(b, ls, day, ss)])
                if t_vars:
                    act = model.NewBoolVar(f'ta_{teacher}_{day}_{slot}')
                    model.Add(sum(t_vars) >= 1).OnlyEnforceIf(act)
                    model.Add(sum(t_vars) == 0).OnlyEnforceIf(act.Not())
                    ta[(day, slot)] = act
                    load_terms.append(act)

            if load_terms:
                ld = model.NewIntVar(0, 12, f'fac_ld_{teacher}_{day}')
                model.Add(ld == sum(load_terms))
                daily_load.append(ld)

                overload = model.NewIntVar(0, 12, f'fac_over_{teacher}_{day}')
                model.AddMaxEquality(overload, [0, ld - 4])
                faculty_penalties.append(W_FAC_OVERLOAD * overload)

        consec_triples = [(0, 1, 2), (1, 2, 3), (5, 6, 7), (6, 7, 8)]
        for day in range(NUM_DAYS):
            for (s1, s2, s3) in consec_triples:
                if (day, s1) in ta and (day, s2) in ta and (day, s3) in ta:
                    consec3 = model.NewBoolVar(f'fac_c3_{teacher}_{day}_{s1}')
                    model.AddBoolAnd([ta[(day, s1)], ta[(day, s2)], ta[(day, s3)]]).OnlyEnforceIf(consec3)
                    model.AddBoolOr([ta[(day, s1)].Not(), ta[(day, s2)].Not(), ta[(day, s3)].Not()]).OnlyEnforceIf(consec3.Not())
                    faculty_penalties.append(W_FAC_CONSEC * consec3)

        for day in range(NUM_DAYS):
            for (s1, s2, s3) in consec_triples:
                if (day, s1) in ta and (day, s2) in ta and (day, s3) in ta:
                    gap = model.NewBoolVar(f'fac_gap_{teacher}_{day}_{s1}')
                    model.AddBoolAnd([ta[(day, s1)], ta[(day, s2)].Not(), ta[(day, s3)]]).OnlyEnforceIf(gap)
                    model.AddBoolOr([ta[(day, s1)].Not(), ta[(day, s2)], ta[(day, s3)].Not()]).OnlyEnforceIf(gap.Not())
                    faculty_penalties.append(W_FAC_GAP * gap)

        for day in range(NUM_DAYS):
            if (day, 0) in ta:
                faculty_penalties.append(W_FAC_H1 * ta[(day, 0)])
            if (day, LAST_SLOT) in ta:
                faculty_penalties.append(W_FAC_H9 * ta[(day, LAST_SLOT)])

    # -------------------------------------------------------------------------
    # STUDENT SCORE
    # -------------------------------------------------------------------------
    active_physical = {}
    for div in range(NUM_DIVS):
        for day in range(NUM_DAYS):
            for s in range(9):
                act_s = model.NewBoolVar(f'stu_act_{div}_{day}_{s}')
                terms = []
                for subj in range(NUM_THEORY_SUBJ):
                    terms.append(tv[(div, subj, day, s)])
                for b in DIV_TO_BATCHES[div]:
                    for ls in range(NUM_LAB_SUBJ):
                        for ss in LAB_START_SLOTS:
                            if ss == s or ss + 1 == s:
                                terms.append(lv[(b, ls, day, ss)])
                if s == 0:
                    terms.append(oe1[day])
                if s in (0, 1):
                    terms.append(oe2[day])

                if terms:
                    model.Add(sum(terms) >= 1).OnlyEnforceIf(act_s)
                    model.Add(sum(terms) == 0).OnlyEnforceIf(act_s.Not())
                else:
                    model.Add(act_s == 0)
                active_physical[(div, day, s)] = act_s

                model.Add(act_s == 0).OnlyEnforceIf(is_break[(div, day, s)])
                model.Add(is_on_campus[(div, day, s)] == act_s + is_break[(div, day, s)])

            model.Add(sum(is_on_campus[(div, day, s)] for s in range(9)) == div_day_hours[(div, day)] + 1)

            # =================================================================
            # Hard Constraint 1 — No Student Idle Gaps:
            # Within both pre-break block and post-break block independently,
            # occupied slots must form a single contiguous block with no holes.
            # =================================================================
            for b in range(9):
                # Pre-break block: slots 0 .. b-1
                for i in range(b - 2):
                    for j in range(i + 1, b - 1):
                        for k in range(j + 1, b):
                            model.AddBoolOr([
                                is_break[(div, day, b)].Not(),
                                active_physical[(div, day, i)].Not(),
                                active_physical[(div, day, j)],
                                active_physical[(div, day, k)].Not()
                            ])

                # Post-break block: slots b+1 .. 8
                for i in range(b + 1, 7):
                    for j in range(i + 1, 8):
                        for k in range(j + 1, 9):
                            model.AddBoolOr([
                                is_break[(div, day, b)].Not(),
                                active_physical[(div, day, i)].Not(),
                                active_physical[(div, day, j)],
                                active_physical[(div, day, k)].Not()
                            ])

            # =================================================================
            # Flexible Break Window Constraints:
            # - At least 2 active slots before break (flexible slot placement)
            # - At least 1 active slot after break (no break at end of day)
            # =================================================================
            for b in range(9):
                after_break_slots = [active_physical[(div, day, s)] for s in range(b + 1, 9)]
                if after_break_slots:
                    model.Add(sum(after_break_slots) >= 1).OnlyEnforceIf(is_break[(div, day, b)])
                else:
                    model.Add(is_break[(div, day, b)] == 0)

                before_break_slots = [active_physical[(div, day, s)] for s in range(0, b)]
                if len(before_break_slots) >= 2:
                    model.Add(sum(before_break_slots) >= 2).OnlyEnforceIf(is_break[(div, day, b)])
                else:
                    model.Add(is_break[(div, day, b)] == 0)

    # ST2: >2 consecutive difficult subjects
    consec_triples = [(0, 1, 2), (1, 2, 3), (5, 6, 7), (6, 7, 8)]
    for div in range(NUM_DIVS):
        for day in range(NUM_DAYS):
            for (s1, s2, s3) in consec_triples:
                diff_flags = []
                for slot in (s1, s2, s3):
                    is_diff = model.NewBoolVar(f'stu_s4_{div}_{day}_{slot}')
                    diff_here = [tv[(div, subj, day, slot)] for subj in DIFFICULT_SUBJ_IDX]
                    model.Add(sum(diff_here) >= 1).OnlyEnforceIf(is_diff)
                    model.Add(sum(diff_here) == 0).OnlyEnforceIf(is_diff.Not())
                    diff_flags.append(is_diff)
                all_diff = model.NewBoolVar(f'stu_s4all_{div}_{day}_{s1}')
                model.AddBoolAnd(diff_flags).OnlyEnforceIf(all_diff)
                model.AddBoolOr([v.Not() for v in diff_flags]).OnlyEnforceIf(all_diff.Not())
                student_penalties.append(W_STU_CONSEC_DIFF * all_diff)

    # ST3: Theory in H9 penalty
    for div in range(NUM_DIVS):
        for subj in range(NUM_THEORY_SUBJ):
            for day in range(NUM_DAYS):
                student_penalties.append(W_STU_THEORY_H9 * tv[(div, subj, day, LAST_SLOT)])

    # ST4: Same theory subject on 3+ consecutive days penalty
    for div in range(NUM_DIVS):
        for subj in range(NUM_THEORY_SUBJ):
            for day in range(NUM_DAYS - 2):
                d1_act = model.NewBoolVar(f'stu_3d_{div}_{subj}_{day}')
                model.Add(sum(tv[(div, subj, day, s)] for s in ACADEMIC_SLOTS) >= 1).OnlyEnforceIf(d1_act)
                model.Add(sum(tv[(div, subj, day, s)] for s in ACADEMIC_SLOTS) == 0).OnlyEnforceIf(d1_act.Not())

                d2_act = model.NewBoolVar(f'stu_3d_{div}_{subj}_{day+1}')
                model.Add(sum(tv[(div, subj, day+1, s)] for s in ACADEMIC_SLOTS) >= 1).OnlyEnforceIf(d2_act)
                model.Add(sum(tv[(div, subj, day+1, s)] for s in ACADEMIC_SLOTS) == 0).OnlyEnforceIf(d2_act.Not())

                d3_act = model.NewBoolVar(f'stu_3d_{div}_{subj}_{day+2}')
                model.Add(sum(tv[(div, subj, day+2, s)] for s in ACADEMIC_SLOTS) >= 1).OnlyEnforceIf(d3_act)
                model.Add(sum(tv[(div, subj, day+2, s)] for s in ACADEMIC_SLOTS) == 0).OnlyEnforceIf(d3_act.Not())

                consec3 = model.NewBoolVar(f'stu_c3_{div}_{subj}_{day}')
                model.AddBoolAnd([d1_act, d2_act, d3_act]).OnlyEnforceIf(consec3)
                model.AddBoolOr([d1_act.Not(), d2_act.Not(), d3_act.Not()]).OnlyEnforceIf(consec3.Not())
                student_penalties.append(W_STU_3DAYS_SAME * consec3)

    # -------------------------------------------------------------------------
    # RESOURCE SCORE
    # -------------------------------------------------------------------------
    cr_totals = []
    for div in range(NUM_DIVS):
        cu = model.NewIntVar(0, 100, f'res_cu_{div}')
        cr_terms = []
        for subj in range(NUM_THEORY_SUBJ):
            for day in range(NUM_DAYS):
                for s in ACADEMIC_SLOTS:
                    cr_terms.append(tv[(div, subj, day, s)])
        cr_terms.append(sum(oe1))
        cr_terms.append(sum(oe2) * 2)
        model.Add(cu == sum(cr_terms))
        cr_totals.append(cu)

    cu_max = model.NewIntVar(0, 100, 'res_cumax')
    cu_min = model.NewIntVar(0, 100, 'res_cumin')
    model.AddMaxEquality(cu_max, cr_totals)
    model.AddMinEquality(cu_min, cr_totals)
    resource_penalties.append(W_RES_CLASSROOM * (cu_max - cu_min))

    room_totals = []
    for r in range(NUM_LABS):
        ru = model.NewIntVar(0, 200, f'res_ru_{r}')
        model.Add(ru == sum(lr[(b, ls, d, ss, r)]
                             for b in range(NUM_BATCHES)
                             for ls in range(NUM_LAB_SUBJ)
                             for d in range(NUM_DAYS)
                             for ss in LAB_START_SLOTS))
        room_totals.append(ru)
    ru_max = model.NewIntVar(0, 200, 'res_rumax')
    ru_min = model.NewIntVar(0, 200, 'res_rumin')
    model.AddMaxEquality(ru_max, room_totals)
    model.AddMinEquality(ru_min, room_totals)
    resource_penalties.append(W_RES_LAB * (ru_max - ru_min))

    # =========================================================================
    # D.  OBJECTIVE — expose 3 scores + minimize their sum
    # =========================================================================
    faculty_score = model.NewIntVar(0, 10_000_000, 'faculty_score')
    student_score = model.NewIntVar(0, 10_000_000, 'student_score')
    resource_score = model.NewIntVar(0, 10_000_000, 'resource_score')

    model.Add(faculty_score == sum(faculty_penalties))
    model.Add(student_score == sum(student_penalties))
    model.Add(resource_score == sum(resource_penalties))

    total_penalty = model.NewIntVar(0, 10_000_000, 'total_penalty')
    model.Add(total_penalty == faculty_score + student_score + resource_score)
    model.Minimize(total_penalty)

    vars_dict = {
        'tv': tv,
        'lv': lv,
        'lr': lr,
        'oe1': oe1,
        'oe2': oe2,
        'is_break': is_break,
        'div_day_hours': div_day_hours,
        'faculty_score': faculty_score,
        'student_score': student_score,
        'resource_score': resource_score,
        'total_penalty': total_penalty,
    }
    return model, vars_dict
