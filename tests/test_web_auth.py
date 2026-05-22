"""Tests for otaman_bridge_ee.web_auth -- PKCE login-flow helper.

Pure data-structure + URL-construction tests. No HTTP, no Zitadel.
"""

from __future__ import annotations

import base64
import hashlib
import urllib.parse

import pytest

from otaman_bridge_ee.web_auth import (
    DEFAULT_PENDING_TTL,
    DEFAULT_SCOPES,
    LoginFlow,
    PendingLoginStore,
    StartedLogin,
    WebAuthConfig,
)


@pytest.fixture
def config():
    return WebAuthConfig(
        issuer="https://otaman.example/auth",
        client_id="bridge-client-id",
        redirect_uri="https://otaman.example/auth/callback",
    )


# ---- WebAuthConfig -----------------------------------------------------


class TestWebAuthConfig:
    def test_authorize_endpoint(self, config):
        assert config.authorize_endpoint() == "https://otaman.example/auth/oauth/v2/authorize"

    def test_token_endpoint(self, config):
        assert config.token_endpoint() == "https://otaman.example/auth/oauth/v2/token"

    def test_strips_trailing_slash_on_issuer(self):
        cfg = WebAuthConfig(
            issuer="https://otaman.example/auth/",
            client_id="x", redirect_uri="https://x/cb",
        )
        assert cfg.authorize_endpoint() == "https://otaman.example/auth/oauth/v2/authorize"

    def test_default_scopes_include_zitadel_roles(self):
        assert "openid" in DEFAULT_SCOPES
        assert "profile" in DEFAULT_SCOPES
        assert "email" in DEFAULT_SCOPES
        assert "urn:zitadel:iam:org:projects:roles" in DEFAULT_SCOPES

    def test_project_id_adds_aud_scope(self):
        cfg = WebAuthConfig(
            issuer="https://x", client_id="c", redirect_uri="https://x/cb",
            project_id="proj-123",
        )
        scopes = cfg.effective_scopes()
        assert "urn:zitadel:iam:org:project:id:proj-123:aud" in scopes

    def test_no_project_id_uses_default_scopes_unchanged(self, config):
        assert config.effective_scopes() == DEFAULT_SCOPES


# ---- PendingLoginStore -------------------------------------------------


class TestPendingLoginStore:
    def test_put_then_take_returns_verifier(self):
        store = PendingLoginStore()
        store.put("state-1", "verifier-1")
        assert store.take("state-1") == "verifier-1"

    def test_take_removes_entry_so_replay_returns_none(self):
        store = PendingLoginStore()
        store.put("state-1", "verifier-1")
        assert store.take("state-1") == "verifier-1"
        # second take is a replay attempt -- must NOT return the verifier
        assert store.take("state-1") is None

    def test_take_unknown_state_returns_none(self):
        store = PendingLoginStore()
        assert store.take("never-existed") is None
        assert store.take("") is None
        assert store.take(None) is None

    def test_take_expired_state_returns_none(self):
        clock = [1000.0]
        store = PendingLoginStore(ttl=60.0, clock=lambda: clock[0])
        store.put("state-1", "verifier-1")
        clock[0] += 70
        assert store.take("state-1") is None
        # And the entry is also gone (take pops even on expiry)
        assert len(store) == 0

    def test_purge_expired_drops_only_expired(self):
        clock = [1000.0]
        store = PendingLoginStore(ttl=60.0, clock=lambda: clock[0])
        store.put("old", "v-old")
        clock[0] += 70
        store.put("fresh", "v-fresh")
        n = store.purge_expired()
        assert n == 1
        assert store.take("old") is None
        assert store.take("fresh") == "v-fresh"

    def test_default_ttl_is_10_minutes(self):
        assert DEFAULT_PENDING_TTL == 600.0


# ---- LoginFlow.start() and PKCE generation -----------------------------


class TestLoginFlowStart:
    def test_returns_started_login_with_authorize_url(self, config):
        flow = LoginFlow(config, PendingLoginStore())
        result = flow.start()
        assert isinstance(result, StartedLogin)
        assert result.authorize_url.startswith("https://otaman.example/auth/oauth/v2/authorize?")

    def test_authorize_url_carries_required_params(self, config):
        flow = LoginFlow(config, PendingLoginStore())
        result = flow.start()
        parsed = urllib.parse.urlparse(result.authorize_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        assert params["client_id"] == "bridge-client-id"
        assert params["redirect_uri"] == "https://otaman.example/auth/callback"
        assert params["response_type"] == "code"
        assert params["state"] == result.state
        assert params["code_challenge_method"] == "S256"
        assert "code_challenge" in params
        assert "openid" in params["scope"]

    def test_state_is_unguessable(self, config):
        flow = LoginFlow(config, PendingLoginStore())
        a = flow.start()
        b = flow.start()
        assert a.state != b.state
        assert len(a.state) >= 40

    def test_verifier_is_unguessable_and_satisfies_rfc7636_length(self, config):
        flow = LoginFlow(config, PendingLoginStore())
        result = flow.start()
        # RFC 7636 sec. 4.1: 43-128 chars
        assert 43 <= len(result.code_verifier) <= 128

    def test_code_challenge_is_s256_of_verifier(self, config):
        flow = LoginFlow(config, PendingLoginStore())
        result = flow.start()
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(result.authorize_url).query))
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(result.code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert params["code_challenge"] == expected

    def test_state_is_registered_in_pending_store(self, config):
        store = PendingLoginStore()
        flow = LoginFlow(config, store)
        result = flow.start()
        # store still has it (callback hasn't run yet)
        assert len(store) == 1
        # take returns the same verifier
        assert store.take(result.state) == result.code_verifier

    def test_two_flows_register_two_entries(self, config):
        store = PendingLoginStore()
        flow = LoginFlow(config, store)
        a = flow.start()
        b = flow.start()
        assert len(store) == 2
        assert store.take(a.state) == a.code_verifier
        assert store.take(b.state) == b.code_verifier

    def test_project_id_extends_scope_in_url(self):
        cfg = WebAuthConfig(
            issuer="https://x", client_id="c", redirect_uri="https://x/cb",
            project_id="proj-42",
        )
        flow = LoginFlow(cfg, PendingLoginStore())
        result = flow.start()
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(result.authorize_url).query))
        assert "urn:zitadel:iam:org:project:id:proj-42:aud" in params["scope"]
