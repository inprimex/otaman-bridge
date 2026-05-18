"""Tests for build_get_recent_activity_tool (v0++ team-mode).

Three layers:
- Pure helpers: _iso_since_hours_ago, _summarize_messages,
  _summarize_team_sessions, _format_recent_activity.
- Tool handler with validation + read aggregation against a fake Inbox
  and fake RunnerClient.
- Identity-gate behavior.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from otaman_bridge.mcp_server import CallContext
from otaman_bridge.mcp_tools import (
    IDENTITY_REQUIRED_TOOLS,
    _format_recent_activity,
    _iso_since_hours_ago,
    _summarize_messages,
    _summarize_team_sessions,
    build_get_recent_activity_tool,
)
from otaman_bridge.runner_client import (
    RunnerAuthError,
    RunnerUnreachableError,
)


# ---- _iso_since_hours_ago -----------------------------------------------


NOW = datetime.datetime(2026, 5, 18, 18, 0, 0, tzinfo=datetime.timezone.utc)


class TestIsoSinceHoursAgo:
    def test_24h_window(self):
        out = _iso_since_hours_ago(24, now=NOW)
        assert out == "2026-05-17T18:00:00Z"

    def test_1h_window(self):
        out = _iso_since_hours_ago(1, now=NOW)
        assert out == "2026-05-18T17:00:00Z"

    def test_168h_window(self):
        out = _iso_since_hours_ago(168, now=NOW)
        assert out == "2026-05-11T18:00:00Z"

    def test_format_is_z_suffix(self):
        out = _iso_since_hours_ago(1, now=NOW)
        assert out.endswith("Z")
        assert len(out) == len("2026-05-18T17:00:00Z")

    def test_naive_now_normalized(self):
        """If `now` lacks tzinfo, treat as UTC rather than crashing."""
        naive = datetime.datetime(2026, 5, 18, 18, 0, 0)
        out = _iso_since_hours_ago(1, now=naive)
        assert out == "2026-05-18T17:00:00Z"

    def test_default_now_uses_real_clock(self):
        out = _iso_since_hours_ago(1)
        # Just verify it parses + ends with Z.
        assert out.endswith("Z")
        datetime.datetime.fromisoformat(out.replace("Z", "+00:00"))


# ---- _summarize_messages ------------------------------------------------


@dataclass
class _M:
    """Stand-in for StoredMessage with the attrs the summarizer reads."""
    type: str = "chat"
    priority: str = "normal"
    read_at: str | None = None
    id: str = "m-1"
    from_user: str = "user-X"
    subject: str = "(no subject)"
    sent_at: str = "2026-05-18T15:00:00Z"


class TestSummarizeMessages:
    def test_empty(self):
        s = _summarize_messages([])
        assert s == {"total": 0, "unread": 0, "by_type": {}, "by_priority": {}}

    def test_counts(self):
        s = _summarize_messages([
            _M(type="chat", priority="normal", read_at=None),
            _M(type="chat", priority="high",  read_at="2026-05-18T16:00:00Z"),
            _M(type="review-request", priority="high", read_at=None),
        ])
        assert s["total"] == 3
        assert s["unread"] == 2
        assert s["by_type"] == {"chat": 2, "review-request": 1}
        assert s["by_priority"] == {"normal": 1, "high": 2}

    def test_missing_attrs_fall_back_to_defaults(self):
        """A StoredMessage missing .type / .priority shouldn't crash —
        getattr fallbacks treat them as chat / normal."""
        bare = SimpleNamespace(read_at=None)
        s = _summarize_messages([bare])
        assert s["by_type"] == {"chat": 1}
        assert s["by_priority"] == {"normal": 1}

    def test_empty_string_type_treated_as_chat(self):
        s = _summarize_messages([_M(type="", priority="")])
        assert s["by_type"] == {"chat": 1}
        assert s["by_priority"] == {"normal": 1}


# ---- _summarize_team_sessions -------------------------------------------


class TestSummarizeTeamSessions:
    def test_empty(self):
        assert _summarize_team_sessions([]) == {"total": 0, "by_repo": {}}

    def test_counts_per_repo(self):
        sessions = [
            {"user_id": "user-A", "repo": "auth-service"},
            {"user_id": "user-B", "repo": "web-app"},
            {"user_id": "user-C", "repo": "web-app"},
            {"user_id": "user-D", "repo": "auth-service"},
        ]
        s = _summarize_team_sessions(sessions)
        assert s == {"total": 4, "by_repo": {"auth-service": 2, "web-app": 2}}

    def test_excludes_caller(self):
        sessions = [
            {"user_id": "me",    "repo": "auth-service"},
            {"user_id": "other", "repo": "web-app"},
        ]
        s = _summarize_team_sessions(sessions, exclude_user_id="me")
        assert s == {"total": 1, "by_repo": {"web-app": 1}}

    def test_unknown_repo_grouped(self):
        sessions = [
            {"user_id": "u", "repo": None},
            {"user_id": "v"},  # missing key entirely
        ]
        s = _summarize_team_sessions(sessions)
        assert s["by_repo"] == {"(unknown)": 2}


# ---- _format_recent_activity --------------------------------------------


class TestFormatRecentActivity:
    def test_empty_inbox(self):
        out = _format_recent_activity(
            window_hours=24,
            inbox_summary={"total": 0, "unread": 0, "by_type": {}, "by_priority": {}},
            inbox_messages=[],
            team_summary=None,
        )
        assert "last 24h" in out
        assert "Inbox: 0 message(s)" in out
        # No types line when zero messages.
        assert "types:" not in out

    def test_with_messages(self):
        msgs = [
            _M(type="review-request", priority="high", subject="JWT rotation",
               from_user="dev-b", sent_at="2026-05-18T15:32:00Z", read_at=None),
            _M(type="chat", priority="normal", subject="hello",
               from_user="dev-b", sent_at="2026-05-18T15:14:00Z",
               read_at="2026-05-18T16:00:00Z"),
        ]
        summary = _summarize_messages(msgs)
        out = _format_recent_activity(
            window_hours=24,
            inbox_summary=summary,
            inbox_messages=msgs,
            team_summary=None,
        )
        assert "Inbox: 2 message(s) (1 unread)" in out
        assert "JWT rotation" in out
        assert "hello" in out
        assert "[high, review-request, unread]" in out
        assert "[normal, chat, read]" in out

    def test_truncates_long_lists(self):
        msgs = [
            _M(subject=f"msg {i}", read_at=None)
            for i in range(15)
        ]
        out = _format_recent_activity(
            window_hours=24,
            inbox_summary=_summarize_messages(msgs),
            inbox_messages=msgs,
            team_summary=None,
        )
        # Shows 10 + "(... 5 more)"
        assert "msg 0" in out and "msg 9" in out
        assert "msg 10" not in out
        assert "5 more" in out

    def test_with_team_summary(self):
        out = _format_recent_activity(
            window_hours=24,
            inbox_summary={"total": 0, "unread": 0, "by_type": {}, "by_priority": {}},
            inbox_messages=[],
            team_summary={"total": 3, "by_repo": {"auth-service": 1, "web-app": 2}},
        )
        assert "Team: 3 active session(s)" in out
        assert "auth-service: 1" in out
        assert "web-app: 2" in out

    def test_team_summary_none_omits_section(self):
        out = _format_recent_activity(
            window_hours=24,
            inbox_summary={"total": 0, "unread": 0, "by_type": {}, "by_priority": {}},
            inbox_messages=[],
            team_summary=None,
        )
        assert "Team:" not in out


# ---- identity-required registration -------------------------------------


def test_get_recent_activity_is_identity_required():
    assert "get_recent_activity" in IDENTITY_REQUIRED_TOOLS


# ---- Handler integration (with fake Inbox + RunnerClient) --------------


class _FakeInbox:
    def __init__(self, *, messages=None, raise_value_error=None):
        self.messages = messages or []
        self.raise_value_error = raise_value_error
        self.calls = []

    def list_messages(self, user_id, **kwargs):
        self.calls.append({"user_id": user_id, **kwargs})
        if self.raise_value_error:
            raise ValueError(self.raise_value_error)
        return list(self.messages)


class _FakeRunner:
    def __init__(self, sessions=None, raises=None):
        self.sessions = sessions or []
        self.raises = raises
        self.calls = 0

    def list_sessions(self):
        self.calls += 1
        if self.raises:
            raise self.raises
        return list(self.sessions)


@pytest.fixture
def tool():
    return build_get_recent_activity_tool(
        inbox=_FakeInbox(messages=[
            _M(id="m-1", subject="hello", read_at=None),
            _M(id="m-2", subject="review", type="review-request",
               priority="high", read_at="2026-05-18T16:00:00Z"),
        ]),
        runner_client=_FakeRunner(sessions=[
            {"user_id": "other-A", "repo": "auth-service"},
            {"user_id": "other-B", "repo": "web-app"},
        ]),
    )


@pytest.fixture
def authd_ctx():
    return CallContext(user_id="me", user_email="me@example", roles=("otaman:developer",))


@pytest.fixture
def loopback_ctx():
    return CallContext(user_id="", user_email=None, roles=())


def _inbox_of(tool):
    return tool.handler.__closure__[0].cell_contents


def _runner_of(tool):
    # 'inbox' is first closure var, 'runner_client' is second.
    return tool.handler.__closure__[1].cell_contents


class TestHandlerValidation:
    def test_loopback_rejected(self, tool, loopback_ctx):
        result = tool.handler({}, loopback_ctx)
        assert result["isError"] is True
        assert "identity" in result["content"][0]["text"].lower()

    @pytest.mark.parametrize("hours", [0, -1, 169, 500])
    def test_bad_hours(self, tool, authd_ctx, hours):
        result = tool.handler({"hours": hours}, authd_ctx)
        assert result["isError"] is True
        assert "hours" in result["content"][0]["text"]

    def test_non_integer_hours(self, tool, authd_ctx):
        result = tool.handler({"hours": "24"}, authd_ctx)
        assert result["isError"] is True

    def test_bool_not_treated_as_int(self, tool, authd_ctx):
        """Python bool is technically int — reject explicitly so True/False
        for hours/limit isn't silently accepted."""
        result = tool.handler({"hours": True}, authd_ctx)
        assert result["isError"] is True

    @pytest.mark.parametrize("limit", [0, -5, 201, 1000])
    def test_bad_limit(self, tool, authd_ctx, limit):
        result = tool.handler({"limit": limit}, authd_ctx)
        assert result["isError"] is True
        assert "limit" in result["content"][0]["text"]


