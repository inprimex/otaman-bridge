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


# Tools that require an authenticated user identity (ctx.user_id non-empty).
# Loopback-bearer callers (same-host CLI introspection) have ctx.user_id=""
# and would receive a tool-level isError. The /mcp route uses this set to
# short-circuit with HTTP 401 + WWW-Authenticate instead, so MCP clients
# (Claude Code) initiate their OAuth dance against the issuer named in
# /.well-known/oauth-protected-resource and retry with a real OIDC bearer.
#
# Identity-less tools (e.g. list_team_sessions) are deliberately omitted:
# they're safe for CLI introspection and there's no user attribution to
# enforce.
#
# TODO: lift this into Tool dataclass metadata so tools self-describe
# their auth requirements instead of being listed by name in two places.
IDENTITY_REQUIRED_TOOLS: frozenset[str] = frozenset({
    "send_message_to_user",
    "check_messages",
    "mark_message_read",
    "request_review",
})


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
    "IDENTITY_REQUIRED_TOOLS",
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


def _compose_review_body(
    *,
    summary: str,
    repo: str | None,
    branch: str | None,
    pr_url: str | None,
    checklist: list[str] | None,
) -> str:
    """Build the structured markdown body for a review request.

    Fields are emitted only when provided — clean output even with
    minimal inputs (summary alone). Kept as a pure function for tests.
    """
    parts: list[str] = [summary.strip()]
    meta_lines: list[str] = []
    if repo:
        meta_lines.append(f"**Repo:** {repo}")
    if branch:
        meta_lines.append(f"**Branch:** `{branch}`")
    if pr_url:
        meta_lines.append(f"**PR / link:** {pr_url}")
    if meta_lines:
        parts.append("\n".join(meta_lines))
    if checklist:
        clean = [item.strip() for item in checklist if item and item.strip()]
        if clean:
            parts.append(
                "**Please check:**\n" + "\n".join(f"- {item}" for item in clean)
            )
    return "\n\n".join(parts)


def _compose_review_subject(
    *,
    summary: str,
    repo: str | None,
    branch: str | None,
) -> str:
    """Auto-generate a subject line from inputs.

    Priority: repo+branch > repo > first 60 chars of summary.
    """
    if repo and branch:
        return f"Review: {repo} / {branch}"
    if repo:
        return f"Review: {repo}"
    head = summary.strip().splitlines()[0] if summary.strip() else "request"
    if len(head) > 60:
        head = head[:57].rstrip() + "..."
    return f"Review: {head}"


