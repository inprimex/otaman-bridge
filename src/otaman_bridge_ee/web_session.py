"""Server-side session store + cookie helper for the bridge web UI.

Per the Zitadel integration spec sec. 4.3, the bridge owns the user-facing
login surface. The browser auth-code flow (added in chunks B+C) lands
authenticated users into a session. This module owns:

- ``Session`` -- what we remember about a logged-in browser
- ``SessionStore`` -- thread-safe in-memory store keyed by opaque session id
- ``SessionCookie`` -- encode/decode the cookie value (just the id) and
  build the ``Set-Cookie`` header with security attributes

Storage is in-process / in-memory for v0 -- adequate for the Greenbin
4-user pilot. Mode 4+ deployments back this with sqlite or redis.
Session ids are generated via :func:`secrets.token_urlsafe` (256 bits
of entropy) so they are unguessable.

Cookie attributes follow OWASP guidance for first-party session
cookies: ``HttpOnly`` (no JS access), ``SameSite=Lax`` (CSRF
mitigation while preserving normal navigation), ``Secure`` (set when
the deployment serves over HTTPS -- production yes, local dev no),
and ``Path=/`` (cookie applies to all bridge routes).
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

SESSION_COOKIE_NAME = "otaman_bridge_sid"
DEFAULT_SESSION_TTL = 8 * 3600.0


@dataclass(frozen=True)
class Session:
    """What we remember about a logged-in browser."""

    id: str
    user_id: str
    email: str | None
    roles: tuple[str, ...]
    expires_at: float
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))


class SessionStore:
    """Thread-safe in-memory store. Lazy expiration on access."""

    def __init__(self, *, ttl: float = DEFAULT_SESSION_TTL, clock=None) -> None:
        self.ttl = ttl
        self._clock = clock or time.time
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, *, user_id: str, email: str | None, roles: tuple[str, ...]) -> Session:
        sid = secrets.token_urlsafe(32)
        sess = Session(
            id=sid,
            user_id=user_id,
            email=email,
            roles=tuple(roles),
            expires_at=self._clock() + self.ttl,
        )
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return None
            if self._clock() >= sess.expires_at:
                del self._sessions[sid]
                return None
            return sess

    def delete(self, sid) -> bool:
        if not sid:
            return False
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def purge_expired(self) -> int:
        now = self._clock()
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if now >= s.expires_at]
            for sid in stale:
                del self._sessions[sid]
        return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


class SessionCookie:
    """Cookie value codec + Set-Cookie header builder."""

    def __init__(self, *, name: str = SESSION_COOKIE_NAME, secure: bool = True) -> None:
        self.name = name
        self.secure = secure

    def parse(self, cookie_header):
        if not cookie_header:
            return None
        for part in cookie_header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, _, v = part.partition("=")
            if k.strip() == self.name and v.strip():
                return v.strip()
        return None

    def set_header(self, sid: str, *, max_age: float) -> str:
        attrs = [
            f"{self.name}={sid}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={int(max_age)}",
        ]
        if self.secure:
            attrs.append("Secure")
        return "; ".join(attrs)

    def clear_header(self) -> str:
        attrs = [
            f"{self.name}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=0",
        ]
        if self.secure:
            attrs.append("Secure")
        return "; ".join(attrs)


__all__ = [
    "SESSION_COOKIE_NAME",
    "DEFAULT_SESSION_TTL",
    "Session",
    "SessionStore",
    "SessionCookie",
]
