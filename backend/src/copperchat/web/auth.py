from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from copperchat.config import Settings
from copperchat.db import Database, utcnow_iso
from copperchat.errors import ConfigError

COOKIE_NAME = "copperchat_session"
SESSION_MAX_AGE_S = 30 * 24 * 3600
_SALT = "copperchat-session"
_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("copperchat-timing-equaliser")

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger(__name__)

_COOKIE_ADVICE = (
    "browsers drop the Secure session cookie over plain http (except on localhost), so "
    "users stay signed out. Use HTTPS (docs/deployment/cloudflare.md; behind an HTTPS proxy "
    "check FORWARDED_ALLOW_IPS) or set COOKIE_SECURE=false for plain-http LAN access."
)
INSECURE_COOKIE_WARNING = f"COOKIE_SECURE=true but a login came over plain http: {_COOKIE_ADVICE}"


@dataclass(frozen=True)
class User:
    id: int
    username: str


class LoginBody(BaseModel):
    username: str
    password: str


class LoginRateLimiter:
    """Counts failed logins per client key in a sliding window."""

    def __init__(
        self, max_failures: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._max = max_failures
        self._window = window_s
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, key: str) -> deque[float]:
        q = self._failures.get(key)
        if q is None:
            return deque()
        cutoff = self._clock() - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        if not q:
            del self._failures[key]  # forget clients whose failures expired
        return q

    def retry_after(self, key: str) -> float | None:
        q = self._prune(key)
        if len(q) < self._max:
            return None
        return max(1.0, q[0] + self._window - self._clock())

    def record_failure(self, key: str) -> None:
        self._prune(key)
        self._failures.setdefault(key, deque()).append(self._clock())

    def tracked_clients(self) -> int:
        return len(self._failures)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def load_or_create_session_secret(settings: Settings) -> str:
    if settings.session_secret is not None and settings.session_secret.get_secret_value():
        return settings.session_secret.get_secret_value()
    path: Path = settings.data_dir / "session_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret)
    return secret


def _iso_seconds_ago(seconds: float) -> str:
    then = datetime.now(UTC) - timedelta(seconds=seconds)
    return then.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=_SALT)


async def ensure_admin(db: Database, settings: Settings) -> None:
    row = await db.fetchone("SELECT id FROM users WHERE username = ?", (settings.admin_username,))
    if row is not None:
        return
    password = settings.admin_password.get_secret_value() if settings.admin_password else ""
    if not password:
        raise ConfigError(
            "ADMIN_PASSWORD must be set on first start; it seeds the login for "
            f"user '{settings.admin_username}' and is stored only as an argon2 hash."
        )
    await db.execute(
        "INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
        (settings.admin_username, _hasher.hash(password), utcnow_iso()),
    )


async def user_from_cookie(state: Any, cookie: str | None) -> User | None:
    sid = _session_id(state, cookie)
    if sid is None:
        return None
    row = await state.db.fetchone(
        "SELECT u.id, u.username FROM auth_sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.id = ?",
        (sid,),
    )
    return User(id=row["id"], username=row["username"]) if row else None


def _session_id(state: Any, cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        payload = state.serializer.loads(cookie, max_age=SESSION_MAX_AGE_S)
    except BadSignature:
        return None
    sid = payload.get("sid") if isinstance(payload, dict) else None
    return sid if isinstance(sid, str) and sid else None


async def require_user(request: Request) -> User:
    user = await user_from_cookie(request.app.state, request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="not logged in")
    return user


async def authenticate_connection(conn: HTTPConnection) -> str | None:
    """Cookie auth for WebSocket upgrades (no FastAPI dependency available there)."""
    user = await user_from_cookie(conn.app.state, conn.cookies.get(COOKIE_NAME))
    return user.username if user else None


CurrentUser = Annotated[User, Depends(require_user)]
"""FastAPI dependency alias: `_: CurrentUser` in a route signature requires a login."""


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def drops_secure_cookie(cookie_secure: bool, scheme: str, host: str | None) -> bool:
    """True when browsers will discard the session cookie: Secure, sent over http, and
    not to localhost (a secure context even over http)."""
    return cookie_secure and scheme == "http" and not _is_loopback(host)


def startup_cookie_warning(settings: Settings) -> str | None:
    """A warning for the startup log when PUBLIC_BASE_URL is plain http but cookies are Secure."""
    if not settings.public_base_url:
        return None
    url = urlsplit(settings.public_base_url)
    if drops_secure_cookie(settings.cookie_secure, url.scheme.lower(), url.hostname):
        return f"PUBLIC_BASE_URL is plain http while COOKIE_SECURE=true: {_COOKIE_ADVICE}"
    return None


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login", status_code=204)
async def login(body: LoginBody, request: Request, response: Response) -> None:
    state = request.app.state
    key = _client_key(request)
    wait = state.login_limiter.retry_after(key)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail="too many failed logins",
            headers={"Retry-After": str(math.ceil(wait))},
        )
    # Count the attempt before the first await: parallel guesses must not all pass
    # the limiter before any failure is recorded. A success clears the count.
    state.login_limiter.record_failure(key)
    row = await state.db.fetchone(
        "SELECT id, password_hash FROM users WHERE username = ?", (body.username,)
    )
    try:
        await asyncio.to_thread(
            _hasher.verify, row["password_hash"] if row else _DUMMY_HASH, body.password
        )
        ok = row is not None
    except (VerificationError, InvalidHashError):
        ok = False
    if not ok or row is None:
        raise HTTPException(status_code=401, detail="invalid username or password")
    state.login_limiter.reset(key)
    sid = secrets.token_urlsafe(32)
    db: Database = state.db
    await db.execute(
        "DELETE FROM auth_sessions WHERE created_at < ?",
        (_iso_seconds_ago(SESSION_MAX_AGE_S),),
    )
    await db.execute(
        "INSERT INTO auth_sessions(id, user_id, created_at) VALUES (?,?,?)",
        (sid, row["id"], utcnow_iso()),
    )
    settings: Settings = state.settings
    # request.url.scheme honours X-Forwarded-Proto from FORWARDED_ALLOW_IPS (uvicorn).
    if drops_secure_cookie(settings.cookie_secure, request.url.scheme, request.url.hostname):
        log.warning(INSECURE_COOKIE_WARNING)
    response.set_cookie(
        COOKIE_NAME,
        state.serializer.dumps({"sid": sid}),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    sid = _session_id(request.app.state, request.cookies.get(COOKIE_NAME))
    if sid is not None:
        await request.app.state.db.execute("DELETE FROM auth_sessions WHERE id = ?", (sid,))
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me")
async def me(request: Request) -> dict[str, str]:
    user = await require_user(request)
    return {"username": user.username}
