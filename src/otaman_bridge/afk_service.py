"""Owns the daemon-side idle-auto-AFK monitor lifecycle.

Extracted out of ``BridgeDaemon`` (F040, phase 3 of the god-object
decomposition — see the bridge-agent/spec-agent bus thread on
2026-07-03, PR #33 for phase 1, PR #34 for phase 2). Wraps
``IdleAFKMonitor`` (the actual idle-detection state machine, unchanged)
with the daemon-specific wiring: building the on-enabled/on-cleared
Telegram notifications and running the monitor's poll loop on the
daemon's shared async loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from otaman_bridge.bus_surface import resolve_project_name
from otaman_bridge.core import InfoMessage
from otaman_bridge.idle_afk import IdleAFKMonitor

if TYPE_CHECKING:
    from otaman_bridge.core import Transport
    from otaman_bridge.daemon import _AsyncLoopThread

_log = logging.getLogger("maestro.bridge.afk_service")


class AfkService:
    """Idle-auto-AFK monitor lifecycle + Telegram notifications.

    ``BridgeDaemon`` holds one instance and wires it up in ``start()``/
    ``stop()``. Inert unless ``idle_minutes > 0`` and a workspace root
    is configured (auto-AFK shares the bus-watcher workspace since
    last-user-activity lives in the same ``.otaman/`` directory).
    """

    def __init__(self, *, transport: Transport, async_loop: _AsyncLoopThread, account: str) -> None:
        self.transport = transport
        self._async = async_loop
        self.account = account
        self.monitor: IdleAFKMonitor | None = None
        self._future = None  # concurrent.futures.Future

    def start(
        self,
        *,
        project_root: Path | None,
        idle_minutes: int,
        project: str,
    ) -> None:
        if idle_minutes <= 0 or project_root is None:
            return

        idle_project = project or resolve_project_name(project_root)
        self.monitor = IdleAFKMonitor(
            project_root=project_root,
            idle_minutes=idle_minutes,
            on_enabled=self._make_notifier(
                project=idle_project,
                title="🌙 AFK auto-enabled",
                body_template=(
                    "Idle auto-AFK triggered: {reason}.\n\n"
                    "Approvals will route here until you return. "
                    "Send a prompt in Claude to clear it."
                ),
            ),
            on_cleared=self._make_notifier(
                project=idle_project,
                title="☀️ AFK cleared",
                body_template="User activity resumed — back to local prompts.",
                include_reason=False,
            ),
        )
        self._future = self._async.submit(self.monitor.run())
        _log.info("idle-afk monitor started (threshold=%d min)", idle_minutes)

    def stop(self) -> None:
        # Event-driven graceful stop, future.cancel() as backstop — same
        # pattern as the bus watcher and listener loop.
        if self.monitor is not None:
            self.monitor.stop()
        if self._future is not None:
            self._future.cancel()
            try:
                self._future.result(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            self._future = None
            self.monitor = None

    def _make_notifier(
        self,
        *,
        project: str,
        title: str,
        body_template: str,
        include_reason: bool = True,
    ):
        """Build an async callback that sends a Telegram InfoMessage when
        the IdleAFKMonitor flips AFK on or clears it.

        Without these notifications the user would see approvals route to
        their phone without warning ("why is my laptop silent?"); one
        message per transition keeps expectations calibrated.
        """
        transport = self.transport
        account = self.account

        async def notify(reason: str = "") -> None:
            body = body_template.format(reason=reason) if include_reason else body_template
            info = InfoMessage(
                account=account,
                project=project,
                severity="info",
                title=title,
                body=body,
                source_agent="bridge-daemon",
                bus_message_id="",
            )
            try:
                await transport.send_info(info)
            except Exception:  # noqa: BLE001
                _log.exception("idle-afk: failed to send notification")

        return notify
