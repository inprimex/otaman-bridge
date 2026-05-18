"""Integration tests for AS metadata overlay routes (mcp-oauth chunk D3).

Drives the live HTTP daemon. The new routes:
- GET /.well-known/oauth-authorization-server
- GET /.well-known/openid-configuration  (same payload, alternate path)
- POST /oauth/register  (501 stub from D3; real handler lands in D4)

Also exercises the chunk B route flip: when the shim is enabled,
/.well-known/oauth-protected-resource advertises the bridge URL (not
the upstream issuer URL) as the authorization server.
"""

from __future__ import annotations

import json
import os
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon, read_endpoint_file
from otaman_bridge.dcr_shim import IdpConfig, MetadataCache
from otaman_bridge.transports.null import NullTransport


# ---- fixtures -------------------------------------------------------------


def _fake_oidc_validator(issuer: str = "http://idp.example") -> object:
    """Same stub shape as test_well_known_routes / test_mcp_oauth_challenge."""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(issuer=issuer),
        validate=lambda _hdr: types.SimpleNamespace(
            ok=False, user_id=None, email=None, roles=(),
        ),
    )


def _shim_config(*, mgmt_base: str = "http://idp.example", trust: str = "open") -> IdpConfig:
    return IdpConfig(
        type="zitadel",
        dcr_shim=True,
        management_base_url=mgmt_base,
        project_id="proj-123",
        registration_trust=trust,
        metadata_cache_seconds=300,
    )


@pytest.fixture
def daemon_with_shim(tmp_path):
    """Daemon with both OIDC validator and the DCR shim active."""
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test", transport=transport, endpoint_file=endpoint,
    )
    daemon.oidc_validator = _fake_oidc_validator()
    daemon.idp_config = _shim_config()
    daemon._idp_metadata_cache = MetadataCache(ttl_seconds=300)
    daemon.start()
    try:
        yield daemon, endpoint
    finally:
        daemon.stop()


@pytest.fixture
def daemon_without_shim(tmp_path):
    """Daemon with OIDC but shim disabled — chunk B behavior preserved."""
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test", transport=transport, endpoint_file=endpoint,
    )
    daemon.oidc_validator = _fake_oidc_validator()
    # idp_config stays None (env didn't enable it)
    daemon.start()
    try:
        yield daemon, endpoint
    finally:
        daemon.stop()


def _get(url, *, headers=None):
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _post(url, *, body=None, headers=None):
    data = (json.dumps(body) if body is not None else b"").encode("utf-8") \
        if not isinstance(body, (bytes, type(None))) else (body or b"")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _daemon_url(endpoint_file: Path) -> str:
    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


# ---- AS metadata overlay (GET /.well-known/oauth-authorization-server) ----


