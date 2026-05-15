"""Tests for _build_web_login_flow_from_env -- env-var builder for the
web login flow on the bridge daemon.

Mirrors the shape of TestBuildOIDCValidatorFromEnv -- pure env parsing,
no HTTP, no Zitadel.
"""

from __future__ import annotations

import pytest

from otaman_bridge.daemon import _build_web_login_flow_from_env


class TestBuildWebLoginFlowFromEnv:
    def test_returns_none_when_auth_mode_unset(self, monkeypatch):
        monkeypatch.delenv("OTAMAN_AUTH_MODE", raising=False)
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.delenv("OIDC_AUDIENCE_BRIDGE", raising=False)
        monkeypatch.delenv("OIDC_BRIDGE_REDIRECT_URI", raising=False)
        assert _build_web_login_flow_from_env() is None

    def test_returns_none_when_auth_mode_is_loopback(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "loopback-bearer")
        monkeypatch.setenv("OIDC_ISSUER", "https://otaman.example/auth")
        monkeypatch.setenv("OIDC_AUDIENCE_BRIDGE", "bridge-client-id")
        monkeypatch.setenv("OIDC_BRIDGE_REDIRECT_URI", "https://otaman.example/auth/callback")
        assert _build_web_login_flow_from_env() is None

    def test_returns_none_when_issuer_missing(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.setenv("OIDC_AUDIENCE_BRIDGE", "bridge-client-id")
        monkeypatch.setenv("OIDC_BRIDGE_REDIRECT_URI", "https://otaman.example/auth/callback")
        assert _build_web_login_flow_from_env() is None

    def test_returns_none_when_audience_missing(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.setenv("OIDC_ISSUER", "https://otaman.example/auth")
        monkeypatch.delenv("OIDC_AUDIENCE_BRIDGE", raising=False)
        monkeypatch.setenv("OIDC_BRIDGE_REDIRECT_URI", "https://otaman.example/auth/callback")
        assert _build_web_login_flow_from_env() is None

    def test_returns_none_when_redirect_uri_missing(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.setenv("OIDC_ISSUER", "https://otaman.example/auth")
        monkeypatch.setenv("OIDC_AUDIENCE_BRIDGE", "bridge-client-id")
        monkeypatch.delenv("OIDC_BRIDGE_REDIRECT_URI", raising=False)
        assert _build_web_login_flow_from_env() is None

    def test_returns_pair_when_fully_configured(self, monkeypatch):
        from otaman_bridge.web_auth import LoginFlow, PendingLoginStore
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.setenv("OIDC_ISSUER", "https://otaman.example/auth")
        monkeypatch.setenv("OIDC_AUDIENCE_BRIDGE", "bridge-client-id")
        monkeypatch.setenv("OIDC_BRIDGE_REDIRECT_URI", "https://otaman.example/auth/callback")
        monkeypatch.delenv("OIDC_PROJECT_ID", raising=False)
        result = _build_web_login_flow_from_env()
        assert result is not None
        flow, store = result
        assert isinstance(flow, LoginFlow)
        assert isinstance(store, PendingLoginStore)
        assert flow.config.issuer == "https://otaman.example/auth"
        assert flow.config.client_id == "bridge-client-id"
        assert flow.config.redirect_uri == "https://otaman.example/auth/callback"
        assert flow.config.project_id is None
        # Same store as flow's so daemon's callback handler can take() the verifier
        assert flow.store is store

    def test_project_id_propagated(self, monkeypatch):
        monkeypatch.setenv("OTAMAN_AUTH_MODE", "oidc")
        monkeypatch.setenv("OIDC_ISSUER", "https://otaman.example/auth")
        monkeypatch.setenv("OIDC_AUDIENCE_BRIDGE", "bridge-client-id")
        monkeypatch.setenv("OIDC_BRIDGE_REDIRECT_URI", "https://otaman.example/auth/callback")
        monkeypatch.setenv("OIDC_PROJECT_ID", "proj-99")
        result = _build_web_login_flow_from_env()
        assert result is not None
        flow, _ = result
        assert flow.config.project_id == "proj-99"
        # And the project-aud scope is in effective_scopes
        assert any("proj-99:aud" in s for s in flow.config.effective_scopes())
