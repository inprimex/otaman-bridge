"""Pluggable identity layer for the bridge's HTTP daemon (CE-only).

This module introduces the seam between CE-style "trust the caller's
self-declared identity" and EE-style "validate via OIDC + session cookie".
The daemon's HTTP handler doesn't know which mode is wired; it just asks
its ``AuthProvider`` to identify each request and to build the 401
challenge when no identity is found.

CE providers (this file):

- ``LoopbackAuthProvider`` — validates the loopback bearer token stored
  in ``~/.maestro/bridge-<account>.endpoint`` for same-host CLI traffic.
  Returns a ``CallContext`` with an empty ``user_id`` (loopback has no
  user identity), so MCP tools that require a user reject it via the
  ``IDENTITY_REQUIRED_TOOLS`` gate.
- ``SimpleAuthProvider`` — trusts whatever the request declares. Reads
  ``X-Otaman-User`` header first, falls back to ``OTAMAN_USER`` env at
  provider construction. Used as CE's default identity source.
- ``CompositeAuthProvider`` — first non-None identify() wins; first
  non-None challenge() wins.

EE providers (``otaman_bridge_ee.auth_oidc``):

- ``OIDCAuthProvider`` — validates Bearer JWT against an
  ``otaman_core.auth_oidc.OIDCValidator``, then session cookies for the
  browser web-login flow.

All providers are frozen dataclasses so they're cheap to compare in
tests and trivially threadsafe (no mutation after construction).
"""

from __future__ import annotations

import os
import secrets as _secrets
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from otaman_bridge.mcp_server import CallContext

__all__ = [
    "AuthProvider",
    "CompositeAuthProvider",
    "LoopbackAuthProvider",
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


# OIDCAuthProvider lives in otaman_bridge_ee.auth_oidc (Phase 2 of the
# CE/EE split, 2026-05-19). The daemon imports it conditionally.


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