class TestASMetadataOverlay:
    """Both paths serve the same overlaid payload when shim is on."""

    @pytest.mark.parametrize("path", [
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ])
    def test_returns_overlay_when_shim_enabled(self, daemon_with_shim, monkeypatch, path):
        daemon, endpoint = daemon_with_shim
        # Stub the upstream fetch so we don't actually call out to idp.example.
        upstream = {
            "issuer": "http://idp.example",
            "authorization_endpoint": "http://idp.example/oauth/v2/authorize",
            "token_endpoint": "http://idp.example/oauth/v2/token",
            "jwks_uri": "http://idp.example/oauth/v2/keys",
        }
        from otaman_bridge import dcr_shim
        monkeypatch.setattr(
            dcr_shim, "fetch_upstream_metadata",
            lambda base_url, **kw: upstream,
        )

        code, headers, body = _get(_daemon_url(endpoint) + path)
        assert code == 200
        ct = headers.get("Content-Type") or headers.get("content-type") or ""
        assert "application/json" in ct
        m = json.loads(body)
        # upstream fields preserved
        assert m["issuer"] == upstream["issuer"]
        assert m["authorization_endpoint"] == upstream["authorization_endpoint"]
        # injected fields present
        assert "registration_endpoint" in m
        assert m["registration_endpoint"].endswith("/oauth/register")
        assert m["registration_endpoint_auth_methods_supported"] == ["none"]
        # registration_endpoint points at the bridge, not at idp
        assert "idp.example" not in m["registration_endpoint"]

    @pytest.mark.parametrize("path", [
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ])
    def test_returns_404_when_shim_disabled(self, daemon_without_shim, path):
        _, endpoint = daemon_without_shim
        code, _, body = _get(_daemon_url(endpoint) + path)
        assert code == 404
        assert b"DCR shim" in body

    def test_caches_upstream_fetch_across_calls(self, daemon_with_shim, monkeypatch):
        daemon, endpoint = daemon_with_shim
        fetch_count = {"n": 0}
        upstream = {"issuer": "http://idp.example"}

        def _counting_fetch(base_url, **kw):
            fetch_count["n"] += 1
            return upstream

        from otaman_bridge import dcr_shim
        monkeypatch.setattr(dcr_shim, "fetch_upstream_metadata", _counting_fetch)

        # First call: fetch upstream.
        c1, _, _ = _get(_daemon_url(endpoint) + "/.well-known/oauth-authorization-server")
        assert c1 == 200
        assert fetch_count["n"] == 1
        # Second call: cached, no upstream fetch.
        c2, _, _ = _get(_daemon_url(endpoint) + "/.well-known/oauth-authorization-server")
        assert c2 == 200
        assert fetch_count["n"] == 1
        # Third via the other path: same cache.
        c3, _, _ = _get(_daemon_url(endpoint) + "/.well-known/openid-configuration")
        assert c3 == 200
        assert fetch_count["n"] == 1

    def test_502_when_upstream_unreachable(self, daemon_with_shim, monkeypatch):
        daemon, endpoint = daemon_with_shim
        from otaman_bridge import dcr_shim

        def _fail(base_url, **kw):
            raise dcr_shim.MetadataFetchError("simulated upstream unreachable")

        monkeypatch.setattr(dcr_shim, "fetch_upstream_metadata", _fail)
        code, _, body = _get(_daemon_url(endpoint) + "/.well-known/oauth-authorization-server")
        assert code == 502
        # Error body surfaces the upstream failure for operators.
        assert b"upstream metadata unavailable" in body
        assert b"simulated upstream unreachable" in body

    def test_unauthenticated_route(self, daemon_with_shim, monkeypatch):
        """Metadata endpoints are public per OAuth spec — no auth headers needed."""
        daemon, endpoint = daemon_with_shim
        from otaman_bridge import dcr_shim
        monkeypatch.setattr(
            dcr_shim, "fetch_upstream_metadata",
            lambda base_url, **kw: {"issuer": "http://idp.example"},
        )
        # No Authorization header
        code, _, _ = _get(_daemon_url(endpoint) + "/.well-known/oauth-authorization-server")
        assert code == 200


# ---- protected-resource route flip ---------------------------------------


class TestProtectedResourceRouteFlip:
    """When shim is enabled, /.well-known/oauth-protected-resource advertises
    the bridge URL as the authorization server (so MCP clients fetch the
    overlay from us). When disabled, chunk B behavior preserved."""

    def test_shim_enabled_advertises_bridge(self, daemon_with_shim):
        daemon, endpoint = daemon_with_shim
        code, _, body = _get(_daemon_url(endpoint) + "/.well-known/oauth-protected-resource")
        assert code == 200
        m = json.loads(body)
        # bridge URL — points at localhost / loopback, NOT idp.example
        assert len(m["authorization_servers"]) == 1
        assert "idp.example" not in m["authorization_servers"][0]
        # Should be the same URL the bridge advertises as the resource.
        assert m["authorization_servers"][0] == m["resource"]

    def test_shim_disabled_advertises_upstream_issuer(self, daemon_without_shim):
        daemon, endpoint = daemon_without_shim
        code, _, body = _get(_daemon_url(endpoint) + "/.well-known/oauth-protected-resource")
        assert code == 200
        m = json.loads(body)
        # Chunk B behavior: advertise the OIDC issuer directly.
        assert m["authorization_servers"] == ["http://idp.example"]


# ---- /oauth/register stub (D3-only) --------------------------------------


class TestRegisterRouteGate:
    """Route-presence gate (real handler tests live in test_dcr_register.py)."""

    def test_returns_404_when_shim_disabled(self, daemon_without_shim):
        _, endpoint = daemon_without_shim
        code, _, body = _post(_daemon_url(endpoint) + "/oauth/register", body={
            "redirect_uris": ["http://localhost:54321/cb"],
        })
        assert code == 404
        assert b"DCR shim" in body
