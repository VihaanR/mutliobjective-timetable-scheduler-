"""Academic calendar ingestion API (design.md §5.2/§6, roadmap P3).

Layer 0 (always available, no API key needed): upload validation - magic-byte sniff plus an
actual parse via pillow/pypdf, never trusting the client's claimed filename or Content-Type -
raw-file serving for a UI preview, term CRUD, and full manual event CRUD.

Layer 1 (optional): POST /api/calendar/extract/{upload_id} hands the stored bytes to
webapp.extract_calendar, which only ever returns candidate `{date, name, kind}` dicts with no
`confirmed`/`source` of their own. This router is what actually inserts them, and it always
inserts `source="extracted", confirmed=False` - hardcoded here, never taken from the candidate
dict or any caller input. See the trust rule in CLAUDE.md §12 / design.md §6: nothing
machine-extracted may affect anything downstream until a human confirms it via
PUT /api/calendar/events/{id}/confirm, the one and only place `confirmed` is ever set to True.

Uploaded bytes are stored under webapp/uploads/ (git-ignored; one of the only two locations this
project may write to, CLAUDE.md §15). The directory is overridable via TIMETABLE_UPLOADS_PATH,
mirroring webapp/db.py's TIMETABLE_DB_PATH override, so tests never touch the real upload folder.
"""
from __future__ import annotations

import hashlib
import io
import os
import uuid
from pathlib import Path

import pypdf
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from sqlmodel import Session, select

from webapp.auth import require_faculty
from webapp.db import get_session
from webapp.extract_calendar import ExtractionUnavailable, extract_events, extraction_available
from webapp.models_db import (
    CalendarEvent, CalendarEventCreate, CalendarEventUpdate,
    CalendarUpload, Term, TermCreate, TermUpdate,
)
from webapp.routers._crud import apply_update, get_or_404

router = APIRouter(prefix="/api", tags=["calendar"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "application/pdf": ".pdf"}
_VALID_KINDS = ("holiday", "exam", "event")
_VALID_SOURCES = ("manual", "extracted")


def get_upload_dir() -> Path:
    """Where uploaded bytes are stored. Reads TIMETABLE_UPLOADS_PATH live on every call (not
    cached at import time), mirroring webapp/db.py's TIMETABLE_DB_PATH pattern, so tests can point
    it at a temp directory via monkeypatch before the app ever touches the filesystem. Called both
    from the upload endpoint and from webapp/server.py's startup lifespan to ensure the directory
    exists (it does not ship in the repo - CLAUDE.md §15)."""
    override = os.environ.get("TIMETABLE_UPLOADS_PATH")
    if override:
        d = Path(override)
    elif os.environ.get("VERCEL"):
        d = Path("/tmp/uploads")
    else:
        d = Path(__file__).resolve().parent.parent / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate_kind(value: str) -> None:
    if value not in _VALID_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {list(_VALID_KINDS)}")


def _validate_source(value: str) -> None:
    if value not in _VALID_SOURCES:
        raise HTTPException(status_code=400, detail=f"source must be one of {list(_VALID_SOURCES)}")


def _validate_image(data: bytes) -> None:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"file is not a valid image: {exc}") from exc


def _validate_pdf(data: bytes) -> None:
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        _ = len(reader.pages)  # force the parser to actually walk the document structure
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"file is not a valid pdf: {exc}") from exc


