"""Export a Solution to human-shareable formats: Excel (per-division grid) and official DJSCE PDF."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from engine.models import ProblemInstance, Solution, expand_requirements, SessionType

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

STANDARD_PERIOD_TIMES = [
    "08:00-09:00",
    "09:00-10:00",
    "10:00-11:00",
    "11:00-12:00",
    "12:00-01:00",
    "01:00-02:00",
    "02:00-03:00",
    "03:00-04:00",
    "04:00-05:00",
    "05:00-06:00",
]

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
SVKM_LOGO = ASSETS_DIR / "svkm_logo.png"
DJSCE_LOGO = ASSETS_DIR / "djsce_logo.png"
SIGNATURES_IMG = ASSETS_DIR / "signatures.png"


def _safe_sheet_title(name: str, existing: set[str]) -> str:
    clean = re.sub(r"[:\\/?*\[\]]", "-", name)
    clean = clean.strip("' ")[:31] or "Sheet"
    base = clean
    counter = 1
    while clean.lower() in existing:
        suffix = f"_{counter}"
        clean = base[: 31 - len(suffix)] + suffix
        counter += 1
    existing.add(clean.lower())
    return clean


def _division_grids(solution: Solution, problem: ProblemInstance) -> dict[str, dict[tuple[int, int], str]]:
    requirements = {r.id: r for r in expand_requirements(problem)}
    assignments = solution.assignment_by_session()
    slots_by_id = {t.id: t for t in problem.time_slots}
    rooms_by_id = problem.room_by_id()

    grids: dict[str, dict[tuple[int, int], str]] = {d.id: {} for d in problem.divisions}
    for req_id, assignment in assignments.items():
        req = requirements.get(req_id)
        if req is None or req.is_break:
            continue
        ts = slots_by_id.get(assignment.time_slot_id)
        if ts is None:
            continue
        room = rooms_by_id.get(assignment.room_id)
        clean_code = req.course_code.split("::")[-1]
        label = f"{clean_code} [{req.session_type.value}]"
        if req.faculty_id:
            label += f" {{{req.faculty_id}}}"
        if room:
            clean_rm = room.id.split("-")[-1] if "-" in room.id else room.id
            label += f" ({clean_rm})"
        if req.batch_id:
            label += f" {req.batch_id}"
        grid = grids.setdefault(req.division_id, {})
        for k in range(req.duration_slots):
            key = (ts.day, ts.period + k)
            if key in grid and grid[key]:
                grid[key] = grid[key] + "\n" + label
            else:
                grid[key] = label

    # mark break slots
    for req_id, assignment in assignments.items():
        req = requirements.get(req_id)
        if req is None or not req.is_break:
            continue
        ts = slots_by_id.get(assignment.time_slot_id)
        if ts is None:
            continue
        grids.setdefault(req.division_id, {})[(ts.day, ts.period)] = "BREAK"

    return grids


def export_xlsx(solution: Solution, problem: ProblemInstance, path: str | Path) -> None:
    grids = _division_grids(solution, problem)
    periods = sorted({t.period for t in problem.time_slots})
    period_labels = {t.period: f"{t.start}-{t.end}" for t in problem.time_slots}

    wb = Workbook()
    wb.remove(wb.active)
    existing_sheets: set[str] = set()
    for division in problem.divisions:
        sheet_title = _safe_sheet_title(division.id, existing_sheets)
        ws = wb.create_sheet(title=sheet_title)
        ws.cell(row=1, column=1, value="Period").font = Font(bold=True)
        for day in range(problem.days_per_week):
            cell = ws.cell(row=1, column=day + 2, value=DAY_NAMES[day])
            cell.font = Font(bold=True)

        grid = grids.get(division.id, {})
        for row_idx, period in enumerate(periods, start=2):
            ws.cell(row=row_idx, column=1, value=period_labels.get(period, str(period)))
            for day in range(problem.days_per_week):
                value = grid.get((day, period), "")
                cell = ws.cell(row=row_idx, column=day + 2, value=value)
                if value == "BREAK":
                    cell.fill = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
        for col_idx in range(1, problem.days_per_week + 2):
            ws.column_dimensions[chr(64 + col_idx)].width = 24

    wb.save(str(path))


class StructuredCell:
    def __init__(self):
        self.is_break = False
        self.is_covered = False  # covered by earlier multi-period slot
        self.duration = 1
        self.lines: list[str] = []


def _build_division_cells(division_id: str, solution: Solution, problem: ProblemInstance) -> dict[tuple[int, int], StructuredCell]:
    requirements = {r.id: r for r in expand_requirements(problem)}
    assignments = solution.assignment_by_session()
    slots_by_id = {t.id: t for t in problem.time_slots}
    rooms_by_id = problem.room_by_id()

    cells: dict[tuple[int, int], StructuredCell] = {}
    for day in range(problem.days_per_week):
        for period in range(10):
            cells[(day, period)] = StructuredCell()

    # 1. Breaks
    for req_id, assignment in assignments.items():
        req = requirements.get(req_id)
        if req is None or not req.is_break or req.division_id != division_id:
            continue
        ts = slots_by_id.get(assignment.time_slot_id)
        if ts is not None and (ts.day, ts.period) in cells:
            cells[(ts.day, ts.period)].is_break = True

    # 2. Teaching sessions
    for req_id, assignment in assignments.items():
        req = requirements.get(req_id)
        if req is None or req.is_break or req.division_id != division_id:
            continue
        ts = slots_by_id.get(assignment.time_slot_id)
        if ts is None or (ts.day, ts.period) not in cells:
            continue
        room = rooms_by_id.get(assignment.room_id)
        clean_code = req.course_code.split("::")[-1]

        # Formatting matching SVKM DJSCE official timetable:
        # e.g.: FMRA [TH] {AG} (53) or CSF [PR] D11 {SSM} (L1)
        type_str = "TH" if req.session_type == SessionType.THEORY else ("PR" if req.session_type == SessionType.PRACTICAL else "TUT")
        line = f"{clean_code} [{type_str}]"
        if req.batch_id:
            line += f" {req.batch_id}"
        if req.faculty_id:
            line += f" {{{req.faculty_id}}}"
        if room and room.id != "NO_ROOM":
            clean_rm = room.id.replace("CR-", "").replace("CR", "")
            line += f" ({clean_rm})"

        start_cell = cells[(ts.day, ts.period)]
        start_cell.lines.append(line)
        if req.duration_slots > start_cell.duration:
            start_cell.duration = req.duration_slots

        for k in range(1, req.duration_slots):
            covered_key = (ts.day, ts.period + k)
            if covered_key in cells:
                cells[covered_key].is_covered = True

    return cells


def export_pdf(solution: Solution, problem: ProblemInstance, path: str | Path) -> None:
    """Generate official DJSCE SVKM Landscape A4 Year-Wise Timetable PDF."""
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)

    faculty_by_id = {f.id: f for f in problem.faculty}
    courses_by_code = {c.code.split("::")[-1]: c for c in problem.courses}
    rooms_by_id = problem.room_by_id()

    # Known DJSCE Room expansions
    KNOWN_ROOM_EXPANSIONS = {
        "51": "CR 51: 5th Floor (CSE-DS Department)",
        "52": "CR 52: 5th Floor (CSE-DS Department)",
        "53": "CR 53: 5th Floor (CSE-DS Department)",
        "CR-51": "CR 51: 5th Floor (CSE-DS Department)",
        "CR-52": "CR 52: 5th Floor (CSE-DS Department)",
        "CR-53": "CR 53: 5th Floor (CSE-DS Department)",
        "L1": "L1: Data Analytics Lab - 5th Floor (CSE-DS Department)",
        "L2": "L2: Machine Learning Lab - 5th Floor (CSE-DS Department)",
        "L3": "L3: Computer Vision Lab - 5th Floor (CSE-DS Department)",
        "L4": "L4: High Performance Computing Lab - 5th Floor (CSE-DS Department)",
        "ICB-L1": "ICB-L1: IoT & Cloud Computing Lab - 5th Floor",
    }

    # Known standard institutional subject expansions
    KNOWN_SUBJECT_NAMES = {
        "CSF": "Computer System Fundamentals",
        "DBS": "Database Systems",
        "DSL": "Data Science Laboratory (Python)",
        "MIS": "Mathematics for Intelligent Systems",
        "DT": "Design Thinking Laboratory",
        "UHV": "Universal Human Values",
        "PAIE": "Python for AI Engineering",
        "FMRA": "Financial Market and Risk Analysis",
        "ADSL": "Advanced Data Structures Lab",
        "ML-I": "Machine Learning-I",
        "ML-III": "Machine Learning-III (Deep Learning)",
        "NLP": "Natural Language Processing",
        "QPM": "Quantitative Project Management",
        "Adv_Stat": "Advanced Statistics for Data Science",
        "Adv Stats": "Advanced Statistics for Data Science",
        "TSA": "Time Series Analysis",
        "AAIA": "Analysis of AI Algorithms",
        "MIII": "Medical Imaging Informatics and Interoperability",
        "CV": "Computer Vision",
        "Robotics": "Robotics and AI",
        "EVS": "Environmental Studies & Constitution of India",
        "Data_Ethics": "Data Ethics and Governance",
        "HPC": "High Performance Computing Laboratory",
        "Green_DS": "Green Data Science",
        "ARC": "Academic Research & Capstone",
        "OE": "Open Elective",
        "CES": "Community Engagement Service",
        "IPD": "Innovative Product Development / Research Work",
    }

    KNOWN_FACULTY_NAMES = {
        "KS": "Dr. Kriti Srivastava",
        "SV": "Dr. Satishkumar Varma",
        "NM": "Dr. Nilesh Marathe",
        "MD": "Prof. Mangal Dandekar",
        "AG": "Dr. Ajitkumar Gudekar",
        "KD": "Dr. Kanchan Dabre",
        "PV": "Prof. Pooja Vartak",
        "PSJ": "Prof. Pradnya Joshi",
        "AS": "Prof. Adil Shaikh",
        "RP": "Prof. Ruchika Patil",
        "PJ": "Dr. Poonam Jadhav",
        "SM": "Dr. Shruti Mathur",
        "SAM": "Prof. Samiran Maity",
        "MAA": "Prof. Mrudul Arkadi",
        "NP": "Dr. Namita Pulgam",
        "PP": "Prof. Priya Parate",
        "GW": "Prof. Govind Wakure",
        "AR": "Prof. Aditi Raut",
        "SSM": "Dr. Shraddha More",
        "TC": "Ms. Trupti Chandak (Visiting)",
        "SF": "Ms. Sonal Foss (Visiting)",
        "ND": "Dr. Niti Desai (Visiting)",
        "PT": "Prof. Pradeep Talwelkar",
        "RK": "Prof. Rupali Karande",
        "AB": "Prof. Alisha Banz",
    }

    for division in problem.divisions:
        pdf.add_page()

        # Division metadata parsing
        raw_id = division.id.split("::")[-1]
        div_label = raw_id if raw_id.startswith("D") else division.id
        sem = getattr(division, "semester", 4)
        if sem in (3, 4):
            class_label = f"SY ({div_label})"
            sem_roman = "IV" if sem == 4 else "III"
            term_label = "January - May 2027" if sem % 2 == 0 else "July - December 2026"
        elif sem in (5, 6):
            class_label = f"TY ({div_label})"
            sem_roman = "VI" if sem == 6 else "V"
            term_label = "January - May 2027" if sem % 2 == 0 else "July - December 2026"
        else:
            class_label = f"BTech ({div_label})"
            sem_roman = "VIII" if sem == 8 else "VII"
            term_label = "January - May 2027" if sem % 2 == 0 else "July - December 2026"

        wef_date = "4th January 2027" if "2027" in term_label else "16th July 2026"

        # ---- HEADER BLOCK ----
        header_top_y = 6.0
        # SVKM & DJSCE Logos
        if SVKM_LOGO.exists():
            pdf.image(str(SVKM_LOGO), x=14, y=header_top_y + 1, w=15, h=15)
        if DJSCE_LOGO.exists():
            pdf.image(str(DJSCE_LOGO), x=268, y=header_top_y + 1, w=15, h=15)

        pdf.set_y(header_top_y)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.cell(0, 3.8, "Shri Vile Parle Kelavani Mandal's", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 4.6, "DWARKADAS J. SANGHVI COLLEGE OF ENGINEERING", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font("Helvetica", "", 6.5)
        pdf.cell(0, 3.4, "(Autonomous College Affiliated to the University of Mumbai)", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.cell(0, 3.4, 'NAAC Accredited with "A" Grade (CGPA : 3.18)', align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(0, 3.8, "Department of Computer Science and Engineering (Data Science)", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.set_font("Helvetica", "", 7.5)
        pdf.cell(0, 3.8, f"Timetable for the academic term: {term_label}", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.ln(1.5)

        # ---- CLASS / SEM / WEF SUB-HEADER BAR ----
        sub_y = pdf.get_y()
        margin_x = 10.0
        content_w = 277.0

        pdf.set_xy(margin_x, sub_y)
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.cell(60, 4.5, f"Class: {class_label}", align="L")
        pdf.cell(157, 4.5, f"Sem: {sem_roman}", align="C")
        pdf.cell(60, 4.5, f"W.E.F.: {wef_date}", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        pdf.ln(1.0)

        # ---- MAIN TIMETABLE GRID ----
        grid_top_y = pdf.get_y()
        col_w_no = 8.0
        col_w_period = 25.0
        weekday_cols = 6  # Mon to Sat
        col_w_day = (content_w - col_w_no - col_w_period) / weekday_cols  # ~40.66 mm
        row_h_header = 4.8
        row_h_slot = 7.3

        # Grid Header Row
        pdf.set_xy(margin_x, grid_top_y)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.cell(col_w_no, row_h_header, "No.", border=1, align="C")
        pdf.cell(col_w_period, row_h_header, "Period", border=1, align="C")
        days_displayed = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        for d_name in days_displayed:
            pdf.cell(col_w_day, row_h_header, d_name, border=1, align="C")
        pdf.ln(row_h_header)

        # Prepare cell data
        cells = _build_division_cells(division.id, solution, problem)

        current_y = grid_top_y + row_h_header
        for period in range(10):
            slot_y = current_y + (period * row_h_slot)

            # Column 1: No.
            pdf.set_xy(margin_x, slot_y)
            pdf.set_font("Helvetica", "", 7)
            pdf.cell(col_w_no, row_h_slot, str(period + 1), border=1, align="C")

            # Column 2: Period Time
            p_time = STANDARD_PERIOD_TIMES[period] if period < len(STANDARD_PERIOD_TIMES) else f"P{period}"
            pdf.cell(col_w_period, row_h_slot, p_time, border=1, align="C")

            # Columns 3 to 7: Monday to Friday
            for day in range(5):
                cell_x = margin_x + col_w_no + col_w_period + (day * col_w_day)
                cell = cells.get((day, period))

                if cell and cell.is_covered:
                    # Covered by previous multi-slot session: skip drawing border to allow merged appearance
                    continue

                dur = cell.duration if cell else 1
                box_h = row_h_slot * dur

                pdf.set_xy(cell_x, slot_y)
                if cell and cell.is_break:
                    pdf.set_fill_color(225, 230, 235)
                    pdf.set_font("Helvetica", "B", 7.5)
                    pdf.cell(col_w_day, box_h, "BREAK", border=1, align="C", fill=True)
                elif cell and cell.lines:
                    # Multi-line cell (e.g. batch labs stacked or single lecture)
                    pdf.rect(cell_x, slot_y, col_w_day, box_h)
                    lines = cell.lines
                    pdf.set_font("Helvetica", "", 5.8)
                    total_text_h = len(lines) * 3.0
                    start_text_y = slot_y + max(0.5, (box_h - total_text_h) / 2.0)
                    for idx, line in enumerate(lines):
                        pdf.set_xy(cell_x + 0.5, start_text_y + (idx * 3.0))
                        pdf.cell(col_w_day - 1.0, 3.0, line[:36], align="C")
                else:
                    pdf.cell(col_w_day, box_h, "", border=1)

            # Column 8: Saturday
            sat_x = margin_x + col_w_no + col_w_period + (5 * col_w_day)
            pdf.set_xy(sat_x, slot_y)
            # Official markers on Saturday mid-day slots
            if period == 4:
                pdf.set_font("Helvetica", "", 7.5)
                pdf.cell(col_w_day, row_h_slot, "*", border=1, align="C")
            elif period == 5:
                pdf.set_font("Helvetica", "", 7.5)
                pdf.cell(col_w_day, row_h_slot, "#", border=1, align="C")
            elif period == 6:
                pdf.set_font("Helvetica", "", 7.5)
                pdf.cell(col_w_day, row_h_slot, "$", border=1, align="C")
            else:
                pdf.cell(col_w_day, row_h_slot, "", border=1)

        # Footnote below grid
        footnote_y = current_y + (10 * row_h_slot) + 0.8
        pdf.set_xy(margin_x, footnote_y)
        pdf.set_font("Helvetica", "", 5.5)
        pdf.cell(
            content_w,
            3.2,
            "*Remedial / extra lectures and doubt-clearing sessions, # Hackathons and various events under the student chapters and clubs, $ Guest lectures and Innovative product development presentations.",
            align="L",
        )

        # ---- LEGEND BOX ----
        legend_top_y = footnote_y + 4.2
        col_w_sub = 95.0
        col_w_loc = 82.0
        col_w_fac = 100.0
        row_h_leg_hdr = 4.2
        row_h_leg = 3.4

        pdf.set_xy(margin_x, legend_top_y)
        pdf.set_font("Helvetica", "B", 6.8)
        pdf.cell(col_w_sub, row_h_leg_hdr, "Subject Names", border=1, align="L")
        pdf.cell(col_w_loc, row_h_leg_hdr, "Class and Lab Names", border=1, align="L")
        pdf.cell(col_w_fac, row_h_leg_hdr, "Faculty Names", border=1, align="L")
        pdf.ln(row_h_leg_hdr)

        # Gather active courses in this division
        active_courses: list[str] = []
        for c_code in division.course_codes:
            clean_c = c_code.split("::")[-1]
            c_obj = courses_by_code.get(clean_c)
            full_name = KNOWN_SUBJECT_NAMES.get(clean_c, getattr(c_obj, "title", clean_c))
            active_courses.append(f"{clean_c}: {full_name}")

        # Add institutional standard subjects if space permits
        if sem in (3, 4):
            for std in ["OE: Open Elective", "CES: Community Engagement Service", "IPD: Innovative Product Development I"]:
                if std not in active_courses:
                    active_courses.append(std)

        # Classroom & lab locations
        loc_entries = [
            "CR 51/52/53: 5th Floor (CSE-DS Department)",
            "L1: Data Analytics Lab - 5th Floor (CSE-DS Department)",
            "L2: Machine Learning Lab - 5th Floor (CSE-DS Department)",
            "L3: Computer Vision Lab - 5th Floor (CSE-DS Department)",
            "L4: High Performance Computing Lab - 5th Floor (CSE-DS Department)",
        ]

        # Faculty teaching in this division
        active_faculty_set: set[str] = set()
        f_by_c = getattr(division, "faculty_by_course", {})
        for c, f_val in f_by_c.items():
            if isinstance(f_val, list):
                for f_item in f_val:
                    if f_item:
                        active_faculty_set.add(f_item)
            elif isinstance(f_val, str) and f_val:
                active_faculty_set.add(f_val)

        active_faculty: list[str] = []
        for fid in sorted(active_faculty_set):
            f_obj = faculty_by_id.get(fid)
            f_name = KNOWN_FACULTY_NAMES.get(fid, getattr(f_obj, "name", fid))
            active_faculty.append(f"{fid}: {f_name}")

        max_leg_rows = max(len(active_courses), len(loc_entries), len(active_faculty), 8)
        max_leg_rows = min(max_leg_rows, 9)  # Cap to fit cleanly on page

        pdf.set_font("Helvetica", "", 5.7)
        leg_row_y = legend_top_y + row_h_leg_hdr
        for r_idx in range(max_leg_rows):
            sub_text = active_courses[r_idx] if r_idx < len(active_courses) else ""
            loc_text = loc_entries[r_idx] if r_idx < len(loc_entries) else ""
            fac_text = active_faculty[r_idx] if r_idx < len(active_faculty) else ""

            pdf.set_xy(margin_x, leg_row_y + (r_idx * row_h_leg))
            pdf.cell(col_w_sub, row_h_leg, sub_text[:54], border=1, align="L")
            pdf.cell(col_w_loc, row_h_leg, loc_text[:46], border=1, align="L")
            pdf.cell(col_w_fac, row_h_leg, fac_text[:56], border=1, align="L")

        # ---- SIGNATURES BLOCK ----
        sig_y = leg_row_y + (max_leg_rows * row_h_leg) + 1.5
        if SIGNATURES_IMG.exists():
            # Exact official signatures strip
            pdf.image(str(SIGNATURES_IMG), x=margin_x + 1, y=sig_y, w=content_w - 2, h=16)
        else:
            # Elegant typographic fallback
            pdf.set_xy(margin_x, sig_y + 4)
            pdf.set_font("Helvetica", "B", 7)
            col_sig = content_w / 5.0
            sig_titles = [
                ("Dr. Ajitkumar G. / Pradnya J. / Mrudul A.", "Timetable Coordinator"),
                ("Dr. Kriti Srivastava", "Head of Department, CSE-DS"),
                ("Dr. Amit Chaudhari", "Timetable Convener"),
                ("Dr. Narendra M. Shekokar", "Vice Principal"),
                ("Dr. Hari Vasudevan", "Principal"),
            ]
            for name, title in sig_titles:
                pdf.cell(col_sig, 3.5, name, align="C")
            pdf.ln(3.5)
            pdf.set_font("Helvetica", "", 6.5)
            for name, title in sig_titles:
                pdf.cell(col_sig, 3.5, title, align="C")

    pdf.output(str(path))
