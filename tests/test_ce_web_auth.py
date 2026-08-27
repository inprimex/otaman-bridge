"""Tests for the CE web-auth refresh layer (ce-refresh-token 1.2)."""

from __future__ import annotations

import json

import pytest
from otaman_core.web_auth import AuthError  # the real error the mapping catches

import otaman_bridge.ce_web_auth as cwa
from otaman_bridge.ce_web_auth import (
    CeWebAuthService,
    RefreshError,
    RefreshTokenStore,
    attach_response,
    login_response,
    refresh_response,
)


class _FakeMgr:
    """Minimal fake CeAuthManager honoring the frozen core interface."""

    def __init__(self, *, enabled=True, users=("alice",)):
        self.enabled = enabled
        self._users = set(users)
        self.attach_calls: list = []

    def login(self, username, password):
        if username in self._users and password == "pw":
            return f"session:{username}"
        raise AuthError("invalid credentials")

    def issue_session_token(self, username):
        if username in self._users:
            return f"session:{username}"
        raise AuthError("unknown user")

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
        with pytest.raises(AuthError):
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


# ---------------------------------------------------------------------------
# HTTP response mapping (login/refresh/attach) — status codes match the runner
# ---------------------------------------------------------------------------


class TestRoutes:
    def _svc(self, tmp_path, **mgr_kw):
        return CeWebAuthService(_FakeMgr(**mgr_kw), RefreshTokenStore(tmp_path))

    # ---- login ----
    def test_login_ok(self, tmp_path):
        status, resp = login_response(self._svc(tmp_path), {"username": "alice", "password": "pw"})
        assert status == 200
        assert resp["token"] == "session:alice"
        assert resp["refresh_token"] and resp["token_type"] == "Bearer"

    def test_login_bad_credentials_401(self, tmp_path):
        status, resp = login_response(
            self._svc(tmp_path), {"username": "alice", "password": "nope"}
        )
        assert status == 401
        assert resp == {"error": "invalid credentials"}

    def test_login_missing_fields_400(self, tmp_path):
        status, _ = login_response(self._svc(tmp_path), {"username": "alice"})
        assert status == 400

    def test_login_not_configured_404(self, tmp_path):
        assert login_response(None, {"username": "a", "password": "b"})[0] == 404
        disabled = self._svc(tmp_path, enabled=False)
        assert login_response(disabled, {"username": "a", "password": "b"})[0] == 404

    # ---- refresh ----
    def test_refresh_ok_rotates(self, tmp_path):
        svc = self._svc(tmp_path)
        _, login = login_response(svc, {"username": "alice", "password": "pw"})
        status, resp = refresh_response(svc, {"refresh_token": login["refresh_token"]})
        assert status == 200
        assert resp["token"] == "session:alice"
        assert resp["refresh_token"] != login["refresh_token"]

    def test_refresh_invalid_401_single_step(self, tmp_path):
        status, resp = refresh_response(self._svc(tmp_path), {"refresh_token": "bogus"})
        assert status == 401  # non-2xx -> client falls back to password in one step
        assert "error" in resp

    def test_refresh_missing_field_400(self, tmp_path):
        assert refresh_response(self._svc(tmp_path), {})[0] == 400

    def test_refresh_reused_token_401(self, tmp_path):
        svc = self._svc(tmp_path)
        _, login = login_response(svc, {"username": "alice", "password": "pw"})
        refresh_response(svc, {"refresh_token": login["refresh_token"]})
        # replay of the now-rotated token
        assert refresh_response(svc, {"refresh_token": login["refresh_token"]})[0] == 401

    # ---- attach ----
    def test_attach_ok(self, tmp_path):
        status, resp = attach_response(self._svc(tmp_path), "Bearer session:alice", {})
        assert status == 200
        assert resp["token"] == "attach:session:alice"

    def test_attach_missing_bearer_401(self, tmp_path):
        assert attach_response(self._svc(tmp_path), "", {})[0] == 401

    def test_attach_not_configured_404(self, tmp_path):
        assert attach_response(None, "Bearer x", {})[0] == 404


# ---------------------------------------------------------------------------
# End-to-end against the REAL otaman_core.web_auth.CeAuthManager
# (proves the mounted core module + issue_session_token seam interoperate)
# ---------------------------------------------------------------------------


class TestRealIntegration:
    def _real_svc(self, tmp_path, *, users=("alice",)):
        import bcrypt
        from otaman_core.web_auth import CeAuthManager, LocalAuthConfig, UserRecord

        recs = [
            UserRecord(
                username=u,
                password_hash=bcrypt.hashpw(b"secret", bcrypt.gensalt()).decode(),
                role="observer",
            )
            for u in users
        ]
        cfg = LocalAuthConfig(enabled=True, users=recs)
        mgr = CeAuthManager(config=cfg, hmac_secret="x" * 40, attach_token_ttl=3600)
        return CeWebAuthService(mgr, RefreshTokenStore(tmp_path)), mgr

    def test_login_refresh_attach_end_to_end(self, tmp_path):
        svc, mgr = self._real_svc(tmp_path)
        login = svc.login("alice", "secret")

        # Refresh mints a REAL fresh session JWT with no password (reload path).
        refreshed = svc.refresh(login["refresh_token"])
        assert refreshed["refresh_token"] != login["refresh_token"]  # rotated
        claims = mgr.verify_session_token(refreshed["token"])
        assert claims["sub"] == "alice" and claims["type"] == "session"

        # The refreshed session JWT exchanges for a REAL attach token.
        attach = svc.attach_token(refreshed["token"])
        aclaims = mgr.verify_attach_token(attach["token"])
        assert aclaims["sub"] == "alice" and aclaims["type"] == "attach"

    def test_response_mappers_end_to_end(self, tmp_path):
        svc, _ = self._real_svc(tmp_path)
        s, login = login_response(svc, {"username": "alice", "password": "secret"})
        assert s == 200 and login["token_type"] == "Bearer"
        s, refreshed = refresh_response(svc, {"refresh_token": login["refresh_token"]})
        assert s == 200 and refreshed["token"]
        s, attach = attach_response(svc, f"Bearer {refreshed['token']}", {})
        assert s == 200 and attach["mode"]

    def test_refresh_rejected_after_user_removed(self, tmp_path):
        from otaman_core.web_auth import LocalAuthConfig

        svc, mgr = self._real_svc(tmp_path)
        login = svc.login("alice", "secret")
        mgr._config = LocalAuthConfig(enabled=True, users=[])  # account removed
        # issue_session_token raises AuthError -> RefreshError -> 401 fallback
        assert refresh_response(svc, {"refresh_token": login["refresh_token"]})[0] == 401

    def test_refresh_token_rejected_at_login(self, tmp_path):
        """The refresh token is refresh-only: it must never authenticate at login."""
        svc, _ = self._real_svc(tmp_path)
        login = svc.login("alice", "secret")
        # Presenting the refresh token as the password fails like any bad password.
        status, _ = login_response(svc, {"username": "alice", "password": login["refresh_token"]})
        assert status == 401
