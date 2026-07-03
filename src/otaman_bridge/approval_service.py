"""Owns the in-flight tool-approval table.

Extracted out of ``BridgeDaemon`` (F040, phase 1 of the god-object
decomposition — see the bridge-agent/spec-agent bus thread on 2026-07-03).
This is the "hook is blocked waiting for a human tap" side of approvals.
The bus spec-change-request side (``_pending_bus`` /
``_PendingBusDecision``) is a separate table with its own lock — nothing
in this module needs the two to be atomic with each other, so splitting
the lock is safe. ``BridgeDaemon._surface_details`` and
``BridgeDaemon._dispatch_inbound_reply`` are the only callers that touch
both tables, and they do so as two independent lookups, not one.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from otaman_bridge.core import ApprovalRequest, ApprovalResponse, TransportHandle

if TYPE_CHECKING:
    from otaman_bridge.core import Transport
    from otaman_bridge.daemon import _AsyncLoopThread

_log = logging.getLogger("maestro.bridge.approval_service")


class _PendingApproval:
    """Thread-safe slot for a waiting hook.

    The deadline is a monotonic timestamp rather than the raw duration
    passed into ``wait()`` so that Snooze can push it out while the
    hook is still blocked in ``wait()``. The original ``timeout`` arg
    is retained for backwards compatibility but ignored in favor of
    ``_deadline``.
    """

    __slots__ = ("event", "response", "request", "handle", "_deadline")

    def __init__(self, request: ApprovalRequest):
        self.event = threading.Event()
        self.response: ApprovalResponse | None = None
        self.request = request
        # TransportHandle returned by Transport.send_approval — stored so
        # inbound replies can edit the original message after the decision.
        self.handle: TransportHandle | None = None
        self._deadline = time.monotonic() + request.timeout_seconds

    def resolve(self, response: ApprovalResponse) -> None:
        self.response = response
        self.event.set()

    def extend_by(self, seconds: float) -> None:
        """Push the deadline to at least ``now + seconds`` (never shortens it)."""
        new_deadline = time.monotonic() + seconds
        if new_deadline > self._deadline:
            self._deadline = new_deadline

    def wait(self, timeout: float) -> ApprovalResponse:  # noqa: ARG002
        """Block until resolved or the deadline passes.

        Polls in small chunks (≤5s) so deadline extensions made by
        Snooze after ``wait()`` starts are picked up.
        """
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return ApprovalResponse(
                    decision="timeout",
                    request_id=self.request.request_id,
                )
            if self.event.wait(timeout=min(remaining, 5.0)):
                assert self.response is not None
                return self.response


class ApprovalService:
    """Registry of tool-call approvals awaiting a hook's blocking wait.

    Owns ``_pending`` and its lock. ``BridgeDaemon`` holds one instance
    and delegates the ``/approval`` route plus the approve/reject/snooze
    reply-dispatch paths to it.
    """

    def __init__(self, *, transport: Transport, async_loop: _AsyncLoopThread) -> None:
        self.transport = transport
        self._async = async_loop
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = threading.Lock()

    def get(self, request_id: str) -> _PendingApproval | None:
        with self._lock:
            return self._pending.get(request_id)

    def count(self) -> int:
        with self._lock:
            return len(self._pending)

    def resolve(self, request_id: str, response: ApprovalResponse) -> bool:
        """Resolve a pending approval by request_id. False if none was pending."""
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return False
        pending.resolve(response)
        return True

    def cancel_all(self, response_factory: Callable[[str], ApprovalResponse]) -> None:
        """Resolve every pending approval (daemon shutdown) and clear the table."""
        with self._lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for pending in pendings:
            pending.resolve(response_factory(pending.request.request_id))

    def handle_approval(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            req = ApprovalRequest.from_dict(body)
        except (TypeError, ValueError) as e:
            return 400, {"error": f"invalid ApprovalRequest: {e}"}

        pending = _PendingApproval(req)
        with self._lock:
            self._pending[req.request_id] = pending

        # Schedule the transport's send_approval on the async loop and
        # capture the TransportHandle for later update() calls.
        try:
            fut = self._async.submit(self.transport.send_approval(req))
            handle = fut.result(timeout=10.0)
            if isinstance(handle, TransportHandle):
                pending.handle = handle
        except Exception as e:
            with self._lock:
                self._pending.pop(req.request_id, None)
            _log.exception("transport.send_approval failed")
            # Fail-safe: return "ask" so the native terminal prompt takes over.
            return 200, ApprovalResponse(
                decision="ask",
                request_id=req.request_id,
                responder="daemon:send-failed",
                message=str(e),
            ).to_dict()

        try:
            response = pending.wait(timeout=req.timeout_seconds)
        finally:
            with self._lock:
                self._pending.pop(req.request_id, None)

        # Let the transport update the original message (strip buttons,
        # append final status). Best-effort — failures are non-fatal.
        if pending.handle is not None and response.decision in ("allow", "deny", "timeout"):
            status_text = {
                "allow": f"✓ approved by {response.responder or 'user'}",
                "deny": f"✗ rejected by {response.responder or 'user'}",
                "timeout": "⏱️ expired",
            }.get(response.decision, response.decision)
            try:
                self._async.submit(self.transport.update(pending.handle, status_text))
            except Exception:
                _log.debug("transport.update scheduling failed", exc_info=True)

        return 200, response.to_dict()

    def handle_snooze(self, request_id: str, *, snooze_seconds: float) -> None:
        """Defer an approval by ``snooze_seconds`` and re-post a fresh card.

        1. Extend the pending approval's deadline so the hook doesn't
           time out during the snooze window (adds a 30s buffer over
           ``snooze_seconds``).
        2. Edit the original card to strip buttons + show "snoozed
           until HH:MM" so the stale card can't be tapped again.
        3. Schedule a coroutine that sleeps ``snooze_seconds`` then calls
           ``transport.send_approval`` again (unless the pending
           approval has been resolved in the meantime). The new handle
           replaces the stored one so any subsequent ``update()`` /
           ``details`` goes to the re-posted card.
        """
        from datetime import datetime, timedelta

        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            _log.info(
                "snooze: no pending approval for %s (already resolved?)",
                request_id,
            )
            return

        pending.extend_by(snooze_seconds + 30)

        # Edit the original card — strip buttons, show the snooze wall-clock.
        if pending.handle is not None:
            snooze_clock = (datetime.now() + timedelta(seconds=snooze_seconds)).strftime("%H:%M")
            try:
                self._async.submit(
                    self.transport.update(
                        pending.handle,
                        f"⏱️ snoozed — re-posting at ~{snooze_clock}",
                    )
                )
            except Exception:  # noqa: BLE001
                _log.debug("snooze: transport.update failed", exc_info=True)

        # Schedule the re-post in the async loop.
        try:
            self._async.submit(self._snooze_repost(request_id, snooze_seconds))
        except Exception:  # noqa: BLE001
            _log.exception("snooze: failed to schedule re-post")

    async def _snooze_repost(self, request_id: str, after_seconds: float) -> None:
        """Sleep, then re-send the approval card if it's still pending."""
        try:
            await asyncio.sleep(after_seconds)
        except asyncio.CancelledError:
            return  # daemon shutting down

        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            _log.info("snooze: %s resolved during snooze; skipping re-post", request_id)
            return

        try:
            new_handle = await self.transport.send_approval(pending.request)
        except Exception:  # noqa: BLE001
            _log.exception("snooze: send_approval re-post failed for %s", request_id)
            return

        with self._lock:
            still_pending = self._pending.get(request_id)
            if still_pending is pending:
                still_pending.handle = new_handle
