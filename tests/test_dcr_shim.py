"""Unit tests for src/otaman_bridge/dcr_shim.py.

Pure-function level: IdpConfig env loading, MetadataCache behavior,
fetch_upstream_metadata (mocked URL fetcher), overlay_metadata.

Integration via the live daemon (route wiring + 404 when shim off, 200
when on) lives in test_dcr_shim_overlay_route.py.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import patch

import pytest

from otaman_bridge_ee.dcr_shim import (
    IdpConfig,
    MetadataCache,
    MetadataFetchError,
    derive_registration_endpoint,
    fetch_upstream_metadata,
    overlay_metadata,
)


# ---- IdpConfig.from_env --------------------------------------------------


class TestIdpConfigFromEnv:
    def test_disabled_when_flag_missing(self):
        assert IdpConfig.from_env(env={}) is None

    def test_disabled_when_flag_false(self):
        assert IdpConfig.from_env(env={"OTAMAN_DCR_SHIM": "false"}) is None
        assert IdpConfig.from_env(env={"OTAMAN_DCR_SHIM": "0"}) is None
        assert IdpConfig.from_env(env={"OTAMAN_DCR_SHIM": "no"}) is None

    def test_enabled_with_truthy_values(self):
        for val in ("1", "true", "TRUE", "yes"):
            cfg = IdpConfig.from_env(env={
                "OTAMAN_DCR_SHIM": val,
                "OIDC_ISSUER": "http://idp.example",
            })
            assert cfg is not None, f"truthy={val} should enable"
            assert cfg.dcr_shim is True

    def test_disabled_when_no_mgmt_base_or_issuer(self):
        """Without somewhere to call the mgmt API, the shim is useless."""
        assert IdpConfig.from_env(env={"OTAMAN_DCR_SHIM": "1"}) is None

    def test_management_base_url_falls_back_to_issuer(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://idp.example:8080",
        })
        assert cfg is not None
        assert cfg.management_base_url == "http://idp.example:8080"

    def test_management_base_url_explicit_wins(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://idp.example",
            "OTAMAN_DCR_SHIM_MGMT_BASE": "http://mgmt.example",
        })
        assert cfg.management_base_url == "http://mgmt.example"

    def test_management_base_url_strips_trailing_slash(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OTAMAN_DCR_SHIM_MGMT_BASE": "http://mgmt.example/",
        })
        assert cfg.management_base_url == "http://mgmt.example"

    def test_default_trust_is_protected(self):
        """F185: safe-by-default when nothing configures trust anywhere."""
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
        })
        assert cfg.registration_trust == "protected"

    def test_trust_open_via_env_backcompat(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
            "OTAMAN_DCR_SHIM_TRUST": "open",
        })
        assert cfg.registration_trust == "open"

    def test_trust_protected_via_env(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
            "OTAMAN_DCR_SHIM_TRUST": "protected",
        })
        assert cfg.registration_trust == "protected"

    def test_invalid_trust_falls_back_to_protected(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
            "OTAMAN_DCR_SHIM_TRUST": "weird-value",
        })
        assert cfg.registration_trust == "protected"

    def test_default_type_is_zitadel(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
        })
        assert cfg.type == "zitadel"

    def test_cache_seconds_default_and_override(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
        })
        assert cfg.metadata_cache_seconds == 300

        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
            "OTAMAN_DCR_SHIM_CACHE_SECS": "30",
        })
        assert cfg.metadata_cache_seconds == 30

    def test_cache_seconds_invalid_falls_back_to_default(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
            "OTAMAN_DCR_SHIM_CACHE_SECS": "not-a-number",
        })
        assert cfg.metadata_cache_seconds == 300

    def test_pat_loaded_from_env(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
            "OTAMAN_DCR_SHIM_PAT": "MY-PAT-TOKEN",
        })
        assert cfg.mgmt_pat == "MY-PAT-TOKEN"

    def test_pat_defaults_to_empty(self):
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
        })
        assert cfg.mgmt_pat == ""

    def test_pat_and_client_credentials_both_loaded(self):
        """Both auth modes can be set in env simultaneously; the runtime
        chooses (PAT wins). This is what bootstrap emits today."""
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
            "OTAMAN_DCR_SHIM_PAT": "P",
            "OTAMAN_DCR_SHIM_CLIENT_ID": "C",
            "OTAMAN_DCR_SHIM_SECRET": "S",
        })
        assert cfg.mgmt_pat == "P"
        assert cfg.machine_user_client_id == "C"
        assert cfg.machine_user_client_secret == "S"

    def test_cache_seconds_minimum_is_1(self):
        """Zero or negative would mean 'never cache'; clamp to 1 so the
        cache always returns something coherent."""
        cfg = IdpConfig.from_env(env={
            "OTAMAN_DCR_SHIM": "1",
            "OIDC_ISSUER": "http://i",
            "OTAMAN_DCR_SHIM_CACHE_SECS": "0",
        })
        assert cfg.metadata_cache_seconds == 1


# ---- F185: platform.yaml terminal.dcr_shim_trust precedence -------------


class TestTrustPrecedence:
    """platform.yaml's terminal.dcr_shim_trust > OTAMAN_DCR_SHIM_TRUST env
    var > "protected" default. Invalid values from either source also
    fall back to "protected"."""

    def _write_platform_yaml(self, tmp_path, terminal_block: str) -> None:
        (tmp_path / "platform.yaml").write_text(terminal_block, encoding="utf-8")

    def test_platform_yaml_wins_over_env(self, tmp_path):
        self._write_platform_yaml(
            tmp_path, "terminal:\n  dcr_shim_trust: open\n",
        )
        cfg = IdpConfig.from_env(
            env={
                "OTAMAN_DCR_SHIM": "1",
                "OIDC_ISSUER": "http://i",
                "OTAMAN_DCR_SHIM_TRUST": "protected",
            },
            project_root=tmp_path,
        )
        assert cfg.registration_trust == "open"

    def test_platform_yaml_absent_falls_to_env(self, tmp_path):
        # No platform.yaml written at all.
        cfg = IdpConfig.from_env(
            env={
                "OTAMAN_DCR_SHIM": "1",
                "OIDC_ISSUER": "http://i",
                "OTAMAN_DCR_SHIM_TRUST": "open",
            },
            project_root=tmp_path,
        )
        assert cfg.registration_trust == "open"

    def test_platform_yaml_present_but_no_dcr_shim_trust_key_falls_to_env(
        self, tmp_path,
    ):
        self._write_platform_yaml(tmp_path, "terminal:\n  other_key: 1\n")
        cfg = IdpConfig.from_env(
            env={
                "OTAMAN_DCR_SHIM": "1",
                "OIDC_ISSUER": "http://i",
                "OTAMAN_DCR_SHIM_TRUST": "open",
            },
            project_root=tmp_path,
        )
        assert cfg.registration_trust == "open"

    def test_no_project_root_no_env_defaults_to_protected(self, tmp_path):
        cfg = IdpConfig.from_env(
            env={"OTAMAN_DCR_SHIM": "1", "OIDC_ISSUER": "http://i"},
            project_root=None,
        )
        assert cfg.registration_trust == "protected"

    def test_invalid_platform_yaml_value_falls_back_to_protected(self, tmp_path):
        self._write_platform_yaml(
            tmp_path, "terminal:\n  dcr_shim_trust: nonsense\n",
        )
        cfg = IdpConfig.from_env(
            env={"OTAMAN_DCR_SHIM": "1", "OIDC_ISSUER": "http://i"},
            project_root=tmp_path,
        )
        assert cfg.registration_trust == "protected"

    def test_malformed_platform_yaml_does_not_raise_falls_to_env(self, tmp_path):
        self._write_platform_yaml(tmp_path, ": not: valid: yaml: [[[")
        cfg = IdpConfig.from_env(
            env={
                "OTAMAN_DCR_SHIM": "1",
                "OIDC_ISSUER": "http://i",
                "OTAMAN_DCR_SHIM_TRUST": "open",
            },
            project_root=tmp_path,
        )
        assert cfg.registration_trust == "open"

    def test_terminal_block_not_a_dict_is_ignored(self, tmp_path):
        self._write_platform_yaml(tmp_path, "terminal: not-a-dict\n")
        cfg = IdpConfig.from_env(
            env={"OTAMAN_DCR_SHIM": "1", "OIDC_ISSUER": "http://i"},
            project_root=tmp_path,
        )
        assert cfg.registration_trust == "protected"


# ---- MetadataCache -------------------------------------------------------


class TestMetadataCache:
    def test_initial_get_returns_none(self):
        c = MetadataCache(ttl_seconds=60)
        assert c.get() is None

    def test_put_then_get_returns_doc(self):
        c = MetadataCache(ttl_seconds=60)
        c.put({"k": "v"})
        assert c.get() == {"k": "v"}

    def test_get_after_ttl_returns_none(self):
        c = MetadataCache(ttl_seconds=10)
        c.put({"k": "v"}, now=1000.0)
        # before expiry
        assert c.get(now=1005.0) == {"k": "v"}
        # at expiry boundary
        assert c.get(now=1010.0) is None
        # past expiry
        assert c.get(now=1100.0) is None

    def test_invalidate(self):
        c = MetadataCache(ttl_seconds=60)
        c.put({"k": "v"})
        c.invalidate()
        assert c.get() is None

    def test_put_replaces_previous_entry(self):
        c = MetadataCache(ttl_seconds=60)
        c.put({"k": "v1"})
        c.put({"k": "v2"})
        assert c.get() == {"k": "v2"}


# ---- fetch_upstream_metadata --------------------------------------------


def _fake_response(*, status: int = 200, body: bytes = b"{}"):
    """Build a fake urlopen result with the minimal duck type."""
    class _Resp:
        def __init__(self):
            self.status = status
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return _Resp()


class _FakeOpener:
    def __init__(self, response_fn):
        self._response_fn = response_fn
    def open(self, req, timeout=None):
        return self._response_fn(req)


class TestFetchUpstreamMetadata:
    def test_happy_path(self):
        doc = {"issuer": "http://idp", "authorization_endpoint": "http://idp/authz"}
        opener = _FakeOpener(lambda req: _fake_response(body=json.dumps(doc).encode()))
        out = fetch_upstream_metadata("http://idp", opener=opener)
        assert out == doc

    def test_fetches_from_openid_configuration_path(self):
        seen_urls = []
        def _resp(req):
            seen_urls.append(req.full_url)
            return _fake_response(body=b'{"k":"v"}')
        opener = _FakeOpener(_resp)
        fetch_upstream_metadata("http://idp", opener=opener)
        assert seen_urls == ["http://idp/.well-known/openid-configuration"]

    def test_strips_trailing_slash_on_base_url(self):
        seen_urls = []
        def _resp(req):
            seen_urls.append(req.full_url)
            return _fake_response(body=b'{}')
        opener = _FakeOpener(_resp)
        fetch_upstream_metadata("http://idp/", opener=opener)
        assert seen_urls == ["http://idp/.well-known/openid-configuration"]

    def test_non_200_raises(self):
        opener = _FakeOpener(lambda req: _fake_response(status=404, body=b"not found"))
        with pytest.raises(MetadataFetchError, match="HTTP 404"):
            fetch_upstream_metadata("http://idp", opener=opener)

    def test_malformed_json_raises(self):
        opener = _FakeOpener(lambda req: _fake_response(body=b"not json"))
        with pytest.raises(MetadataFetchError, match="malformed JSON"):
            fetch_upstream_metadata("http://idp", opener=opener)

    def test_non_object_json_raises(self):
        """RFC 8414 says metadata is a JSON object; arrays / scalars must error."""
        opener = _FakeOpener(lambda req: _fake_response(body=b'["a","b"]'))
        with pytest.raises(MetadataFetchError, match="non-object"):
            fetch_upstream_metadata("http://idp", opener=opener)

    def test_url_error_raises(self):
        def _raise(req):
            raise urllib.error.URLError("connection refused")
        opener = _FakeOpener(_raise)
        with pytest.raises(MetadataFetchError, match="unreachable"):
            fetch_upstream_metadata("http://idp", opener=opener)


# ---- overlay_metadata ---------------------------------------------------


class TestOverlayMetadata:
    def test_injects_registration_endpoint(self):
        doc = {"issuer": "http://i"}
        out = overlay_metadata(doc, registration_endpoint="http://bridge/oauth/register")
        assert out["registration_endpoint"] == "http://bridge/oauth/register"

    def test_injects_auth_methods(self):
        out = overlay_metadata({}, registration_endpoint="http://x")
        assert out["registration_endpoint_auth_methods_supported"] == ["none"]

    def test_constrains_token_endpoint_auth_methods_to_none(self):
        """The shim only emits public PKCE clients. Even though Zitadel
        advertises client_secret_basic and friends, we must constrain
        the AS metadata to ["none"] so MCP clients don't try basic auth
        and get rejected at token exchange (regression from D7 probe)."""
        doc = {
            "token_endpoint_auth_methods_supported": [
                "none", "client_secret_basic", "client_secret_post", "private_key_jwt",
            ],
        }
        out = overlay_metadata(doc, registration_endpoint="http://x")
        assert out["token_endpoint_auth_methods_supported"] == ["none"]

    def test_constrains_pkce_methods_to_s256(self):
        """Public clients require PKCE; S256 is the only RFC-7636-compliant
        method. Some IdPs also advertise 'plain' which is deprecated."""
        doc = {"code_challenge_methods_supported": ["plain", "S256"]}
        out = overlay_metadata(doc, registration_endpoint="http://x")
        assert out["code_challenge_methods_supported"] == ["S256"]

    def test_preserves_upstream_fields(self):
        doc = {
            "issuer": "http://i",
            "authorization_endpoint": "http://i/authz",
            "token_endpoint": "http://i/token",
            "scopes_supported": ["openid"],
        }
        out = overlay_metadata(doc, registration_endpoint="http://r")
        for k, v in doc.items():
            assert out[k] == v

    def test_does_not_mutate_input(self):
        doc = {
            "issuer": "http://i",
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        }
        original = dict(doc)
        _ = overlay_metadata(doc, registration_endpoint="http://r")
        assert doc == original
        assert "registration_endpoint" not in doc


# ---- derive_registration_endpoint --------------------------------------


class TestDeriveRegistrationEndpoint:
    def test_appends_path(self):
        assert derive_registration_endpoint(bridge_public_url="http://b:8090") \
            == "http://b:8090/oauth/register"

    def test_strips_trailing_slash(self):
        assert derive_registration_endpoint(bridge_public_url="http://b:8090/") \
            == "http://b:8090/oauth/register"
