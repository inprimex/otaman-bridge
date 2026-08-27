"""Tests for the CE web-auth refresh layer (ce-refresh-token 1.2)."""

from __future__ import annotations

import json

import pytest

import otaman_bridge.ce_web_auth as cwa
from otaman_bridge.ce_web_auth import (
    CeWebAuthService,
    RefreshError,
    RefreshTokenStore,
)


class _AuthError(ValueError):
    """Stand-in for otaman_core.web_auth.AuthError."""


class _FakeMgr:
    """Minimal fake CeAuthManager honoring the frozen core interface."""

    def __init__(self, *, enabled=True, users=("alice",)):
        self.enabled = enabled
        self._users = set(users)
        self.attach_calls: list = []

    def login(self, username, password):
        if username in self._users and password == "pw":
            return f"session:{username}"
        raise _AuthError("invalid credentials")

    def issue_session_token(self, username):
        if username in self._users:
            return f"session:{username}"
        raise _AuthError("unknown user")

    def attach_token(self, session_jwt, available_session_ids=None):
        self.attach_calls.append((session_jwt, available_session_ids))
        return {
            "token": f"attach:{session_jwt}",
            "expires_at": "2026-01-01T00:00:00+00:00",
            "mode": "read",
        }


# ---------------------------------------------------------------------------
# RefreshTokenStore
# ---------------------------------------------------------------------------


class TestRefreshStore:
    def test_issue_then_rotate(self, tmp_path):
        store = RefreshTokenStore(tmp_path)
        tok = store.issue("alice")
        assert store.count() == 1
        username, new_tok = store.consume_and_rotate(tok)
        assert username == "alice"
        assert new_tok != tok
        assert store.count() == 1  # one rotated in, one consumed out

    def test_single_use_old_token_rejected(self, tmp_path):
        store = RefreshTokenStore(tmp_path)
        tok = store.issue("alice")
        _, _new = store.consume_and_rotate(tok)
        with pytest.raises(RefreshError):
            store.consume_and_rotate(tok)  # replay of the consumed token

    def test_unknown_token_rejected(self, tmp_path):
        store = RefreshTokenStore(tmp_path)
        with pytest.raises(RefreshError):
            store.consume_and_rotate("nope-not-a-real-token")

    def test_expired_token_rejected(self, tmp_path, monkeypatch):
        store = RefreshTokenStore(tmp_path, ttl=100)
        tok = store.issue("alice")
        monkeypatch.setattr(cwa, "_now", lambda: 10**12)  # far future — token TTL elapsed
        with pytest.raises(RefreshError):
            store.consume_and_rotate(tok)

    def test_revoke_user_kills_tokens(self, tmp_path):
        store = RefreshTokenStore(tmp_path)
        t1 = store.issue("alice")
        store.issue("alice")
        store.issue("bob")
        removed = store.revoke_user("alice")
        assert removed == 2
        assert store.count() == 1  # bob survives
        with pytest.raises(RefreshError):
            store.consume_and_rotate(t1)

    def test_survives_restart(self, tmp_path):
        tok = RefreshTokenStore(tmp_path).issue("alice")
        # Fresh store from the same dir — token must still validate.
        reopened = RefreshTokenStore(tmp_path)
        username, _new = reopened.consume_and_rotate(tok)
        assert username == "alice"

    def test_raw_token_not_persisted_only_hash(self, tmp_path):
        store = RefreshTokenStore(tmp_path)
        tok = store.issue("alice")
        on_disk = (tmp_path / "ce_refresh_tokens.json").read_text(encoding="utf-8")
        assert tok not in on_disk  # raw bearer token never at rest
        assert json.loads(on_disk)  # but a hashed record is present

    def test_corrupt_file_starts_empty(self, tmp_path):
        (tmp_path / "ce_refresh_tokens.json").write_text("{ not json", encoding="utf-8")
        store = RefreshTokenStore(tmp_path)
        assert store.count() == 0


# ---------------------------------------------------------------------------
# CeWebAuthService
# ---------------------------------------------------------------------------


class TestService:
    def _svc(self, tmp_path, **mgr_kw):
        mgr = _FakeMgr(**mgr_kw)
        return CeWebAuthService(mgr, RefreshTokenStore(tmp_path)), mgr

    def test_login_issues_token_and_refresh(self, tmp_path):
        svc, _ = self._svc(tmp_path)
        out = svc.login("alice", "pw")
        assert out["token"] == "session:alice"
        assert out["refresh_token"]

    def test_login_bad_credentials_raises(self, tmp_path):
        svc, _ = self._svc(tmp_path)
        with pytest.raises(_AuthError):
            svc.login("alice", "wrong")

    def test_refresh_rotates_and_reissues_session(self, tmp_path):
        svc, _ = self._svc(tmp_path)
        first = svc.login("alice", "pw")
        out = svc.refresh(first["refresh_token"])
        assert out["token"] == "session:alice"  # fresh session, no password
        assert out["refresh_token"] != first["refresh_token"]  # rotated

    def test_refresh_single_use(self, tmp_path):
        svc, _ = self._svc(tmp_path)
        first = svc.login("alice", "pw")
        svc.refresh(first["refresh_token"])
        with pytest.raises(RefreshError):
            svc.refresh(first["refresh_token"])  # old token dead after rotation

    def test_refresh_unknown_token(self, tmp_path):
        svc, _ = self._svc(tmp_path)
        with pytest.raises(RefreshError):
            svc.refresh("bogus")

    def test_revoked_user_refresh_fails_in_one_step(self, tmp_path):
        svc, _ = self._svc(tmp_path)
        first = svc.login("alice", "pw")
        svc.revoke_user("alice")
        with pytest.raises(RefreshError):
            svc.refresh(first["refresh_token"])

    def test_refresh_for_removed_user_fails_and_revokes(self, tmp_path):
        svc, mgr = self._svc(tmp_path)
        first = svc.login("alice", "pw")
        mgr._users.discard("alice")  # account removed between login and refresh
        with pytest.raises(RefreshError):
            svc.refresh(first["refresh_token"])
        assert svc._store.count() == 0  # rotated token revoked — no lingering access

    def test_attach_token_delegates_to_core(self, tmp_path):
        svc, mgr = self._svc(tmp_path)
        out = svc.attach_token("session:alice", ["s1"])
        assert out["token"] == "attach:session:alice"
        assert mgr.attach_calls == [("session:alice", ["s1"])]
