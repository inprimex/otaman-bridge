"""Unit tests for the auth provider seam.

Covers each provider in isolation plus the composite's ordering rules.
Daemon-integration tests (verifying the same chain semantics as before
the refactor) live in test_bridge_daemon.py / test_mcp_route.py — those
exercise the wired-up daemon end-to-end and must still pass unchanged
(no behavior change is the Phase 1 contract).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from otaman_bridge.auth import (
    CompositeAuthProvider,
    LoopbackAuthProvider,
    SimpleAuthProvider,
)
from otaman_bridge.mcp_server import CallContext
from otaman_bridge_ee.auth_oidc import OIDCAuthProvider


# ---------------------------------------------------------------------------
# Stubs


@dataclass
class _OIDCResult:
    ok: bool
    user_id: str | None = None
    email: str | None = None
    roles: tuple[str, ...] = ()
    error: str = ""


class _StubValidator:
    """Stand-in for otaman_core.auth_oidc.OIDCValidator.

    Returns a configurable result regardless of the token contents,
    captures the last token it saw so tests can assert the header
    is passed through verbatim.
    """

    def __init__(self, result: _OIDCResult):
        self.result = result
        self.last_header: str | None = None

    def validate(self, header: str) -> _OIDCResult:
        self.last_header = header
        return self.result


@dataclass
class _Session:
    user_id: str
    email: str | None
    roles: tuple[str, ...]


class _StubSessionStore:
    def __init__(self, sessions: dict[str, _Session]):
        self._sessions = sessions

    def get(self, sid: str) -> _Session | None:
        return self._sessions.get(sid)


class _StubSessionCookie:
    """Parses ``otaman_session=<sid>`` cookie shape from a Cookie header."""

    def parse(self, cookie_header: str) -> str | None:
        if not cookie_header:
            return None
        for pair in cookie_header.split(";"):
            k, _, v = pair.strip().partition("=")
            if k == "otaman_session":
                return v or None
        return None


# ---------------------------------------------------------------------------
# LoopbackAuthProvider


class TestLoopbackAuthProvider:
    def test_matching_bearer_returns_empty_user_context(self):
        provider = LoopbackAuthProvider(token="abc123")
        ctx = provider.identify({"Authorization": "Bearer abc123"})
        assert ctx is not None
        assert ctx.user_id == ""
        assert ctx.user_email is None
        assert ctx.roles == ()

    def test_mismatching_bearer_returns_none(self):
        provider = LoopbackAuthProvider(token="abc123")
        assert provider.identify({"Authorization": "Bearer wrong"}) is None

    def test_no_authorization_header_returns_none(self):
        provider = LoopbackAuthProvider(token="abc123")
        assert provider.identify({}) is None

    def test_non_bearer_scheme_returns_none(self):
        provider = LoopbackAuthProvider(token="abc123")
        assert provider.identify({"Authorization": "Basic abc123"}) is None

    def test_constant_time_compare(self):
        # Sanity: we use secrets.compare_digest, but verify the surface
        # rejects a token that's a prefix of the real one.
        provider = LoopbackAuthProvider(token="abcdef")
        assert provider.identify({"Authorization": "Bearer abc"}) is None
        assert provider.identify({"Authorization": "Bearer abcdef"}) is not None

    def test_no_challenge(self):
        provider = LoopbackAuthProvider(token="abc123")
        assert provider.challenge("any.host") is None


# ---------------------------------------------------------------------------
# SimpleAuthProvider


class TestSimpleAuthProvider:
    def test_header_overrides_env(self):
        provider = SimpleAuthProvider(env_user="alice")
        ctx = provider.identify({"X-Otaman-User": "bob"})
        assert ctx is not None
        assert ctx.user_id == "bob"

    def test_env_used_when_header_missing(self):
        provider = SimpleAuthProvider(env_user="alice")
        ctx = provider.identify({})
        assert ctx is not None
        assert ctx.user_id == "alice"

    def test_none_when_no_signal(self):
        provider = SimpleAuthProvider(env_user=None)
        assert provider.identify({}) is None

    def test_empty_header_falls_back_to_env(self):
        provider = SimpleAuthProvider(env_user="alice")
        ctx = provider.identify({"X-Otaman-User": ""})
        assert ctx is not None
        assert ctx.user_id == "alice"

    def test_whitespace_header_treated_as_empty(self):
        provider = SimpleAuthProvider(env_user="alice")
        ctx = provider.identify({"X-Otaman-User": "   "})
        assert ctx is not None
        assert ctx.user_id == "alice"

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_USER", "from-env")
        provider = SimpleAuthProvider.from_env()
        assert provider.env_user == "from-env"

    def test_from_env_empty(self, monkeypatch):
        monkeypatch.delenv("OTAMAN_USER", raising=False)
        provider = SimpleAuthProvider.from_env()
        assert provider.env_user is None

    def test_no_challenge(self):
        provider = SimpleAuthProvider(env_user="alice")
        assert provider.challenge("any.host") is None


# ---------------------------------------------------------------------------
# OIDCAuthProvider


def _const(value):
    """Tiny helper to build a constant-returning getter for the OIDC provider."""
    return lambda: value


class TestOIDCAuthProviderBearer:
    def test_valid_bearer_returns_user_context(self):
        validator = _StubValidator(_OIDCResult(
            ok=True, user_id="sub-123", email="u@example.com",
            roles=("admin",),
        ))
        provider = OIDCAuthProvider(validator_getter=_const(validator))
        ctx = provider.identify({"Authorization": "Bearer eyJfoo"})
        assert ctx is not None
        assert ctx.user_id == "sub-123"
        assert ctx.user_email == "u@example.com"
        assert ctx.roles == ("admin",)
        # Validator should see the full header including "Bearer " prefix.
        assert validator.last_header == "Bearer eyJfoo"

    def test_invalid_bearer_returns_none(self):
        validator = _StubValidator(_OIDCResult(ok=False, error="exp"))
        provider = OIDCAuthProvider(validator_getter=_const(validator))
        assert provider.identify({"Authorization": "Bearer expired"}) is None

    def test_no_bearer_no_cookie_returns_none(self):
        validator = _StubValidator(_OIDCResult(ok=False))
        provider = OIDCAuthProvider(validator_getter=_const(validator))
        assert provider.identify({}) is None

    def test_inert_when_validator_getter_returns_none(self):
        # Tests the runtime-mutability contract: when the daemon's
        # oidc_validator is unset, the provider returns None for
        # identify and challenge — even if a Bearer header is present.
        provider = OIDCAuthProvider(validator_getter=_const(None))
        assert provider.identify({"Authorization": "Bearer x"}) is None
        assert provider.challenge("any.host") is None
        assert provider.challenge_with_error("any.host", "insufficient_scope") is None

    def test_tracks_validator_changes_over_time(self):
        # Simulates daemon.oidc_validator being set after construction.
        current = {"v": None}
        provider = OIDCAuthProvider(validator_getter=lambda: current["v"])
        assert provider.identify({"Authorization": "Bearer x"}) is None
        current["v"] = _StubValidator(_OIDCResult(ok=True, user_id="late"))
        ctx = provider.identify({"Authorization": "Bearer x"})
        assert ctx is not None and ctx.user_id == "late"


class TestOIDCAuthProviderSessionCookie:
    def test_valid_session_returns_user_context(self):
        store = _StubSessionStore({"sid-1": _Session(
            user_id="user-A", email="a@example.com", roles=("user",),
        )})
        provider = OIDCAuthProvider(
            validator_getter=_const(_StubValidator(_OIDCResult(ok=False))),
            session_store_getter=_const(store),
            session_cookie_getter=_const(_StubSessionCookie()),
        )
        ctx = provider.identify({"Cookie": "otaman_session=sid-1"})
        assert ctx is not None
        assert ctx.user_id == "user-A"
        assert ctx.user_email == "a@example.com"
        assert ctx.roles == ("user",)

    def test_unknown_sid_returns_none(self):
        provider = OIDCAuthProvider(
            validator_getter=_const(_StubValidator(_OIDCResult(ok=False))),
            session_store_getter=_const(_StubSessionStore({})),
            session_cookie_getter=_const(_StubSessionCookie()),
        )
        assert provider.identify({"Cookie": "otaman_session=unknown"}) is None

    def test_no_cookie_returns_none(self):
        provider = OIDCAuthProvider(
            validator_getter=_const(_StubValidator(_OIDCResult(ok=False))),
            session_store_getter=_const(_StubSessionStore({"sid-1": _Session("u", None, ())})),
            session_cookie_getter=_const(_StubSessionCookie()),
        )
        assert provider.identify({}) is None

    def test_no_session_support_skips_cookie_path(self):
        # When the session getters return None (web login disabled),
        # the cookie path is skipped silently.
        provider = OIDCAuthProvider(
            validator_getter=_const(_StubValidator(_OIDCResult(ok=False))),
        )
        assert provider.identify({"Cookie": "otaman_session=sid-1"}) is None


class TestOIDCAuthProviderChallenge:
    def test_challenge_points_at_protected_resource_metadata(self):
        provider = OIDCAuthProvider(
            validator_getter=_const(_StubValidator(_OIDCResult(ok=False))),
            resource_url_fn=lambda h: f"https://{h}",
        )
        ch = provider.challenge("bridge.example.com")
        assert ch is not None
        assert 'resource_metadata="https://bridge.example.com/.well-known/oauth-protected-resource"' in ch
        assert 'error="invalid_token"' in ch

    def test_challenge_with_error_overrides_error_code(self):
        provider = OIDCAuthProvider(
            validator_getter=_const(_StubValidator(_OIDCResult(ok=False))),
            resource_url_fn=lambda h: f"https://{h}",
        )
        ch = provider.challenge_with_error("bridge.example.com", "insufficient_scope")
        assert ch is not None
        assert 'error="insufficient_scope"' in ch
        assert 'invalid_token' not in ch

    def test_default_resource_url_fn_uses_loopback(self):
        provider = OIDCAuthProvider(
            validator_getter=_const(_StubValidator(_OIDCResult(ok=False))),
        )
        ch = provider.challenge("")
        assert ch is not None
        assert "http://127.0.0.1" in ch


# ---------------------------------------------------------------------------
# CompositeAuthProvider


class TestCompositeAuthProvider:
    def test_first_non_none_identify_wins(self):
        provider = CompositeAuthProvider(providers=(
            SimpleAuthProvider(env_user=None),
            LoopbackAuthProvider(token="abc"),
        ))
        ctx = provider.identify({"Authorization": "Bearer abc"})
        assert ctx is not None
        assert ctx.user_id == ""  # loopback semantics

    def test_simple_wins_over_loopback_when_header_present(self):
        provider = CompositeAuthProvider(providers=(
            SimpleAuthProvider(env_user=None),
            LoopbackAuthProvider(token="abc"),
        ))
        # Both providers could fire; SimpleAuthProvider is ordered first.
        ctx = provider.identify({
            "X-Otaman-User": "alice",
            "Authorization": "Bearer abc",
        })
        assert ctx is not None
        assert ctx.user_id == "alice"

    def test_all_none_returns_none(self):
        provider = CompositeAuthProvider(providers=(
            SimpleAuthProvider(env_user=None),
            LoopbackAuthProvider(token="abc"),
        ))
        assert provider.identify({}) is None

    def test_first_non_none_challenge_wins(self):
        oidc = OIDCAuthProvider(
            validator_getter=_const(_StubValidator(_OIDCResult(ok=False))),
            resource_url_fn=lambda h: f"https://{h}",
        )
        provider = CompositeAuthProvider(providers=(
            oidc,
            LoopbackAuthProvider(token="abc"),
        ))
        ch = provider.challenge("bridge.example.com")
        assert ch is not None
        assert "https://bridge.example.com" in ch

    def test_no_challenge_when_all_providers_return_none(self):
        provider = CompositeAuthProvider(providers=(
            SimpleAuthProvider(env_user=None),
            LoopbackAuthProvider(token="abc"),
        ))
        assert provider.challenge("any.host") is None

    def test_first_of_type_finds_oidc(self):
        oidc = OIDCAuthProvider(
            validator_getter=_const(_StubValidator(_OIDCResult(ok=False))),
        )
        provider = CompositeAuthProvider(providers=(
            LoopbackAuthProvider(token="abc"),
            oidc,
        ))
        assert provider.first_of_type(OIDCAuthProvider) is oidc

    def test_first_of_type_returns_none_when_absent(self):
        provider = CompositeAuthProvider(providers=(
            LoopbackAuthProvider(token="abc"),
        ))
        assert provider.first_of_type(OIDCAuthProvider) is None

    def test_empty_composite_returns_none(self):
        provider = CompositeAuthProvider(providers=())
        assert provider.identify({"Authorization": "Bearer x"}) is None
        assert provider.challenge("any.host") is None
