"""Tests for build_kill_session_for_user_tool (v0++ admin).

Two-layer auth:
- Identity (added to IDENTITY_REQUIRED_TOOLS; HTTP-layer 401 for loopback).
- Role (handler checks ctx.roles for ADMIN_ROLE; returns isError otherwise).
"""

from __future__ import annotations

import pytest

from otaman_bridge.mcp_server import CallContext
from otaman_bridge.mcp_tools import (
    ADMIN_ROLE,
    IDENTITY_REQUIRED_TOOLS,
    build_kill_session_for_user_tool,
)
from otaman_bridge.runner_client import (
    RunnerAuthError,
    RunnerUnreachableError,
    SessionNotFoundError,
)


# ---- registration / identity gate --------------------------------------


def test_kill_session_is_identity_required():
    """HTTP-layer 401 + WWW-Authenticate fires for loopback callers."""
    assert "kill_session_for_user" in IDENTITY_REQUIRED_TOOLS


def test_admin_role_constant_matches_bootstrap_roles():
    """The role we check for must match the bootstrap-created project roles
    (zitadel-bootstrap.py uses otaman:admin / :approver / :developer / :viewer)."""
    assert ADMIN_ROLE == "otaman:admin"


# ---- handler --------------------------------------------------------


class _StubRunnerClient:
    def __init__(self, *, raises=None):
        self.raises = raises
        self.calls = []

    def kill_session(self, session_id):
        self.calls.append(session_id)
        if self.raises is not None:
            raise self.raises


def _admin_ctx(user_id="admin-user", roles=(ADMIN_ROLE,)):
    return CallContext(user_id=user_id, user_email="admin@example", roles=tuple(roles))


def _dev_ctx():
    return CallContext(
        user_id="dev-user", user_email="dev@example",
        roles=("otaman:developer",),
    )


def _loopback_ctx():
    return CallContext(user_id="", user_email=None, roles=())


@pytest.fixture
def tool():
    return build_kill_session_for_user_tool(runner_client=_StubRunnerClient())


def _runner_of(tool):
    return tool.handler.__closure__[0].cell_contents


# ---- role gate ------------------------------------------------------


class TestRoleGate:
    def test_loopback_rejected(self, tool):
        result = tool.handler({"session_id": "x"}, _loopback_ctx())
        assert result["isError"] is True
        assert "identity" in result["content"][0]["text"].lower()

    def test_non_admin_rejected(self, tool):
        result = tool.handler({"session_id": "x"}, _dev_ctx())
        assert result["isError"] is True
        msg = result["content"][0]["text"]
        assert "otaman:admin" in msg
        assert "developer" in msg  # surfaces current roles for debugging

    def test_admin_proceeds(self, tool):
        result = tool.handler({"session_id": "sess-1"}, _admin_ctx())
        assert result.get("isError") is not True
        # Runner was called with the session_id.
        assert _runner_of(tool).calls == ["sess-1"]

    def test_admin_role_among_others_works(self, tool):
        """Order / additional roles shouldn't matter — just need :admin in the set."""
        ctx = _admin_ctx(roles=("otaman:developer", "otaman:approver", ADMIN_ROLE))
        result = tool.handler({"session_id": "sess-1"}, ctx)
        assert result.get("isError") is not True

    def test_empty_roles_tuple_rejected(self, tool):
        ctx = CallContext(user_id="u", user_email=None, roles=())
        result = tool.handler({"session_id": "x"}, ctx)
        assert result["isError"] is True

    def test_none_roles_treated_as_empty(self, tool):
        """Defensive: getattr fallback when roles is missing/None."""
        ctx = CallContext(user_id="u", user_email=None, roles=())
        # Force None via setattr to simulate a buggy/legacy CallContext.
        object.__setattr__(ctx, "roles", None)
        result = tool.handler({"session_id": "x"}, ctx)
        assert result["isError"] is True


# ---- input validation ----------------------------------------------


class TestValidation:
    def test_missing_session_id(self, tool):
        result = tool.handler({}, _admin_ctx())
        assert result["isError"] is True
        assert "session_id" in result["content"][0]["text"]

    @pytest.mark.parametrize("bad", ["", None, 42, [], {}])
    def test_bad_session_id_type(self, tool, bad):
        result = tool.handler({"session_id": bad}, _admin_ctx())
        assert result["isError"] is True

    def test_non_string_reason_rejected(self, tool):
        result = tool.handler(
            {"session_id": "sess-1", "reason": 42}, _admin_ctx(),
        )
        assert result["isError"] is True
        assert "reason" in result["content"][0]["text"]


# ---- happy path + runner error mapping -----------------------------


class TestHappyPath:
    def test_minimal_success(self, tool):
        result = tool.handler({"session_id": "sess-abc"}, _admin_ctx())
        assert result.get("isError") is not True
        assert "Killed session sess-abc" in result["content"][0]["text"]
        sc = result["structuredContent"]
        assert sc["session_id"] == "sess-abc"
        assert sc["killed_by"] == "admin-user"
        assert sc["reason"] is None

    def test_with_reason_surfaces_in_text(self, tool):
        result = tool.handler(
            {"session_id": "sess-1", "reason": "stuck after deploy"},
            _admin_ctx(),
        )
        assert "stuck after deploy" in result["content"][0]["text"]
        assert result["structuredContent"]["reason"] == "stuck after deploy"


class TestRunnerErrorMapping:
    def _make_tool(self, exc):
        return build_kill_session_for_user_tool(
            runner_client=_StubRunnerClient(raises=exc),
        )

    def test_session_not_found_maps_to_isError(self):
        tool = self._make_tool(SessionNotFoundError("sess-gone"))
        result = tool.handler({"session_id": "sess-gone"}, _admin_ctx())
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert "session not found" in text.lower()
        assert "sess-gone" in text

    def test_runner_auth_error_maps_to_isError(self):
        tool = self._make_tool(RunnerAuthError("stale token"))
        result = tool.handler({"session_id": "sess-1"}, _admin_ctx())
        assert result["isError"] is True
        assert "runner auth failed" in result["content"][0]["text"]

    def test_runner_unreachable_maps_to_isError(self):
        tool = self._make_tool(RunnerUnreachableError("connection refused"))
        result = tool.handler({"session_id": "sess-1"}, _admin_ctx())
        assert result["isError"] is True
        assert "runner unreachable" in result["content"][0]["text"]

    def test_value_error_maps_to_isError(self):
        tool = self._make_tool(ValueError("bad session_id"))
        result = tool.handler({"session_id": "x"}, _admin_ctx())
        assert result["isError"] is True
        assert "invalid session_id" in result["content"][0]["text"]


# ---- tool schema ----------------------------------------------------


class TestSchema:
    def test_metadata(self, tool):
        assert tool.name == "kill_session_for_user"
        assert "admin" in tool.description.lower()
        assert tool.input_schema["required"] == ["session_id"]
        props = tool.input_schema["properties"]
        assert "session_id" in props and props["session_id"]["type"] == "string"
        assert "reason" in props
