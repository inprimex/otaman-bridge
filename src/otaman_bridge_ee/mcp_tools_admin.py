"""EE admin-gated wrappers around CE MCP tools.

Per the CE/EE split design (Q2 (a) decision), CE ships
``kill_session_for_user`` without a role gate — small-team mutual trust
is the CE assumption. EE wraps the same builder with
``require_role="otaman:admin"`` so only callers whose JWT roles include
``otaman:admin`` can kill another user's session.

The daemon picks the EE builder when this module is importable; CE-only
builds fall back to the un-gated CE builder.

Role naming aligns with the project's existing role hierarchy
(otaman:admin / otaman:approver / otaman:developer / otaman:viewer —
see zitadel-bootstrap.py in otaman-deploy).
"""

from __future__ import annotations

from otaman_bridge.mcp_server import Tool
from otaman_bridge.mcp_tools import build_kill_session_for_user_tool

__all__ = ["ADMIN_ROLE", "build_kill_session_for_user_tool_admin"]

ADMIN_ROLE = "otaman:admin"


def build_kill_session_for_user_tool_admin(*, runner_client) -> Tool:
    """EE variant: kill_session_for_user gated on the ``otaman:admin`` role."""
    return build_kill_session_for_user_tool(
        runner_client=runner_client,
        require_role=ADMIN_ROLE,
    )
