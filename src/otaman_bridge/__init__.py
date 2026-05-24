"""Otaman remote-approval bridge.

The bridge routes PreToolUse permission prompts and bus notifications to
a messenger (Telegram, Slack, Discord, Matrix, ...) via a pluggable
``Transport`` abstraction. Nothing outside ``bridge/transports/`` may
import a transport-specific library — see ``scripts/check_transport_boundary.py``.

Phase T2a (this module's current scope): core types, daemon, NullTransport.
Phase T2b: TelegramTransport.
Phase T2c: PreToolUse hook + AFK flag.
Phase T2d: Bus surfacing + SSH auto-AFK.
"""

from otaman_bridge.core import (  # noqa: F401
    ApprovalRequest,
    ApprovalResponse,
    InboundReply,
    InfoMessage,
    Transport,
    TransportHandle,
    get_transport,
    register_transport,
)

__all__ = [
    "ApprovalRequest",
    "ApprovalResponse",
    "InboundReply",
    "InfoMessage",
    "Transport",
    "TransportHandle",
    "get_transport",
    "register_transport",
]
