"""Owns the bus spec-change-request surfacing pipeline.

Extracted out of ``BridgeDaemon`` (F040, phase 2 of the god-object
decomposition — see the bridge-agent/spec-agent bus thread on
2026-07-03, PR #33 for phase 1). This is the "human-in-the-loop bus
approval" side: the ``BusWatcher`` polling loop, the pending-bus-decision
table, and the Approve/Reject/Acknowledge/Comment write-back paths.

Has its own lock, independent of ``ApprovalService``'s — nothing
requires the two pending tables to be read/written atomically together.
``BridgeDaemon._dispatch_inbound_reply`` and ``BridgeDaemon._surface_details``
are the only callers that touch both, and they do so as two independent
lookups.

**Restart recovery**: without ``_recover_undecided_pendings``, a bus
card the daemon already surfaced to Telegram before a restart becomes a
dead tap — the in-memory table starts empty on restart, and the bus
watcher's on-disk surfaced-state dedup means a fresh scan won't
re-dispatch it either (state says "already surfaced"). Telegram button
callback_data carries the bare ``request_id`` (the bus message stem),
independent of any daemon-process object identity, so a tap on a card
still visible on the user's phone resolves correctly as long as
``_pending_bus`` has an entry for that stem — this method reconstructs
those entries on daemon start by cross-referencing the surfaced-state
file against the still-active, not-yet-acked bus messages. ``handle``
stays ``None`` on recovered entries (we never had a ``TransportHandle``
for the pre-restart card), so the card-edit-on-decision step silently
no-ops — the decision still gets written correctly.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from otaman_bridge.bus_decision import (
    record_decision,
    write_acknowledge,
    write_reply_message,
)
from otaman_bridge.bus_surface import (
    BusMessage,
    decide,
    iter_bus_messages,
    load_surface_overrides,
    resolve_project_name,
)
from otaman_bridge.bus_watcher import BusWatcher, build_approval_request, load_surfaced_state
from otaman_bridge.core import ApprovalRequest, InboundReply, InfoMessage, TransportHandle

if TYPE_CHECKING:
    from otaman_bridge.core import Transport
    from otaman_bridge.daemon import _AsyncLoopThread

_log = logging.getLogger("maestro.bridge.bus_surface_service")


class _PendingBusDecision:
    """Holds a bus spec-change-request between ``send_approval`` and the
    button tap that resolves it.

    Unlike the tool-call approval table there's no thread blocked on a
    reply — the originating agent's proposal already sits on disk. We
    just remember enough context (the BusMessage + card handle) to
    write the ack + broadcast when the decision arrives, and to edit
    the card.
    """

    __slots__ = ("request", "msg", "handle", "project_root", "created_at")

    def __init__(
        self,
        request: ApprovalRequest,
        msg: BusMessage,
        project_root: Path,
    ):
        self.request = request
        self.msg = msg
        self.project_root = project_root
        self.handle: TransportHandle | None = None
        self.created_at = time.monotonic()


class BusSurfaceService:
    """Bus-watcher lifecycle + pending-bus-decision registry.

    ``BridgeDaemon`` holds one instance, wires it up in ``start()``/
    ``stop()``, and delegates the bus branches of reply dispatch to it.
    """

    def __init__(self, *, transport: Transport, async_loop: _AsyncLoopThread, account: str) -> None:
        self.transport = transport
        self._async = async_loop
        self.account = account
        self._pending_bus: dict[str, _PendingBusDecision] = {}
        self._lock = threading.Lock()
        self.bus_watcher_root: Path | None = None
        self.bus_watcher_project: str = ""
        self.bus_watcher: BusWatcher | None = None
        self._bus_watcher_future = None  # concurrent.futures.Future

    # ----- lifecycle --------------------------------------------------

    def start(self, *, bus_watcher_root: Path | None, bus_watcher_project: str) -> None:
        """Start the bus watcher if a workspace root is configured."""
        self.bus_watcher_root = bus_watcher_root
        self.bus_watcher_project = bus_watcher_project
        if bus_watcher_root is None:
            return

        # Project name defaults to platform.yaml's `project:` field so
        # bus-watcher-surfaced messages land in the same Telegram topic
        # as PreToolUse approvals. Falls back to the folder name only
        # when no platform.yaml / no project key is present.
        project_name = bus_watcher_project or resolve_project_name(bus_watcher_root)
        self.bus_watcher_project = project_name

        recovered = self._recover_undecided_pendings()
        if recovered:
            _log.info(
                "bus surface: recovered %d undecided pending(s) after restart",
                recovered,
            )

        _pm_event_cb = None
        try:
            from otaman_bridge.pm_sync_handler import (
                PmSyncHandler as _PmSyncHandler,  # noqa: PLC0415
            )

            _pm_event_cb = _PmSyncHandler(bus_watcher_root).handle_event
        except Exception:
            _log.warning("pm sync: could not load PmSyncHandler; PM sync disabled")

        self.bus_watcher = BusWatcher(
            project_root=bus_watcher_root,
            account=self.account,
            project=project_name,
            on_info=self.on_info,
            on_approval=self.on_approval,
            on_event=_pm_event_cb,
        )
        self._bus_watcher_future = self._async.submit(self.bus_watcher.run())
        _log.info(
            "bus watcher started for %s (project=%s)",
            bus_watcher_root,
            project_name,
        )

    def stop(self) -> None:
        # Bus pendings don't block anything — just drop them. They'll
        # re-surface on next daemon start because the state file
        # dedup is in-memory only within a single watcher instance
        # (fresh daemon reads state from disk but bus messages that
        # were surfaced but un-decided stay idempotent: ack absent,
        # watcher's state says "already surfaced" — recovered by
        # _recover_undecided_pendings() on the next start()).
        with self._lock:
            self._pending_bus.clear()

        # stop() flips an asyncio.Event; the future then exits via its
        # normal path and we cancel as a backstop.
        if self.bus_watcher is not None:
            self.bus_watcher.stop()
        if self._bus_watcher_future is not None:
            self._bus_watcher_future.cancel()
            try:
                self._bus_watcher_future.result(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            self._bus_watcher_future = None
            self.bus_watcher = None

    # ----- restart recovery --------------------------------------------

    def _recover_undecided_pendings(self) -> int:
        """Reconstruct pending entries for cards surfaced before a restart
        that never received a decision. See module docstring."""
        assert self.bus_watcher_root is not None
        root = self.bus_watcher_root
        surfaced_state = load_surfaced_state(root)
        if not surfaced_state:
            return 0
        overrides = load_surface_overrides(root)
        acks_dir = root / ".agents" / "bus" / "active" / "acks"

        recovered = 0
        for msg in iter_bus_messages(root):
            if msg.stem not in surfaced_state:
                continue  # never surfaced (or new since restart) — normal scan handles it
            if (acks_dir / f"{msg.stem}.human.ack").exists():
                continue  # already decided
            decision = decide(msg, overrides=overrides)
            if not decision.interactive:
                continue  # only Approve/Reject/Acknowledge cards need recovery

            req = build_approval_request(
                msg,
                account=self.account,
                project=self.bus_watcher_project,
            )
            pending = _PendingBusDecision(req, msg, root)
            with self._lock:
                self._pending_bus.setdefault(msg.stem, pending)
            recovered += 1
        return recovered

    # ----- registry access ----------------------------------------------

    def get(self, request_id: str) -> _PendingBusDecision | None:
        with self._lock:
            return self._pending_bus.get(request_id)

    def dispatch(self, reply: InboundReply) -> bool:
        """If ``reply.request_id`` matches a pending bus card, resolve it
        and return True. Returns False for a non-bus request_id so the
        caller can fall through to tool-call approval dispatch."""
        pending = self.get(reply.request_id)
        if pending is None:
            return False
        self._dispatch_bus_decision(reply, pending)
        return True

    # ----- bus watcher callbacks (T2d-2: info-only) ----------------------

    async def on_info(self, info: InfoMessage) -> None:
        """Forward a non-interactive bus message to the transport."""
        await self.transport.send_info(info)

    async def on_approval(
        self,
        req: ApprovalRequest,
        msg: BusMessage,
    ) -> None:
        """Surface an interactive bus spec-change-request to Telegram.

        Registers a ``_PendingBusDecision`` keyed by the bus message
        stem (= ``req.request_id``) so that when the user taps
        Approve / Reject, the listener dispatch can find the original
        BusMessage and write the ack + broadcast. The approval card
        gets Approve/Reject/Details buttons via the standard
        ``transport.send_approval`` path.
        """
        if self.bus_watcher_root is None:
            # Shouldn't happen — watcher is only started when root is set.
            _log.warning(
                "bus approval for %s but no bus_watcher_root configured",
                req.request_id,
            )
            return

        pending = _PendingBusDecision(req, msg, self.bus_watcher_root)
        with self._lock:
            self._pending_bus[req.request_id] = pending

        try:
            handle = await self.transport.send_approval(req)
            pending.handle = handle
        except Exception:  # noqa: BLE001
            _log.exception(
                "bus approval: send_approval failed for %s",
                req.request_id,
            )
            # Drop from registry so the watcher's retry-on-fail path
            # can re-surface on the next scan (state file wasn't
            # written because this callback raises back up to the
            # watcher's dispatch guard).
            with self._lock:
                self._pending_bus.pop(req.request_id, None)
            raise

    # ----- decision write-back -------------------------------------------

    def _dispatch_bus_decision(
        self,
        reply: InboundReply,
        pending: _PendingBusDecision,
    ) -> None:
        """Resolve a bus spec-change-request tap.

        Approve → ``approved`` ack + ``spec-change-approved`` broadcast.
        Reject  → ``rejected`` ack + ``spec-change-rejected`` to proposer.
        Details → dump full message body (payload is already in hand,
            no follow-up surfacing needed; caller handles this).
        Other actions are ignored — the card stays as-is, user can try
        again.
        """
        action = reply.action

        if action == "comment":
            # Free-text reply to a bus card. Writes an info message
            # from human to the original proposer. Decision stays
            # pending — user may follow up with Approve/Reject.
            self._record_bus_comment(reply, pending)
            return

        if action == "acknowledge":
            # "to: human" messages (design §5.6) get Acknowledge
            # instead of Approve/Reject. We write the ack file +
            # optional reply, then clear the registry.
            self._record_bus_acknowledge(reply, pending)
            return

        decision_map = {"approve": "approved", "reject": "rejected"}
        decision = decision_map.get(action)
        if decision is None:
            _log.info(
                "bus decision: non-decision action %r for %s (ignored)",
                action,
                reply.request_id,
            )
            return

        # For non-SCR bus cards (e.g., `to: human` messages), Approve
        # means "acknowledged" — not a spec-change-approval broadcast.
        # Only spec-change-request types route through record_decision.
        if pending.msg.type != "spec-change-request":
            self._record_bus_acknowledge(reply, pending)
            return

        try:
            ack_path, broadcast_path = record_decision(
                pending.project_root,
                pending.msg,
                decision=decision,
                responder=reply.responder,
                comment=reply.comment or "",
            )
            _log.info(
                "bus decision: %s for %s → %s + %s",
                decision,
                pending.msg.stem,
                ack_path.name,
                broadcast_path.name,
            )
        except Exception:  # noqa: BLE001
            _log.exception(
                "bus decision: record_decision failed for %s",
                pending.msg.stem,
            )
            # Leave in registry so the user can retry tapping.
            return

        # Clear the pending slot so a second tap is a no-op.
        with self._lock:
            self._pending_bus.pop(reply.request_id, None)

        # Edit the card to show the result so the user can't tap again.
        if pending.handle is not None:
            status_text = {
                "approved": f"✓ approved by {reply.responder or 'user'}",
                "rejected": f"✗ rejected by {reply.responder or 'user'}",
            }.get(decision, decision)
            try:
                self._async.submit(self.transport.update(pending.handle, status_text))
            except Exception:  # noqa: BLE001
                _log.debug("bus decision: transport.update failed", exc_info=True)

    def _record_bus_comment(
        self,
        reply: InboundReply,
        pending: _PendingBusDecision,
    ) -> None:
        """Write a free-text reply bus message for a card that stays pending.

        For spec-change-requests, a comment is supplementary — the
        Approve/Reject decision is still open. We DON'T clear the
        registry here; the user may tap a decision button after.
        """
        text = (reply.comment or "").strip()
        if not text:
            _log.info(
                "bus comment: empty reply for %s (ignored)",
                pending.msg.stem,
            )
            return
        try:
            reply_path = write_reply_message(
                pending.project_root,
                pending.msg,
                text=text,
                responder=reply.responder,
            )
            _log.info(
                "bus comment: wrote %s (in_reply_to=%s)",
                reply_path.name,
                pending.msg.stem,
            )
        except Exception:  # noqa: BLE001
            _log.exception(
                "bus comment: write_reply_message failed for %s",
                pending.msg.stem,
            )

    def _record_bus_acknowledge(
        self,
        reply: InboundReply,
        pending: _PendingBusDecision,
    ) -> None:
        """Record an Acknowledge tap on a ``to: human`` card.

        Writes the ack file + optional reply, clears the pending
        slot, and edits the card to confirm.
        """
        try:
            ack_path, reply_path = write_acknowledge(
                pending.project_root,
                pending.msg,
                responder=reply.responder,
                comment=reply.comment or "",
            )
            _log.info(
                "bus ack: wrote %s for %s%s",
                ack_path.name,
                pending.msg.stem,
                f" + reply {reply_path.name}" if reply_path else "",
            )
        except Exception:  # noqa: BLE001
            _log.exception(
                "bus ack: write_acknowledge failed for %s",
                pending.msg.stem,
            )
            return

        with self._lock:
            self._pending_bus.pop(reply.request_id, None)

        if pending.handle is not None:
            try:
                self._async.submit(
                    self.transport.update(
                        pending.handle,
                        f"👍 acknowledged by {reply.responder or 'user'}",
                    )
                )
            except Exception:  # noqa: BLE001
                _log.debug("bus ack: transport.update failed", exc_info=True)
