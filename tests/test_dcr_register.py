"""Unit + integration tests for the /oauth/register handler (chunk D4).

Three layers:
- Pure functions: parse_register_request, compute_fingerprint,
  build_zitadel_oidc_payload, find_or_create_client (with stubbed mgmt),
  to_rfc7591_response.
- ZitadelMgmtClient: client_credentials token grant + find + create, all
  driven against a fake urllib opener.
- Live daemon: POST /oauth/register with the daemon's mgmt-client
  monkey-patched onto a stub.
"""

from __future__ import annotations

import json
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon, read_endpoint_file
from otaman_bridge.dcr_shim import (
    ALLOWED_GRANT_TYPES,
    ALLOWED_RESPONSE_TYPES,
    DCRError,
    IdpConfig,
    MetadataCache,
    RegisterRequest,
    ZitadelMgmtClient,
    ZitadelMgmtError,
    build_zitadel_oidc_payload,
    compute_fingerprint,
    find_or_create_client,
    parse_register_request,
    to_rfc7591_response,
)
from otaman_bridge.transports.null import NullTransport


# ---- parse_register_request ----------------------------------------------


class TestParseRegisterRequest:
    def _ok(self, **overrides):
        body = {"redirect_uris": ["http://localhost:54321/cb"]}
        body.update(overrides)
        return parse_register_request(body)

    def test_minimal_happy_path(self):
        req = self._ok()
        assert req.redirect_uris == ("http://localhost:54321/cb",)
        assert req.token_endpoint_auth_method == "none"
        assert req.grant_types == ("authorization_code",)
        assert req.response_types == ("code",)

    def test_full_happy_path(self):
        req = self._ok(
            client_name="Claude Code",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="openid profile email",
            software_id="claude-code",
            software_version="2.1.143",
        )
        assert req.client_name == "Claude Code"
        assert req.grant_types == ("authorization_code", "refresh_token")
        assert req.scope == "openid profile email"
        assert req.software_id == "claude-code"

    def test_body_must_be_object(self):
        with pytest.raises(DCRError) as e:
            parse_register_request(["not", "an", "object"])
        assert e.value.error == "invalid_client_metadata"

    def test_missing_redirect_uris(self):
        with pytest.raises(DCRError) as e:
            parse_register_request({})
        assert e.value.error == "invalid_redirect_uri"

    def test_empty_redirect_uris(self):
        with pytest.raises(DCRError) as e:
            parse_register_request({"redirect_uris": []})
        assert e.value.error == "invalid_redirect_uri"

    @pytest.mark.parametrize("uri", [
        "https://example.com/cb",
        "http://example.com/cb",
        "http://10.0.0.1:54321/cb",
        "custom-scheme://cb",
        "http://localhost.evil.com/cb",
    ])
    def test_non_loopback_redirect_rejected(self, uri):
        with pytest.raises(DCRError) as e:
            parse_register_request({"redirect_uris": [uri]})
        assert e.value.error == "invalid_redirect_uri"

    @pytest.mark.parametrize("uri", [
        "http://localhost/cb",
        "http://localhost:0/cb",
        "http://localhost:54321/oauth/callback",
        "http://127.0.0.1:54321/cb",
        "http://127.0.0.1/cb",
    ])
    def test_loopback_redirect_accepted(self, uri):
        req = parse_register_request({"redirect_uris": [uri]})
        assert uri in req.redirect_uris

    def test_token_endpoint_auth_method_must_be_none(self):
        with pytest.raises(DCRError) as e:
            parse_register_request({
                "redirect_uris": ["http://localhost:1/cb"],
                "token_endpoint_auth_method": "client_secret_basic",
            })
        assert e.value.error == "invalid_client_metadata"

    def test_unsupported_grant_type_rejected(self):
        with pytest.raises(DCRError) as e:
            parse_register_request({
                "redirect_uris": ["http://localhost:1/cb"],
                "grant_types": ["password"],
            })
        assert e.value.error == "invalid_client_metadata"
        assert "password" in e.value.description

    def test_unsupported_response_type_rejected(self):
        with pytest.raises(DCRError) as e:
            parse_register_request({
                "redirect_uris": ["http://localhost:1/cb"],
                "response_types": ["token"],
            })
        assert e.value.error == "invalid_client_metadata"

    def test_non_string_field_rejected(self):
        with pytest.raises(DCRError) as e:
            parse_register_request({
                "redirect_uris": ["http://localhost:1/cb"],
                "client_name": 42,
            })
        assert e.value.error == "invalid_client_metadata"


