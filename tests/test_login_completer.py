"""Tests for LoginCompleter -- callback-flow glue.

Uses stubs for TokenExchanger, OIDCValidator, and SessionStore so the
test verifies orchestration logic without depending on any of them.
"""

from __future__ import annotations

import pytest

from otaman_bridge_ee.web_auth import (
    LoginCompleteError,
    LoginCompleter,
    PendingLoginStore,
    TokenExchangeError,
    TokenResponse,
)
from otaman_bridge_ee.web_session import SessionStore


# ---- Helpers -----------------------------------------------------------


class _StubExchanger:
    """Drop-in for TokenExchanger that returns whatever it was constructed with."""

    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def exchange_code(self, code, code_verifier):
        self.calls.append((code, code_verifier))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


class _StubValidatorResult:
    def __init__(self, ok, user_id=None, email=None, roles=(), error=None):
        self.ok = ok
        self.user_id = user_id
        self.email = email
        self.roles = roles
        self.error = error


class _StubValidator:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def validate(self, header):
        self.calls.append(header)
        return self.result


def _make_completer(
    *,
    exchanger=None,
    validator_result=None,
    pending_store=None,
    session_store=None,
):
    if exchanger is None:
        exchanger = _StubExchanger(response=TokenResponse(
            access_token="at-1", id_token="it-1", refresh_token=None,
            expires_in=3600, token_type="Bearer",
        ))
    if validator_result is None:
        validator_result = _StubValidatorResult(
            ok=True, user_id="user-42", email="u@e", roles=("otaman:viewer",),
        )
    if pending_store is None:
        pending_store = PendingLoginStore()
    if session_store is None:
        session_store = SessionStore()
    return LoginCompleter(
        token_exchanger=exchanger,
        validator=_StubValidator(validator_result),
        session_store=session_store,
        pending_store=pending_store,
    ), exchanger, pending_store, session_store


# ---- Tests -------------------------------------------------------------


class TestLoginCompleter:
    def test_happy_path_creates_session(self):
        completer, exchanger, pending, sessions = _make_completer()
        pending.put("state-1", "verifier-abc")
        session = completer.complete(code="auth-code-xyz", state="state-1")
        assert session.user_id == "user-42"
        assert session.email == "u@e"
        assert session.roles == ("otaman:viewer",)
        # The exchanger got the verifier from the pending store
        assert exchanger.calls == [("auth-code-xyz", "verifier-abc")]
        # And the session is in the store
        assert sessions.get(session.id) is session

    def test_unknown_state_raises(self):
        completer, _, _, _ = _make_completer()
        with pytest.raises(LoginCompleteError, match="unknown or expired"):
            completer.complete(code="c", state="never-seen-this-state")

    def test_replayed_state_raises(self):
        completer, _, pending, _ = _make_completer()
        pending.put("state-1", "verifier-abc")
        completer.complete(code="c", state="state-1")
        # Second call with same state -- pending_store.take already removed it
        with pytest.raises(LoginCompleteError, match="unknown or expired"):
            completer.complete(code="c", state="state-1")

    def test_missing_code_or_state_raises(self):
        completer, _, _, _ = _make_completer()
        with pytest.raises(LoginCompleteError, match="missing code or state"):
            completer.complete(code="", state="state-1")
        with pytest.raises(LoginCompleteError, match="missing code or state"):
            completer.complete(code="c", state="")

    def test_missing_id_token_raises(self):
        # TokenResponse with no id_token (Zitadel misconfig)
        bad_response = TokenResponse(
            access_token="at", id_token=None, refresh_token=None,
            expires_in=3600, token_type="Bearer",
        )
        completer, _, pending, _ = _make_completer(
            exchanger=_StubExchanger(response=bad_response),
        )
        pending.put("state-1", "verifier-abc")
        with pytest.raises(LoginCompleteError, match="missing id_token"):
            completer.complete(code="c", state="state-1")

    def test_invalid_id_token_raises(self):
        completer, _, pending, _ = _make_completer(
            validator_result=_StubValidatorResult(ok=False, error="expired"),
        )
        pending.put("state-1", "verifier-abc")
        with pytest.raises(LoginCompleteError, match="id_token validation failed: expired"):
            completer.complete(code="c", state="state-1")

    def test_id_token_without_sub_raises(self):
        completer, _, pending, _ = _make_completer(
            validator_result=_StubValidatorResult(ok=True, user_id=None),
        )
        pending.put("state-1", "verifier-abc")
        with pytest.raises(LoginCompleteError, match="no sub claim"):
            completer.complete(code="c", state="state-1")

    def test_token_exchange_error_propagates(self):
        completer, _, pending, _ = _make_completer(
            exchanger=_StubExchanger(raise_exc=TokenExchangeError("HTTP 502")),
        )
        pending.put("state-1", "verifier-abc")
        # Note: NOT wrapped in LoginCompleteError -- caller distinguishes
        with pytest.raises(TokenExchangeError, match="502"):
            completer.complete(code="c", state="state-1")

    def test_state_consumed_even_when_validation_fails(self):
        """Defense-in-depth: even when id_token validation fails, the
        state must NOT remain in pending_store -- we already took it."""
        completer, _, pending, _ = _make_completer(
            validator_result=_StubValidatorResult(ok=False, error="bad sig"),
        )
        pending.put("state-1", "verifier-abc")
        with pytest.raises(LoginCompleteError):
            completer.complete(code="c", state="state-1")
        assert len(pending) == 0