def _sniff_and_validate(data: bytes) -> str:
    """Identify the file by magic bytes - never by the client's claimed filename/mime - then
    actually parse it with the matching library so a mislabeled or corrupt file is rejected.
    Returns the validated (detected) mime type, or raises 400."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        _validate_image(data)
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        _validate_image(data)
        return "image/jpeg"
    if data[:5] == b"%PDF-":
        _validate_pdf(data)
        return "application/pdf"
    raise HTTPException(status_code=400, detail="unrecognized file type; only pdf/jpg/png are accepted")


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Stream-read with an early abort once `limit` is exceeded, so an oversized upload doesn't
    have to be buffered in full before it's rejected."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=400, detail=f"file exceeds the {limit}-byte upload limit")
        chunks.append(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------------- upload
@router.post("/calendar/upload", status_code=201)
async def upload_calendar_file(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    data = await _read_capped(file, MAX_UPLOAD_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    mime = _sniff_and_validate(data)

    digest = hashlib.sha256(data).hexdigest()
    stored_name = f"{uuid.uuid4().hex}{_MIME_EXT[mime]}"
    dest = get_upload_dir() / stored_name
    dest.write_bytes(data)

    upload = CalendarUpload(
        filename=file.filename or stored_name,
        mime=mime,
        path=str(dest),
        sha256=digest,
    )
    session.add(upload)
    session.commit()
    session.refresh(upload)
    return {"upload_id": upload.id}


@router.get("/calendar/uploads")
def list_uploads(session: Session = Depends(get_session), _=Depends(require_faculty)):
    return session.exec(select(CalendarUpload).order_by(CalendarUpload.uploaded_at.desc())).all()


@router.get("/calendar/uploads/{upload_id}/file")
def get_upload_file(upload_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    upload = get_or_404(session, CalendarUpload, upload_id)
    if not os.path.exists(upload.path):
        raise HTTPException(status_code=404, detail=f"stored file for upload {upload_id} is missing on disk")
    return FileResponse(upload.path, media_type=upload.mime, filename=upload.filename)


# --------------------------------------------------------------------------- extraction (Layer 1)
@router.post("/calendar/extract/{upload_id}")
def extract_calendar_events(
    upload_id: int,
    term_id: int | None = None,
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    upload = get_or_404(session, CalendarUpload, upload_id)

    # Checked before anything else so a missing API key always surfaces as a clean 501, never a
    # crash and never masked by an unrelated 400/404 below (design.md §6 / CLAUDE.md §12).
    if not extraction_available():
        raise HTTPException(
            status_code=501,
            detail="AI calendar extraction is unavailable: set ANTHROPIC_API_KEY to enable it",
        )

    if term_id is not None:
        term = get_or_404(session, Term, term_id)
    else:
        term = session.exec(select(Term).order_by(Term.id.desc())).first()
        if term is None:
            raise HTTPException(
                status_code=400,
                detail="create a term first (or pass ?term_id=) before extracting calendar events",
            )

    if not os.path.exists(upload.path):
        raise HTTPException(status_code=404, detail=f"stored file for upload {upload_id} is missing on disk")
    data = Path(upload.path).read_bytes()

    try:
        candidates = extract_events(upload, data)
    except ExtractionUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    created: list[CalendarEvent] = []
    for item in candidates:
        # Trust rule: extraction may ONLY EVER create source="extracted", confirmed=False rows -
        # both hardcoded here, never read from `item` (which has no such fields) or any request
        # input. This is the one place besides the manual-create endpoint that may write a
        # calendar_event row, and it can never produce a confirmed one.
        event = CalendarEvent(
            term_id=term.id,
            date=item["date"],
            name=item["name"],
            kind=item.get("kind") if item.get("kind") in _VALID_KINDS else "event",
            source="extracted",
            confirmed=False,
        )
        session.add(event)
        created.append(event)
    session.commit()
    for event in created:
        session.refresh(event)

    return {
        "upload_id": upload_id,
        "term_id": term.id,
        "created": len(created),
        "events": created,
    }


# --------------------------------------------------------------------------- events
@router.get("/calendar/events")
def list_events(
    term_id: int | None = None,
    confirmed: bool | None = None,
    session: Session = Depends(get_session),
    _=Depends(require_faculty),
):
    stmt = select(CalendarEvent)
    if term_id is not None:
        stmt = stmt.where(CalendarEvent.term_id == term_id)
    if confirmed is not None:
        stmt = stmt.where(CalendarEvent.confirmed == confirmed)
    return session.exec(stmt.order_by(CalendarEvent.date)).all()


@router.post("/calendar/events", status_code=201)
def create_event(body: CalendarEventCreate, session: Session = Depends(get_session),
                 _=Depends(require_faculty)):
    get_or_404(session, Term, body.term_id)
    _validate_kind(body.kind)
    _validate_source(body.source)
    if body.source != "manual":
        # This endpoint is the manual-entry door only; it can never be used to fabricate an
        # "extracted" row (trust rule - only the extract endpoint above may do that, and only
        # ever as confirmed=False).
        raise HTTPException(status_code=400, detail="manual event creation requires source='manual'")

    event = CalendarEvent(
        term_id=body.term_id,
        date=body.date,
        name=body.name,
        kind=body.kind,
        source="manual",
        confirmed=False,  # always False regardless of what the client sent - see .../confirm
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


@router.put("/calendar/events/{event_id}")
def update_event(event_id: int, body: CalendarEventUpdate, session: Session = Depends(get_session),
                 _=Depends(require_faculty)):
    event = get_or_404(session, CalendarEvent, event_id)
    if body.term_id is not None:
        get_or_404(session, Term, body.term_id)
    if body.kind is not None:
        _validate_kind(body.kind)
    apply_update(event, body)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


@router.delete("/calendar/events/{event_id}")
def delete_event(event_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    event = get_or_404(session, CalendarEvent, event_id)
    session.delete(event)
    session.commit()
    return {"deleted": event_id}


@router.put("/calendar/events/{event_id}/confirm")
def confirm_event(event_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    """The one and only place `confirmed` may be set to True (CLAUDE.md §12 trust rule)."""
    event = get_or_404(session, CalendarEvent, event_id)
    event.confirmed = True
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


# --------------------------------------------------------------------------- terms
@router.get("/terms")
def list_terms(session: Session = Depends(get_session), _=Depends(require_faculty)):
    return session.exec(select(Term)).all()


@router.post("/terms", status_code=201)
def create_term(body: TermCreate, session: Session = Depends(get_session), _=Depends(require_faculty)):
    term = Term.model_validate(body)
    session.add(term)
    session.commit()
    session.refresh(term)
    return term


@router.get("/terms/{term_id}")
def get_term(term_id: int, session: Session = Depends(get_session), _=Depends(require_faculty)):
    return get_or_404(session, Term, term_id)


@router.put("/terms/{term_id}")
def update_term(term_id: int, body: TermUpdate, session: Session = Depends(get_session),
                _=Depends(require_faculty)):
    term = get_or_404(session, Term, term_id)
    apply_update(term, body)
    session.add(term)
    session.commit()
    session.refresh(term)
    return term
