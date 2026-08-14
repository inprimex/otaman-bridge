"""Tests for bridge/daemon.py — HTTP server + pending-approval bookkeeping."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from otaman_bridge.daemon import (
    BridgeDaemon,
    endpoint_path,
    read_endpoint_file,
    write_endpoint_file,
)
from otaman_bridge.transports.null import NullTransport

# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture(autouse=True)
def _clean_registry():
    # Preserve the built-in 'null' registration after each test.
    import importlib

    import otaman_bridge.transports.null

    importlib.reload(otaman_bridge.transports.null)
    yield


@pytest.fixture
def running_daemon(tmp_path):
    """A daemon bound to an ephemeral port with a NullTransport."""
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


def _post(url: str, body: dict, token: str | None = None, timeout: float = 5.0):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout)


def _get(url: str, token: str | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout)


def _body(resp) -> dict:
    return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Endpoint file


class TestEndpointFile:
    def test_write_and_read_roundtrip(self, tmp_path):
        path = tmp_path / "endpoint"
        write_endpoint_file(
            path,
            port=12345,
            token="abc",
            pid=999,
            account="personal",
            transport="null",
        )
        data = read_endpoint_file(path)
        assert data["port"] == 12345
        assert data["token"] == "abc"
        assert data["pid"] == 999
        assert data["account"] == "personal"
        assert data["transport"] == "null"
        assert "started_at" in data

    def test_read_missing_returns_none(self, tmp_path):
        assert read_endpoint_file(tmp_path / "ghost") is None

    def test_read_malformed_returns_none(self, tmp_path):
        path = tmp_path / "bad"
        path.write_text("not json", encoding="utf-8")
        assert read_endpoint_file(path) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod 0600 semantics are POSIX")
    def test_mode_600_on_posix(self, tmp_path):
        import stat

        path = tmp_path / "endpoint"
        write_endpoint_file(
            path,
            port=1,
            token="t",
            pid=1,
            account="a",
            transport="null",
        )
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_endpoint_path_default(self, monkeypatch):
        # No override env vars set → default to ~/.maestro/ (back-compat)
        monkeypatch.delenv("OTAMAN_BRIDGE_DIR", raising=False)
        monkeypatch.delenv("MAESTRO_BRIDGE_DIR", raising=False)
        p = endpoint_path("personal")
        assert p.name == "bridge-personal.endpoint"
        assert p.parent.name == ".maestro"

    def test_endpoint_path_otaman_override(self, monkeypatch, tmp_path):
        # OTAMAN_BRIDGE_DIR pointing at a custom dir → endpoint lands there.
        # This is how otaman-native deployments redirect to ~/.otaman/.
        target = tmp_path / "alt-bridge-dir"
        monkeypatch.setenv("OTAMAN_BRIDGE_DIR", str(target))
        monkeypatch.delenv("MAESTRO_BRIDGE_DIR", raising=False)
        p = endpoint_path("otaman-dev")
        assert p == target / "bridge-otaman-dev.endpoint"

    def test_endpoint_path_maestro_legacy_alias(self, monkeypatch, tmp_path):
        # MAESTRO_BRIDGE_DIR is the legacy alias — works but lower precedence.
        monkeypatch.delenv("OTAMAN_BRIDGE_DIR", raising=False)
        monkeypatch.setenv("MAESTRO_BRIDGE_DIR", str(tmp_path / "legacy"))
        p = endpoint_path("personal")
        assert p.parent == tmp_path / "legacy"

    def test_endpoint_path_otaman_wins_over_maestro(self, monkeypatch, tmp_path):
        # When both set, OTAMAN_BRIDGE_DIR wins (matches the env-var
        # priority pattern in otaman_core/_resolve.py).
        monkeypatch.setenv("OTAMAN_BRIDGE_DIR", str(tmp_path / "otaman"))
        monkeypatch.setenv("MAESTRO_BRIDGE_DIR", str(tmp_path / "maestro"))
        p = endpoint_path("personal")
        assert p.parent == tmp_path / "otaman"


# ---------------------------------------------------------------------------
# Lifecycle


class TestLifecycle:
    def test_starts_and_writes_endpoint_file(self, tmp_path):
        transport = NullTransport()
        endpoint = tmp_path / ".maestro" / "bridge-x.endpoint"
        daemon = BridgeDaemon(
            account="x",
            transport=transport,
            endpoint_file=endpoint,
        )
        daemon.start()
        try:
            assert endpoint.is_file()
            data = read_endpoint_file(endpoint)
            assert data["port"] == daemon.port
            assert data["account"] == "x"
            assert data["token"] == daemon.token
        finally:
            daemon.stop()

    def test_stop_removes_endpoint_file(self, tmp_path):
        transport = NullTransport()
        endpoint = tmp_path / ".maestro" / "bridge-x.endpoint"
        daemon = BridgeDaemon(
            account="x",
            transport=transport,
            endpoint_file=endpoint,
        )
        daemon.start()
        daemon.stop()
        assert not endpoint.exists()

    def test_stale_endpoint_is_replaced(self, tmp_path):
        """A stale endpoint (port unreachable) is overwritten — the common
        recovery path when the prior daemon was killed hard (Ctrl-C twice,
        OOM, power loss) and didn't unlink its endpoint file."""
        transport = NullTransport()
        endpoint = tmp_path / ".maestro" / "bridge-x.endpoint"
        endpoint.parent.mkdir(parents=True, exist_ok=True)
        # Port 1 is reserved (tcpmux) — basically never listening locally.
        endpoint.write_text(
            json.dumps(
                {"port": 1, "token": "stale", "pid": 99999, "account": "x", "transport": "null"}
            )
        )
        daemon = BridgeDaemon(
            account="x",
            transport=transport,
            endpoint_file=endpoint,
        )
        daemon.start()
        try:
            # New endpoint file should have the fresh port + token
            data = json.loads(endpoint.read_text(encoding="utf-8"))
            assert data["port"] == daemon.port
            assert data["token"] == daemon.token
            assert data["token"] != "stale"
        finally:
            daemon.stop()

    def test_live_endpoint_blocks_new_daemon(self, tmp_path):
        """If a real daemon IS listening, starting a second one must fail."""
        transport1 = NullTransport()
        endpoint = tmp_path / ".maestro" / "bridge-x.endpoint"
        d1 = BridgeDaemon(
            account="x",
            transport=transport1,
            endpoint_file=endpoint,
        )
        d1.start()
        try:
            d2 = BridgeDaemon(
                account="x",
                transport=NullTransport(),
                endpoint_file=endpoint,
            )
            with pytest.raises(RuntimeError, match="IS running"):
                d2.start()
        finally:
            d1.stop()

    def test_invalid_account_name_rejected(self):
        with pytest.raises(ValueError):
            BridgeDaemon(account="bad name!", transport=NullTransport())

    def test_stop_is_idempotent(self, tmp_path):
        daemon = BridgeDaemon(
            account="x",
            transport=NullTransport(),
            endpoint_file=tmp_path / ".maestro" / "bridge-x.endpoint",
        )
        daemon.start()
        daemon.stop()
        daemon.stop()  # must not raise