def build_request_review_tool(
    *,
    inbox: Inbox,
    session_store: SessionStore,
) -> Tool:
    """Build the request_review MCP tool.

    Higher-level wrapper around inbox.write_message that emits a
    structured review request: takes (target_user_id, summary) plus
    optional repo / branch / pr_url / urgency / checklist, composes a
    well-formatted markdown body, auto-generates a subject, and writes
    the message with type=review-request so the recipient's
    check_messages output can distinguish it visually.

    Same identity model as send_message_to_user — requires a real
    OIDC bearer (ctx.user_id non-empty); loopback bearer is gated at
    the HTTP layer via IDENTITY_REQUIRED_TOOLS.
    """

    def handler(args: dict, ctx: CallContext) -> dict:
        target_user_id = args.get("target_user_id")
        summary = args.get("summary")
        if not target_user_id or not isinstance(target_user_id, str):
            return _mcp_error("missing or invalid target_user_id")
        if not summary or not isinstance(summary, str) or not summary.strip():
            return _mcp_error("missing or empty summary")
        if not ctx.user_id:
            return _mcp_error(
                "sender identity required but call is unauthenticated"
                " (loopback-bearer calls have no user identity)"
            )

        repo = args.get("repo")
        branch = args.get("branch")
        pr_url = args.get("pr_url")
        urgency = args.get("urgency", "normal")
        if urgency not in ("low", "normal", "high"):
            return _mcp_error(
                f"invalid urgency {urgency!r}: must be low / normal / high"
            )
        checklist = args.get("checklist")
        if checklist is not None and not isinstance(checklist, list):
            return _mcp_error("checklist must be an array of strings")
        if isinstance(checklist, list):
            for item in checklist:
                if not isinstance(item, str):
                    return _mcp_error("checklist must be an array of strings")

        for field_name, value in (
            ("repo", repo), ("branch", branch), ("pr_url", pr_url),
        ):
            if value is not None and not isinstance(value, str):
                return _mcp_error(f"{field_name} must be a string")

        body = _compose_review_body(
            summary=summary, repo=repo, branch=branch, pr_url=pr_url,
            checklist=checklist,
        )
        subject = args.get("subject") or _compose_review_subject(
            summary=summary, repo=repo, branch=branch,
        )

        try:
            sent = inbox.write_message(
                from_user=ctx.user_id,
                from_email=ctx.user_email,
                to_user=target_user_id,
                subject=subject,
                body=body,
                in_reply_to=None,
                priority=urgency,
                msg_type="review-request",
            )
        except ValueError as exc:
            return _mcp_error(f"invalid message: {exc}")

        return {
            "content": [{"type": "text", "text": (
                f"Review request sent to {target_user_id} "
                f"(subject: {sent.subject!r}, id: {sent.id})."
            )}],
            "structuredContent": {
                "message_id": sent.id,
                "to_user": sent.to_user,
                "subject": sent.subject,
                "sent_at": sent.sent_at,
                "urgency": urgency,
                "type": "review-request",
            },
        }

    return Tool(
        name="request_review",
        description=(
            "Ask a teammate to review your code. Composes a structured "
            "review-request message with repo / branch / PR / checklist "
            "context. Use list_team_sessions first to find the reviewer's "
            "user_id. The recipient sees it via check_messages tagged as "
            "type=review-request."
        ),
        input_schema={
            "type": "object",
            "required": ["target_user_id", "summary"],
            "properties": {
                "target_user_id": {
                    "type": "string",
                    "description": "Zitadel sub of the reviewer (from list_team_sessions)",
                },
                "summary": {
                    "type": "string",
                    "description": "What you want reviewed and why (markdown OK)",
                },
                "repo": {
                    "type": "string",
                    "description": "Optional: repo name (e.g. 'auth-service')",
                },
                "branch": {
                    "type": "string",
                    "description": "Optional: branch name (e.g. 'wip/jwt-rotation')",
                },
                "pr_url": {
                    "type": "string",
                    "description": "Optional: PR / MR URL or other link to the code",
                },
                "urgency": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "default": "normal",
                    "description": "Maps to message priority.",
                },
                "checklist": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: specific items the reviewer should check.",
                },
                "subject": {
                    "type": "string",
                    "description": "Optional: override the auto-generated subject line.",
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


def build_check_messages_tool(*, inbox: Inbox) -> Tool:
    """Build the check_messages MCP tool.

    Reads the calling user's inbox. By default returns unread only;
    `unread_only=false` returns everything (subject to limit). Reading
    via this tool does NOT mark messages read -- that's a separate
    explicit mark_message_read call (so "I looked but didn't ack" is
    possible).
    """

    def handler(args: dict, ctx: CallContext) -> dict:
        if not ctx.user_id:
            return _mcp_error(
                "caller identity required (loopback bearer has no user identity)"
            )
        unread_only = bool(args.get("unread_only", True))
        from_user = args.get("from_user")
        since = args.get("since")
        limit = args.get("limit", 50)
        if not isinstance(limit, int):
            return _mcp_error(f"limit must be int, got {type(limit).__name__}")
        try:
            messages = inbox.list_messages(
                ctx.user_id,
                unread_only=unread_only,
                from_user=from_user,
                since=since,
                limit=limit,
            )
        except ValueError as exc:
            return _mcp_error(str(exc))

        out = [
            {
                "message_id": m.id,
                "from_user": m.from_user,
                "from_email": m.from_email,
                "subject": m.subject,
                "body": m.body,
                "sent_at": m.sent_at,
                "read_at": m.read_at,
                "in_reply_to": m.in_reply_to,
                "priority": m.priority,
                "type": m.type,
            }
            for m in messages
        ]
        text = _format_messages_text(out, unread_only=unread_only)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {
                "messages": out,
                "total": len(out),
                "oldest_unread_at": (
                    next((m["sent_at"] for m in reversed(out) if m["read_at"] is None), None)
                ),
            },
        }

    return Tool(
        name="check_messages",
        description=(
            "List messages in the caller's inbox. By default returns "
            "unread only. Does NOT mark messages read -- use "
            "mark_message_read for that. Filter by from_user / since / "
            "limit. Newest first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "unread_only": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true (default), return only unread messages.",
                },
                "from_user": {
                    "type": "string",
                    "description": "Filter to messages from this user_id.",
                },
                "since": {
                    "type": "string",
                    "description": "ISO timestamp; return messages sent strictly after this.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
        },
        handler=handler,
    )


def _format_messages_text(messages: list[dict], *, unread_only: bool) -> str:
    if not messages:
        return "No unread messages." if unread_only else "No messages in inbox."
    label = "unread" if unread_only else "total"
    lines = [f"{len(messages)} {label} message(s):"]
    for m in messages:
        who = m.get("from_email") or m.get("from_user") or "(unknown)"
        sent = m.get("sent_at") or "?"
        subject = m.get("subject") or "(no subject)"
        prio = m.get("priority") or "normal"
        prio_tag = "" if prio == "normal" else f" [{prio}]"
        lines.append(f"  - {sent} from {who}{prio_tag}: {subject}")
    return "\n".join(lines)


__all__.extend(["build_check_messages_tool"])


def build_mark_message_read_tool(*, inbox: Inbox) -> Tool:
    """Build the mark_message_read MCP tool.

    Marks a single message read by id. With mark_all_before=true also
    marks all unread messages with sent_at <= the target message's
    sent_at -- useful for "I caught up to here". Idempotent: marking
    an already-read message returns 0 (no change).
    """

    def handler(args: dict, ctx: CallContext) -> dict:
        if not ctx.user_id:
            return _mcp_error(
                "caller identity required (loopback bearer has no user identity)"
            )
        message_id = args.get("message_id")
        if not message_id or not isinstance(message_id, str):
            return _mcp_error("missing or invalid message_id")
        mark_all_before = bool(args.get("mark_all_before", False))

        try:
            count = inbox.mark_read(
                ctx.user_id, message_id, mark_all_before=mark_all_before,
            )
        except Exception as exc:
            return _mcp_error(f"mark_read failed: {exc}")

        if count == 0:
            text = (
                f"No change: message {message_id!r} is already read"
                " or does not exist."
            )
        elif mark_all_before:
            text = f"Marked {count} message(s) read (up through {message_id})."
        else:
            text = f"Marked message {message_id!r} read."

        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"marked": count, "message_id": message_id},
        }

    return Tool(
        name="mark_message_read",
        description=(
            "Mark a message as read in the caller's inbox. Idempotent. "
            "Optional mark_all_before to also mark older unread messages "
            "as read in one shot."
        ),
        input_schema={
            "type": "object",
            "required": ["message_id"],
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Id of the message to mark read.",
                },
                "mark_all_before": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, also mark all unread messages with "
                        "sent_at strictly older or equal to this one as read."
                    ),
                },
            },
        },
        handler=handler,
    )


__all__.extend(["build_mark_message_read_tool"])
