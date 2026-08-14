"""Tests for /.well-known/oauth-protected-resource (RFC 9728).

Drives the live HTTP daemon. The route is unauthenticated by design:
MCP clients fetch it before they have any token, to discover which
authorization server they need to talk to.
"""

from __future__ import annotations

import json
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import (
    BridgeDaemon,
    _build_protected_resource_metadata,
    _resolve_public_resource_url,
    read_endpoint_file,
)
from otaman_bridge.transports.null import NullTransport

# --- pure helper tests -----------------------------------------------------


class TestBuildProtectedResourceMetadata:
    def test_default_shape(self):
        m = _build_protected_resource_metadata(
            issuer="https://issuer.example",
            resource="http://localhost:8090",
        )
        assert m == {
            "resource": "http://localhost:8090",
            "authorization_servers": ["https://issuer.example"],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["openid", "profile", "email"],
        }

    def test_custom_scopes(self):
        m = _build_protected_resource_metadata(
            issuer="https://issuer.example",
            resource="http://r",
            scopes=("openid",),
        )
        assert m["scopes_supported"] == ["openid"]

    def test_authorization_servers_is_a_list(self):
        # RFC 9728: this field is an ARRAY even when there's only one.
        m = _build_protected_resource_metadata(
            issuer="https://i",
            resource="http://r",
        )
        assert isinstance(m["authorization_servers"], list)
        assert len(m["authorization_servers"]) == 1


class TestResolvePublicResourceUrl:
    def test_uses_host_header_by_default(self, monkeypatch):
        monkeypatch.delenv("OTAMAN_BRIDGE_PUBLIC_URL", raising=False)
        assert _resolve_public_resource_url("bridge.example:8090") == "http://bridge.example:8090"

    def test_falls_back_to_loopback_when_host_empty(self, monkeypatch):
        monkeypatch.delenv("OTAMAN_BRIDGE_PUBLIC_URL", raising=False)
        assert _resolve_public_resource_url("") == "http://127.0.0.1"

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_BRIDGE_PUBLIC_URL", "https://prod.bridge.example")
        assert _resolve_public_resource_url("ignored.host") == "https://prod.bridge.example"

    def test_env_override_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_BRIDGE_PUBLIC_URL", "https://prod.bridge.example/")
        assert _resolve_public_resource_url("ignored") == "https://prod.bridge.example"


# --- integration via running daemon ---------------------------------------


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


def _get(url, *, headers=None):
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _daemon_url(endpoint_file: Path) -> str:
    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


def _fake_oidc_validator(issuer: str = "https://issuer.example") -> object:
    """Minimal stand-in for OIDCValidator.

    The /.well-known route only reads ``.config.issuer`` so we don't need
    to build the real thing (which would try to fetch JWKS).
    """
    return types.SimpleNamespace(config=types.SimpleNamespace(issuer=issuer))


class TestProtectedResourceRoute:
    def test_returns_404_when_oidc_not_configured(self, running_daemon):
        """No OIDC env vars at daemon start → validator is None → 404."""
        _, endpoint = running_daemon
        code, _, body = _get(_daemon_url(endpoint) + "/.well-known/oauth-protected-resource")
        assert code == 404
        assert b"OIDC not configured" in body

    def test_returns_200_with_metadata_when_oidc_configured(self, running_daemon):
        daemon, endpoint = running_daemon
        daemon.oidc_validator = _fake_oidc_validator("https://zitadel.example")

        code, headers, body = _get(_daemon_url(endpoint) + "/.well-known/oauth-protected-resource")
        assert code == 200
        ct = headers.get("Content-Type") or headers.get("content-type") or ""
        assert "application/json" in ct
        m = json.loads(body)
        assert m["authorization_servers"] == ["https://zitadel.example"]
        assert m["bearer_methods_supported"] == ["header"]
        assert "openid" in m["scopes_supported"]
        # resource should be a URL pointing at the bridge itself.
        assert m["resource"].startswith("http://127.0.0.1:")

    def test_env_override_changes_resource_url(self, running_daemon, monkeypatch):
        daemon, endpoint = running_daemon
        daemon.oidc_validator = _fake_oidc_validator()
        monkeypatch.setenv("OTAMAN_BRIDGE_PUBLIC_URL", "https://bridge.example.com")

        code, _, body = _get(_daemon_url(endpoint) + "/.well-known/oauth-protected-resource")
        assert code == 200
        m = json.loads(body)
        assert m["resource"] == "https://bridge.example.com"

    def test_no_auth_required(self, running_daemon):
        """Route works with no Authorization header at all (per RFC 9728)."""
        daemon, endpoint = running_daemon
        daemon.oidc_validator = _fake_oidc_validator()
        # No auth header in _get → must still 200.
        code, _, _ = _get(_daemon_url(endpoint) + "/.well-known/oauth-protected-resource")
        assert code == 200

    def test_trailing_slash_tolerated(self, running_daemon):
        """The do_GET dispatcher strips trailing slashes; route still matches."""
        daemon, endpoint = running_daemon
        daemon.oidc_validator = _fake_oidc_validator()
        code, _, _ = _get(_daemon_url(endpoint) + "/.well-known/oauth-protected-resource/")
        assert code == 200

    def test_other_well_known_paths_still_404(self, running_daemon):
        """Regression: only the protected-resource route is wired; others 404.

        Specifically, .well-known/oauth-authorization-server is intentionally
        NOT served by the resource server — the MCP client fetches that from
        the issuer (Zitadel) directly per the MCP authorization spec.
        """
        daemon, endpoint = running_daemon
        daemon.oidc_validator = _fake_oidc_validator()
        code, _, _ = _get(_daemon_url(endpoint) + "/.well-known/oauth-authorization-server")
        assert code == 404
