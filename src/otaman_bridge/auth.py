"""Pluggable identity layer for the bridge's HTTP daemon.

This module introduces the seam between CE-style "trust the caller's
self-declared identity" and EE-style "validate via OIDC + session cookie".
The daemon's HTTP handler doesn't know which mode is wired; it just asks
its ``AuthProvider`` to identify each request and to build the 401
challenge when no identity is found.

Phase 1 of the CE/EE split (per
``otaman-meta/strategy/bridge-ce-ee-split.md`` §5) only introduces the
abstraction. No behavior change: the daemon composes the same provider
chain it had inline (OIDC → session-cookie → loopback when OIDC is
configured; loopback alone otherwise). Phase 2 extracts
``OIDCAuthProvider`` to the proprietary EE repo and wires
``SimpleAuthProvider`` as the CE default.

The four providers:

- ``LoopbackAuthProvider`` — validates the loopback bearer token stored
  in ``~/.maestro/bridge-<account>.endpoint`` for same-host CLI traffic.
  Returns a ``CallContext`` with an empty ``user_id`` (loopback has no
  user identity), so MCP tools that require a user reject it via the
  ``IDENTITY_REQUIRED_TOOLS`` gate. Ships in CE.
- ``SimpleAuthProvider`` — trusts whatever the request declares. Reads
  ``X-Otaman-User`` header first, falls back to ``OTAMAN_USER`` env at
  provider construction. Used as CE's default identity source (Phase 2).
- ``OIDCAuthProvider`` — validates Bearer JWT against an
  ``otaman_core.auth_oidc.OIDCValidator``, then session cookies for the
  browser flow. Moves to the EE repo in Phase 2.
- ``CompositeAuthProvider`` — first non-None identify() wins; first
  non-None challenge() wins.

All providers are frozen dataclasses so they're cheap to compare in
tests and trivially threadsafe (no mutation after construction).
"""

from __future__ import annotations

import os
import secrets as _secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from otaman_bridge.mcp_server import CallContext

__all__ = [
    "AuthProvider",
    "CompositeAuthProvider",
    "LoopbackAuthProvider",
    "OIDCAuthProvider",
    "SimpleAuthProvider",
]


class AuthProvider(Protocol):
    """Identify a request's caller. Implementations are pluggable.

    ``identify(headers)`` returns a ``CallContext`` if the request is
    identifiable, else ``None``. ``None`` triggers the daemon's 401
    response, which uses ``challenge(host)`` to build the
    ``WWW-Authenticate`` header. ``challenge`` returns ``None`` when
    the provider doesn't have an authorization server to point clients
    at (the loopback / simple providers).
    """

    def identify(self, headers: Mapping[str, str]) -> CallContext | None: ...
    def challenge(self, host: str) -> str | None: ...


# ---------------------------------------------------------------------------
# Loopback (same-host CLI)


@dataclass(frozen=True)
class LoopbackAuthProvider:
    """Validates the daemon's loopback bearer token.

    The token is generated at daemon startup and written to the
    per-account endpoint file. CLI introspection (``maestro bridge
    status`` etc.) reads it from there. Loopback callers have no
    user identity — the returned ``CallContext.user_id`` is empty,
    which causes MCP tools requiring a user to reject via the
    identity-required gate.
    """

    token: str

    def identify(self, headers: Mapping[str, str]) -> CallContext | None:
        header = headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        supplied = header[len("Bearer "):].strip()
        if not _secrets.compare_digest(supplied, self.token):
            return None
        return CallContext(user_id="", user_email=None, roles=())

    def challenge(self, host: str) -> str | None:
        # Loopback has no authorization server. The daemon falls back to
        # a plain 401 when no provider issues a challenge.
        return None


# ---------------------------------------------------------------------------
# Simple (CE default — trust the caller's declared identity)


@dataclass(frozen=True)
class SimpleAuthProvider:
    """CE default identity source — trust the request, no validation.

    Reads ``X-Otaman-User`` request header first; falls back to the
    ``env_user`` captured at provider construction (typically from
    ``OTAMAN_USER``). This is the "small team, mutual trust" model
    documented for CE — see [[project_bridge_ce_ee_split]] §6 Q1.

    Per the locked Q1 decision, CE deployments assume single-user-per-
    machine. If two machines both set ``OTAMAN_USER=admin`` they will
    collide in shared inbox paths — documented limitation, not a bug.
    """

    env_user: str | None = None

    @classmethod
    def from_env(cls) -> "SimpleAuthProvider":
        """Build a provider that pulls the fallback user from env."""
        return cls(env_user=os.environ.get("OTAMAN_USER", "").strip() or None)

    def identify(self, headers: Mapping[str, str]) -> CallContext | None:
        declared = headers.get("X-Otaman-User", "").strip()
        user = declared or self.env_user
        if not user:
            return None
        return CallContext(user_id=user, user_email=None, roles=())

    def challenge(self, host: str) -> str | None:
        # No real authorization server — caller just needs to set the
        # header. Daemon falls back to a plain 401.
        return None


