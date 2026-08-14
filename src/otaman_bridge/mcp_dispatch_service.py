"""Owns the MCP tool registry, the runner client, and the inbox.

Extracted out of ``BridgeDaemon`` (F040, phase 5 of the god-object
decomposition — see the bridge-agent/spec-agent bus thread on
2026-07-03; PRs #33/#34/#35/#36 for phases 1-4). Builds the
``MCPServer`` instance, registers every MCP tool (team-visibility,
messaging, admin kill-session), and owns the ``RunnerClient`` +
``Inbox`` those tools depend on.

``mcp_server``, ``_runner_client``, and ``inbox`` stay frozen
forwarding properties on ``BridgeDaemon`` — a couple of tests reach
into them directly (``daemon._runner_client = stub``, `daemon.inbox`,
``daemon.mcp_server.register(...)``), and the HTTP handler dispatches
MCP requests via ``daemon.mcp_server.handle_request(...)``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_log = logging.getLogger("maestro.bridge.mcp_dispatch_service")  # legacy: renamed at core 1.0


class McpDispatchService:
    """MCP tool registry + its dependencies (RunnerClient, Inbox).

    ``session_store`` is read once at construction time (same timing
    as the pre-extraction code): the tool builders capture whatever
    the auth stack's session_store is at that point in ``BridgeDaemon``
    ``__init__`` — before or after doesn't matter here since nothing
    else mutates it between AuthStack's construction and this one.
    """

    def __init__(self, *, session_store) -> None:
        from otaman_bridge.inbox import Inbox
        from otaman_bridge.mcp_server import MCPServer
        from otaman_bridge.mcp_tools import (
            PRIVACY_EMAILS,
            PRIVACY_OPAQUE,
            build_check_messages_tool,
            build_get_recent_activity_tool,
            build_kill_session_for_user_tool,
            build_list_team_sessions_tool,
            build_mark_message_read_tool,
            build_request_review_tool,
            build_send_message_to_user_tool,
        )
        from otaman_bridge.runner_client import RunnerClient

        self.mcp_server = MCPServer()
        self._runner_client = RunnerClient()

        privacy = os.environ.get("OTAMAN_BRIDGE_PRIVACY_MODE", PRIVACY_EMAILS).strip()
        if privacy not in (PRIVACY_EMAILS, PRIVACY_OPAQUE):
            _log.warning(
                "invalid OTAMAN_BRIDGE_PRIVACY_MODE=%r, using emails",
                privacy,
            )
            privacy = PRIVACY_EMAILS
        # Only register list_team_sessions when session_store exists --
        # the tool's email lookup depends on it. If web auth is
        # disabled, the tool falls back to opaque (no email source).
        if session_store is not None:
            self.mcp_server.register(
                build_list_team_sessions_tool(
                    runner_client=self._runner_client,
                    session_store=session_store,
                    privacy_mode=privacy,
                )
            )
            _log.info("MCP: list_team_sessions registered (privacy=%s)", privacy)

        # Messaging tools (v0+): send_message_to_user / check_messages /
        # mark_message_read. Inbox storage under ~/.otaman/inboxes/ by
        # default; override via OTAMAN_BRIDGE_INBOX_ROOT env var. These
        # tools work without web auth (they read ctx.user_id from any
        # of the three auth paths; loopback bearer is rejected at handler).
        inbox_root = os.environ.get("OTAMAN_BRIDGE_INBOX_ROOT", "").strip()
        self.inbox = Inbox(root=Path(inbox_root)) if inbox_root else Inbox()
        self.mcp_server.register(
            build_send_message_to_user_tool(
                inbox=self.inbox,
                session_store=session_store,
            )
        )
        self.mcp_server.register(build_check_messages_tool(inbox=self.inbox))
        self.mcp_server.register(build_mark_message_read_tool(inbox=self.inbox))
        self.mcp_server.register(
            build_request_review_tool(
                inbox=self.inbox,
                session_store=session_store,
            )
        )
        self.mcp_server.register(
            build_get_recent_activity_tool(
                inbox=self.inbox,
                runner_client=self._runner_client,
            )
        )
        # Pick the admin-gated EE builder when EE is installed; else CE's
        # un-gated builder (Q2 (a) decision: CE = mutual-trust small team).
        try:
            from otaman_bridge_ee.mcp_tools_admin import (
                build_kill_session_for_user_tool_admin as _build_kill_session,
            )
        except ImportError:
            _build_kill_session = build_kill_session_for_user_tool
        self.mcp_server.register(
            _build_kill_session(
                runner_client=self._runner_client,
            )
        )
        _log.info("MCP: messaging tools registered (inbox=%s)", self.inbox.root)
