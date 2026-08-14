"""Tests for the root '/' route on the bridge daemon.

Chunk 2 of the manual-test prep: a minimal HTML landing page so a
browser hitting / after /auth/callback's 302 sees a real page that
proves the session cookie is doing its job.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon, read_endpoint_file
from otaman_bridge.transports.null import NullTransport
from otaman_bridge_ee.web_session import SessionCookie, SessionStore


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


def _request(url, *, cookie=None):
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", errors="replace")


def _daemon_url(endpoint_file: Path) -> str:
    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


def _wire_session(daemon):
    daemon.session_store = SessionStore()
    daemon.session_cookie = SessionCookie(secure=False)


class TestRootRoute:
    def test_root_returns_html_with_login_link_when_no_cookie(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_session(daemon)
        base = _daemon_url(endpoint)
        code, headers, body = _request(f"{base}/")
        assert code == 200
        ct = headers.get("Content-Type") or headers.get("content-type") or ""
        assert "text/html" in ct
        assert "/auth/login" in body
        assert "Log in" in body or "log in" in body

    def test_root_with_valid_cookie_shows_user(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_session(daemon)
        sess = daemon.session_store.create(
            user_id="user-42",
            email="dev-a@example",
            roles=("otaman:developer",),
        )
        base = _daemon_url(endpoint)
        code, _, body = _request(f"{base}/", cookie=f"otaman_bridge_sid={sess.id}")
        assert code == 200
        assert "user-42" in body
        assert "dev-a@example" in body
        assert "otaman:developer" in body
        assert "/auth/logout" in body

    def test_root_with_unknown_cookie_shows_login_link(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_session(daemon)
        base = _daemon_url(endpoint)
        code, _, body = _request(f"{base}/", cookie="otaman_bridge_sid=never-existed")
        assert code == 200
        assert "/auth/login" in body
        # Should NOT leak the unknown sid value
        assert "never-existed" not in body

    def test_root_works_without_session_store_configured(self, running_daemon):
        daemon, endpoint = running_daemon
        # No web auth at all -- root still serves something useful
        daemon.session_store = None
        daemon.session_cookie = None
        base = _daemon_url(endpoint)
        code, _, body = _request(f"{base}/")
        assert code == 200
        # When web auth isn't configured, no /auth/login link (would 503)
        assert "not configured" in body.lower() or "loopback" in body.lower()

    def test_status_route_still_works(self, running_daemon):
        """Regression: /status is a separate route that returns JSON."""
        daemon, endpoint = running_daemon
        base = _daemon_url(endpoint)
        code, headers, body = _request(f"{base}/status")
        assert code == 200
        ct = headers.get("Content-Type") or headers.get("content-type") or ""
        assert "json" in ct
        # Shouldn't be HTML
        assert "<html" not in body.lower()
