"""Tests for mark_message_read MCP tool (team-mode v0+ chunk 4)."""

from __future__ import annotations

import time
import pytest

from otaman_bridge.inbox import Inbox
from otaman_bridge.mcp_server import CallContext
from otaman_bridge.mcp_tools import build_mark_message_read_tool


@pytest.fixture
def inbox(tmp_path):
    return Inbox(root=tmp_path / "inboxes")


@pytest.fixture
def ctx_b():
    return CallContext(user_id="user-B", user_email="b@example", roles=())


@pytest.fixture
def tool(inbox):
    return build_mark_message_read_tool(inbox=inbox)


def _put(inbox, to_user, body):
    return inbox.write_message(
        from_user="user-A", from_email="a@x",
        to_user=to_user, body=body,
    )


class TestMarkHappyPath:
    def test_marks_single(self, tool, inbox, ctx_b):
        m = _put(inbox, "user-B", "hi")
        result = tool.handler({"message_id": m.id}, ctx_b)
        assert result["structuredContent"]["marked"] == 1
        # Message is now read
        msgs = inbox.list_messages("user-B", unread_only=False)
        assert msgs[0].read_at is not None

    def test_idempotent_already_read(self, tool, inbox, ctx_b):
        m = _put(inbox, "user-B", "hi")
        tool.handler({"message_id": m.id}, ctx_b)
        result = tool.handler({"message_id": m.id}, ctx_b)
        assert result["structuredContent"]["marked"] == 0
        assert "already read" in result["content"][0]["text"]

    def test_unknown_message_id_no_change(self, tool, ctx_b):
        result = tool.handler({"message_id": "nope"}, ctx_b)
        assert result["structuredContent"]["marked"] == 0

    def test_mark_all_before(self, tool, inbox, ctx_b):
        m1 = _put(inbox, "user-B", "m1")
        time.sleep(1.1)
        m2 = _put(inbox, "user-B", "m2")
        time.sleep(1.1)
        m3 = _put(inbox, "user-B", "m3")
        result = tool.handler(
            {"message_id": m2.id, "mark_all_before": True}, ctx_b,
        )
        assert result["structuredContent"]["marked"] == 2
        # m3 still unread
        unread = inbox.list_messages("user-B")
        assert {m.id for m in unread} == {m3.id}

    def test_text_includes_id_on_success(self, tool, inbox, ctx_b):
        m = _put(inbox, "user-B", "hi")
        result = tool.handler({"message_id": m.id}, ctx_b)
        assert m.id in result["content"][0]["text"]


class TestMarkValidation:
    def test_missing_message_id(self, tool, ctx_b):
        result = tool.handler({}, ctx_b)
        assert result["isError"] is True
        assert "message_id" in result["content"][0]["text"]

    def test_empty_message_id(self, tool, ctx_b):
        result = tool.handler({"message_id": ""}, ctx_b)
        assert result["isError"] is True

    def test_loopback_caller_rejected(self, tool):
        ctx = CallContext(user_id="", user_email=None, roles=())
        result = tool.handler({"message_id": "x"}, ctx)
        assert result["isError"] is True


class TestSchema:
    def test_tool_metadata(self, tool):
        assert tool.name == "mark_message_read"
        assert tool.input_schema["required"] == ["message_id"]
        assert tool.input_schema["properties"]["mark_all_before"]["default"] is False
