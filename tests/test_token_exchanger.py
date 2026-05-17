"""Tests for TokenExchanger -- /oauth/v2/token round-trip helper.

Uses an injected fetcher to avoid real HTTP. The default urllib path is
exercised indirectly by the chunk-C.3 integration test.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from otaman_bridge.web_auth import (
    TokenExchanger,
    TokenExchangeError,
    TokenResponse,
    WebAuthConfig,
)


@pytest.fixture
def config():
    return WebAuthConfig(
        issuer="https://otaman.example/auth",
        client_id="bridge-client-id",
        redirect_uri="https://otaman.example/auth/callback",
    )


def _fetcher_returning(payload):
    """Make a fetcher that returns a fixed JSON-encoded payload."""
    captured = {}
    def f(url, body, timeout):
        captured["url"] = url
        captured["body"] = body
        captured["timeout"] = timeout
        return json.dumps(payload).encode("utf-8")
    return f, captured


def _fetcher_raising(exc):
    def f(url, body, timeout):  # noqa: ARG001
        raise exc
    return f


# ---- TokenResponse parsing --------------------------------------------


class TestTokenResponse:
    def test_from_dict_minimal(self):
        r = TokenResponse.from_dict({"access_token": "abc"})
        assert r.access_token == "abc"
        assert r.id_token is None
        assert r.refresh_token is None
        assert r.expires_in == 0
        assert r.token_type == "Bearer"

    def test_from_dict_full(self):
        r = TokenResponse.from_dict({
            "access_token": "at",
            "id_token": "it",
            "refresh_token": "rt",
            "expires_in": 3600,
            "token_type": "Bearer",
        })
        assert r.access_token == "at"
        assert r.id_token == "it"
        assert r.refresh_token == "rt"
        assert r.expires_in == 3600

    def test_from_dict_missing_access_token_raises(self):
        with pytest.raises(ValueError, match="access_token"):
            TokenResponse.from_dict({"id_token": "it"})


# ---- TokenExchanger.exchange_code --------------------------------------


class TestExchangeCode:
    def test_returns_token_response_on_success(self, config):
        fetcher, captured = _fetcher_returning({
            "access_token": "at-1",
            "id_token": "it-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "token_type": "Bearer",
        })
        x = TokenExchanger(config, fetcher=fetcher)
        r = x.exchange_code("auth-code-xyz", "verifier-abc")
        assert isinstance(r, TokenResponse)
        assert r.access_token == "at-1"
        assert r.id_token == "it-1"
        assert r.refresh_token == "rt-1"
        assert r.expires_in == 3600

    def test_posts_to_correct_endpoint(self, config):
        fetcher, captured = _fetcher_returning({"access_token": "x"})
        TokenExchanger(config, fetcher=fetcher).exchange_code("c", "v")
        assert captured["url"] == "https://otaman.example/auth/oauth/v2/token"

    def test_request_body_has_required_form_params(self, config):
        fetcher, captured = _fetcher_returning({"access_token": "x"})
        TokenExchanger(config, fetcher=fetcher).exchange_code("auth-code", "v-abc")
        params = dict(urllib.parse.parse_qsl(captured["body"].decode("ascii")))
        assert params["grant_type"] == "authorization_code"
        assert params["code"] == "auth-code"
        assert params["code_verifier"] == "v-abc"
        assert params["client_id"] == "bridge-client-id"
        assert params["redirect_uri"] == "https://otaman.example/auth/callback"

    def test_oauth_error_response_raises(self, config):
        fetcher, _ = _fetcher_returning({
            "error": "invalid_grant",
            "error_description": "code expired",
        })
        x = TokenExchanger(config, fetcher=fetcher)
        with pytest.raises(TokenExchangeError, match="invalid_grant"):
            x.exchange_code("c", "v")

    def test_missing_access_token_raises(self, config):
        fetcher, _ = _fetcher_returning({"id_token": "it-only"})
        x = TokenExchanger(config, fetcher=fetcher)
        with pytest.raises(TokenExchangeError, match="access_token"):
            x.exchange_code("c", "v")

    def test_malformed_json_raises(self, config):
        def bad_fetcher(url, body, timeout):  # noqa: ARG001
            return b"not-json-at-all"
        x = TokenExchanger(config, fetcher=bad_fetcher)
        with pytest.raises(TokenExchangeError, match="not JSON"):
            x.exchange_code("c", "v")

    def test_network_error_wrapped_as_exchange_error(self, config):
        fetcher = _fetcher_raising(ConnectionRefusedError("nope"))
        x = TokenExchanger(config, fetcher=fetcher)
        with pytest.raises(TokenExchangeError, match="failed"):
            x.exchange_code("c", "v")

    def test_existing_TokenExchangeError_propagates_unwrapped(self, config):
        fetcher = _fetcher_raising(TokenExchangeError("HTTP 401 from token endpoint"))
        x = TokenExchanger(config, fetcher=fetcher)
        with pytest.raises(TokenExchangeError, match="401"):
            x.exchange_code("c", "v")
