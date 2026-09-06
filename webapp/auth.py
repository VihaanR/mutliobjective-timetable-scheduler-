"""Authentication primitives (design.md §11 Auth).

Real password hashing and real session cookies, with **no new dependency**: password hashing uses
stdlib `hashlib.scrypt`, and session tokens are a hand-rolled HMAC-signed cookie (stdlib `hmac`/
`hashlib`/`json`/`base64`) rather than `passlib`/`itsdangerous`/`python-jose` - consistent with
this repo's habit of justifying every line in `requirements.txt`.

Two roles: `"faculty"` (a teacher - full dashboard/CRUD/generate/adjust/compare access, individual
timetable, drag-and-drop edit rights) and `"student"` (read-only, scoped to their own division).
The signing secret is generated once and persisted as a DB row (`AppSecret`), not a file, so it
survives restarts without adding a third allowed write location beyond `platform.db`/`uploads`
(CLAUDE.md §15).

Honest gaps (documented, not silently ignored): no `Secure` cookie flag yet (this runs over plain
HTTP in local dev - add `Secure=True` once served over HTTPS); no CSRF token (mitigated by
`SameSite=Lax` on a same-origin, fetch-based app with no cross-site form posting); no
password-reset flow (a teacher resets another user's password via the existing edit form).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request
from sqlmodel import Session, select

from webapp.db import get_session
from webapp.models_db import AppSecret, Faculty

SESSION_COOKIE = "tt_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days

# Development fallback used by the README/login screen for a first-run sample teacher account.
DEV_SAMPLE_FACULTY_CODE = "NM"
DEV_SAMPLE_EMAIL = "nilesh.marathe@djsce.edu.in"
DEV_SAMPLE_PASSWORD = "Timetable@123"

Role = Literal["faculty", "student"]

_SCRYPT_PARAMS = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT_PARAMS)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT_PARAMS)
    return hmac.compare_digest(digest, expected)


def matches_dev_sample_teacher_credentials(email: str, password: str) -> bool:
    return email == DEV_SAMPLE_EMAIL and password == DEV_SAMPLE_PASSWORD


def get_secret(session: Session) -> str:
    """The HMAC signing secret for session tokens - generated once, reused after."""
    row = session.exec(select(AppSecret)).first()
    if row is not None:
        return row.value
    row = AppSecret(value=secrets.token_hex(32))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.value


def create_session_token(payload: dict, secret: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    body = dict(payload, exp=time.time() + ttl_seconds)
    b64 = base64.urlsafe_b64encode(json.dumps(body).encode("utf-8")).rstrip(b"=")
    sig = hmac.new(secret.encode("utf-8"), b64, hashlib.sha256).hexdigest()
    return f"{b64.decode('ascii')}.{sig}"


def verify_session_token(token: str, secret: str) -> dict | None:
    try:
        b64, sig = token.split(".")
    except ValueError:
        return None
    expected_sig = hmac.new(secret.encode("utf-8"), b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        padded = b64 + "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def get_current_principal(request: Request, session: Session = Depends(get_session)) -> dict | None:
    """Reads/verifies the `tt_session` cookie. Returns `{"role", "id"}` or `None` - never raises,
    so callers decide whether the route requires a login at all (see `require_faculty`/
    `require_student` below, and the page-level guards in `webapp/server.py`)."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    secret = get_secret(session)
    payload = verify_session_token(token, secret)
    if payload is None or "role" not in payload or "id" not in payload:
        return None
    return {"role": payload["role"], "id": payload["id"]}


def require_faculty(principal: dict | None = Depends(get_current_principal)) -> dict:
    if principal is None:
        raise HTTPException(status_code=401, detail="login required")
    if principal["role"] != "faculty":
        raise HTTPException(status_code=403, detail="teacher access required")
    return principal


def require_student(principal: dict | None = Depends(get_current_principal)) -> dict:
    if principal is None:
        raise HTTPException(status_code=401, detail="login required")
    if principal["role"] != "student":
        raise HTTPException(status_code=403, detail="student access required")
    return principal


def require_faculty_or_pre_bootstrap(
    session: Session = Depends(get_session),
    principal: dict | None = Depends(get_current_principal),
) -> dict | None:
    """Same guard as `require_faculty`, EXCEPT it's a no-op until the very first
    `POST /api/auth/bootstrap` succeeds (i.e. while no Faculty row has credentials set yet). Before
    that first login exists there is no way to authenticate at all, so the platform is in an open
    "initial setup" window: seeding the reference dataset and creating/editing Faculty rows (to
    have a code worth bootstrapping against) both need this. The instant any faculty has
    credentials, this behaves exactly like `require_faculty` - so an anonymous caller can never
    force-wipe/re-seed or read faculty data on a platform that has already been set up."""
    if session.exec(select(Faculty).where(Faculty.password_hash.is_not(None))).first() is None:
        return principal
    return require_faculty(principal)
