"""Idle-based auto-AFK — flip AFK on when the human goes quiet.

Runs inside the bridge daemon's async loop as a periodic task. Watches
``<maestro-root>/.maestro/last-user-activity`` (written by the
UserPromptSubmit hook ``hooks/user-activity.sh``) and compares its
mtime to ``now``.

State machine:

    activity recent        → clear AFK if source=idle-auto (user is back)
    activity > threshold   → enable AFK with source=idle-auto (user away)
    no activity file yet   → no decision (we only know idleness once we
                             see one prompt go through)

Manual AFK (``source: manual``) and unattended (``source: unattended``)
AFK are never touched — user intent beats our heuristic.

The monitor reads the activity file's *mtime* rather than parsing the
timestamp inside, so a stale file that nobody updates still reports
the correct elapsed time. We write the timestamp inside too for
debuggability (operators can ``cat`` it), but mtime is authoritative.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("maestro.bridge.idle_afk")


AFK_FILENAME = "afk"
ACTIVITY_FILENAME = "last-user-activity"
DEFAULT_POLL_SECONDS = 60.0
# If idle_minutes is 0 or negative, the monitor is a no-op. Callers use
# this to disable the feature without branching at construct time.
MIN_IDLE_MINUTES = 1

_IDLE_SOURCE = "idle-auto"


def _state_dir(project_root: Path) -> Path:
    return project_root / ".maestro"


def _afk_file(project_root: Path) -> Path:
    return _state_dir(project_root) / AFK_FILENAME


def _activity_file(project_root: Path) -> Path:
    return _state_dir(project_root) / ACTIVITY_FILENAME


def _read_afk_source(path: Path) -> str | None:
    """Peek at an AFK file's ``source:`` line; None if absent/unreadable.

    Cheaper than a full YAML parse — we only need to know whether
    *we* set it (so we're allowed to clear it) or someone else did.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("source:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def _write_idle_afk(project_root: Path, idle_seconds: float) -> None:
    state_dir = _state_dir(project_root)
    state_dir.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    minutes = int(idle_seconds / 60)
    _afk_file(project_root).write_text(
        f"enabled_at: {now_iso}\n"
        f"source: {_IDLE_SOURCE}\n"
        f"signal: idle-minutes={minutes}\n"
        f"enabled_by: bridge-daemon\n",
        encoding="utf-8",
    )


class IdleAFKMonitor:
    """Async task: flip AFK on/off based on user activity mtime.

    Wiring: constructed by ``BridgeDaemon`` when ``idle_auto_afk_minutes``
    is positive. Runs inside the daemon's async loop alongside the bus
    watcher. Emits ``InfoMessage`` via an optional ``on_enabled`` callback
    so the user gets a Telegram heads-up when AFK flips on idle-auto (the
    alternative — silent flip — makes for a confusing "why are approvals
    going to my phone?" experience).
    """

    def __init__(
        self,
        project_root: Path,
        *,
        idle_minutes: int,
        poll_interval: float = DEFAULT_POLL_SECONDS,
        on_enabled=None,  # async callable: (reason: str) -> None
        on_cleared=None,  # async callable: () -> None
    ) -> None:
        self.project_root = project_root
        self.idle_minutes = max(MIN_IDLE_MINUTES, idle_minutes)
        self.poll_interval = max(5.0, poll_interval)
        self._on_enabled = on_enabled
        self._on_cleared = on_cleared
        self._stopping = asyncio.Event()
        self._last_notified_state: str | None = None  # "enabled" | "cleared"

    async def run(self) -> None:
        """Run until cancelled / stop() called."""
        _log.info(
            "idle-afk monitor started for %s (threshold=%d min, poll=%.0fs)",
            self.project_root, self.idle_minutes, self.poll_interval,
        )
        try:
            while not self._stopping.is_set():
                try:
                    await self._check_once()
                except Exception:  # noqa: BLE001
                    _log.exception("idle-afk monitor: check failed")
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.poll_interval,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        finally:
            _log.info("idle-afk monitor stopped")

    def stop(self) -> None:
        self._stopping.set()

    async def _check_once(self) -> None:
        """One tick of the state machine."""
        activity = _activity_file(self.project_root)
        afk = _afk_file(self.project_root)

        if not activity.is_file():
            # Never seen a prompt yet — can't reason about idleness.
            return

        try:
            mtime = activity.stat().st_mtime
        except OSError:
            return

        idle_seconds = time.time() - mtime
        threshold_seconds = self.idle_minutes * 60
        is_idle = idle_seconds >= threshold_seconds

        current_source = _read_afk_source(afk) if afk.is_file() else None

        if is_idle:
            # We only auto-enable if nothing's holding AFK already.
            # Manual / unattended entries stay untouched.
            if current_source is None:
                _write_idle_afk(self.project_root, idle_seconds)
                _log.info(
                    "idle-afk: enabled AFK (idle=%d min, threshold=%d min)",
                    int(idle_seconds / 60), self.idle_minutes,
                )
                if self._on_enabled and self._last_notified_state != "enabled":
                    try:
                        await self._on_enabled(
                            f"{int(idle_seconds / 60)} min of inactivity"
                        )
                    except Exception:  # noqa: BLE001
                        _log.exception("idle-afk: on_enabled callback failed")
                    self._last_notified_state = "enabled"
        else:
            # Activity is fresh. If *we* set AFK, clear it — user's back.
            # Leave manual / unattended alone.
            if current_source == _IDLE_SOURCE:
                try:
                    afk.unlink()
                    _log.info("idle-afk: cleared AFK (user active)")
                except OSError:
                    return
                if self._on_cleared and self._last_notified_state != "cleared":
                    try:
                        await self._on_cleared()
                    except Exception:  # noqa: BLE001
                        _log.exception("idle-afk: on_cleared callback failed")
                    self._last_notified_state = "cleared"
