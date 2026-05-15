"""Tests for /auth/logout + session-cookie auth on existing routes.

Phase 4.3 chunk D -- closes the web login flow.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon, read_endpoint_file
from otaman_bridge.transports.null import NullTransport
from otaman_bridge.web_auth import LoginFlow, PendingLoginStore, WebAuthConfig
from otaman_bridge.web_session import SessionCookie, SessionStore


@pytest.fixture
def running_daemon(tmp_path):
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test", transport=transport, endpoint_file=endpoint,
    )
    daemon.start()
    try:
        yield daemon, endpoint
    finally:
        daemon.stop()


def _request(url, *, method="GET", cookie=None, token=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _daemon_url(endpoint_file: Path) -> str:
    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


def _wire_full_web_auth(daemon):
    cfg = WebAuthConfig(
        issuer="https://otaman.example/auth",
        client_id="bridge-client-id",
        redirect_uri="http://otaman.example/auth/callback",
    )
    daemon.web_login_flow = LoginFlow(cfg, PendingLoginStore())
    daemon.session_store = SessionStore()
    daemon.session_cookie = SessionCookie(secure=False)


class TestAuthLogout:
    def test_logout_clears_session_and_returns_clear_cookie(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        sess = daemon.session_store.create(
            user_id="u1", email="a@b", roles=("otaman:viewer",),
        )
        base = _daemon_url(endpoint)
        code, headers, _ = _request(
            f"{base}/auth/logout", method="POST",
            cookie=f"otaman_bridge_sid={sess.id}",
        )
        assert code == 204
        # Session removed from store
        assert daemon.session_store.get(sess.id) is None
        # Set-Cookie clears the cookie (Max-Age=0)
        set_cookie = headers.get("Set-Cookie") or headers.get("set-cookie")
        assert set_cookie is not None
        assert "otaman_bridge_sid=" in set_cookie
        assert "Max-Age=0" in set_cookie

    def test_logout_without_cookie_is_idempotent_204(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        base = _daemon_url(endpoint)
        code, _, _ = _request(f"{base}/auth/logout", method="POST")
        assert code == 204

    def test_logout_with_unknown_cookie_is_idempotent_204(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        base = _daemon_url(endpoint)
        code, _, _ = _request(
            f"{base}/auth/logout", method="POST",
            cookie="otaman_bridge_sid=never-existed",
        )
        assert code == 204

    def test_logout_returns_503_when_unconfigured(self, running_daemon):
        daemon, endpoint = running_daemon
        daemon.session_store = None
        daemon.session_cookie = None
        base = _daemon_url(endpoint)
        code, _, _ = _request(f"{base}/auth/logout", method="POST")
        assert code == 503


class TestSessionCookieAuth:
    """Cookies should authenticate to existing routes when session_store
    is configured. Falls through to OIDC bearer + loopback bearer."""

    def _shutdown_url(self, endpoint):
        return f"{_daemon_url(endpoint)}/shutdown"

    def test_valid_session_cookie_accepted_on_protected_route(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        sess = daemon.session_store.create(
            user_id="u1", email=None, roles=(),
        )
        # /shutdown is auth'd; if cookie works we get 200 not 401
        code, _, _ = _request(
            self._shutdown_url(endpoint), method="POST",
            cookie=f"otaman_bridge_sid={sess.id}",
        )
        assert code != 401, "valid session cookie was rejected"

    def test_unknown_session_cookie_falls_through_to_other_auth(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        # Unknown cookie + no Bearer = 401 (no fallback succeeds)
        code, _, _ = _request(
            self._shutdown_url(endpoint), method="POST",
            cookie="otaman_bridge_sid=never-existed",
        )
        assert code == 401

    def test_loopback_bearer_still_works_when_no_cookie(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        # No cookie, but valid loopback bearer -> accepted (existing path)
        code, _, _ = _request(
            self._shutdown_url(endpoint), method="POST",
            token=daemon.token,
        )
        assert code != 401