class TestHandlerHappyPath:
    def test_default_window_24h(self, tool, authd_ctx):
        result = tool.handler({}, authd_ctx)
        assert "isError" not in result or result.get("isError") is not True
        sc = result["structuredContent"]
        assert sc["window_hours"] == 24
        assert sc["user_id"] == "me"
        assert sc["inbox"]["total"] == 2
        assert sc["inbox"]["unread"] == 1
        assert sc["inbox"]["by_type"]["review-request"] == 1
        assert sc["inbox"]["by_type"]["chat"] == 1

    def test_custom_window(self, tool, authd_ctx):
        result = tool.handler({"hours": 1}, authd_ctx)
        sc = result["structuredContent"]
        assert sc["window_hours"] == 1
        # 'since' is now-1h, so should be quite recent.
        assert sc["since"].startswith("2026-") or sc["since"].startswith("20")

    def test_passes_since_to_inbox(self, tool, authd_ctx):
        tool.handler({"hours": 24}, authd_ctx)
        call = _inbox_of(tool).calls[0]
        assert call["user_id"] == "me"
        assert "since" in call
        assert call["since"].endswith("Z")

    def test_team_section_excludes_caller_default(self, tool, authd_ctx):
        result = tool.handler({}, authd_ctx)
        # The fake runner returned 2 sessions, neither from 'me' → both visible.
        sc = result["structuredContent"]
        assert sc["team"]["total"] == 2
        assert sc["team"]["by_repo"]["auth-service"] == 1
        assert sc["team"]["by_repo"]["web-app"] == 1

    def test_disable_team_section(self, tool, authd_ctx):
        result = tool.handler({"include_team_sessions": False}, authd_ctx)
        sc = result["structuredContent"]
        assert "team" not in sc
        assert _runner_of(tool).calls == 0

    def test_text_summary_in_content(self, tool, authd_ctx):
        result = tool.handler({}, authd_ctx)
        text = result["content"][0]["text"]
        assert "Inbox: 2 message(s)" in text
        assert "Team: 2 active session(s)" in text

    def test_messages_in_structured_content(self, tool, authd_ctx):
        result = tool.handler({}, authd_ctx)
        msgs = result["structuredContent"]["inbox"]["messages"]
        assert len(msgs) == 2
        # Confirm one is marked read=True, one read=False.
        reads = sorted(m["read"] for m in msgs)
        assert reads == [False, True]


