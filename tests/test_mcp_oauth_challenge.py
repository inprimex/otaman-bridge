"""Tests for the WWW-Authenticate challenge on /mcp (RFC 6750 + 9728).

Chunk C of the mcp-oauth wave: when an MCP client hits /mcp without
identity-bearing auth, the daemon returns HTTP 401 with a
WWW-Authenticate: Bearer challenge that points at this bridge's
``/.well-known/oauth-protected-resource``. MCP clients (Claude Code)
discover the OIDC issuer from there and run an auth_code+PKCE flow
to obtain a real bearer.

Three triggers for the challenge:
  1. No Authorization header at all → 401 + challenge.
  2. Invalid bearer (no matching auth path) → 401 + challenge.
  3. Loopback bearer (auth-ok-but-identity-less) calling a tool from
     IDENTITY_REQUIRED_TOOLS → 401 + challenge (instead of a tool-level
     isError that no client could recover from).

Loopback bearer continues to work for identity-less tools
(``list_team_sessions``, ``tools/list``).
"""

from __future__ import annotations

import json
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon, read_endpoint_file
from otaman_bridge.transports.null import NullTransport
from otaman_bridge.web_session import SessionCookie, SessionStore


def _fake_oidc_validator(issuer: str = "https://zitadel.example") -> object:
    """Stand-in for OIDCValidator.

    Two things are touched on the real validator in the auth path we
    exercise here: ``.config.issuer`` (read by the /.well-known route)
    and ``.validate(header)`` (called by _auth_identify on every Bearer
    request). The stub's ``validate()`` always reports ok=False so the
    request falls through to the loopback-bearer / session-cookie paths.
    """
    return types.SimpleNamespace(
        config=types.SimpleNamespace(issuer=issuer),
        validate=lambda _hdr: types.SimpleNamespace(
            ok=False, user_id=None, email=None, roles=(),
        ),
    )


@pytest.fixture
def daemon_with_oidc(tmp_path):
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test", transport=transport, endpoint_file=endpoint,
    )
    daemon.session_store = SessionStore()
    daemon.session_cookie = SessionCookie(secure=False)
    daemon.oidc_validator = _fake_oidc_validator()
    # session_store was None at __init__, so list_team_sessions wasn't
    # auto-registered (it needs the store for email lookup). Register
    # it now so the identity-less regression test can call it.
    from otaman_bridge.mcp_tools import build_list_team_sessions_tool
    if "list_team_sessions" not in daemon.mcp_server.tools:
        daemon.mcp_server.register(build_list_team_sessions_tool(
            runner_client=daemon._runner_client,
            session_store=daemon.session_store,
        ))
    daemon.start()
    try:
        yield daemon, endpoint
    finally:
        daemon.stop()


@pytest.fixture
def daemon_without_oidc(tmp_path):
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test", transport=transport, endpoint_file=endpoint,
    )
    daemon.start()
    try:
        yield daemon, endpoint
    finally:
        daemon.stop()


def _post(url, *, body=None, token=None):
    data = (json.dumps(body) if body is not None else "").encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _daemon_url(endpoint_file: Path) -> str:
    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


def _www_auth(headers: dict) -> str:
    return headers.get("WWW-Authenticate") or headers.get("www-authenticate") or ""


# --- 401 + challenge on missing or invalid auth ---------------------------


class TestUnauthenticatedRequestChallenge:
    def test_no_auth_returns_401_with_challenge(self, daemon_with_oidc):
        _, endpoint = daemon_with_oidc
        code, headers, _ = _post(_daemon_url(endpoint) + "/mcp", body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        assert code == 401
        challenge = _www_auth(headers)
        assert challenge.startswith("Bearer ")
        assert "resource_metadata=" in challenge
        assert "/.well-known/oauth-protected-resource" in challenge
        assert 'error="invalid_token"' in challenge

    def test_invalid_bearer_returns_401_with_challenge(self, daemon_with_oidc):
        _, endpoint = daemon_with_oidc
        code, headers, _ = _post(_daemon_url(endpoint) + "/mcp",
                                 token="not-the-real-token", body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        assert code == 401
        assert "resource_metadata=" in _www_auth(headers)

    def test_no_challenge_when_oidc_unconfigured(self, daemon_without_oidc):
        """Without OIDC there's no authorization server to point clients at,
        so emitting the challenge would just confuse them."""
        _, endpoint = daemon_without_oidc
        code, headers, _ = _post(_daemon_url(endpoint) + "/mcp", body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        assert code == 401
        assert _www_auth(headers) == ""


# --- 401 + challenge on loopback-bearer calling identity-required tool ----


class TestIdentityRequiredToolChallenge:
    @pytest.mark.parametrize("tool_name", [
        "send_message_to_user",
        "check_messages",
        "mark_message_read",
    ])
    def test_loopback_bearer_gets_challenge(self, daemon_with_oidc, tool_name):
        daemon, endpoint = daemon_with_oidc
        code, headers, body = _post(_daemon_url(endpoint) + "/mcp",
                                    token=daemon.token, body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool_name, "arguments": {}},
        })
        assert code == 401
        challenge = _www_auth(headers)
        assert "resource_metadata=" in challenge
        # insufficient_scope is the more precise error when the caller had
        # *some* auth but lacked the user identity the tool needs.
        assert 'error="insufficient_scope"' in challenge
        # The body shouldn't be a JSON-RPC envelope; it's a plain HTTP error.
        assert b"jsonrpc" not in body

    def test_loopback_bearer_can_still_list_tools(self, daemon_with_oidc):
        """tools/list is a meta-method, not a tool invocation; never gated."""
        daemon, endpoint = daemon_with_oidc
        code, _, body = _post(_daemon_url(endpoint) + "/mcp",
                              token=daemon.token, body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        assert code == 200
        envelope = json.loads(body)
        assert "result" in envelope
        names = {t["name"] for t in envelope["result"]["tools"]}
        # All four tools are listed; the gate is at call time, not list time.
        assert "send_message_to_user" in names

    def test_loopback_bearer_can_still_call_list_team_sessions(self, daemon_with_oidc):
        """list_team_sessions is identity-less by design; loopback bearer OK.

        Calls into the runner client which we haven't stubbed in this
        fixture, so we accept either a normal MCP error result (runner
        unreachable etc.) OR a regular response — what matters here is
        that the route did NOT return 401.
        """
        daemon, endpoint = daemon_with_oidc
        code, _, body = _post(_daemon_url(endpoint) + "/mcp",
                              token=daemon.token, body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_team_sessions", "arguments": {}},
        })
        assert code == 200
        envelope = json.loads(body)
        assert envelope.get("jsonrpc") == "2.0"
        # Either a result (real or isError-shaped) or an MCP error code.
        assert ("result" in envelope) or ("error" in envelope)
