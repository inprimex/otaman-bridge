"""Integration tests for the /auth/login route on the bridge daemon.

Mirrors test_bridge_oidc.py's pattern: stand up a live daemon with a
NullTransport and exercise the route via urllib. Web login flow is
monkey-patched onto the daemon's `web_login_flow` attribute so the
test doesn't depend on env-var setup.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon
from otaman_bridge.transports.null import NullTransport
from otaman_bridge_ee.web_auth import LoginFlow, PendingLoginStore, WebAuthConfig


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


def _request(url: str, *, allow_redirects: bool = False) -> tuple[int, dict[str, str], bytes]:
    """Issue a GET. When allow_redirects is False, treat 3xx as the final
    response (return its status + headers + body) instead of following.
    """

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
    from otaman_bridge.daemon import read_endpoint_file

    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


def _wire_web_login(daemon, *, project_id=None):
    """Attach a real LoginFlow with a fresh PendingLoginStore to the daemon."""
    cfg = WebAuthConfig(
        issuer="https://otaman.example/auth",
        client_id="bridge-client-id",
        redirect_uri="https://otaman.example/auth/callback",
        project_id=project_id,
    )
    store = PendingLoginStore()
    daemon.web_login_flow = LoginFlow(cfg, store)
    return daemon.web_login_flow


class TestAuthLoginRoute:
    def test_503_when_web_login_not_configured(self, running_daemon):
        daemon, endpoint = running_daemon
        daemon.web_login_flow = None
        base = _daemon_url(endpoint)
        code, headers, body = _request(f"{base}/auth/login")
        assert code == 503

    def test_302_redirect_when_configured(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_web_login(daemon)
        base = _daemon_url(endpoint)
        code, headers, _ = _request(f"{base}/auth/login")
        assert code == 302
        loc = headers.get("Location") or headers.get("location")
        assert loc is not None
        assert loc.startswith("https://otaman.example/auth/oauth/v2/authorize?")

    def test_redirect_url_carries_pkce_params(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_web_login(daemon)
        base = _daemon_url(endpoint)
        _, headers, _ = _request(f"{base}/auth/login")
        loc = headers.get("Location") or headers.get("location")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
        assert params["client_id"] == "bridge-client-id"
        assert params["redirect_uri"] == "https://otaman.example/auth/callback"
        assert params["response_type"] == "code"
        assert params["code_challenge_method"] == "S256"
        assert "state" in params and len(params["state"]) >= 40
        assert "code_challenge" in params
        assert "openid" in params["scope"]

    def test_state_registered_in_pending_store(self, running_daemon):
        daemon, endpoint = running_daemon
        flow = _wire_web_login(daemon)
        base = _daemon_url(endpoint)
        before = len(flow.store)
        _, headers, _ = _request(f"{base}/auth/login")
        assert len(flow.store) == before + 1
        loc = headers.get("Location") or headers.get("location")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
        # callback would call store.take(state) -- it's there
        verifier = flow.store.take(params["state"])
        assert verifier is not None
        assert len(verifier) >= 40

    def test_response_has_no_store_cache_directive(self, running_daemon):
        daemon, endpoint = running_daemon
        _wire_web_login(daemon)
        base = _daemon_url(endpoint)
        _, headers, _ = _request(f"{base}/auth/login")
        cache_control = headers.get("Cache-Control") or headers.get("cache-control") or ""
        # The redirect MUST NOT be cached -- state + verifier are single-use
        assert "no-store" in cache_control.lower()

    def test_two_requests_get_different_states(self, running_daemon):
        daemon, endpoint = running_daemon
        flow = _wire_web_login(daemon)
        base = _daemon_url(endpoint)
        _, h1, _ = _request(f"{base}/auth/login")
        _, h2, _ = _request(f"{base}/auth/login")
        s1 = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(h1["Location"]).query))["state"]
        s2 = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(h2["Location"]).query))["state"]
        assert s1 != s2
        # Both registered in the store
        assert len(flow.store) == 2
