"""NullTransport — logs instead of sending.

Ships in v1 as a ratchet against transport abstraction leaks: if a
feature can't be satisfied with NullTransport, the daemon/core is
reaching past the Transport contract and the PR is blocked until the
offending code moves into a transport module.

NullTransport is also the default for tests — no network, no bot, just
an in-memory log that assertions can inspect.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from otaman_bridge.core import (
    ApprovalRequest,
    InboundReply,
    InfoMessage,
    TransportHandle,
    register_transport,
)

_log = logging.getLogger("maestro.bridge.null")  # legacy: logger renamed at otaman-core 1.0


@dataclass
class NullTransport:
    """Log-only transport; emits no outbound traffic, receives no inbound.

    Attributes:
        name: Transport name (``"null"``).
        sent_approvals: Every ApprovalRequest passed to ``send_approval``.
        sent_infos: Every InfoMessage passed to ``send_info``.
        updates: Every (handle, status) pair passed to ``update``.
        inbound_queue: Tests can push InboundReplies here; ``listen()``
            consumes them. Mimics a real transport's inbound stream
            without actually polling anything.
        allowlist: Users whose ID passes ``allowlist_check``. Empty set
            means *reject all* (defensive default); pass ``{"*"}`` to
            allow everyone.
    """

    name: str = "null"
    sent_approvals: list[ApprovalRequest] = field(default_factory=list)
    sent_infos: list[InfoMessage] = field(default_factory=list)
    updates: list[tuple[TransportHandle, str]] = field(default_factory=list)
    inbound_queue: asyncio.Queue[InboundReply] = field(default_factory=asyncio.Queue)
    allowlist: set[str] = field(default_factory=set)
    # The asyncio loop that's currently iterating listen() — captured lazily
    # so ``push_reply`` from another thread can dispatch onto it via
    # ``run_coroutine_threadsafe``. ``asyncio.Queue`` is not thread-safe.
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)

    async def send_approval(self, req: ApprovalRequest) -> TransportHandle:
        self.sent_approvals.append(req)
        _log.info(
            "null: send_approval request_id=%s tool=%s reason=%s",
            req.request_id,
            req.tool_name,
            req.reason or "(none)",
        )
        return TransportHandle(
            transport=self.name,
            data={"request_id": req.request_id, "seq": len(self.sent_approvals)},
        )

    async def send_info(self, msg: InfoMessage) -> TransportHandle:
        self.sent_infos.append(msg)
        _log.info(
            "null: send_info severity=%s title=%s",
            msg.severity,
            msg.title,
        )
        return TransportHandle(
            transport=self.name,
            data={"seq": len(self.sent_infos)},
        )

    async def update(self, handle: TransportHandle, status: str) -> None:
        self.updates.append((handle, status))
        _log.info("null: update handle=%r status=%s", handle.data, status)

    async def listen(self) -> AsyncIterator[InboundReply]:
        """Yield replies from the in-memory queue."""
        self._loop = asyncio.get_running_loop()
        try:
            while True:
                reply = await self.inbound_queue.get()
                yield reply
        finally:
            self._loop = None

    async def allowlist_check(self, user_id: str) -> bool:
        if "*" in self.allowlist:
            return True
        return user_id in self.allowlist

    # --- test helpers -----------------------------------------------------

    def push_reply(self, reply: InboundReply) -> None:
        """Enqueue a reply for ``listen()`` to yield. Test-only.

        If ``listen()`` is currently running on another thread's event loop,
        dispatches the put onto that loop via ``run_coroutine_threadsafe``
        so the waiting ``get()`` is actually woken. If ``listen()`` hasn't
        been started yet, falls back to a direct (same-thread) put_nowait.
        """
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.inbound_queue.put(reply),
                self._loop,
            )
        else:
            self.inbound_queue.put_nowait(reply)

    def reset(self) -> None:
        """Clear all recorded state. Test-only."""
        self.sent_approvals.clear()
        self.sent_infos.clear()
        self.updates.clear()
        while not self.inbound_queue.empty():
            try:
                self.inbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break


register_transport("null", NullTransport)
