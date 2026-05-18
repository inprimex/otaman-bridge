"""Integration tests for messaging tools on the bridge /mcp route (v0+ chunk 5).

Live daemon via NullTransport; tools are registered automatically by
BridgeDaemon.__init__. Drives requests over HTTP through the bridge's
JSON-RPC dispatcher, end to end.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon, read_endpoint_file
from otaman_bridge.transports.null import NullTransport


@pytest.fixture
def running_daemon(tmp_path, monkeypatch):
    # Use a per-test inbox root so tests don't trample each other.
    monkeypatch.setenv("OTAMAN_BRIDGE_INBOX_ROOT", str(tmp_path / "inboxes"))
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


def _post(url, *, body, token):
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _daemon_url(endpoint_file: Path) -> str:
    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


# ---- tool registration -----------------------------------------------


class TestRegistration:
    def test_all_three_tools_in_tools_list(self, running_daemon):
        daemon, endpoint = running_daemon
        base = _daemon_url(endpoint)
        _, resp = _post(f"{base}/mcp", token=daemon.token, body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        names = {t["name"] for t in resp["result"]["tools"]}
        assert "send_message_to_user" in names
        assert "check_messages" in names
        assert "mark_message_read" in names

    def test_inbox_root_from_env(self, running_daemon, tmp_path):
        daemon, _ = running_daemon
        # Inbox root should match OTAMAN_BRIDGE_INBOX_ROOT (set by fixture)
        assert str(daemon.inbox.root).startswith(str(tmp_path / "inboxes"))


# ---- end-to-end flow via real HTTP -----------------------------------


class TestEndToEnd:
    def test_send_then_check_then_mark(self, running_daemon):
        """Loopback bearer has ctx.user_id=''. Per the mcp-oauth wave
        (chunk C), identity-required tools now short-circuit at the HTTP
        layer with 401 (not a tool-level isError inside HTTP 200), so MCP
        clients can run their OAuth flow against the issuer named in
        /.well-known/oauth-protected-resource. Per-user end-to-end with
        a real OIDC token is in the manual-test runbook addendum.
        """
        daemon, endpoint = running_daemon
        base = _daemon_url(endpoint)

        # 1. send -- rejected at HTTP layer because loopback bearer has no user_id
        send_code, send_resp = _post(f"{base}/mcp", token=daemon.token, body={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "send_message_to_user",
                "arguments": {"target_user_id": "user-B", "body": "hi"},
            },
        })
        assert send_code == 401
        assert "send_message_to_user" in send_resp["error"]

        # 2. check -- same loopback rejection path
        check_code, check_resp = _post(f"{base}/mcp", token=daemon.token, body={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "check_messages", "arguments": {}},
        })
        assert check_code == 401
        assert "check_messages" in check_resp["error"]

        # 3. mark -- same
        mark_code, mark_resp = _post(f"{base}/mcp", token=daemon.token, body={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "mark_message_read",
                "arguments": {"message_id": "x"},
            },
        })
        assert mark_code == 401
        assert "mark_message_read" in mark_resp["error"]

    def test_send_then_check_via_injected_user_context(self, running_daemon, tmp_path):
        """Drive the full flow by writing directly to the daemon's inbox
        as if user-A sent it, then call check_messages via a real session
        cookie for user-B. This proves the route works for authenticated
        callers, complementing the loopback-rejection test above.
        """
        daemon, endpoint = running_daemon
        # Inject a session for user-B so cookie auth works for check
        from otaman_bridge.web_session import SessionStore, SessionCookie
        daemon.session_store = SessionStore()
        daemon.session_cookie = SessionCookie(secure=False)
        sess = daemon.session_store.create(
            user_id="user-B", email="b@x", roles=("otaman:developer",),
        )
        # Write a message to user-B's inbox directly
        daemon.inbox.write_message(
            from_user="user-A", from_email="a@x",
            to_user="user-B", body="Hello B from test",
        )
        # Call check_messages as user-B
        base = _daemon_url(endpoint)
        req = urllib.request.Request(
            f"{base}/mcp",
            data=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "check_messages", "arguments": {}},
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Cookie": f"otaman_bridge_sid={sess.id}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        assert "result" in data
        msgs = data["result"]["structuredContent"]["messages"]
        assert len(msgs) == 1
        assert msgs[0]["from_user"] == "user-A"
        assert "Hello B from test" in msgs[0]["body"]
