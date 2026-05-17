"""Tests for send_message_to_user MCP tool (team-mode v0+ chunk 2)."""

from __future__ import annotations

import pytest

from otaman_bridge.inbox import Inbox
from otaman_bridge.mcp_server import CallContext
from otaman_bridge.mcp_tools import build_send_message_to_user_tool
from otaman_bridge.web_session import SessionStore


@pytest.fixture
def inbox(tmp_path):
    return Inbox(root=tmp_path / "inboxes")


@pytest.fixture
def session_store():
    return SessionStore()


@pytest.fixture
def sender_ctx():
    return CallContext(
        user_id="user-A",
        user_email="a@example",
        roles=("otaman:developer",),
    )


@pytest.fixture
def tool(inbox, session_store):
    return build_send_message_to_user_tool(inbox=inbox, session_store=session_store)


# ---- happy path -------------------------------------------------------


class TestSendHappyPath:
    def test_writes_to_recipient_inbox(self, tool, inbox, sender_ctx):
        result = tool.handler(
            {"target_user_id": "user-B", "body": "Hello B!"},
            sender_ctx,
        )
        assert "structuredContent" in result
        assert "message_id" in result["structuredContent"]
        # message is in user-B's inbox
        msgs = inbox.list_messages("user-B", unread_only=False)
        assert len(msgs) == 1
        assert msgs[0].from_user == "user-A"
        assert msgs[0].from_email == "a@example"
        assert msgs[0].to_user == "user-B"
        assert msgs[0].body.startswith("Hello B!")

    def test_structured_content_returns_message_id(self, tool, sender_ctx):
        result = tool.handler(
            {"target_user_id": "user-B", "body": "Hi"},
            sender_ctx,
        )
        sc = result["structuredContent"]
        assert sc["to_user"] == "user-B"
        assert sc["message_id"]
        assert sc["sent_at"]
        assert sc["subject"]  # auto-derived

    def test_optional_subject_used(self, tool, inbox, sender_ctx):
        tool.handler(
            {"target_user_id": "user-B", "body": "Body", "subject": "Custom subject"},
            sender_ctx,
        )
        assert inbox.list_messages("user-B", unread_only=False)[0].subject == "Custom subject"

    def test_priority_and_type_propagate(self, tool, inbox, sender_ctx):
        tool.handler(
            {
                "target_user_id": "user-B", "body": "urgent please",
                "priority": "high", "type": "review-request",
            },
            sender_ctx,
        )
        msg = inbox.list_messages("user-B", unread_only=False)[0]
        assert msg.priority == "high"
        assert msg.type == "review-request"

    def test_in_reply_to_propagates(self, tool, inbox, sender_ctx):
        tool.handler(
            {
                "target_user_id": "user-B", "body": "re: that thing",
                "in_reply_to": "20260518T140000Z-userA-greeting",
            },
            sender_ctx,
        )
        msg = inbox.list_messages("user-B", unread_only=False)[0]
        assert msg.in_reply_to == "20260518T140000Z-userA-greeting"

    def test_email_omitted_when_sender_email_none(self, tool, inbox):
        # Sender's session has no email known (corner case)
        ctx = CallContext(user_id="user-A", user_email=None, roles=())
        tool.handler(
            {"target_user_id": "user-B", "body": "hi"},
            ctx,
        )
        msg = inbox.list_messages("user-B", unread_only=False)[0]
        assert msg.from_user == "user-A"
        assert msg.from_email is None


# ---- validation -------------------------------------------------------


class TestSendValidation:
    def test_missing_target_user_id(self, tool, sender_ctx):
        result = tool.handler({"body": "hi"}, sender_ctx)
        assert result["isError"] is True
        assert "target_user_id" in result["content"][0]["text"]

    def test_empty_target_user_id(self, tool, sender_ctx):
        result = tool.handler({"target_user_id": "", "body": "hi"}, sender_ctx)
        assert result["isError"] is True

    def test_missing_body(self, tool, sender_ctx):
        result = tool.handler({"target_user_id": "user-B"}, sender_ctx)
        assert result["isError"] is True
        assert "body" in result["content"][0]["text"]

    def test_empty_body(self, tool, sender_ctx):
        result = tool.handler({"target_user_id": "user-B", "body": ""}, sender_ctx)
        assert result["isError"] is True
        result = tool.handler({"target_user_id": "user-B", "body": "  \n  "}, sender_ctx)
        assert result["isError"] is True

    def test_loopback_caller_rejected(self, tool):
        """Loopback bearer = same-host CLI = no user identity = can't send."""
        ctx = CallContext(user_id="", user_email=None, roles=())
        result = tool.handler({"target_user_id": "B", "body": "hi"}, ctx)
        assert result["isError"] is True
        assert "unauthenticated" in result["content"][0]["text"].lower()

    def test_bad_priority(self, tool, sender_ctx):
        result = tool.handler(
            {"target_user_id": "B", "body": "x", "priority": "urgent"},
            sender_ctx,
        )
        assert result["isError"] is True
        assert "priority" in result["content"][0]["text"]

    def test_invalid_target_user_id_with_slash(self, tool, sender_ctx):
        """Path-traversal-style user_ids must be rejected by inbox layer."""
        result = tool.handler(
            {"target_user_id": "../escape", "body": "x"},
            sender_ctx,
        )
        assert result["isError"] is True


# ---- tool schema ------------------------------------------------------


class TestToolSchema:
    def test_tool_metadata(self, tool):
        assert tool.name == "send_message_to_user"
        assert "send a message" in tool.description.lower()
        assert tool.input_schema["required"] == ["target_user_id", "body"]
        props = tool.input_schema["properties"]
        assert "target_user_id" in props
        assert "body" in props
        assert "subject" in props
        assert "priority" in props
        assert props["priority"]["enum"] == ["low", "normal", "high"]
