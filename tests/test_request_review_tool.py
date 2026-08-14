"""Tests for build_request_review_tool (v0++ team-mode).

Three layers:
- Pure body / subject composition helpers
- Tool handler with validation + write path against a fake Inbox
- Identity-gate behavior (loopback bearer rejected)
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from otaman_bridge.mcp_server import CallContext
from otaman_bridge.mcp_tools import (
    IDENTITY_REQUIRED_TOOLS,
    _compose_review_body,
    _compose_review_subject,
    build_request_review_tool,
)

# ---- _compose_review_body -----------------------------------------------


class TestComposeReviewBody:
    def test_summary_only(self):
        body = _compose_review_body(
            summary="please review my auth refactor",
            repo=None,
            branch=None,
            pr_url=None,
            checklist=None,
        )
        assert body == "please review my auth refactor"

    def test_summary_plus_meta(self):
        body = _compose_review_body(
            summary="auth refactor",
            repo="auth-service",
            branch="wip/jwt-rotation",
            pr_url="https://github.com/example/auth-service/pull/42",
            checklist=None,
        )
        # Summary first, then meta block.
        assert body.startswith("auth refactor\n\n")
        assert "**Repo:** auth-service" in body
        assert "**Branch:** `wip/jwt-rotation`" in body
        assert "**PR / link:** https://github.com/example/auth-service/pull/42" in body

    def test_checklist_rendered_as_bullets(self):
        body = _compose_review_body(
            summary="x",
            repo=None,
            branch=None,
            pr_url=None,
            checklist=["confirm JWT roles", "verify clock skew"],
        )
        assert "**Please check:**" in body
        assert "- confirm JWT roles" in body
        assert "- verify clock skew" in body

    def test_checklist_strips_blank_entries(self):
        body = _compose_review_body(
            summary="x",
            repo=None,
            branch=None,
            pr_url=None,
            checklist=["a", "", "  ", "b"],
        )
        # Only non-blank items survive.
        assert body.count("- ") == 2
        assert "- a" in body and "- b" in body

    def test_checklist_all_blank_omits_section(self):
        body = _compose_review_body(
            summary="x",
            repo=None,
            branch=None,
            pr_url=None,
            checklist=["", "  "],
        )
        assert "Please check" not in body

    def test_summary_is_stripped(self):
        body = _compose_review_body(
            summary="  spaced out  \n\n",
            repo=None,
            branch=None,
            pr_url=None,
            checklist=None,
        )
        assert body == "spaced out"

    def test_omits_empty_meta_section(self):
        body = _compose_review_body(
            summary="x",
            repo="",
            branch="",
            pr_url="",
            checklist=None,
        )
        # Empty strings are falsy → no meta block, no checklist.
        assert body == "x"


# ---- _compose_review_subject --------------------------------------------


class TestComposeReviewSubject:
    def test_repo_and_branch(self):
        assert (
            _compose_review_subject(
                summary="anything",
                repo="auth-service",
                branch="wip/jwt",
            )
            == "Review: auth-service / wip/jwt"
        )

    def test_repo_only(self):
        assert (
            _compose_review_subject(
                summary="anything",
                repo="auth-service",
                branch=None,
            )
            == "Review: auth-service"
        )

    def test_summary_fallback(self):
        assert (
            _compose_review_subject(
                summary="please review the JWT rotation flow",
                repo=None,
                branch=None,
            )
            == "Review: please review the JWT rotation flow"
        )

    def test_long_summary_truncated(self):
        long = "a" * 200
        subject = _compose_review_subject(summary=long, repo=None, branch=None)
        assert subject.startswith("Review: ")
        # 57 chars + "..." after the prefix.
        assert subject.endswith("...")
        assert len(subject) <= len("Review: ") + 60

    def test_summary_first_line_only(self):
        subject = _compose_review_subject(
            summary="line one\nline two\nline three",
            repo=None,
            branch=None,
        )
        assert subject == "Review: line one"

    def test_empty_summary_uses_placeholder(self):
        assert (
            _compose_review_subject(
                summary="",
                repo=None,
                branch=None,
            )
            == "Review: request"
        )

    def test_subject_blank_branch_skips_slash(self):
        """Empty branch should not produce 'Review: repo / '"""
        assert (
            _compose_review_subject(
                summary="x",
                repo="auth-service",
                branch="",
            )
            == "Review: auth-service"
        )


# ---- identity-required registration -------------------------------------


def test_request_review_is_identity_required():
    """request_review writes to inboxes; must be in the gate set."""
    assert "request_review" in IDENTITY_REQUIRED_TOOLS


# ---- handler integration tests (with fake Inbox) ------------------------


@dataclass
class _FakeSent:
    id: str
    to_user: str
    subject: str
    sent_at: str = "2026-05-18T16:00:00Z"


class _FakeInbox:
    """Records calls to write_message + returns canned sends."""

    def __init__(self):
        self.writes: list[dict] = []
        self.raise_value_error = False

    def write_message(self, **kwargs):
        if self.raise_value_error:
            raise ValueError("inbox rejected: target user not allowed")
        self.writes.append(kwargs)
        return _FakeSent(
            id=f"id-{len(self.writes)}",
            to_user=kwargs["to_user"],
            subject=kwargs["subject"] or "(none)",
        )


@pytest.fixture
def tool():
    return build_request_review_tool(
        inbox=_FakeInbox(),
        session_store=SimpleNamespace(),
    )


@pytest.fixture
def authd_ctx():
    return CallContext(user_id="user-A", user_email="a@example", roles=("otaman:developer",))


@pytest.fixture
def loopback_ctx():
    return CallContext(user_id="", user_email=None, roles=())


def _inbox_of(tool):
    """Reach into the closure for the fake Inbox to inspect writes."""
    # tool.handler is the closure; the fake Inbox is in __closure__[0]
    return tool.handler.__closure__[0].cell_contents


class TestRequestReviewHandlerValidation:
    def test_missing_target_user_id(self, tool, authd_ctx):
        result = tool.handler({"summary": "x"}, authd_ctx)
        assert result["isError"] is True
        assert "target_user_id" in result["content"][0]["text"]

    def test_missing_summary(self, tool, authd_ctx):
        result = tool.handler({"target_user_id": "u"}, authd_ctx)
        assert result["isError"] is True
        assert "summary" in result["content"][0]["text"]

    def test_blank_summary(self, tool, authd_ctx):
        result = tool.handler({"target_user_id": "u", "summary": "   "}, authd_ctx)
        assert result["isError"] is True

    def test_loopback_caller_rejected(self, tool, loopback_ctx):
        result = tool.handler(
            {"target_user_id": "u", "summary": "x"},
            loopback_ctx,
        )
        assert result["isError"] is True
        assert "unauthenticated" in result["content"][0]["text"].lower()

    @pytest.mark.parametrize("bad", ["urgent", "critical", "", "HIGH"])
    def test_bad_urgency_rejected(self, tool, authd_ctx, bad):
        result = tool.handler(
            {"target_user_id": "u", "summary": "x", "urgency": bad},
            authd_ctx,
        )
        assert result["isError"] is True
        assert "urgency" in result["content"][0]["text"]

    def test_checklist_must_be_array(self, tool, authd_ctx):
        result = tool.handler(
            {"target_user_id": "u", "summary": "x", "checklist": "not an array"},
            authd_ctx,
        )
        assert result["isError"] is True
        assert "array" in result["content"][0]["text"]

    def test_checklist_items_must_be_strings(self, tool, authd_ctx):
        result = tool.handler(
            {"target_user_id": "u", "summary": "x", "checklist": ["ok", 42]},
            authd_ctx,
        )
        assert result["isError"] is True

    @pytest.mark.parametrize("field", ["repo", "branch", "pr_url"])
    def test_metadata_fields_must_be_strings(self, tool, authd_ctx, field):
        result = tool.handler(
            {"target_user_id": "u", "summary": "x", field: 42},
            authd_ctx,
        )
        assert result["isError"] is True
        assert field in result["content"][0]["text"]


class TestRequestReviewHandlerHappyPath:
    def test_minimal_call(self, tool, authd_ctx):
        result = tool.handler(
            {"target_user_id": "user-B", "summary": "please review my refactor"},
            authd_ctx,
        )
        assert "isError" not in result or result.get("isError") is not True
        inbox = _inbox_of(tool)
        assert len(inbox.writes) == 1
        write = inbox.writes[0]
        assert write["from_user"] == "user-A"
        assert write["to_user"] == "user-B"
        assert write["msg_type"] == "review-request"
        assert write["priority"] == "normal"
        assert write["body"] == "please review my refactor"
        assert write["subject"] == "Review: please review my refactor"

    def test_full_call_with_all_fields(self, tool, authd_ctx):
        result = tool.handler(
            {
                "target_user_id": "user-B",
                "summary": "verify the new JWT-rotation flow",
                "repo": "auth-service",
                "branch": "wip/jwt-rotation",
                "pr_url": "https://github.com/example/auth-service/pull/42",
                "urgency": "high",
                "checklist": ["confirm role claim shape", "verify clock skew"],
            },
            authd_ctx,
        )
        assert "isError" not in result or result.get("isError") is not True
        write = _inbox_of(tool).writes[0]
        assert write["msg_type"] == "review-request"
        assert write["priority"] == "high"
        assert write["subject"] == "Review: auth-service / wip/jwt-rotation"
        body = write["body"]
        assert "verify the new JWT-rotation flow" in body
        assert "**Repo:** auth-service" in body
        assert "**Branch:** `wip/jwt-rotation`" in body
        assert "**PR / link:** https://github.com/example/auth-service/pull/42" in body
        assert "- confirm role claim shape" in body
        assert "- verify clock skew" in body

    def test_explicit_subject_override(self, tool, authd_ctx):
        tool.handler(
            {
                "target_user_id": "user-B",
                "summary": "x",
                "subject": "Custom subject line",
            },
            authd_ctx,
        )
        assert _inbox_of(tool).writes[0]["subject"] == "Custom subject line"

    def test_structured_content_response_shape(self, tool, authd_ctx):
        result = tool.handler(
            {
                "target_user_id": "user-B",
                "summary": "x",
                "urgency": "low",
            },
            authd_ctx,
        )
        sc = result["structuredContent"]
        assert sc["message_id"] == "id-1"
        assert sc["to_user"] == "user-B"
        assert sc["urgency"] == "low"
        assert sc["type"] == "review-request"

    def test_inbox_value_error_returns_isError(self, tool, authd_ctx):
        inbox = _inbox_of(tool)
        inbox.raise_value_error = True
        result = tool.handler(
            {"target_user_id": "../escape", "summary": "x"},
            authd_ctx,
        )
        assert result["isError"] is True
        assert "invalid message" in result["content"][0]["text"]


class TestToolSchema:
    def test_tool_metadata(self, tool):
        assert tool.name == "request_review"
        assert "review" in tool.description.lower()
        assert tool.input_schema["required"] == ["target_user_id", "summary"]
        props = tool.input_schema["properties"]
        for field in (
            "target_user_id",
            "summary",
            "repo",
            "branch",
            "pr_url",
            "urgency",
            "checklist",
            "subject",
        ):
            assert field in props, f"missing field {field} in schema"
        assert props["urgency"]["enum"] == ["low", "normal", "high"]
        assert props["urgency"]["default"] == "normal"
