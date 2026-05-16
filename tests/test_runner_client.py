"""Tests for otaman_bridge.runner_client -- bridge to runner HTTP helper."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from otaman_bridge.runner_client import (
    RunnerAuthError,
    RunnerClient,
    RunnerUnreachableError,
)


# ---- Helpers -----------------------------------------------------------


def _write_endpoint(path: Path, *, host="127.0.0.1", port=8091, token="TKN"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"host={host}\nport={port}\ntoken={token}\npid=1234\n",
        encoding="utf-8",
    )


class _StubResponse:
    def __init__(self, body, status=200):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StubOpener:
    """Mocks urllib opener to return canned responses. Captures requests."""

    def __init__(self, *, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, dict(req.headers), timeout))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


# ---- _read_endpoint ----------------------------------------------------


class TestReadEndpoint:
    def test_missing_file_raises_unreachable(self, tmp_path):
        ep = tmp_path / "nope.endpoint"
        client = RunnerClient(endpoint_file=ep)
        with pytest.raises(RunnerUnreachableError, match="not found"):
            client._read_endpoint()

    def test_parses_well_formed_endpoint(self, tmp_path):
        ep = tmp_path / "runner.endpoint"
        _write_endpoint(ep, host="127.0.0.1", port=9999, token="abc")
        host, port, token = RunnerClient(endpoint_file=ep)._read_endpoint()
        assert host == "127.0.0.1"
        assert port == 9999
        assert token == "abc"

    def test_missing_required_field_raises(self, tmp_path):
        ep = tmp_path / "runner.endpoint"
        ep.parent.mkdir(parents=True, exist_ok=True)
        ep.write_text("host=127.0.0.1\npid=1\n", encoding="utf-8")  # no port, no token
        with pytest.raises(RunnerUnreachableError, match="missing required fields"):
            RunnerClient(endpoint_file=ep)._read_endpoint()


# ---- list_sessions -----------------------------------------------------


class TestListSessions:
    def test_returns_session_list_on_200(self, tmp_path):
        ep = tmp_path / "runner.endpoint"
        _write_endpoint(ep)
        opener = _StubOpener(response=_StubResponse(json.dumps({
            "sessions": [
                {"session_id": "s1", "user": "u1", "agent": "a", "repo": "r",
                 "session_name": "n1", "started_at": "2026-05-15T00:00:00Z"},
                {"session_id": "s2", "user": "u2", "agent": "a", "repo": "r",
                 "session_name": "n2", "started_at": "2026-05-15T00:00:00Z"},
            ],
        })))
        client = RunnerClient(endpoint_file=ep, opener=opener)
        sessions = client.list_sessions()
        assert len(sessions) == 2
        assert sessions[0]["user"] == "u1"
        assert sessions[1]["user"] == "u2"

    def test_sends_bearer_token_from_endpoint_file(self, tmp_path):
        ep = tmp_path / "runner.endpoint"
        _write_endpoint(ep, token="my-secret-token")
        opener = _StubOpener(response=_StubResponse(json.dumps({"sessions": []})))
        RunnerClient(endpoint_file=ep, opener=opener).list_sessions()
        url, headers, timeout = opener.calls[0]
        assert "/sessions" in url
        assert headers.get("Authorization") == "Bearer my-secret-token"

    def test_401_raises_auth_error(self, tmp_path):
        import urllib.error
        ep = tmp_path / "runner.endpoint"
        _write_endpoint(ep)
        opener = _StubOpener(raise_exc=urllib.error.HTTPError(
            url="http://x", code=401, msg="Unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"error":"bad token"}'),
        ))
        client = RunnerClient(endpoint_file=ep, opener=opener)
        with pytest.raises(RunnerAuthError, match="401"):
            client.list_sessions()

    def test_500_raises_unreachable(self, tmp_path):
        import urllib.error
        ep = tmp_path / "runner.endpoint"
        _write_endpoint(ep)
        opener = _StubOpener(raise_exc=urllib.error.HTTPError(
            url="http://x", code=500, msg="Server Error", hdrs=None,
            fp=io.BytesIO(b'{"error":"internal"}'),
        ))
        client = RunnerClient(endpoint_file=ep, opener=opener)
        with pytest.raises(RunnerUnreachableError, match="500"):
            client.list_sessions()

    def test_connection_refused_raises_unreachable(self, tmp_path):
        import urllib.error
        ep = tmp_path / "runner.endpoint"
        _write_endpoint(ep)
        opener = _StubOpener(raise_exc=urllib.error.URLError("Connection refused"))
        client = RunnerClient(endpoint_file=ep, opener=opener)
        with pytest.raises(RunnerUnreachableError, match="unreachable"):
            client.list_sessions()

    def test_malformed_json_raises_unreachable(self, tmp_path):
        ep = tmp_path / "runner.endpoint"
        _write_endpoint(ep)
        opener = _StubOpener(response=_StubResponse(b"not-json"))
        client = RunnerClient(endpoint_file=ep, opener=opener)
        with pytest.raises(RunnerUnreachableError, match="not valid JSON"):
            client.list_sessions()

    def test_response_missing_sessions_key_raises_unreachable(self, tmp_path):
        ep = tmp_path / "runner.endpoint"
        _write_endpoint(ep)
        opener = _StubOpener(response=_StubResponse(json.dumps({"other": []})))
        client = RunnerClient(endpoint_file=ep, opener=opener)
        with pytest.raises(RunnerUnreachableError, match="missing 'sessions'"):
            client.list_sessions()

    def test_empty_session_list_returns_empty_list(self, tmp_path):
        """No sessions != error. Must return [] cleanly."""
        ep = tmp_path / "runner.endpoint"
        _write_endpoint(ep)
        opener = _StubOpener(response=_StubResponse(json.dumps({"sessions": []})))
        client = RunnerClient(endpoint_file=ep, opener=opener)
        assert client.list_sessions() == []
