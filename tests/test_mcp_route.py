"""Integration tests for POST /mcp on the bridge daemon.

Live daemon via NullTransport. The MCPServer is built automatically by
BridgeDaemon.__init__ with list_team_sessions registered. Tests inject
a stub RunnerClient on `daemon._runner_client` and exercise the route.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon, read_endpoint_file
from otaman_bridge.transports.null import NullTransport
from otaman_bridge.web_session import SessionCookie, SessionStore


# ---- Fixtures ---------------------------------------------------------


@pytest.fixture
def running_daemon(tmp_path):
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(
        account="test", transport=transport, endpoint_file=endpoint,
    )
    # Wire web-auth so session_store + MCP list_team_sessions are built.
    # BridgeDaemon.__init__ already did this if env was set; here we
    # do it after construction (env is empty in tests).
    daemon.session_store = SessionStore()
    daemon.session_cookie = SessionCookie(secure=False)
    # Re-register the tool now that session_store exists.
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


def _post(url, *, body=None, cookie=None, token=None):
    data = (json.dumps(body) if body is not None else "").encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
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


class _StubRunner:
    """Stub RunnerClient -- minimal interface for the tool."""
    def __init__(self, sessions=None):
        self.sessions = sessions or []
    def list_sessions(self):
        return self.sessions


# ---- Auth boundary ----------------------------------------------------


class TestMCPAuth:
    def test_unauthenticated_request_returns_401(self, running_daemon):
        daemon, endpoint = running_daemon
        base = _daemon_url(endpoint)
        code, _, _ = _post(f"{base}/mcp", body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        assert code == 401

    def test_loopback_bearer_authenticates(self, running_daemon):
        daemon, endpoint = running_daemon
        base = _daemon_url(endpoint)
        code, _, body = _post(f"{base}/mcp", token=daemon.token, body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        assert code == 200
        resp = json.loads(body)
        assert "result" in resp

    def test_session_cookie_authenticates(self, running_daemon):
        daemon, endpoint = running_daemon
        sess = daemon.session_store.create(
            user_id="user-A", email="a@x", roles=("otaman:developer",),
        )
        base = _daemon_url(endpoint)
        code, _, body = _post(
            f"{base}/mcp",
            cookie=f"otaman_bridge_sid={sess.id}",
            body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert code == 200


# ---- JSON-RPC dispatch -----------------------------------------------


class TestMCPDispatch:
    def test_tools_list_returns_list_team_sessions(self, running_daemon):
        daemon, endpoint = running_daemon
        base = _daemon_url(endpoint)
        _, _, body = _post(f"{base}/mcp", token=daemon.token, body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        resp = json.loads(body)
        tools = resp["result"]["tools"]
        assert any(t["name"] == "list_team_sessions" for t in tools)

    def test_initialize_returns_capabilities(self, running_daemon):
        daemon, endpoint = running_daemon
        base = _daemon_url(endpoint)
        _, _, body = _post(f"{base}/mcp", token=daemon.token, body={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        resp = json.loads(body)
        assert resp["result"]["serverInfo"]["name"] == "otaman-bridge"

    def test_tools_call_invokes_tool_with_caller_context(self, running_daemon):
        daemon, endpoint = running_daemon
        # Inject a stub runner that returns one session for a different user
        daemon._runner_client = _StubRunner(sessions=[
            {"session_id": "s1", "user": "user-B", "agent": "x",
             "repo": "auth-service", "session_name": "n1",
             "started_at": "2026-05-17T00:00:00Z"},
        ])
        # Re-register the tool with the new runner stub
        from otaman_bridge.mcp_tools import build_list_team_sessions_tool
        daemon.mcp_server.tools.pop("list_team_sessions", None)
        daemon.mcp_server.register(build_list_team_sessions_tool(
            runner_client=daemon._runner_client,
            session_store=daemon.session_store,
        ))

        # Caller is user-A via session cookie
        sess = daemon.session_store.create(
            user_id="user-A", email="a@x", roles=(),
        )
        base = _daemon_url(endpoint)
        _, _, body = _post(
            f"{base}/mcp", cookie=f"otaman_bridge_sid={sess.id}",
            body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "list_team_sessions", "arguments": {}}},
        )
        resp = json.loads(body)
        sessions = resp["result"]["structuredContent"]["sessions"]
        # User-A should see user-B's session
        assert len(sessions) == 1
        assert sessions[0]["user_id"] == "user-B"


# ---- Error envelopes -------------------------------------------------


class TestMCPErrors:
    def test_invalid_json_body_returns_parse_error(self, running_daemon):
        daemon, endpoint = running_daemon
        base = _daemon_url(endpoint)
        # Send raw non-JSON body with bearer auth
        req = urllib.request.Request(
            f"{base}/mcp", data=b"not-json-at-all",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {daemon.token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            body = e.read()
            status = e.code
        # HTTP 200 with error in envelope
        assert status == 200
        resp = json.loads(body)
        assert resp["error"]["code"] == -32700  # PARSE_ERROR

    def test_unknown_method_returns_method_not_found(self, running_daemon):
        daemon, endpoint = running_daemon
        base = _daemon_url(endpoint)
        _, _, body = _post(f"{base}/mcp", token=daemon.token, body={
            "jsonrpc": "2.0", "id": 1, "method": "not/a/method",
        })
        resp = json.loads(body)
        assert resp["error"]["code"] == -32601  # METHOD_NOT_FOUND
