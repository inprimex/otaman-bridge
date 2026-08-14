"""OIDC-backed AuthProvider implementation (EE).

Validates Bearer JWTs against an ``otaman_core.auth_oidc.OIDCValidator``
and also accepts session cookies issued by the bridge's web-login flow.
Builds RFC 6750 + RFC 9728 WWW-Authenticate challenges pointing at the
bridge's protected-resource-metadata endpoint so MCP clients (Claude
Code) can discover the authorization server and run auth_code+PKCE.

This module was moved here from ``otaman_bridge.auth`` in Phase 2 of the
CE/EE split. The Protocol + ``SimpleAuthProvider`` + ``LoopbackAuthProvider``
+ ``CompositeAuthProvider`` stay in CE; only the OIDC implementation is
EE-exclusive because OIDC validation is the gate to multi-tenant SSO,
which is paid-tier functionality.

Dependencies-as-getters pattern: ``validator_getter`` /
``session_store_getter`` / ``session_cookie_getter`` are callables that
the provider invokes on each request. This lets the daemon (or tests)
reassign its underlying ``oidc_validator`` / ``session_store`` /
``session_cookie`` attributes after construction without rebuilding the
composite auth chain. See the equivalent docs in
``otaman_bridge.auth.OIDCAuthProvider`` for the motivation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from otaman_bridge.mcp_server import CallContext

__all__ = ["OIDCAuthProvider"]


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

    When ``validator_getter()`` returns ``None`` the provider is inert:
    ``identify`` returns ``None`` and ``challenge`` returns ``None``,
    so the daemon falls back to plain 401 without a challenge.
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

        Used by the identity-required MCP tool rejection path, which
        sends ``error="insufficient_scope"`` instead of ``invalid_token``
        because the caller IS authenticated (loopback bearer) but the
        tool requires a user identity. Signals to MCP clients that they
        should upgrade to an OIDC bearer rather than retry.

        Returns ``None`` when the OIDC validator is unset — daemon
        falls back to plain 401 without a challenge.
        """
        if self.validator_getter() is None:
            return None
        rm_url = f"{self.resource_url_fn(host)}/.well-known/oauth-protected-resource"
        return f'Bearer resource_metadata="{rm_url}", error="{error}"'
