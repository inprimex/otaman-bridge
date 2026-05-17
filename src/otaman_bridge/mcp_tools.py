"""MCP tool implementations exposed by the bridge.

Currently just ``list_team_sessions`` (team-mode v0). Future tools live
here too: kill_session, get_session_log, send_message_to_user, etc.

Each builder function returns a ``Tool`` ready to be registered on an
``MCPServer``. The builder closes over the dependencies (RunnerClient,
SessionStore, etc.) so the handler can be a plain callable matching
the MCP spec's tool handler shape.
"""

from __future__ import annotations

import logging
from typing import Callable

from otaman_bridge.mcp_server import CallContext, Tool
from otaman_bridge.runner_client import (
    RunnerAuthError,
    RunnerClient,
    RunnerUnreachableError,
)
from otaman_bridge.web_session import SessionStore

_log = logging.getLogger("otaman.bridge.mcp.tools")

# Privacy mode for surfacing other users' identities to a calling user.
PRIVACY_EMAILS = "emails"     # default; useful for small trusted teams
PRIVACY_OPAQUE = "opaque"     # strip user_email, just return user_id


def build_list_team_sessions_tool(
    *,
    runner_client: RunnerClient,
    session_store: SessionStore,
    privacy_mode: str = PRIVACY_EMAILS,
) -> Tool:
    """Build the ``list_team_sessions`` MCP tool.

    The tool lists active otaman sessions across all users on this
    bridge. By default excludes the caller's own sessions (most useful
    for "what is my team doing"); pass ``include_self=true`` to include
    them.

    Per the team-mode v0 design, privacy_mode controls what's exposed
    about other users:

    - "emails" (default): include user_email so the LLM can present
      "User B (dev-b@example) is on auth-service". Use for trusted
      small teams (Greenbin pilot).
    - "opaque": just user_id, no email. Use when teammates' identities
      shouldn't leak even within the bridge.
    """
    if privacy_mode not in (PRIVACY_EMAILS, PRIVACY_OPAQUE):
        raise ValueError(f"invalid privacy_mode: {privacy_mode!r}")

    def handler(args: dict, ctx: CallContext) -> dict:
        include_self = bool(args.get("include_self", False))
        try:
            raw_sessions = runner_client.list_sessions()
        except RunnerUnreachableError as exc:
            # Per v0 design: explicit error -> LLM says "list unavailable"
            # rather than "no sessions" (would mask the real problem).
            _log.warning("list_team_sessions: runner unreachable: %s", exc)
            return {
                "isError": True,
                "content": [{"type": "text", "text": (
                    "Team session list is unavailable: the runner is not "
                    f"reachable from this bridge ({exc}). This is not the "
                    "same as 'no team sessions' -- it means we can't tell."
                )}],
            }
        except RunnerAuthError as exc:
            _log.warning("list_team_sessions: runner auth failed: %s", exc)
            return {
                "isError": True,
                "content": [{"type": "text", "text": (
                    "Team session list is unavailable: the bridge's loopback "
                    f"token for the runner is stale ({exc}). The runner may "
                    "have restarted; the bridge needs to re-read the endpoint "
                    "file."
                )}],
            }

        sessions_out = []
        for s in raw_sessions:
            user_id = s.get("user") or ""
            is_self = (user_id == ctx.user_id)
            if is_self and not include_self:
                continue
            entry = {
                "session_id": s.get("session_id"),
                "user_id": user_id,
                "repo": s.get("repo"),
                "agent": s.get("agent"),
                "session_name": s.get("session_name"),
                "started_at": s.get("started_at"),
                "is_self": is_self,
            }
            if privacy_mode == PRIVACY_EMAILS:
                entry["user_email"] = _lookup_user_email(session_store, user_id)
            sessions_out.append(entry)

        return {
            "content": [{"type": "text", "text": _format_sessions(sessions_out)}],
            "structuredContent": {
                "sessions": sessions_out,
                "total": len(sessions_out),
            },
        }

    return Tool(
        name="list_team_sessions",
        description=(
            "List active otaman sessions across all users on this bridge. "
            "Each session represents a Claude Code instance another user "
            "has spawned. By default excludes your own sessions; pass "
            "include_self=true to include them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_self": {
                    "type": "boolean",
                    "description": (
                        "Include sessions belonging to the calling user. "
                        "Default false -- you usually want OTHERS' sessions."
                    ),
                    "default": False,
                },
            },
        },
        handler=handler,
    )


# ---- helpers ---------------------------------------------------------


