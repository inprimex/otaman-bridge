"""F052 — /pm-sync/<provider> webhook must require a shared secret.

Prior to this fix the route accepted and acted on completely
unauthenticated payloads, writing real bus messages from
attacker-controlled input (2026-07-02 GAP audit, Security lens).
Drives the live HTTP daemon rather than calling the handler directly,
since the whole point is verifying the HTTP-layer gate.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon, read_endpoint_file
from otaman_bridge.transports.null import NullTransport

_SECRET_ENV = "OTAMAN_BRIDGE_PM_SYNC_WEBHOOK_SECRET"
_SECRET = "test-shared-secret-value"


class _StubPmSyncHandler:
    """Stands in for the real PmSyncHandler so these tests exercise only
    the HTTP-layer auth gate, not the PM-sync routing logic (that's
    covered by test_pm_sync_handler.py)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def handle_inbound_webhook(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {"ok": True, "event_type": "stub"}


@pytest.fixture
def daemon_with_stub_pm_sync(tmp_path):
    transport = NullTransport(allowlist={"*"})
    endpoint = tmp_path / ".maestro" / "bridge-test.endpoint"
    daemon = BridgeDaemon(account="test", transport=transport, endpoint_file=endpoint)
    stub = _StubPmSyncHandler()
    daemon._pm_sync_handler = stub
    daemon.start()
    try:
        yield daemon, stub
    finally:
        daemon.stop()


def _post(url, *, body, headers=None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _daemon_url(endpoint_file: Path) -> str:
    fields = read_endpoint_file(endpoint_file)
    return f"http://127.0.0.1:{fields['port']}"


class TestPmSyncWebhookAuth:
    def test_secret_not_configured_returns_503_and_does_not_process(
        self,
        daemon_with_stub_pm_sync,
        monkeypatch,
    ):
        monkeypatch.delenv(_SECRET_ENV, raising=False)
        daemon, stub = daemon_with_stub_pm_sync
        status, body = _post(
            f"{_daemon_url(daemon.endpoint_file)}/pm-sync/easy8",
            body={"issue": {"id": 1}},
        )
        assert status == 503
        assert "not configured" in body["error"]
        assert stub.calls == []

    def test_missing_auth_header_returns_401_and_does_not_process(
        self,
        daemon_with_stub_pm_sync,
        monkeypatch,
    ):
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        daemon, stub = daemon_with_stub_pm_sync
        status, body = _post(
            f"{_daemon_url(daemon.endpoint_file)}/pm-sync/easy8",
            body={"issue": {"id": 1}},
        )
        assert status == 401
        assert stub.calls == []

    def test_wrong_secret_returns_401_and_does_not_process(
        self,
        daemon_with_stub_pm_sync,
        monkeypatch,
    ):
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        daemon, stub = daemon_with_stub_pm_sync
        status, body = _post(
            f"{_daemon_url(daemon.endpoint_file)}/pm-sync/easy8",
            body={"issue": {"id": 1}},
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert status == 401
        assert stub.calls == []

    def test_correct_secret_processes_normally(
        self,
        daemon_with_stub_pm_sync,
        monkeypatch,
    ):
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        daemon, stub = daemon_with_stub_pm_sync
        payload = {"issue": {"id": 42}}
        status, body = _post(
            f"{_daemon_url(daemon.endpoint_file)}/pm-sync/easy8",
            body=payload,
            headers={"Authorization": f"Bearer {_SECRET}"},
        )
        assert status == 200
        assert body == {"ok": True, "event_type": "stub"}
        assert stub.calls == [payload]

    def test_non_bearer_auth_header_is_rejected(
        self,
        daemon_with_stub_pm_sync,
        monkeypatch,
    ):
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        daemon, stub = daemon_with_stub_pm_sync
        status, _ = _post(
            f"{_daemon_url(daemon.endpoint_file)}/pm-sync/easy8",
            body={"issue": {"id": 1}},
            headers={"Authorization": _SECRET},  # missing "Bearer " prefix
        )
        assert status == 401
        assert stub.calls == []
