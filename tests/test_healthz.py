"""Tests for the /healthz endpoint (containerized-agent-execution task 4.1)."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from otaman_bridge.daemon import BridgeDaemon
from otaman_bridge.transports.null import NullTransport


# ---------------------------------------------------------------------------
# Helpers


def _make_daemon(tmp_path: Path) -> BridgeDaemon:
    return BridgeDaemon(
        account="test",
        transport=NullTransport(),
        host="127.0.0.1",
        port=0,
        endpoint_file=tmp_path / "endpoint.json",
    )


def _get(url: str, *, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def _get_raw(url: str, *, timeout: float = 5.0):
    """Return (status, body_dict) without raising on non-2xx."""
    import urllib.error
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---------------------------------------------------------------------------
# Basic healthz tests


class TestHealthz:
    def test_returns_200_when_running(self, tmp_path):
        d = _make_daemon(tmp_path)
        d.start()
        try:
            status, body = _get(f"http://127.0.0.1:{d.port}/healthz")
            assert status == 200
            assert body["status"] == "ok"
        finally:
            d.stop()

    def test_body_includes_account_and_uptime(self, tmp_path):
        d = _make_daemon(tmp_path)
        d.start()
        try:
            _, body = _get(f"http://127.0.0.1:{d.port}/healthz")
            assert body["account"] == "test"
            assert isinstance(body["uptime_seconds"], int)
            assert body["uptime_seconds"] >= 0
        finally:
            d.stop()

    def test_returns_503_during_shutdown(self, tmp_path):
        """Once stop() is called the endpoint should return 503 if still reachable."""
        d = _make_daemon(tmp_path)
        d.start()
        port = d.port

        # Mark shutdown_requested without tearing down the server yet.
        d._shutdown_requested.set()
        try:
            status, body = _get_raw(f"http://127.0.0.1:{port}/healthz")
            assert status == 503
            assert body["status"] == "degraded"
            assert "reason" in body
        finally:
            d.stop()

    def test_no_auth_required(self, tmp_path):
        """healthz must be reachable without any Authorization header."""
        d = _make_daemon(tmp_path)
        d.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{d.port}/healthz")
            # Deliberately omit Authorization header.
            with urllib.request.urlopen(req, timeout=5.0) as r:
                assert r.status == 200
        finally:
            d.stop()

    def test_distinct_from_status_endpoint(self, tmp_path):
        """healthz and status are separate endpoints with different schemas."""
        d = _make_daemon(tmp_path)
        d.start()
        try:
            _, healthz = _get(f"http://127.0.0.1:{d.port}/healthz")
            _, status = _get(f"http://127.0.0.1:{d.port}/status")
            # /healthz is narrow; /status has transport + pid + port
            assert "transport" not in healthz
            assert "transport" in status
            assert "status" in healthz         # healthz-specific field
            assert "status" not in status      # status endpoint has no "status" key
        finally:
            d.stop()


# ---------------------------------------------------------------------------
# Experimental-mode extras in healthz


class TestHealthzExperimentalMode:
    def test_no_extras_without_workspace(self, tmp_path):
        d = _make_daemon(tmp_path)
        d.start()
        try:
            _, body = _get(f"http://127.0.0.1:{d.port}/healthz")
            assert "runtime_mode" not in body
            assert "experimental_warning" not in body
        finally:
            d.stop()

    def test_runtime_mode_included_when_workspace_set(self, tmp_path):
        """When bus_watcher_root is set, /healthz includes runtime_mode."""
        # Create a minimal flat-layout workspace with platform.yaml
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".agents" / "bus" / "active").mkdir(parents=True)
        (workspace / "platform.yaml").write_text(
            "project: test\nversion: '1.0'\nrepos: []\n",
            encoding="utf-8",
        )
        d = BridgeDaemon(
            account="test",
            transport=NullTransport(),
            host="127.0.0.1",
            port=0,
            endpoint_file=tmp_path / "ep.json",
            bus_watcher_root=workspace,
        )
        d.start()
        try:
            _, body = _get(f"http://127.0.0.1:{d.port}/healthz")
            # runtime_mode absent means single (no field present is fine)
            assert body["status"] == "ok"
        finally:
            d.stop()

    def test_experimental_warning_in_response_when_mode_set(self, tmp_path):
        """When runtime_mode is experimental_multi_tenant, healthz includes warning."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".agents" / "bus" / "active").mkdir(parents=True)
        (workspace / "platform.yaml").write_text(
            "project: test\nversion: '1.0'\nrepos: []\n"
            "runtime:\n  multi_tenant:\n    mode: experimental_multi_tenant\n",
            encoding="utf-8",
        )
        d = BridgeDaemon(
            account="test",
            transport=NullTransport(),
            host="127.0.0.1",
            port=0,
            endpoint_file=tmp_path / "ep.json",
            bus_watcher_root=workspace,
        )
        d.start()
        try:
            _, body = _get(f"http://127.0.0.1:{d.port}/healthz")
            assert body.get("runtime_mode") == "experimental_multi_tenant"
            assert "experimental_warning" in body
            assert "EXPERIMENTAL" in body["experimental_warning"]
        finally:
            d.stop()