class TestRunnerFailureModes:
    def _make_tool_with_runner_error(self, exc):
        return build_get_recent_activity_tool(
            inbox=_FakeInbox(),
            runner_client=_FakeRunner(raises=exc),
        )

    def test_runner_unreachable_surfaces_in_text_not_error(self, authd_ctx):
        tool = self._make_tool_with_runner_error(
            RunnerUnreachableError("connection refused"),
        )
        result = tool.handler({}, authd_ctx)
        # Inbox section still works; team section gracefully unavailable.
        assert result.get("isError") is not True
        assert "Team snapshot unavailable" in result["content"][0]["text"]
        assert "runner unreachable" in result["structuredContent"]["team_error"]
        # No team key — only team_error when failure.
        assert "team" not in result["structuredContent"]

    def test_runner_auth_failure(self, authd_ctx):
        tool = self._make_tool_with_runner_error(RunnerAuthError("bad token"))
        result = tool.handler({}, authd_ctx)
        assert result.get("isError") is not True
        assert "runner auth failed" in result["structuredContent"]["team_error"]

    def test_runner_generic_failure(self, authd_ctx):
        tool = self._make_tool_with_runner_error(RuntimeError("disk full"))
        result = tool.handler({}, authd_ctx)
        assert result.get("isError") is not True
        assert "runner error" in result["structuredContent"]["team_error"]


class TestInboxFailureModes:
    def test_inbox_value_error(self, authd_ctx):
        tool = build_get_recent_activity_tool(
            inbox=_FakeInbox(raise_value_error="limit out of range"),
            runner_client=_FakeRunner(),
        )
        result = tool.handler({}, authd_ctx)
        assert result["isError"] is True
        assert "inbox query failed" in result["content"][0]["text"]


class TestToolSchema:
    def test_metadata(self, tool):
        assert tool.name == "get_recent_activity"
        assert "recent" in tool.description.lower()
        # All three optional params present.
        props = tool.input_schema["properties"]
        assert "hours" in props and props["hours"]["default"] == 24
        assert "limit" in props and props["limit"]["default"] == 50
        assert "include_team_sessions" in props
        assert props["hours"]["maximum"] == 168
        assert props["limit"]["maximum"] == 200
        # No required fields — this is a read tool.
        assert tool.input_schema.get("required", []) == []
