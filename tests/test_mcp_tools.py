"""Tests for otaman_bridge.mcp_tools.list_team_sessions tool."""

from __future__ import annotations

import pytest

from otaman_bridge.mcp_server import CallContext
from otaman_bridge.mcp_tools import (
    PRIVACY_EMAILS,
    PRIVACY_OPAQUE,
    build_list_team_sessions_tool,
)
from otaman_bridge.runner_client import RunnerAuthError, RunnerUnreachableError
from otaman_bridge.web_session import SessionStore


# ---- Stubs ------------------------------------------------------------


class _StubRunner:
    def __init__(self, sessions=None, raise_exc=None):
        self.sessions = sessions or []
        self.raise_exc = raise_exc
        self.call_count = 0

    def list_sessions(self):
        self.call_count += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return list(self.sessions)


def _session_row(session_id, user, repo="auth-service", agent="backend-agent"):
    return {
        "session_id": session_id,
        "user": user,
        "agent": agent,
        "repo": repo,
        "session_name": f"otaman-{session_id}",
        "started_at": "2026-05-17T10:00:00Z",
    }


@pytest.fixture
def ctx_a():
    return CallContext(user_id="user-A", user_email="a@example", roles=("otaman:developer",))


@pytest.fixture
def ctx_b():
    return CallContext(user_id="user-B", user_email="b@example", roles=("otaman:developer",))


@pytest.fixture
def session_store_with_users():
    """SessionStore pre-populated so user_id -> email lookups work."""
    store = SessionStore()
    store.create(user_id="user-A", email="a@example", roles=())
    store.create(user_id="user-B", email="b@example", roles=())
    return store


# ---- happy paths ------------------------------------------------------


class TestHappyPath:
    def test_returns_other_users_sessions_by_default(self, ctx_a, session_store_with_users):
        runner = _StubRunner(sessions=[
            _session_row("s1", "user-A"),
            _session_row("s2", "user-B"),
        ])
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
        )
        result = tool.handler({}, ctx_a)
        sessions = result["structuredContent"]["sessions"]
        # Caller is user-A; should NOT see their own session
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s2"
        assert sessions[0]["user_id"] == "user-B"
        assert sessions[0]["is_self"] is False

    def test_include_self_returns_all(self, ctx_a, session_store_with_users):
        runner = _StubRunner(sessions=[
            _session_row("s1", "user-A"),
            _session_row("s2", "user-B"),
        ])
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
        )
        result = tool.handler({"include_self": True}, ctx_a)
        sessions = result["structuredContent"]["sessions"]
        assert len(sessions) == 2
        ids = {s["session_id"]: s["is_self"] for s in sessions}
        assert ids == {"s1": True, "s2": False}

    def test_email_included_in_default_privacy_mode(self, ctx_a, session_store_with_users):
        runner = _StubRunner(sessions=[_session_row("s2", "user-B")])
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
        )
        result = tool.handler({}, ctx_a)
        assert result["structuredContent"]["sessions"][0]["user_email"] == "b@example"

    def test_text_content_renders_emails(self, ctx_a, session_store_with_users):
        runner = _StubRunner(sessions=[_session_row("s2", "user-B", repo="my-repo")])
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
        )
        result = tool.handler({}, ctx_a)
        text = result["content"][0]["text"]
        assert "b@example" in text
        assert "my-repo" in text

    def test_empty_list_text_says_no_sessions(self, ctx_a, session_store_with_users):
        runner = _StubRunner(sessions=[])
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
        )
        result = tool.handler({}, ctx_a)
        assert result["structuredContent"]["sessions"] == []
        assert "No active team sessions" in result["content"][0]["text"]


# ---- privacy ----------------------------------------------------------


class TestPrivacyMode:
    def test_opaque_mode_strips_email(self, ctx_a, session_store_with_users):
        runner = _StubRunner(sessions=[_session_row("s2", "user-B")])
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
            privacy_mode=PRIVACY_OPAQUE,
        )
        result = tool.handler({}, ctx_a)
        entry = result["structuredContent"]["sessions"][0]
        assert "user_email" not in entry
        assert entry["user_id"] == "user-B"

    def test_opaque_mode_text_falls_back_to_user_id(self, ctx_a, session_store_with_users):
        runner = _StubRunner(sessions=[_session_row("s2", "user-B")])
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
            privacy_mode=PRIVACY_OPAQUE,
        )
        result = tool.handler({}, ctx_a)
        text = result["content"][0]["text"]
        assert "b@example" not in text
        assert "user-B" in text

    def test_invalid_privacy_mode_raises(self):
        runner = _StubRunner()
        with pytest.raises(ValueError, match="invalid privacy_mode"):
            build_list_team_sessions_tool(
                runner_client=runner, session_store=SessionStore(),
                privacy_mode="bogus",
            )


# ---- error paths ------------------------------------------------------


class TestErrorPaths:
    def test_runner_unreachable_returns_mcp_error_result(self, ctx_a, session_store_with_users):
        runner = _StubRunner(raise_exc=RunnerUnreachableError("connection refused"))
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
        )
        result = tool.handler({}, ctx_a)
        assert result["isError"] is True
        assert "unavailable" in result["content"][0]["text"]
        assert "connection refused" in result["content"][0]["text"]

    def test_runner_auth_error_returns_mcp_error_result(self, ctx_a, session_store_with_users):
        runner = _StubRunner(raise_exc=RunnerAuthError("HTTP 401"))
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
        )
        result = tool.handler({}, ctx_a)
        assert result["isError"] is True
        assert "loopback token" in result["content"][0]["text"]


# ---- email lookup edge cases ------------------------------------------


class TestEmailLookup:
    def test_email_none_when_no_session_for_user(self, ctx_a):
        store = SessionStore()  # empty -- user-B has no session here
        runner = _StubRunner(sessions=[_session_row("s2", "user-B")])
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=store,
        )
        result = tool.handler({}, ctx_a)
        entry = result["structuredContent"]["sessions"][0]
        assert entry["user_email"] is None

    def test_empty_user_id_skips_lookup(self, ctx_a, session_store_with_users):
        """Sessions spawned without a user (system / pre-team-mode) have user=''."""
        runner = _StubRunner(sessions=[_session_row("s9", "")])
        tool = build_list_team_sessions_tool(
            runner_client=runner, session_store=session_store_with_users,
        )
        result = tool.handler({}, ctx_a)
        entry = result["structuredContent"]["sessions"][0]
        assert entry["user_id"] == ""
        assert entry["user_email"] is None
        # And it's NOT considered "self" -- caller's user_id is user-A, not ""
        assert entry["is_self"] is False
