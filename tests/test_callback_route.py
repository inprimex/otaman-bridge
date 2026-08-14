"""Integration tests for the /auth/callback route on the bridge daemon.

Exercises the live HTTP daemon. Pre-stages a state in the daemon's
PendingLoginStore (via the LoginFlow) and then injects a stub
LoginCompleter for the actual code-exchange step so the test doesn't
talk to Zitadel.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon, read_endpoint_file
from otaman_bridge.transports.null import NullTransport
from otaman_bridge_ee.web_auth import (
    LoginCompleteError,
    LoginFlow,
    PendingLoginStore,
    TokenExchangeError,
    WebAuthConfig,
)
from otaman_bridge_ee.web_session import Session, SessionCookie, SessionStore


@pytest.fixture
def running_daemon(tmp_path):
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test",
        transport=transport,
        endpoint_file=endpoint,
    )
    daemon.start()
    try:
        yield daemon, endpoint
    finally:
        daemon.stop()


def _request(url, *, allow_redirects=False):
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):  # noqa: ARG002
            return None

    opener = (
        urllib.request.build_opener(_NoRedirect())
        if not allow_redirects
        else urllib.request.build_opener()
    )
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _daemon_url(endpoint_file: Path) -> str:
    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


def _wire_full_web_auth(daemon):
    """Stand up real LoginFlow + SessionStore + SessionCookie on the daemon
    (without a real LoginCompleter -- tests inject their own)."""
    cfg = WebAuthConfig(
        issuer="https://otaman.example/auth",
        client_id="bridge-client-id",
        redirect_uri="http://otaman.example/auth/callback",
    )
    daemon.web_login_flow = LoginFlow(cfg, PendingLoginStore())
    daemon.session_store = SessionStore()
    daemon.session_cookie = SessionCookie(secure=False)


class _StubCompleter:
    """Stub for LoginCompleter -- returns a pre-built session OR raises."""

    def __init__(self, *, session=None, raise_exc=None):
        self.session = session
        self.raise_exc = raise_exc
        self.calls = []

    def complete(self, *, code, state):
        self.calls.append((code, state))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.session


class TestAuthCallbackRoute:
    def test_503_when_web_login_not_configured(self, running_daemon):
        daemon, endpoint = running_daemon
        daemon.web_login_flow = None
        daemon.login_completer = None
        base = _daemon_url(endpoint)
        code, _, _ = _request(f"{base}/auth/callback?code=abc&state=xyz")
        assert code == 503

    def test_400_when_missing_code_or_state(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        daemon.login_completer = _StubCompleter()  # not actually called
        base = _daemon_url(endpoint)
        code, _, _ = _request(f"{base}/auth/callback?state=only")
        assert code == 400
        code, _, _ = _request(f"{base}/auth/callback?code=only")
        assert code == 400
        code, _, _ = _request(f"{base}/auth/callback")
        assert code == 400

    def test_302_with_session_cookie_on_success(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        sess = Session(
            id="session-id-1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            user_id="u1",
            email="a@b",
            roles=("otaman:viewer",),
            expires_at=9_999_999_999.0,
        )
        daemon.login_completer = _StubCompleter(session=sess)
        base = _daemon_url(endpoint)
        code, headers, _ = _request(f"{base}/auth/callback?code=auth-code&state=stored-state")
        assert code == 302
        # Set-Cookie header carries our session id
        set_cookie = headers.get("Set-Cookie") or headers.get("set-cookie")
        assert set_cookie is not None
        assert "otaman_bridge_sid=session-id-1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=Lax" in set_cookie
        # Redirects to / by default
        loc = headers.get("Location") or headers.get("location")
        assert loc == "/"

    def test_400_on_login_complete_error(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        daemon.login_completer = _StubCompleter(
            raise_exc=LoginCompleteError("unknown or expired state"),
        )
        base = _daemon_url(endpoint)
        code, _, _ = _request(f"{base}/auth/callback?code=c&state=stale")
        assert code == 400

    def test_502_on_token_exchange_error(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        daemon.login_completer = _StubCompleter(
            raise_exc=TokenExchangeError("HTTP 502 from token endpoint"),
        )
        base = _daemon_url(endpoint)
        code, _, _ = _request(f"{base}/auth/callback?code=c&state=stored")
        # Network failure mapping -- not the user's fault, retry-able
        assert code == 502

    def test_oauth_error_param_returns_400(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_full_web_auth(daemon)
        daemon.login_completer = _StubCompleter()
        base = _daemon_url(endpoint)
        # Zitadel returns ?error=access_denied when user denies consent
        code, _, _ = _request(
            f"{base}/auth/callback?error=access_denied&error_description=user+denied"
        )
        assert code == 400
