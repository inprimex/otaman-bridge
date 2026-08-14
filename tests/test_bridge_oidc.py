"""Tests for the OIDC integration in otaman_bridge.daemon.

Mirrors the runner's OIDC integration tests (otaman-runner@e4c29e1).

Covers two surfaces:

1. ``_build_oidc_validator_from_env`` — env-var parsing helper that
   constructs an OIDCValidator (or None) at daemon startup. Tested
   in-process via monkeypatch.
2. ``Handler._auth_ok`` — the auth boundary on the HTTP server. Tested
   through a live daemon with the ``oidc_validator`` attribute
   monkey-patched to a stub validator that controls the accept/reject
   decision deterministically.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from otaman_bridge.daemon import (
    BridgeDaemon,
    _build_oidc_validator_from_env,
)
from otaman_bridge.transports.null import NullTransport

# ---------------------------------------------------------------------------
# Fixtures shared with the rest of the daemon tests


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
        yield daemon, transport
    finally:
        daemon.stop()


def _notify(url: str, token: str | None) -> int:
    """POST /notify with a minimal info-message body; returns HTTP status."""
    payload = json.dumps(
        {
            "account": "test",
            "project": "test-proj",
            "severity": "info",
            "title": "test",
            "body": "test",
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------------------
# _build_oidc_validator_from_env


class TestBuildValidatorFromEnv:
    def test_returns_none_when_mode_unset(self, monkeypatch):
        monkeypatch.delenv("OTAMAN_AUTH_MODE", raising=False)
        assert _build_oidc_validator_from_env() is None

    def test_returns_none_when_mode_not_oidc(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "bearer")
        assert _build_oidc_validator_from_env() is None

    def test_returns_none_when_issuer_missing(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.setenv("OIDC_AUDIENCE_BRIDGE", "client-id")
        assert _build_oidc_validator_from_env() is None

    def test_returns_none_when_audience_missing(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.setenv("OIDC_ISSUER", "http://zitadel.test")
        monkeypatch.delenv("OIDC_AUDIENCE_BRIDGE", raising=False)
        assert _build_oidc_validator_from_env() is None

    def test_builds_validator_when_fully_configured(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.setenv("OIDC_ISSUER", "http://zitadel.test/auth")
        monkeypatch.setenv("OIDC_AUDIENCE_BRIDGE", "bridge-client-id")
        validator = _build_oidc_validator_from_env()
        assert validator is not None
        assert validator.config.issuer == "http://zitadel.test/auth"
        assert validator.config.audience == "bridge-client-id"
        assert validator.config.required_role is None

    def test_required_role_propagated(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.setenv("OIDC_ISSUER", "http://x")
        monkeypatch.setenv("OIDC_AUDIENCE_BRIDGE", "y")
        monkeypatch.setenv("OIDC_REQUIRED_ROLE", "otaman:developer")
        v = _build_oidc_validator_from_env()
        assert v is not None
        assert v.config.required_role == "otaman:developer"

    def test_jwks_uri_override_used(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.setenv("OIDC_ISSUER", "http://x")
        monkeypatch.setenv("OIDC_AUDIENCE_BRIDGE", "y")
        monkeypatch.setenv("OIDC_JWKS_URI", "http://x/custom-jwks")
        v = _build_oidc_validator_from_env()
        assert v is not None
        assert v.config.jwks_uri == "http://x/custom-jwks"


# ---------------------------------------------------------------------------
# Handler._auth_ok with OIDC enabled


class _StubValidator:
    """Drop-in OIDCValidator stub for testing the dual-path auth logic."""

    def __init__(self, *, accept: bool, user_id: str = "stub-user", error: str | None = None):
        self._accept = accept
        self._user_id = user_id
        self._error = error

    def validate(self, header):
        class _Result:
            pass

        r = _Result()
        r.ok = self._accept
        r.user_id = self._user_id
        r.email = None
        r.roles = ()
        r.error = self._error
        return r


class TestAuthOkWithOIDC:
    """OIDC-enabled daemon — verify the dual-path auth logic."""

    def _url(self, daemon, path):
        return f"http://{daemon.host}:{daemon._server.server_address[1]}{path}"

    def test_oidc_accepted_token_authorizes(self, running_daemon):
        daemon, _ = running_daemon
        daemon.oidc_validator = _StubValidator(accept=True)
        status = _notify(self._url(daemon, "/notify"), token="any-token-shape")
        assert status == 202

    def test_oidc_rejected_token_falls_back_to_loopback_bearer(self, running_daemon):
        """OIDC stub rejects → handler tries loopback bearer next.

        Passing the daemon's real loopback token should still authorize.
        This is the same-host CLI introspection use case.
        """
        daemon, _ = running_daemon
        daemon.oidc_validator = _StubValidator(accept=False, error="bad token")
        status = _notify(self._url(daemon, "/notify"), token=daemon.token)
        assert status == 202

    def test_oidc_rejected_and_wrong_bearer_returns_401(self, running_daemon):
        daemon, _ = running_daemon
        daemon.oidc_validator = _StubValidator(accept=False, error="bad token")
        status = _notify(self._url(daemon, "/notify"), token="not-the-loopback-token")
        assert status == 401

    def test_no_oidc_configured_still_works(self, running_daemon):
        """Regression guard: with daemon.oidc_validator None, loopback bearer is the only path."""
        daemon, _ = running_daemon
        assert daemon.oidc_validator is None  # default — env not set in tests
        status = _notify(self._url(daemon, "/notify"), token=daemon.token)
        assert status == 202
        status = _notify(self._url(daemon, "/notify"), token="wrong")
        assert status == 401

    def test_oidc_enabled_missing_header_returns_401(self, running_daemon):
        daemon, _ = running_daemon
        daemon.oidc_validator = _StubValidator(accept=False)
        status = _notify(self._url(daemon, "/notify"), token=None)
        assert status == 401

    def test_oidc_enabled_non_bearer_scheme_returns_401(self, running_daemon):
        """Even with OIDC configured, non-Bearer headers should be rejected."""
        daemon, _ = running_daemon
        daemon.oidc_validator = _StubValidator(accept=True)  # would accept any header
        url = self._url(daemon, "/notify")
        payload = json.dumps(
            {
                "account": "test",
                "project": "test-proj",
                "severity": "info",
                "title": "x",
                "body": "x",
            }
        ).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Basic abcdef")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                got = resp.status
        except urllib.error.HTTPError as e:
            got = e.code
        assert got == 401