def _lookup_user_email(session_store: SessionStore, user_id: str):
    """Find the email for a Zitadel sub by scanning active bridge sessions.

    Returns None if no live session for that user is found. This is the
    only way we know emails -- runner stores just the sub claim.
    """
    if not user_id:
        return None
    try:
        with session_store._lock:  # noqa: SLF001 -- read-only scan
            for sess in session_store._sessions.values():  # noqa: SLF001
                if sess.user_id == user_id:
                    return sess.email
    except AttributeError:
        # Defensive: if SessionStore internals change, fall back to nothing.
        return None
    return None


def _format_sessions(entries: list[dict]) -> str:
    """Human-readable text for the LLM to use without parsing structured content."""
    if not entries:
        return "No active team sessions."
    lines = [f"{len(entries)} active team session(s):"]
    for e in entries:
        who = e.get("user_email") or e.get("user_id") or "(unknown)"
        repo = e.get("repo") or "(no repo)"
        started = e.get("started_at") or "?"
        suffix = " [self]" if e.get("is_self") else ""
        lines.append(f"  - {who}: {repo}, started {started}{suffix}")
    return "\n".join(lines)


__all__ = [
    "PRIVACY_EMAILS",
    "PRIVACY_OPAQUE",
    "build_list_team_sessions_tool",
]


from otaman_bridge.inbox import Inbox


def build_send_message_to_user_tool(
    *,
    inbox: Inbox,
    session_store: SessionStore,
) -> Tool:
    """Build the send_message_to_user MCP tool.

    Writes a message to the recipient's per-user inbox. Sender identity
    comes from the MCP CallContext (auth boundary); from_email is
    denormalized from session_store at send time so the recipient sees
    a readable name without needing their own session lookup.

    Per v0+ design (decision Q&A): unknown recipient (no live session,
    no email known) is accepted -- we skip the from_email field rather
    than reject. Recipient sees from_user_id only.
    """

    def handler(args: dict, ctx: CallContext) -> dict:
        target_user_id = args.get("target_user_id")
        body = args.get("body")
        if not target_user_id or not isinstance(target_user_id, str):
            return _mcp_error("missing or invalid target_user_id")
        if not body or not isinstance(body, str) or not body.strip():
            return _mcp_error("missing or empty body")
        if not ctx.user_id:
            return _mcp_error(
                "sender identity required but call is unauthenticated"
                " (loopback-bearer calls have no user identity)"
            )
        subject = args.get("subject")
        in_reply_to = args.get("in_reply_to")
        priority = args.get("priority", "normal")
        msg_type = args.get("type", "chat")

        try:
            sent = inbox.write_message(
                from_user=ctx.user_id,
                from_email=ctx.user_email,   # may be None; that's fine
                to_user=target_user_id,
                subject=subject,
                body=body,
                in_reply_to=in_reply_to,
                priority=priority,
                msg_type=msg_type,
            )
        except ValueError as exc:
            return _mcp_error(f"invalid message: {exc}")

        return {
            "content": [{"type": "text", "text": (
                f"Sent message to {target_user_id} "
                f"(subject: {sent.subject!r}, id: {sent.id})."
            )}],
            "structuredContent": {
                "message_id": sent.id,
                "to_user": sent.to_user,
                "subject": sent.subject,
                "sent_at": sent.sent_at,
            },
        }

    return Tool(
        name="send_message_to_user",
        description=(
            "Send a message to another otaman team member by user_id. "
            "The recipient sees it in their inbox via check_messages. "
            "Use list_team_sessions first to find their user_id."
        ),
        input_schema={
            "type": "object",
            "required": ["target_user_id", "body"],
            "properties": {
                "target_user_id": {
                    "type": "string",
                    "description": "Zitadel sub of the recipient (from list_team_sessions)",
                },
                "body": {
                    "type": "string",
                    "description": "Message body (markdown OK)",
                },
                "subject": {
                    "type": "string",
                    "description": "Optional one-line subject. Default: derived from body's first line, max 80 chars.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "default": "normal",
                },
                "in_reply_to": {
                    "type": "string",
                    "description": "Optional: message id this replies to.",
                },
                "type": {
                    "type": "string",
                    "enum": ["chat", "review-request", "task-handoff", "approval-request"],
                    "default": "chat",
                    "description": "Message category. Default chat; richer types are for tools that wrap send_message_to_user.",
                },
            },
        },
        handler=handler,
    )


def _mcp_error(message: str) -> dict:
    """Build a CallToolResult-style error result for MCP."""
    return {
        "isError": True,
        "content": [{"type": "text", "text": message}],
    }


__all__.extend(["build_send_message_to_user_tool"])