# ---------------------------------------------------------------------------
# Auth


class TestAuth:
    def test_missing_bearer_rejected(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/notify"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(url, {"account": "x", "project": "p", "severity": "info", "title": "t"})
        assert exc.value.code == 401

    def test_wrong_token_rejected(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/notify"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(
                url,
                {"account": "x", "project": "p", "severity": "info", "title": "t"},
                token="wrong-token",
            )
        assert exc.value.code == 401

    def test_correct_token_accepted(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/notify"
        resp = _post(
            url,
            {"account": "x", "project": "p", "severity": "info", "title": "t"},
            token=daemon.token,
        )
        assert resp.status == 202

    def test_status_does_not_require_auth(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/status"
        resp = _get(url)  # no token
        body = _body(resp)
        assert body["account"] == "test"
        assert body["transport"] == "null"


# ---------------------------------------------------------------------------
# /healthz


class TestHealthz:
    def test_returns_200_when_running(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/healthz"
        resp = _get(url)  # no auth token needed
        assert resp.status == 200
        body = _body(resp)
        assert body["ok"] is True
        assert body["transport"] == "null"
        assert isinstance(body["uptime_seconds"], int)

    def test_does_not_require_auth(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/healthz"
        resp = _get(url)  # no token
        assert resp.status == 200

    def test_returns_503_after_shutdown_requested(self, tmp_path):
        transport = NullTransport(allowlist={"*"})
        endpoint = tmp_path / "bridge.endpoint"
        daemon = BridgeDaemon(account="test", transport=transport, endpoint_file=endpoint)
        daemon.start()
        port = daemon.port
        daemon._shutdown_requested.set()
        try:
            url = f"http://127.0.0.1:{port}/healthz"
            try:
                _get(url)
                raise AssertionError("expected HTTP 503")
            except urllib.error.HTTPError as exc:
                assert exc.code == 503
                body = json.loads(exc.read().decode())
                assert body["ok"] is False
        finally:
            daemon._shutdown_requested.clear()
            daemon.stop()


# ---------------------------------------------------------------------------
# /notify


class TestNotify:
    def test_accepts_valid_info_message(self, running_daemon):
        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/notify"
        resp = _post(
            url,
            {
                "account": "personal",
                "project": "demo",
                "severity": "info",
                "title": "task done",
                "body": "backend finished 3.1",
                "source_agent": "",
                "bus_message_id": "",
            },
            token=daemon.token,
        )
        assert resp.status == 202

        # Give the async loop a beat to process
        for _ in range(10):
            if transport.sent_infos:
                break
            time.sleep(0.05)
        assert len(transport.sent_infos) == 1
        assert transport.sent_infos[0].title == "task done"

    def test_rejects_invalid_body(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/notify"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(url, {"not": "an info message"}, token=daemon.token)
        assert exc.value.code == 400


# ---------------------------------------------------------------------------
# /approval — blocking path


def _approval_body(request_id: str = "test-1"):
    return {
        "account": "personal",
        "project": "demo",
        "repo": "auth",
        "agent": "backend",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "reason": "listing",
        "priority": "normal",
        "timeout_seconds": 5,
        "request_id": request_id,
    }


class TestApproval:
    def test_approval_resolves_via_reply(self, running_daemon):
        daemon, transport = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        reply_url = f"http://127.0.0.1:{daemon.port}/reply"

        body = _approval_body()
        result_holder: dict = {}

        def do_request():
            try:
                resp = _post(url, body, token=daemon.token, timeout=10.0)
                result_holder["body"] = _body(resp)
            except Exception as e:
                result_holder["error"] = e

        t = threading.Thread(target=do_request)
        t.start()

        # Wait until the transport has received the approval (request is pending)
        for _ in range(40):
            if transport.sent_approvals:
                break
            time.sleep(0.05)
        assert transport.sent_approvals, "approval never reached transport"

        # Deliver a decision via /reply
        _post(
            reply_url,
            {
                "decision": "allow",
                "request_id": body["request_id"],
                "responder": "test:harness",
            },
            token=daemon.token,
        )

        t.join(timeout=5.0)
        assert "error" not in result_holder, result_holder.get("error")
        assert result_holder["body"]["decision"] == "allow"
        assert result_holder["body"]["responder"] == "test:harness"

    def test_approval_times_out(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        body = _approval_body("timeout-1")
        body["timeout_seconds"] = 1  # fast timeout

        t0 = time.monotonic()
        resp = _post(url, body, token=daemon.token, timeout=10.0)
        elapsed = time.monotonic() - t0

        data = _body(resp)
        assert data["decision"] == "timeout"
        assert data["request_id"] == "timeout-1"
        # Timeout fires at ~1s; allow generous overhead for thread scheduling.
        assert elapsed >= 1.0
        assert elapsed < 5.0

    def test_approval_rejects_bad_body(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/approval"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(url, {"broken": "body"}, token=daemon.token)
        assert exc.value.code == 400


# ---------------------------------------------------------------------------
# /reply — 404 path


class TestReply:
    def test_reply_for_unknown_request_returns_404(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/reply"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(
                url,
                {
                    "decision": "allow",
                    "request_id": "does-not-exist",
                    "responder": "x",
                },
                token=daemon.token,
            )
        assert exc.value.code == 404

    def test_reply_requires_request_id(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/reply"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(url, {"decision": "allow", "responder": "x"}, token=daemon.token)
        assert exc.value.code == 400


# ---------------------------------------------------------------------------
# /status and /shutdown


class TestStatus:
    def test_status_reports_fields(self, running_daemon):
        daemon, _ = running_daemon
        url = f"http://127.0.0.1:{daemon.port}/status"
        data = _body(_get(url))
        assert data["account"] == "test"
        assert data["transport"] == "null"
        assert data["pid"] == os.getpid()
        assert data["port"] == daemon.port
        assert data["pending_approvals"] == 0
        assert data["uptime_seconds"] >= 0


class TestShutdown:
    def test_shutdown_removes_endpoint_file(self, tmp_path):
        transport = NullTransport()
        endpoint = tmp_path / ".maestro" / "bridge-x.endpoint"
        daemon = BridgeDaemon(
            account="x",
            transport=transport,
            endpoint_file=endpoint,
        )
        daemon.start()
        port = daemon.port
        token = daemon.token
        try:
            resp = _post(f"http://127.0.0.1:{port}/shutdown", {}, token=token)
            assert resp.status == 200

            # Wait for endpoint file to disappear
            for _ in range(40):
                if not endpoint.exists():
                    break
                time.sleep(0.05)
            assert not endpoint.exists()
        finally:
            daemon.stop()


# ---------------------------------------------------------------------------
# Unknown routes


class TestUnknownRoutes:
    def test_get_unknown_route_404(self, running_daemon):
        daemon, _ = running_daemon
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"http://127.0.0.1:{daemon.port}/nope")
        assert exc.value.code == 404

    def test_post_unknown_route_404(self, running_daemon):
        daemon, _ = running_daemon
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"http://127.0.0.1:{daemon.port}/nope", {}, token=daemon.token)
        assert exc.value.code == 404


# ---------------------------------------------------------------------------
# Shutdown cancels pending approvals (fail-safe)


class TestShutdownCancelsPending:
    def test_pending_approvals_resolve_to_ask(self, tmp_path):
        transport = NullTransport()
        endpoint = tmp_path / ".maestro" / "bridge-x.endpoint"
        daemon = BridgeDaemon(
            account="x",
            transport=transport,
            endpoint_file=endpoint,
        )
        daemon.start()
        try:
            url = f"http://127.0.0.1:{daemon.port}/approval"
            body = _approval_body("cancel-1")
            body["timeout_seconds"] = 30

            result_holder: dict = {}

            def do_request():
                try:
                    resp = _post(url, body, token=daemon.token, timeout=30.0)
                    result_holder["body"] = _body(resp)
                except Exception as e:
                    result_holder["error"] = e

            t = threading.Thread(target=do_request)
            t.start()

            # Wait until approval is pending
            for _ in range(40):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)
            assert transport.sent_approvals
        finally:
            daemon.stop()

        t.join(timeout=5.0)
        assert "error" not in result_holder, result_holder.get("error")
        # Shutdown should have resolved the pending approval to "ask"
        # (fail-safe — let Claude's native terminal prompt handle it).
        assert result_holder["body"]["decision"] == "ask"
