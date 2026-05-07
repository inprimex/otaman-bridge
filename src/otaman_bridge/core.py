"""Bridge core — transport-agnostic types and Transport Protocol.

This module is the load-bearing architectural boundary (§10 of the
design doc). Nothing here may reference Telegram / Slack / Discord /
Matrix. Transport-specific code lives in ``bridge/transports/``.

**Decision verbs** (``ApprovalResponse.decision``):
    allow | deny | ask | timeout

Each transport maps these to its native UI:
- Telegram → inline buttons
- Slack → block-kit actions
- Discord → components v2
- Matrix → reactions + threads
"""

from __future__ import annotations

import datetime as _dt
import secrets as _secrets
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Literal, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Enums (as Literal types for cheap, stdlib-only validation)

Severity = Literal["info", "approval", "blocking"]
Decision = Literal["allow", "deny", "ask", "timeout"]
Action = Literal["approve", "reject", "comment", "view-diff", "snooze"]


# ---------------------------------------------------------------------------
# Dataclasses carried across the hook ↔ daemon ↔ transport boundary.


def _new_request_id() -> str:
    """Generate an opaque, unguessable request ID.

    Random hex (not incrementing counter) per §9.3 — prevents guessing
    approval callback IDs for other pending requests.
    """
    ts = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{_secrets.token_hex(4)}"


@dataclass
class ApprovalRequest:
    """A PreToolUse permission prompt awaiting human decision."""

    account: str
    project: str
    repo: str
    agent: str
    tool_name: str
    tool_input: dict[str, Any]
    reason: str = ""
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    timeout_seconds: int = 540
    request_id: str = field(default_factory=_new_request_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRequest":
        return cls(**data)


@dataclass
class ApprovalResponse:
    """A human decision on an ApprovalRequest."""

    decision: Decision
    request_id: str
    responder: str = ""  # e.g. "telegram:@roman", "cli:local"
    message: str = ""    # optional note (e.g. rejection reason)
    updated_input: dict[str, Any] | None = None  # for edit-before-allow

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if out["updated_input"] is None:
            del out["updated_input"]
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalResponse":
        return cls(**data)


@dataclass
class InfoMessage:
    """Fire-and-forget notification (no reply expected)."""

    account: str
    project: str
    severity: Severity
    title: str
    body: str = ""
    source_agent: str = ""
    bus_message_id: str = ""  # if surfaced from a bus message

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InfoMessage":
        return cls(**data)


@dataclass
class InboundReply:
    """Reply from a transport user (button tap / comment / free-text)."""

    request_id: str
    action: Action
    responder: str
    comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InboundReply":
        return cls(**data)


@dataclass
class TransportHandle:
    """Opaque handle returned by Transport.send_* for later editing.

    Shape is transport-defined (e.g. Telegram {chat_id, message_id};
    Slack {channel, ts}). Callers treat it as opaque and pass it back
    to ``Transport.update()`` unchanged.
    """

    transport: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TransportHandle":
        return cls(**data)


# ---------------------------------------------------------------------------
# Transport Protocol


@runtime_checkable
class Transport(Protocol):
    """Messenger adapter — everything transport-specific lives behind this.

    v1 implementations:
        - NullTransport (always; tests + ratchet)
        - TelegramTransport (Phase T2b)

    Post-v1:
        - SlackTransport (Phase T5)
        - DiscordTransport, MatrixTransport, WebhookTransport (post-v1)
    """

    name: str

    async def send_approval(self, req: ApprovalRequest) -> TransportHandle:
        """Surface an approval request. Return handle for later editing."""
        ...

    async def send_info(self, msg: InfoMessage) -> TransportHandle:
        """Surface a fire-and-forget notification."""
        ...

    async def update(self, handle: TransportHandle, status: str) -> None:
        """Edit a previously-sent message (e.g. "✓ approved 19:42")."""
        ...

    async def listen(self) -> AsyncIterator[InboundReply]:
        """Long-poll the messenger and yield inbound replies.

        Daemon drives this in its event loop; transport-specific errors
        become retry loops inside the transport, not panics upstream.
        """
        ...

    async def allowlist_check(self, user_id: str) -> bool:
        """Return True iff this user is allowed to reply."""
        ...


# ---------------------------------------------------------------------------
# Transport registry — lets config select `transport: telegram|slack|...`


_TRANSPORTS: dict[str, type[Transport]] = {}


def register_transport(name: str, cls: type[Transport]) -> None:
    """Register a transport implementation by name.

    ``name`` must match the ``accounts.<name>.transport`` config value.
    Calling this twice with the same name overwrites the previous
    registration (useful in tests).
    """
    if not name or not name.replace("-", "").isalnum():
        raise ValueError(f"Transport name must be alnum/-, got {name!r}")
    _TRANSPORTS[name] = cls


def get_transport(name: str) -> type[Transport]:
    """Look up a registered transport class. Raises KeyError if unknown."""
    if name not in _TRANSPORTS:
        raise KeyError(
            f"Unknown transport: {name!r}. Registered: "
            f"{sorted(_TRANSPORTS) or '(none)'}"
        )
    return _TRANSPORTS[name]


def list_transports() -> list[str]:
    """Return registered transport names, sorted."""
    return sorted(_TRANSPORTS)


def _reset_registry_for_tests() -> None:
    """Clear the registry. Tests only."""
    _TRANSPORTS.clear()