# ---- compute_fingerprint -------------------------------------------------


class TestComputeFingerprint:
    def test_deterministic(self):
        f1 = compute_fingerprint(
            software_id="claude-code",
            redirect_uris=("http://localhost:1/cb",),
        )
        f2 = compute_fingerprint(
            software_id="claude-code",
            redirect_uris=("http://localhost:1/cb",),
        )
        assert f1 == f2
        assert len(f1) == 16

    def test_order_independent_redirect_uris(self):
        f1 = compute_fingerprint(
            software_id="x",
            redirect_uris=("http://localhost:1/a", "http://localhost:2/b"),
        )
        f2 = compute_fingerprint(
            software_id="x",
            redirect_uris=("http://localhost:2/b", "http://localhost:1/a"),
        )
        assert f1 == f2

    def test_distinct_inputs_different_fingerprints(self):
        f1 = compute_fingerprint(software_id="A", redirect_uris=("http://x",))
        f2 = compute_fingerprint(software_id="B", redirect_uris=("http://x",))
        f3 = compute_fingerprint(software_id="A", redirect_uris=("http://y",))
        assert f1 != f2
        assert f1 != f3
        assert f2 != f3

    def test_no_software_id_uses_empty_string(self):
        f = compute_fingerprint(software_id=None, redirect_uris=("http://x",))
        assert len(f) == 16


# ---- build_zitadel_oidc_payload -----------------------------------------


