"""Master XLSX Allocation Sync Utility.
Syncs the authoritative load distribution from Load_Distribution_(5)_3-11-2026.xlsx
into platform.db for SY Sem IV, TY Sem VI, and BTech Sem VIII.
"""
from __future__ import annotations
from sqlmodel import Session, select
from webapp.models_db import Branch, Course, Division, Allocation, Faculty

def sync_master_xlsx_into_db(session: Session) -> dict:
    facs = {f.code: f for f in session.exec(select(Faculty)).all()}

    # Ensure any missing faculty exists
    for code, name in [
        ("ND", "Prof. Neha Deshmukh"),
        ("AB", "Prof. Ashutosh Bagwe"),
        ("KT", "Prof. Ketki"),
        ("RK", "Prof. Rahul K"),
        ("SAM", "Prof. S. A. More"),
        ("SF", "Ms. Sonal Fosa"),
        ("AVG", "Dr. A. V. Godbole"),
        ("MA", "Prof. Mayur A"),
    ]:
        if code not in facs:
            f = Faculty(code=code, name=name, max_load_hours_per_week=20, max_consecutive_sessions=2)
            session.add(f)
            session.commit()
            facs[code] = f

    def get_fac_id(code):
        if not code or code in ("-", "Nill", "None", ""):
            return None
        code = code.replace("PP TUT", "PP").strip()
        f = facs.get(code)
        return f.id if f else None

    # 1. SY Sem IV (Branch 5)
    b_sy = session.get(Branch, 5)
    if not b_sy:
        return {"error": "Branch 5 not found"}
    sy_divs = {d.name: d for d in session.exec(select(Division).where(Division.branch_id == 5)).all()}

    sy_courses_def = [
        ("ML-I", "Machine Learning - I", "Major", 3, 2, 0, False),
        ("DS", "Data Structures", "Major", 3, 2, 0, False),
        ("SDS", "Statistics for Data Science", "Major", 0, 2, 0, False),
        ("WE", "Web Engineering Laboratory", "Major", 0, 2, 0, False),
        ("EcoFin", "Economics and Financial Management", "Minor", 2, 0, 0, False),
        ("CMPM", "Computational Methods and Pricing Models", "Minor", 3, 2, 0, False),
        ("OE", "Open Elective", "Open-Elective", 3, 0, 0, False),
        ("ARC", "Academic Research Component", "Skill-Based", 1, 0, 0, False),
    ]

    sy_courses = {}
    for code, title, cat, th, pr, tut, heavy in sy_courses_def:
        c = session.exec(select(Course).where(Course.branch_id == 5, Course.code == code)).first()
        if not c:
            c = Course(branch_id=5, code=code, title=title, category=cat, theory_per_week=th, practical_per_week=pr, tutorial_per_week=tut, is_heavy=heavy)
            session.add(c)
            session.commit()
        else:
            c.title = title
            c.category = cat
            c.theory_per_week = th
            c.practical_per_week = pr
            c.tutorial_per_week = tut
            session.add(c)
            session.commit()
        sy_courses[code] = c

    # Remove any stale/legacy courses and their allocations in Branch 5
    valid_sy_cids = {c.id for c in sy_courses.values()}
    for old_c in session.exec(select(Course).where(Course.branch_id == 5)).all():
        if old_c.id not in valid_sy_cids:
            for old_a in session.exec(select(Allocation).where(Allocation.course_id == old_c.id)).all():
                session.delete(old_a)
            session.delete(old_c)
    session.commit()

    sy_alloc_matrix = {
        "D1": {
            "ML-I": ("SSM", "SSM", "KRS"),
            "DS": ("NM", "TC", "NM"),
            "SDS": (None, "AG", "RK"),
            "WE": (None, "PSJ", "GW"),
            "EcoFin": ("MD", None, None),
            "CMPM": ("MAA", "MAA", "MAA"),
            "OE": ("AG", None, None),
            "ARC": ("PV", None, None),
        },
        "D2": {
            "ML-I": ("KRS", "KRS", "SM"),
            "DS": ("SAM", "SAM", "SAM"),
            "SDS": (None, "RK", "PT"),
            "WE": (None, "GW", "SSM"),
            "EcoFin": ("MD", None, None),
            "CMPM": ("RP", "RP", "MD"),
            "OE": ("PT", None, None),
            "ARC": ("SSM", None, None),
        },
        "D3": {
            "ML-I": ("KRS", "SM", "KRS"),
            "DS": ("NP", "TC", "NP"),
            "SDS": (None, "MD", "MD"),
            "WE": (None, "PSJ", "GW"),
            "EcoFin": ("PT", None, None),
            "CMPM": ("MD", "MD", "MD"),
            "OE": ("PP", None, None),
            "ARC": ("RP", None, None),
        }
    }

    sy_count = 0
    for div_name, allocs in sy_alloc_matrix.items():
        div = sy_divs.get(div_name)
        if not div: continue
        for c_code, (th_fac, b1_fac, b2_fac) in allocs.items():
            course = sy_courses.get(c_code)
            if not course: continue
            alloc = session.exec(select(Allocation).where(Allocation.division_id == div.id, Allocation.course_id == course.id)).first()
            if not alloc:
                alloc = Allocation(
                    division_id=div.id,
                    course_id=course.id,
                    faculty_id=get_fac_id(th_fac),
                    batch1_faculty_id=get_fac_id(b1_fac),
                    batch2_faculty_id=get_fac_id(b2_fac),
                )
            else:
                alloc.faculty_id = get_fac_id(th_fac)
                alloc.batch1_faculty_id = get_fac_id(b1_fac)
                alloc.batch2_faculty_id = get_fac_id(b2_fac)
            session.add(alloc)
            sy_count += 1
    session.commit()

    # 2. TY Sem VI (Branch 19)
    b_ty = session.get(Branch, 19)
    ty_divs = {d.name: d for d in session.exec(select(Division).where(Division.branch_id == 19)).all()}
    ty_courses_def = [
        ("ML-III", "Machine Learning - III", "Major", 3, 2, 0, False),
        ("NLP", "Natural Language Processing", "Major", 3, 2, 0, False),
        ("Adv_Stat", "Advanced Statistics Laboratory", "Major", 0, 2, 0, False),
        ("ADSL", "Advanced Data Science Laboratory", "Major", 0, 2, 0, False),
        ("TSA", "Time Series Analysis", "Minor", 3, 2, 0, False),
        ("CV", "Computer Vision", "Minor", 3, 2, 0, False),
        ("QPM", "Quality and Project Management", "Minor", 3, 2, 0, False),
        ("EVS", "Environmental Studies", "Ability-Enhancement", 2, 0, 0, False),
        ("ARC", "Academic Research Component", "Skill-Based", 1, 0, 0, False),
    ]
    ty_courses = {}
    for code, title, cat, th, pr, tut, heavy in ty_courses_def:
        c = session.exec(select(Course).where(Course.branch_id == 19, Course.code == code)).first()
        if not c:
            c = Course(branch_id=19, code=code, title=title, category=cat, theory_per_week=th, practical_per_week=pr, tutorial_per_week=tut, is_heavy=heavy)
            session.add(c)
            session.commit()
        else:
            c.title = title
            c.category = cat
            c.theory_per_week = th
            c.practical_per_week = pr
            c.tutorial_per_week = tut
            session.add(c)
            session.commit()
        ty_courses[code] = c

    ty_alloc_matrix = {
        "D1": {
            "ML-III": ("SM", "SM", "RK"),
            "NLP": ("PV", "PV", "PT"),
            "Adv_Stat": (None, "NP", "KD"),
            "ADSL": (None, "PP", "PP"),
            "TSA": ("PSJ", "ND", "PSJ"),
            "CV": ("MAA", "PJ", "MAA"),
            "QPM": ("SF", "AG", "AS"),
            "EVS": ("PT", None, None),
            "ARC": ("KD", None, None),
        },
        "D2": {
            "ML-III": ("PSJ", "PSJ", "SM"),
            "NLP": ("KD", "KD", "KD"),
            "Adv_Stat": (None, "AR", "AR"),
            "ADSL": (None, "AS", "AS"),
            "TSA": ("SSM", "SSM", "SSM"),
            "CV": ("PJ", "PJ", "SV"),
            "QPM": ("AG", "AG", "AG"),
            "EVS": ("PT", None, None),
            "ARC": ("SM", None, None),
        },
        "D3": {
            "ML-III": ("AS", "AS", "RK"),
            "NLP": ("KD", "PT", "PT"),
            "Adv_Stat": (None, "AB", "PJ"),
            "ADSL": (None, "MAA", "PP"),
            "TSA": (None, "ND", "ND"),
            "CV": (None, "SV", "MAA"),
            "QPM": ("SF", "SAM", "SAM"),
            "EVS": ("PT", None, None),
            "ARC": ("PSJ", None, None),
        }
    }

    ty_count = 0
    for div_name, allocs in ty_alloc_matrix.items():
        div = ty_divs.get(div_name)
        if not div: continue
        for c_code, (th_fac, b1_fac, b2_fac) in allocs.items():
            course = ty_courses.get(c_code)
            if not course: continue
            alloc = session.exec(select(Allocation).where(Allocation.division_id == div.id, Allocation.course_id == course.id)).first()
            if not alloc:
                alloc = Allocation(
                    division_id=div.id,
                    course_id=course.id,
                    faculty_id=get_fac_id(th_fac),
                    batch1_faculty_id=get_fac_id(b1_fac),
                    batch2_faculty_id=get_fac_id(b2_fac),
                )
            else:
                alloc.faculty_id = get_fac_id(th_fac)
                alloc.batch1_faculty_id = get_fac_id(b1_fac)
                alloc.batch2_faculty_id = get_fac_id(b2_fac)
            session.add(alloc)
            ty_count += 1
    session.commit()

    # 3. BTech Sem VIII (Branch 18)
    btech_divs = {d.name: d for d in session.exec(select(Division).where(Division.branch_id == 18)).all()}
    btech_courses_def = [
        ("HPC", "High Performance Computing Lab", "Major", 2, 2, 0, False),
        ("Green_DS", "Green Data Science", "Major", 2, 0, 0, False),
        ("Data_Ethics", "Data Ethics", "Major", 3, 0, 0, False),
        ("ARC", "Academic Research Component", "Skill-Based", 1, 0, 0, False),
        ("OJT", "On Job Training / Project", "Internship", 0, 2, 0, False),
    ]
    btech_courses = {}
    for code, title, cat, th, pr, tut, heavy in btech_courses_def:
        c = session.exec(select(Course).where(Course.branch_id == 18, Course.code == code)).first()
        if not c:
            c = Course(branch_id=18, code=code, title=title, category=cat, theory_per_week=th, practical_per_week=pr, tutorial_per_week=tut, is_heavy=heavy)
            session.add(c)
            session.commit()
        else:
            c.title = title
            c.category = cat
            c.theory_per_week = th
            c.practical_per_week = pr
            c.tutorial_per_week = tut
            session.add(c)
            session.commit()
        btech_courses[code] = c

    btech_alloc_matrix = {
        "D1": {
            "HPC": ("AR", "AR", "NM"),
            "Green_DS": ("GW", None, None),
            "Data_Ethics": ("AS", None, None),
            "ARC": ("PP", None, None),
            "OJT": (None, "AG", "NM"),
        },
        "D2": {
            "Green_DS": ("GW", None, None),
            "Data_Ethics": ("AS", None, None),
            "ARC": ("AS", None, None),
            "OJT": (None, "KRS", "SV"),
        }
    }

    btech_count = 0
    for div_name, allocs in btech_alloc_matrix.items():
        div = btech_divs.get(div_name)
        if not div: continue
        for c_code, (th_fac, b1_fac, b2_fac) in allocs.items():
            course = btech_courses.get(c_code)
            if not course: continue
            alloc = session.exec(select(Allocation).where(Allocation.division_id == div.id, Allocation.course_id == course.id)).first()
            if not alloc:
                alloc = Allocation(
                    division_id=div.id,
                    course_id=course.id,
                    faculty_id=get_fac_id(th_fac),
                    batch1_faculty_id=get_fac_id(b1_fac),
                    batch2_faculty_id=get_fac_id(b2_fac),
                )
            else:
                alloc.faculty_id = get_fac_id(th_fac)
                alloc.batch1_faculty_id = get_fac_id(b1_fac)
                alloc.batch2_faculty_id = get_fac_id(b2_fac)
            session.add(alloc)
            btech_count += 1
    session.commit()

    return {
        "status": "success",
        "sy_allocations": sy_count,
        "ty_allocations": ty_count,
        "btech_allocations": btech_count,
        "total_synced": sy_count + ty_count + btech_count
    }
