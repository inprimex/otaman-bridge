"""Tests for check_messages MCP tool (team-mode v0+ chunk 3)."""

from __future__ import annotations

import time
import pytest

from otaman_bridge.inbox import Inbox
from otaman_bridge.mcp_server import CallContext
from otaman_bridge.mcp_tools import build_check_messages_tool


@pytest.fixture
def inbox(tmp_path):
    return Inbox(root=tmp_path / "inboxes")


@pytest.fixture
def reader_ctx():
    return CallContext(user_id="user-B", user_email="b@example", roles=())


@pytest.fixture
def tool(inbox):
    return build_check_messages_tool(inbox=inbox)


def _put(inbox, *, to_user, from_user="user-A", body="hi", subject=None,
         priority="normal", msg_type="chat"):
    return inbox.write_message(
        from_user=from_user, from_email=f"{from_user}@x",
        to_user=to_user, body=body, subject=subject,
        priority=priority, msg_type=msg_type,
    )


# ---- happy path -------------------------------------------------------


class TestCheckHappyPath:
    def test_empty_inbox_returns_empty(self, tool, reader_ctx):
        result = tool.handler({}, reader_ctx)
        assert result["structuredContent"]["messages"] == []
        assert "No unread messages" in result["content"][0]["text"]

    def test_returns_unread_by_default(self, tool, inbox, reader_ctx):
        m1 = _put(inbox, to_user="user-B", body="first")
        m2 = _put(inbox, to_user="user-B", body="second")
        inbox.mark_read("user-B", m1.id)
        result = tool.handler({}, reader_ctx)
        ids = [m["message_id"] for m in result["structuredContent"]["messages"]]
        assert ids == [m2.id]

    def test_unread_only_false_returns_all(self, tool, inbox, reader_ctx):
        m1 = _put(inbox, to_user="user-B", body="first")
        m2 = _put(inbox, to_user="user-B", body="second")
        inbox.mark_read("user-B", m1.id)
        result = tool.handler({"unread_only": False}, reader_ctx)
        assert result["structuredContent"]["total"] == 2

    def test_from_user_filter(self, tool, inbox, reader_ctx):
        _put(inbox, to_user="user-B", from_user="user-A")
        _put(inbox, to_user="user-B", from_user="user-C")
        result = tool.handler({"from_user": "user-A"}, reader_ctx)
        msgs = result["structuredContent"]["messages"]
        assert len(msgs) == 1 and msgs[0]["from_user"] == "user-A"

    def test_since_filter(self, tool, inbox, reader_ctx):
        m1 = _put(inbox, to_user="user-B", body="m1")
        time.sleep(1.1)
        m2 = _put(inbox, to_user="user-B", body="m2")
        result = tool.handler({"since": m1.sent_at}, reader_ctx)
        ids = [m["message_id"] for m in result["structuredContent"]["messages"]]
        assert ids == [m2.id]

    def test_limit(self, tool, inbox, reader_ctx):
        for i in range(5):
            _put(inbox, to_user="user-B", body=f"m{i}")
        result = tool.handler({"limit": 2}, reader_ctx)
        assert result["structuredContent"]["total"] == 2

    def test_text_renders_email_and_subject(self, tool, inbox, reader_ctx):
        _put(inbox, to_user="user-B", from_user="user-A",
             subject="Important question", body="x")
        result = tool.handler({}, reader_ctx)
        text = result["content"][0]["text"]
        assert "user-A@x" in text
        assert "Important question" in text

    def test_text_priority_tag(self, tool, inbox, reader_ctx):
        _put(inbox, to_user="user-B", body="urgent", priority="high")
        text = tool.handler({}, reader_ctx)["content"][0]["text"]
        assert "[high]" in text

    def test_structured_includes_all_fields(self, tool, inbox, reader_ctx):
        msg = _put(inbox, to_user="user-B", body="hi", subject="S",
                   priority="high", msg_type="review-request")
        result = tool.handler({"unread_only": False}, reader_ctx)
        m = result["structuredContent"]["messages"][0]
        assert m["message_id"] == msg.id
        assert m["from_user"] == "user-A"
        assert m["from_email"] == "user-A@x"
        assert m["subject"] == "S"
        assert m["body"].startswith("hi")
        assert m["sent_at"] == msg.sent_at
        assert m["read_at"] is None
        assert m["priority"] == "high"
        assert m["type"] == "review-request"


# ---- validation -------------------------------------------------------


class TestCheckValidation:
    def test_loopback_caller_rejected(self, tool):
        ctx = CallContext(user_id="", user_email=None, roles=())
        result = tool.handler({}, ctx)
        assert result["isError"] is True
        assert "identity required" in result["content"][0]["text"].lower()

    def test_non_int_limit_rejected(self, tool, reader_ctx):
        result = tool.handler({"limit": "fifty"}, reader_ctx)
        assert result["isError"] is True

    def test_out_of_range_limit_returns_error(self, tool, reader_ctx):
        # Inbox raises ValueError on limit out of [1,200]
        result = tool.handler({"limit": 0}, reader_ctx)
        assert result["isError"] is True
        result = tool.handler({"limit": 1000}, reader_ctx)
        assert result["isError"] is True


# ---- tool schema ------------------------------------------------------


class TestSchema:
    def test_tool_metadata(self, tool):
        assert tool.name == "check_messages"
        assert "inbox" in tool.description.lower()
        props = tool.input_schema["properties"]
        assert "unread_only" in props
        assert props["unread_only"]["default"] is True
        assert props["limit"]["maximum"] == 200
