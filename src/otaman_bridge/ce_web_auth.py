"""CE web-auth surface + session-refresh layer (ce-refresh-token 1.2).

Mounts ``otaman_core.web_auth.CeAuthManager`` as the runner-free CE auth surface
(``POST /api/auth/login``, ``POST /api/terminal/attach-token``) and adds a
session-refresh layer on top so CE users are not re-prompted for the password
after every browser reload (F128 held the password in memory only):

- **login** issues a refresh token alongside the session JWT.
- **``POST /api/auth/refresh``** exchanges a valid refresh token for a fresh
  session JWT *without* the password, rotating the token (single-use).
- **revocation** takes effect on the next refresh attempt.

The refresh token is opaque, expiring, server-revocable, and **rejected at
login** (refresh-only) — its exposure is strictly less dangerous than the
password, which is never persisted (F128). Transport is the JSON body (the web
client stores it in ``sessionStorage``), never a cookie — a refresh cookie
against the configurable runner/bridge URL would be a blocked third-party
cookie (web-agent's settled contract, tokenProvider.ts).

Session-JWT minting stays in core (one implementation, per Roman's
extract-to-core decision): this layer calls
``CeAuthManager.issue_session_token(username)`` — a password-free issuance seam
— to re-establish the session on refresh. The refresh store itself is the only
new state and lives entirely bridge-side.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger("maestro.bridge.ce_web_auth")  # legacy: renamed at core 1.0

#: Refresh token lifetime — long enough to span reloads/restarts, still expiring.
DEFAULT_REFRESH_TTL = 14 * 24 * 3600  # 14 days
_STORE_FILE = "ce_refresh_tokens.json"


class RefreshError(ValueError):
    """Raised when a refresh token is invalid, expired, revoked, or unknown."""


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def _hash(raw_token: str) -> str:
    """Server-side lookup key — the store never holds the raw bearer token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Record:
    username: str
    exp: int

    def to_json(self) -> dict[str, Any]:
        return {"username": self.username, "exp": self.exp}


class RefreshTokenStore:
    """Persistent, single-use refresh-token store (opaque tokens, hashed at rest).

    Records are keyed by ``sha256(token)`` so a store leak never yields a usable
    bearer token. Persisted atomically (tmp + ``os.replace``, 0600) under
    ``state_dir`` so refresh survives a bridge restart; a corrupt/missing file
    simply starts empty (clients fall back to the password prompt — clean).
    """

    def __init__(self, state_dir: Path, *, ttl: int = DEFAULT_REFRESH_TTL) -> None:
        self._path = Path(state_dir) / _STORE_FILE
        self._ttl = ttl
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._load()

    # ---- persistence ------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._records = {}
            return
        records: dict[str, _Record] = {}
        if isinstance(data, dict):
            for key, rec in data.items():
                if isinstance(rec, dict) and "username" in rec and "exp" in rec:
                    try:
                        records[key] = _Record(str(rec["username"]), int(rec["exp"]))
                    except (TypeError, ValueError):
                        continue
        self._records = records
        self._prune()  # drop anything already expired on load

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {k: r.to_json() for k, r in self._records.items()}
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)

    def _prune(self) -> None:
        now = _now()
        expired = [k for k, r in self._records.items() if r.exp <= now]
        for k in expired:
            del self._records[k]

    # ---- operations -------------------------------------------------------

    def issue(self, username: str) -> str:
        """Mint, persist, and return a new opaque refresh token for ``username``."""
        raw = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._records[_hash(raw)] = _Record(username, _now() + self._ttl)
            self._save()
        return raw

    def consume_and_rotate(self, raw_token: str) -> tuple[str, str]:
        """Single-use exchange: validate ``raw_token``, delete it, issue a new one.

        Returns ``(username, new_raw_token)``. Raises :class:`RefreshError` if the
        token is unknown, expired, or already used (rotated) — so the caller
        returns non-2xx and the client falls back to the password prompt in one
        step, with no retry loop.
        """
        with self._lock:
            self._prune()
            key = _hash(raw_token)
            record = self._records.get(key)
            if record is None:
                raise RefreshError("unknown or already-used refresh token")
            if record.exp <= _now():
                del self._records[key]
                self._save()
                raise RefreshError("refresh token expired")
            # Single-use: the presented token is consumed regardless of what
            # happens next, so a replay of the same token always fails.
            del self._records[key]
            new_raw = secrets.token_urlsafe(32)
            self._records[_hash(new_raw)] = _Record(record.username, _now() + self._ttl)
            self._save()
            return record.username, new_raw

    def revoke_user(self, username: str) -> int:
        """Revoke ALL refresh tokens for ``username``; returns how many were removed.

        Takes effect on the next refresh attempt (the tokens are simply gone).
        """
        with self._lock:
            victims = [k for k, r in self._records.items() if r.username == username]
            for k in victims:
                del self._records[k]
            if victims:
                self._save()
            return len(victims)

    def count(self) -> int:
        with self._lock:
            self._prune()
            return len(self._records)


class CeWebAuthService:
    """CE web-auth surface: core login/attach + the bridge refresh layer.

    ``auth_manager`` is a mounted ``otaman_core.web_auth.CeAuthManager`` (the one
    shared implementation). This service adds only the refresh token — issuance
    at login, rotation at refresh, and revocation — and delegates all JWT work
    (password verify, session/attach minting) to the core manager.
    """

    def __init__(self, auth_manager: Any, store: RefreshTokenStore) -> None:
        self._mgr = auth_manager
        self._store = store

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._mgr, "enabled", False))

    def login(self, username: str, password: str) -> dict[str, str]:
        """Verify credentials; return ``{token, refresh_token}``.

        Delegates password verification + session-JWT issuance to core; on
        success issues a refresh token. Raises whatever the core manager raises
        (``AuthError``) on bad credentials — the route maps that to 401.
        """
        session_jwt = self._mgr.login(username, password)
        refresh_token = self._store.issue(username)
        return {"token": session_jwt, "refresh_token": refresh_token}

    def refresh(self, refresh_token: str) -> dict[str, str]:
        """Exchange a valid refresh token for fresh ``{token, refresh_token}``.

        Single-use rotation. Raises :class:`RefreshError` on any invalid /
        expired / revoked token, OR if the bound user no longer exists (the core
        ``issue_session_token`` raises) — the route maps either to non-2xx so the
        client falls back to the password prompt in one step.
        """
        username, new_refresh = self._store.consume_and_rotate(refresh_token)
        try:
            session_jwt = self._mgr.issue_session_token(username)
        except Exception as exc:  # noqa: BLE001 — core AuthError (user gone/disabled)
            # The rotated token was already issued; revoke the user's tokens so a
            # removed account can't keep rotating. Then fail closed.
            self._store.revoke_user(username)
            raise RefreshError(f"cannot re-establish session for {username!r}: {exc}") from exc
        return {"token": session_jwt, "refresh_token": new_refresh}

    def attach_token(
        self, session_jwt: str, available_session_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Delegate to core: verify the session JWT, issue a scoped attach JWT."""
        return self._mgr.attach_token(session_jwt, available_session_ids)

    def revoke_user(self, username: str) -> int:
        """Revoke all of ``username``'s refresh tokens (next refresh fails)."""
        return self._store.revoke_user(username)


__all__ = [
    "DEFAULT_REFRESH_TTL",
    "CeWebAuthService",
    "RefreshError",
    "RefreshTokenStore",
]