class TestBuildZitadelOidcPayload:
    def test_grant_translation(self):
        p = build_zitadel_oidc_payload(
            name="dcr-shim:abc",
            redirect_uris=["http://localhost:1/cb"],
            grant_types=("authorization_code", "refresh_token"),
        )
        assert p["grantTypes"] == [
            "OIDC_GRANT_TYPE_AUTHORIZATION_CODE",
            "OIDC_GRANT_TYPE_REFRESH_TOKEN",
        ]

    def test_grant_translation_authcode_only(self):
        p = build_zitadel_oidc_payload(
            name="x",
            redirect_uris=["http://localhost:1/cb"],
            grant_types=("authorization_code",),
        )
        assert p["grantTypes"] == ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]

    def test_always_native_public_pkce(self):
        p = build_zitadel_oidc_payload(
            name="x", redirect_uris=["http://localhost:1/cb"],
            grant_types=("authorization_code",),
        )
        assert p["appType"] == "OIDC_APP_TYPE_NATIVE"
        assert p["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_NONE"
        assert p["devMode"] is False

    def test_emits_jwt_tokens_with_roles(self):
        """The bridge's OIDC validator validates JWTs locally; opaque
        bearer tokens (OIDC_TOKEN_TYPE_BEARER) would require introspection
        calls back to Zitadel and break ctx.user_id extraction. Project
        roles must land in the access token (accessTokenRoleAssertion=true)
        so the bridge can use them for authorization decisions."""
        p = build_zitadel_oidc_payload(
            name="x", redirect_uris=["http://localhost:1/cb"],
            grant_types=("authorization_code",),
        )
        assert p["accessTokenType"] == "OIDC_TOKEN_TYPE_JWT"
        assert p["accessTokenRoleAssertion"] is True
        assert p["idTokenRoleAssertion"] is True

    def test_redirect_uris_passed_through(self):
        uris = ["http://localhost:1/a", "http://localhost:2/b"]
        p = build_zitadel_oidc_payload(
            name="x", redirect_uris=uris,
            grant_types=("authorization_code",),
        )
        assert p["redirectUris"] == uris


# ---- find_or_create_client (with stub mgmt client) ----------------------


class _StubMgmtClient:
    """Stand-in for ZitadelMgmtClient. Records calls and returns canned data."""
    def __init__(self, *, find_returns=None, create_returns=None, create_raises=None):
        self.find_returns = find_returns
        self.create_returns = create_returns
        self.create_raises = create_raises
        self.calls = []

    def find_app_by_name(self, *, project_id, name):
        self.calls.append(("find", project_id, name))
        return self.find_returns

    def create_oidc_app(self, *, project_id, payload):
        self.calls.append(("create", project_id, payload["name"]))
        if self.create_raises:
            raise self.create_raises
        return self.create_returns


def _req():
    return RegisterRequest(
        redirect_uris=("http://localhost:54321/cb",),
        software_id="claude-code",
    )


class TestFindOrCreateClient:
    def test_reuses_existing_app(self):
        stub = _StubMgmtClient(find_returns={
            "id": "app-A",
            "oidcConfig": {"clientId": "CLIENT-XYZ"},
        })
        cid = find_or_create_client(
            mgmt_client=stub, project_id="proj", request=_req(),
        )
        assert cid == "CLIENT-XYZ"
        # No create call when reused.
        assert [c[0] for c in stub.calls] == ["find"]

    def test_creates_when_not_found(self):
        stub = _StubMgmtClient(
            find_returns=None,
            create_returns={"appId": "app-A", "clientId": "NEW-CLIENT"},
        )
        cid = find_or_create_client(
            mgmt_client=stub, project_id="proj", request=_req(),
        )
        assert cid == "NEW-CLIENT"
        assert [c[0] for c in stub.calls] == ["find", "create"]

    def test_409_race_falls_back_to_lookup(self):
        """If two laptops with the same fingerprint create concurrently,
        the loser gets 409; we retry the lookup once."""
        # First find: None. Create: raises 409. Second find: hits.
        stub = _StubMgmtClient()
        stub._find_calls = 0
        races_existing = {"id": "app-A", "oidcConfig": {"clientId": "RACED-CLIENT"}}

        def find_side_effect(*, project_id, name):
            stub.calls.append(("find", project_id, name))
            stub._find_calls += 1
            return None if stub._find_calls == 1 else races_existing

        def create_raises(*, project_id, payload):
            stub.calls.append(("create", project_id, payload["name"]))
            raise ZitadelMgmtError("already exists", status=409, body="App name already exists")

        stub.find_app_by_name = find_side_effect
        stub.create_oidc_app = create_raises
        cid = find_or_create_client(
            mgmt_client=stub, project_id="proj", request=_req(),
        )
        assert cid == "RACED-CLIENT"

    def test_create_failure_other_than_race_propagates(self):
        stub = _StubMgmtClient(
            find_returns=None,
            create_raises=ZitadelMgmtError("permission denied", status=403),
        )
        with pytest.raises(ZitadelMgmtError):
            find_or_create_client(
                mgmt_client=stub, project_id="proj", request=_req(),
            )

    def test_fingerprint_determines_app_name(self):
        stub = _StubMgmtClient(find_returns=None,
                               create_returns={"appId": "x", "clientId": "Y"})
        req = _req()
        find_or_create_client(
            mgmt_client=stub, project_id="proj", request=req,
            name_prefix="dcr-shim:",
        )
        # Find + create both target the same fingerprint-derived name.
        names = [c[2] for c in stub.calls if c[0] in ("find", "create")]
        assert len(names) == 2
        assert names[0] == names[1]
        assert names[0].startswith("dcr-shim:")
        assert len(names[0]) == len("dcr-shim:") + 16


# ---- ZitadelMgmtClient (against fake urllib opener) ---------------------


class _FakeResponse:
    def __init__(self, *, status: int, body: bytes):
        self.status = status
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeOpener:
    """Records every request and returns canned responses keyed by URL."""
    def __init__(self, responses: dict):
        self.responses = responses
        self.requests = []
    def open(self, req, timeout=None):
        self.requests.append((req.method, req.full_url, dict(req.headers), req.data))
        body, status = self.responses.get(
            req.full_url, (b'{"error":"not stubbed"}', 404)
        )
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", req.headers, _FakeBody(body))
        return _FakeResponse(status=status, body=body)


class _FakeBody:
    """Minimal stand-in for HTTPError's `fp` attribute (which has .read() + .close())."""
    def __init__(self, b):
        self._b = b
    def read(self):
        return self._b
    def close(self):
        pass


def _mgmt_client_with_responses(responses):
    return ZitadelMgmtClient(
        base_url="http://mgmt.example",
        token_url="http://mgmt.example/oauth/v2/token",
        client_id="svc-id",
        client_secret="svc-secret",
        org_id="org-1",
        expected_host="mgmt.example",
        opener=_FakeOpener(responses),
    )


class TestZitadelMgmtClientPATMode:
    """PAT mode: token-endpoint never called; PAT used directly as Bearer.

    Critical for deployments behind h2c-incapable TLS terminators
    (Cloudflare Tunnel) where the client_credentials JWT path fails
    on Zitadel's mgmt API.
    """

    def test_pat_skips_token_endpoint(self):
        """No token endpoint call when pat is set."""
        c = ZitadelMgmtClient(
            base_url="http://mgmt.example",
            token_url="http://mgmt.example/oauth/v2/token",
            pat="MY-PAT-TOKEN", org_id="org-1",
            opener=_FakeOpener({}),
        )
        tok = c._get_access_token()
        assert tok == "MY-PAT-TOKEN"
        # Zero requests made — PAT used directly without any HTTP call.
        assert c._opener.requests == []

    def test_pat_wins_over_client_credentials(self):
        """When both pat and client_id+secret are set, pat takes precedence."""
        c = ZitadelMgmtClient(
            base_url="http://mgmt.example",
            token_url="http://mgmt.example/oauth/v2/token",
            client_id="svc", client_secret="sec",
            pat="MY-PAT-TOKEN", org_id="org-1",
            opener=_FakeOpener({}),
        )
        assert c._get_access_token() == "MY-PAT-TOKEN"
        assert c._opener.requests == []

    def test_pat_used_for_mgmt_calls(self):
        c = ZitadelMgmtClient(
            base_url="http://mgmt.example",
            token_url="http://mgmt.example/oauth/v2/token",
            pat="MY-PAT-TOKEN", org_id="org-1",
            opener=_FakeOpener({
                "http://mgmt.example/management/v1/projects/p/apps/_search": (
                    json.dumps({"result": []}).encode(), 200,
                ),
            }),
        )
        c.find_app_by_name(project_id="p", name="dcr-shim:x")
        # The mgmt request should carry the PAT as Bearer.
        search_req = next(r for r in c._opener.requests if r[1].endswith("/apps/_search"))
        h = {k.lower(): v for k, v in search_req[2].items()}
        assert h["authorization"] == "Bearer MY-PAT-TOKEN"

    def test_no_auth_configured_raises(self):
        """Neither pat nor client_id+secret → raises on first use."""
        c = ZitadelMgmtClient(
            base_url="http://mgmt.example",
            token_url="http://mgmt.example/oauth/v2/token",
            org_id="org-1",
            opener=_FakeOpener({}),
        )
        with pytest.raises(ZitadelMgmtError, match="no auth configured"):
            c._get_access_token()


class TestZitadelMgmtClientAuth:
    def test_get_access_token_calls_token_endpoint(self):
        opener_responses = {
            "http://mgmt.example/oauth/v2/token": (
                json.dumps({"access_token": "T1", "expires_in": 3600}).encode(),
                200,
            ),
        }
        c = _mgmt_client_with_responses(opener_responses)
        tok = c._get_access_token()
        assert tok == "T1"
        # Inspect the recorded request: POST, form-encoded body, Basic auth.
        opener = c._opener
        assert opener.requests[0][0] == "POST"
        assert opener.requests[0][1] == "http://mgmt.example/oauth/v2/token"
        headers = opener.requests[0][2]
        # urllib lower-cases header names in Request.headers
        h_lower = {k.lower(): v for k, v in headers.items()}
        assert h_lower["content-type"] == "application/x-www-form-urlencoded"
        assert h_lower["authorization"].startswith("Basic ")
        # Body has client_credentials grant + Zitadel-IAM scope.
        body = opener.requests[0][3].decode()
        assert "grant_type=client_credentials" in body
        assert "urn%3Azitadel%3Aiam%3Aorg%3Aproject%3Aid%3Azitadel%3Aaud" in body

    def test_get_access_token_caches(self):
        opener_responses = {
            "http://mgmt.example/oauth/v2/token": (
                json.dumps({"access_token": "T1", "expires_in": 3600}).encode(), 200,
            ),
        }
        c = _mgmt_client_with_responses(opener_responses)
        c._get_access_token(now=1000.0)
        c._get_access_token(now=1100.0)
        c._get_access_token(now=2000.0)
        # Only one token-endpoint call.
        assert sum(1 for r in c._opener.requests if r[1].endswith("/token")) == 1

    def test_get_access_token_refreshes_near_expiry(self):
        responses_by_call = [
            (json.dumps({"access_token": "T1", "expires_in": 60}).encode(), 200),
            (json.dumps({"access_token": "T2", "expires_in": 60}).encode(), 200),
        ]
        call_n = {"i": 0}
        def _opener_open(req, timeout=None):
            i = call_n["i"]; call_n["i"] += 1
            body, status = responses_by_call[min(i, 1)]
            return _FakeResponse(status=status, body=body)
        opener = types.SimpleNamespace(open=_opener_open)
        c = ZitadelMgmtClient(
            base_url="http://mgmt.example",
            token_url="http://mgmt.example/oauth/v2/token",
            client_id="x", client_secret="y", org_id="o",
            expected_host="mgmt.example", opener=opener,
        )
        # First call mints T1 at t=1000 (expires at 1060).
        assert c._get_access_token(now=1000.0) == "T1"
        # At t=1031, leeway is 30 → still valid (1060-30=1030, so 1031 > 1030 → refresh).
        assert c._get_access_token(now=1031.0) == "T2"

    def test_token_endpoint_http_error_raises(self):
        c = _mgmt_client_with_responses({
            "http://mgmt.example/oauth/v2/token": (b'{"error":"invalid_client"}', 401),
        })
        with pytest.raises(ZitadelMgmtError) as e:
            c._get_access_token()
        assert e.value.status == 401


class TestZitadelMgmtClientApi:
    def _client(self, *more_responses):
        responses = {
            "http://mgmt.example/oauth/v2/token": (
                json.dumps({"access_token": "AT", "expires_in": 3600}).encode(), 200,
            ),
        }
        for d in more_responses:
            responses.update(d)
        return _mgmt_client_with_responses(responses)

    def test_find_app_by_name_returns_match(self):
        search_resp = {"result": [{"id": "app-1", "name": "dcr-shim:abc",
                                    "oidcConfig": {"clientId": "CID"}}]}
        c = self._client({
            "http://mgmt.example/management/v1/projects/proj/apps/_search": (
                json.dumps(search_resp).encode(), 200,
            ),
        })
        app = c.find_app_by_name(project_id="proj", name="dcr-shim:abc")
        assert app["id"] == "app-1"

    def test_find_app_by_name_returns_none_when_empty(self):
        c = self._client({
            "http://mgmt.example/management/v1/projects/proj/apps/_search": (
                json.dumps({"result": []}).encode(), 200,
            ),
        })
        assert c.find_app_by_name(project_id="proj", name="anything") is None

    def test_create_oidc_app_sends_correct_headers(self):
        c = self._client({
            "http://mgmt.example/management/v1/projects/proj/apps/oidc": (
                json.dumps({"appId": "a-1", "clientId": "C-NEW"}).encode(), 200,
            ),
        })
        resp = c.create_oidc_app(project_id="proj", payload={"name": "x"})
        assert resp["clientId"] == "C-NEW"
        # Find the recorded POST to /apps/oidc. The token request happens first.
        oidc_requests = [
            r for r in c._opener.requests
            if r[1].endswith("/apps/oidc")
        ]
        assert len(oidc_requests) == 1
        headers = {k.lower(): v for k, v in oidc_requests[0][2].items()}
        assert headers["authorization"] == "Bearer AT"
        assert headers["x-zitadel-orgid"] == "org-1"
        assert headers["host"] == "mgmt.example"

    def test_mgmt_http_error_carries_status_and_body(self):
        c = self._client({
            "http://mgmt.example/management/v1/projects/proj/apps/oidc": (
                json.dumps({"code": 3, "message": "invalid argument"}).encode(), 400,
            ),
        })
        with pytest.raises(ZitadelMgmtError) as e:
            c.create_oidc_app(project_id="proj", payload={})
        assert e.value.status == 400
        assert "invalid argument" in (e.value.body or "")


# ---- to_rfc7591_response -----------------------------------------------


class TestRFC7591Response:
    def test_minimal_shape(self):
        req = RegisterRequest(redirect_uris=("http://localhost:1/cb",))
        out = to_rfc7591_response(request=req, client_id="C-1", now_unix=1700000000)
        assert out["client_id"] == "C-1"
        assert out["client_id_issued_at"] == 1700000000
        assert out["client_secret"] == ""
        assert out["redirect_uris"] == ["http://localhost:1/cb"]
        assert out["grant_types"] == ["authorization_code"]
        assert out["response_types"] == ["code"]
        assert out["token_endpoint_auth_method"] == "none"
        # Optional fields absent when not provided.
        assert "client_name" not in out
        assert "scope" not in out

    def test_includes_optional_fields_when_present(self):
        req = RegisterRequest(
            redirect_uris=("http://localhost:1/cb",),
            client_name="CC",
            scope="openid",
            software_id="claude-code",
            software_version="2.1",
        )
        out = to_rfc7591_response(request=req, client_id="C", now_unix=0)
        assert out["client_name"] == "CC"
        assert out["scope"] == "openid"
        assert out["software_id"] == "claude-code"
        assert out["software_version"] == "2.1"


# ---- integration: POST /oauth/register via live daemon ------------------


def _fake_oidc_validator(issuer="http://idp.example") -> object:
    return types.SimpleNamespace(
        config=types.SimpleNamespace(issuer=issuer),
        validate=lambda _hdr: types.SimpleNamespace(
            ok=False, user_id=None, email=None, roles=(),
        ),
    )


def _shim_config(*, trust="open", with_creds=True) -> IdpConfig:
    return IdpConfig(
        type="zitadel",
        dcr_shim=True,
        management_base_url="http://idp.example",
        project_id="proj-1",
        machine_user_client_id="svc" if with_creds else "",
        machine_user_client_secret="sec" if with_creds else "",
        org_id="org-1" if with_creds else "",
        expected_host="idp.example",
        registration_trust=trust,
        metadata_cache_seconds=300,
    )


@pytest.fixture
def daemon_shim_open(tmp_path):
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test", transport=transport, endpoint_file=endpoint,
    )
    daemon.oidc_validator = _fake_oidc_validator()
    daemon.idp_config = _shim_config(trust="open")
    daemon._idp_metadata_cache = MetadataCache(ttl_seconds=300)
    daemon.start()
    try:
        yield daemon, endpoint
    finally:
        daemon.stop()