# ---------------------------------------------------------------------------
# OIDC (EE — moves to otaman-bridge-ee in Phase 2)


@dataclass(frozen=True)
class OIDCAuthProvider:
    """Validates Bearer JWTs + session cookies against an OIDC issuer.

    Tries two paths in order:

    1. ``Authorization: Bearer <jwt>`` — passes the full header to the
       validator (an ``otaman_core.auth_oidc.OIDCValidator``).
    2. ``Cookie: <session_cookie>`` — parses via ``session_cookie``,
       looks up in ``session_store``. Browser web-login flow only;
       both are optional (None when web-login is disabled).

    Builds an RFC 6750 ``WWW-Authenticate: Bearer`` challenge pointing
    at the bridge's RFC 9728 protected-resource-metadata endpoint so
    MCP clients (Claude Code) can discover the authorization server
    and run the auth_code+PKCE flow without preconfigured tokens.

    Dependencies are passed as **getters** rather than direct references
    so the provider tracks the daemon's mutable state. The daemon's
    ``oidc_validator`` / ``session_store`` / ``session_cookie``
    attributes can be reassigned at runtime (some tests do this) and
    the provider sees the new values without rebuilding the composite.
    When ``validator_getter()`` returns ``None`` the provider is inert:
    ``identify`` returns ``None`` and ``challenge`` returns ``None``,
    so the daemon falls back to plain 401 without a challenge.

    This provider will move to ``otaman_bridge_ee/auth_oidc.py`` in
    Phase 2 (the vendored CE copy in EE will use a thin
    ``EEAuthProvider`` that wraps this one alongside the OIDC role
    gate).
    """

    validator_getter: Callable[[], Any]
    session_store_getter: Callable[[], Any] = field(default=lambda: None)
    session_cookie_getter: Callable[[], Any] = field(default=lambda: None)
    resource_url_fn: Callable[[str], str] = field(
        default=lambda host: f"http://{(host or '127.0.0.1').strip()}"
    )

    def identify(self, headers: Mapping[str, str]) -> CallContext | None:
        validator = self.validator_getter()
        header = headers.get("Authorization", "")
        if validator is not None and header.startswith("Bearer "):
            result = validator.validate(header)
            if result.ok:
                return CallContext(
                    user_id=result.user_id or "",
                    user_email=result.email,
                    roles=tuple(result.roles),
                )
        session_store = self.session_store_getter()
        session_cookie = self.session_cookie_getter()
        if session_store is not None and session_cookie is not None:
            cookie_header = headers.get("Cookie", "")
            sid = session_cookie.parse(cookie_header)
            if sid is not None:
                sess = session_store.get(sid)
                if sess is not None:
                    return CallContext(
                        user_id=sess.user_id,
                        user_email=sess.email,
                        roles=tuple(sess.roles),
                    )
        return None

    def challenge(self, host: str) -> str | None:
        if self.validator_getter() is None:
            return None
        rm_url = f"{self.resource_url_fn(host)}/.well-known/oauth-protected-resource"
        return f'Bearer resource_metadata="{rm_url}", error="invalid_token"'

    def challenge_with_error(self, host: str, error: str) -> str | None:
        """Like ``challenge`` but lets callers override the error code.

        Used by ``IDENTITY_REQUIRED_TOOLS`` rejection path, which sends
        ``error="insufficient_scope"`` instead of ``invalid_token``
        because the caller IS authenticated (loopback bearer) but the
        tool requires a user identity. This signals to MCP clients
        that they should upgrade to an OIDC bearer rather than retry.

        Returns ``None`` when the OIDC validator is unset — daemon
        falls back to plain 401 without a challenge.
        """
        if self.validator_getter() is None:
            return None
        rm_url = f"{self.resource_url_fn(host)}/.well-known/oauth-protected-resource"
        return f'Bearer resource_metadata="{rm_url}", error="{error}"'


# ---------------------------------------------------------------------------
# Composite (try each provider in order; first hit wins)


@dataclass(frozen=True)
class CompositeAuthProvider:
    """Tries each child provider in order.

    Identify: first non-None wins. Challenge: first non-None wins —
    so when composing ``[OIDC, Loopback]`` and OIDC is configured,
    the 401 includes the OIDC WWW-Authenticate. When composing just
    ``[Loopback]`` (CE-style), the 401 is a plain 401 with no challenge.
    """

    providers: tuple[AuthProvider, ...]

    def identify(self, headers: Mapping[str, str]) -> CallContext | None:
        for p in self.providers:
            ctx = p.identify(headers)
            if ctx is not None:
                return ctx
        return None

    def challenge(self, host: str) -> str | None:
        for p in self.providers:
            ch = p.challenge(host)
            if ch is not None:
                return ch
        return None

    def first_of_type(self, t: type) -> Any:
        """Return the first child provider of type ``t``, or None.

        Used by the daemon to find an ``OIDCAuthProvider`` in the chain
        for ``challenge_with_error`` calls when rejecting identity-
        required MCP tools that were called via loopback.
        """
        for p in self.providers:
            if isinstance(p, t):
                return p
        return None
