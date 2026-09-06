"""Attach branch identity (department / year / semester) to a solved grid.

`engine.view.solution_to_grids` is deliberately branch-unaware: the engine only knows opaque
division-id and course-code strings, and has no concept of a "branch" at all. That is fine for a
single-branch grid, where the reader already knows which class they are looking at, but not for
anything that merges several branches — a cross-year teaching timetable has to say whether a
09:00 Monday lecture is SY, TY or Final Year, and (because DJSCE reuses D1/D2/D3 in every year)
"D1" alone does not identify a division.

So the branch labels are stitched on here, after solving, from the `division_meta` map recorded on
the run (`webapp.problem_builder.build_division_meta`). Doing it as a post-processing pass keeps
the engine free of platform concepts and means an already-stored grid can be re-labelled without
re-solving.
"""
from __future__ import annotations

from webapp.problem_builder import unqualify

ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


def semester_roman(semester: int) -> str:
    """4 -> 'IV'. Falls back to the plain number outside the table's range rather than raising —
    a label is presentation, and must never be the thing that fails a run."""
    if 0 < semester < len(ROMAN):
        return ROMAN[semester]
    return str(semester)


def class_label(meta: dict) -> str:
    """Short human name for one division, e.g. 'SY Sem IV · D1'. Built from whichever parts are
    actually populated so a partially-filled branch still gets a usable label."""
    parts: list[str] = []
    year = meta.get("year_label") or ""
    semester = meta.get("semester") or 0
    if year:
        parts.append(year)
    if semester:
        parts.append(f"Sem {semester_roman(semester)}")
    head = " ".join(parts)
    division = meta.get("division_name") or ""
    if head and division:
        return f"{head} · {division}"
    return head or division or "—"


def _entry_labels(meta: dict) -> dict:
    return {
        "branch_id": meta.get("branch_id"),
        "branch_code": meta.get("branch_code", ""),
        "department": meta.get("department", ""),
        "department_name": meta.get("department_name", ""),
        "year_label": meta.get("year_label", ""),
        "year_name": meta.get("year_name", ""),
        "semester": meta.get("semester", 0),
        "semester_roman": semester_roman(meta.get("semester", 0) or 0),
        "division_name": meta.get("division_name", ""),
        "class_label": class_label(meta),
    }


def annotate_grids(grids: dict, division_meta: dict) -> dict:
    """Return `grids` with branch labels added to every division and every cell entry.

    Mutates and returns the same object (it is freshly built per run and not shared). Entries keep
    their qualified `division_id`/`course` as the identity, and gain display fields alongside:
    `course_code` is the unqualified code a human reads ('OE', not 'CSE-DS-SY-SEM3::OE'), while
    `course` is left untouched so anything already keying off it keeps working.

    A division with no metadata is labelled with empty strings rather than skipped, so a grid from
    a run predating `division_meta` still renders instead of erroring.
    """
    for division in grids.get("divisions", []):
        meta = division_meta.get(division.get("id"), {})
        labels = _entry_labels(meta)
        division.update(labels)
        # display name for the division tab: the bare 'D1', not the qualified engine id
        division["name"] = labels["division_name"] or unqualify(str(division.get("id", "")))

        for entries in division.get("cells", {}).values():
            for entry in entries:
                # An entry's own division_id is authoritative (a cell can, in principle, be
                # regrouped across divisions by a later view), so re-resolve per entry rather than
                # inheriting the enclosing division's labels.
                entry_meta = division_meta.get(entry.get("division_id"), meta)
                entry.update(_entry_labels(entry_meta))
                entry["course_code"] = unqualify(str(entry.get("course", "")))
                entry["division_name"] = (
                    entry_meta.get("division_name")
                    or unqualify(str(entry.get("division_id", "")))
                )
    return grids