@pytest.fixture
def daemon_shim_protected(tmp_path):
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test", transport=transport, endpoint_file=endpoint,
    )
    daemon.oidc_validator = _fake_oidc_validator()
    daemon.idp_config = _shim_config(trust="protected")
    daemon._idp_metadata_cache = MetadataCache(ttl_seconds=300)
    daemon.start()
    try:
        yield daemon, endpoint
    finally:
        daemon.stop()


@pytest.fixture
def daemon_shim_no_creds(tmp_path):
    """Shim flag on but machine user creds missing → /oauth/register 503."""
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test", transport=transport, endpoint_file=endpoint,
    )
    daemon.oidc_validator = _fake_oidc_validator()
    daemon.idp_config = _shim_config(with_creds=False)
    daemon._idp_metadata_cache = MetadataCache(ttl_seconds=300)
    daemon.start()
    try:
        yield daemon, endpoint
    finally:
        daemon.stop()


def _post_register(url, *, body, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _daemon_url(endpoint_file: Path) -> str:
    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


class TestRegisterRouteIntegration:
    def _attach_stub(self, daemon, **kwargs):
        """Stick a _StubMgmtClient on the daemon as the cached mgmt client."""
        stub = _StubMgmtClient(**kwargs)
        daemon._dcr_mgmt_client_cached = stub
        return stub

    def test_happy_path_creates_and_returns_201(self, daemon_shim_open):
        daemon, endpoint = daemon_shim_open
        self._attach_stub(daemon,
            find_returns=None,
            create_returns={"appId": "a1", "clientId": "C-CREATED"},
        )
        code, _, body = _post_register(_daemon_url(endpoint) + "/oauth/register", body={
            "redirect_uris": ["http://localhost:54321/cb"],
            "client_name": "Claude Code",
            "software_id": "claude-code",
        })
        assert code == 201
        resp = json.loads(body)
        assert resp["client_id"] == "C-CREATED"
        assert resp["client_secret"] == ""
        assert resp["client_name"] == "Claude Code"

    def test_reuse_existing_returns_201_same_client_id(self, daemon_shim_open):
        daemon, endpoint = daemon_shim_open
        self._attach_stub(daemon, find_returns={
            "id": "a1",
            "oidcConfig": {"clientId": "C-REUSED"},
        })
        code, _, body = _post_register(_daemon_url(endpoint) + "/oauth/register", body={
            "redirect_uris": ["http://localhost:54321/cb"],
        })
        assert code == 201
        assert json.loads(body)["client_id"] == "C-REUSED"

    def test_invalid_redirect_uri_returns_400(self, daemon_shim_open):
        daemon, endpoint = daemon_shim_open
        self._attach_stub(daemon)
        code, _, body = _post_register(_daemon_url(endpoint) + "/oauth/register", body={
            "redirect_uris": ["https://evil.example/cb"],
        })
        assert code == 400
        err = json.loads(body)
        assert err["error"] == "invalid_redirect_uri"

    def test_invalid_client_metadata_returns_400(self, daemon_shim_open):
        daemon, endpoint = daemon_shim_open
        self._attach_stub(daemon)
        code, _, body = _post_register(_daemon_url(endpoint) + "/oauth/register", body={
            "redirect_uris": ["http://localhost:1/cb"],
            "grant_types": ["password"],
        })
        assert code == 400
        assert json.loads(body)["error"] == "invalid_client_metadata"

    def test_malformed_json_returns_400(self, daemon_shim_open):
        _, endpoint = daemon_shim_open
        # Send raw bytes that aren't JSON.
        req = urllib.request.Request(
            _daemon_url(endpoint) + "/oauth/register",
            data=b"this is not json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                code = resp.status
                body = resp.read()
        except urllib.error.HTTPError as e:
            code = e.code
            body = e.read()
        assert code == 400
        assert json.loads(body)["error"] == "invalid_client_metadata"

    def test_no_creds_returns_503(self, daemon_shim_no_creds):
        _, endpoint = daemon_shim_no_creds
        code, _, body = _post_register(_daemon_url(endpoint) + "/oauth/register", body={
            "redirect_uris": ["http://localhost:1/cb"],
        })
        assert code == 503
        err = json.loads(body)
        assert err["error"] == "server_error"
        assert "not configured" in err["error_description"]

    def test_upstream_zitadel_error_returns_502(self, daemon_shim_open):
        daemon, endpoint = daemon_shim_open
        self._attach_stub(daemon,
            find_returns=None,
            create_raises=ZitadelMgmtError("permission denied", status=403),
        )
        code, _, body = _post_register(_daemon_url(endpoint) + "/oauth/register", body={
            "redirect_uris": ["http://localhost:1/cb"],
        })
        assert code == 502
        err = json.loads(body)
        assert err["error"] == "server_error"
        assert "permission denied" in err["error_description"]

    def test_protected_trust_rejects_anon(self, daemon_shim_protected):
        _, endpoint = daemon_shim_protected
        code, headers, _ = _post_register(_daemon_url(endpoint) + "/oauth/register", body={
            "redirect_uris": ["http://localhost:1/cb"],
        })
        assert code == 401
        # Inherits the chunk C WWW-Authenticate challenge.
        chal = headers.get("WWW-Authenticate") or headers.get("www-authenticate") or ""
        assert "Bearer" in chal
