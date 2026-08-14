"""CE-mode tests for build_kill_session_for_user_tool — no role gate.

Per the CE/EE split design (Q2 (a) decision), CE's builder is un-gated.
Identity is still required (the tool is in IDENTITY_REQUIRED_TOOLS so
loopback-bearer callers hit a 401 at the HTTP layer), but the handler
itself doesn't enforce ``otaman:admin`` — small-team mutual trust is
the CE assumption.

The EE-gated variant is tested in test_kill_session_for_user_tool.py
via the otaman_bridge_ee.mcp_tools_admin wrapper.
"""

from __future__ import annotations

from otaman_bridge.mcp_server import CallContext
from otaman_bridge.mcp_tools import build_kill_session_for_user_tool


class _StubRunnerClient:
    def __init__(self, *, raises=None):
        self.calls = []
        self._raises = raises

    def kill_session(self, session_id: str) -> None:
        self.calls.append(session_id)
        if self._raises is not None:
            raise self._raises


def _ce_tool():
    return build_kill_session_for_user_tool(runner_client=_StubRunnerClient())


class TestCEBuilderUnGated:
    """CE's builder skips the role check by default — Q2 (a) decision."""

    def test_caller_with_no_roles_can_kill(self):
        tool = build_kill_session_for_user_tool(runner_client=_StubRunnerClient())
        ctx = CallContext(user_id="alice", user_email=None, roles=())
        result = tool.handler({"session_id": "550e8400-e29b-41d4-a716-446655440000"}, ctx)
        assert "isError" not in result
        assert "structuredContent" in result
        assert result["structuredContent"]["killed_by"] == "alice"

    def test_loopback_caller_rejected_via_identity_check(self):
        # Even un-gated, the identity check still fires (loopback bearer
        # has empty user_id, which is the defensive guard inside the
        # handler).
        tool = build_kill_session_for_user_tool(runner_client=_StubRunnerClient())
        ctx = CallContext(user_id="", user_email=None, roles=())
        result = tool.handler({"session_id": "abc"}, ctx)
        assert result.get("isError") is True


class TestExplicitRequireRole:
    """When require_role is passed in, the gate kicks in even on the CE builder.

    EE's wrapper uses this — sanity check that CE's builder honors the
    parameter when set.
    """

    def test_explicit_role_required_rejects_without_it(self):
        tool = build_kill_session_for_user_tool(
            runner_client=_StubRunnerClient(),
            require_role="otaman:admin",
        )
        ctx = CallContext(user_id="alice", user_email=None, roles=("otaman:developer",))
        result = tool.handler({"session_id": "550e8400-e29b-41d4-a716-446655440000"}, ctx)
        assert result.get("isError") is True
        assert "otaman:admin" in result["content"][0]["text"]

    def test_explicit_role_required_allows_when_present(self):
        tool = build_kill_session_for_user_tool(
            runner_client=_StubRunnerClient(),
            require_role="otaman:admin",
        )
        ctx = CallContext(user_id="alice", user_email=None, roles=("otaman:admin",))
        result = tool.handler({"session_id": "550e8400-e29b-41d4-a716-446655440000"}, ctx)
        assert "isError" not in result
