"""Bus watcher — poll ``.agents/bus/active/`` and surface new messages.

The daemon's second responsibility (design §5.6). Watches for new bus
messages at a configurable interval (default 2s polling — matches the
design's fallback when inotify/fsevents aren't practical cross-platform).

For each new message the surface policy approves:
  - **Info-only** messages go to ``transport.send_info`` (no round-trip).
  - **Interactive** messages (T2d-3) go to ``transport.send_approval``
    with a bus-flavored request_id; dispatch routes the reply back here
    to write the appropriate bus response files.

Deduplication: message IDs we've already surfaced are persisted to
``<maestro-root>/.maestro/bus-surfaced.state`` — JSON dict of
``{msg_stem: timestamp}``. Pruned after 7 days so the file can't grow
unbounded (archived messages stop appearing on disk anyway).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from otaman_bridge.bus_surface import (
    BusMessage,
    SurfaceDecision,
    decide,
    iter_bus_messages,
    load_surface_overrides,
)
from otaman_bridge.core import ApprovalRequest, InfoMessage

_log = logging.getLogger("maestro.bridge.bus_watcher")


POLL_INTERVAL_SECONDS = 2.0      # how often we re-scan the bus
PRUNE_OLDER_THAN_SECONDS = 7 * 24 * 60 * 60   # drop state entries older than this
STATE_FILENAME = "bus-surfaced.state"


def _state_path(project_root: Path) -> Path:
    return project_root / ".maestro" / STATE_FILENAME


def _load_state(project_root: Path) -> dict[str, float]:
    """Return ``{msg_stem: surfaced_at_unix_ts}``. Empty dict if absent/corrupt."""
    path = _state_path(project_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce values to floats; drop malformed entries.
    out: dict[str, float] = {}
    for k, v in data.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _save_state(project_root: Path, state: dict[str, float]) -> None:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Prune old entries so the file doesn't grow unbounded.
    cutoff = time.time() - PRUNE_OLDER_THAN_SECONDS
    pruned = {k: v for k, v in state.items() if v >= cutoff}
    path.write_text(json.dumps(pruned, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Message → surface payload conversion


_SEVERITY_EMOJI = {
    "info": "🟢",
    "approval": "🟡",
    "blocking": "🔴",
}


def _format_info_title(msg: BusMessage) -> str:
    emoji = _SEVERITY_EMOJI.get("info", "ℹ️")
    return f"{emoji} [{msg.from_} → {msg.to}] {msg.type}"


def _format_info_body(msg: BusMessage) -> str:
    lines = []
    if msg.subject:
        lines.append(f"**{msg.subject}**")
    if msg.priority and msg.priority != "normal":
        lines.append(f"Priority: {msg.priority}")
    # Truncate the body so phone notifications stay short.
    body = msg.body.strip()
    max_chars = 800
    if len(body) > max_chars:
        body = body[:max_chars] + "\n… (truncated)"
    if body:
        lines.append("")
        lines.append(body)
    return "\n".join(lines)


def build_info_message(
    msg: BusMessage,
    *,
    account: str,
    project: str,
    severity: str = "info",
) -> InfoMessage:
    """Convert a BusMessage into a transport-neutral InfoMessage."""
    return InfoMessage(
        account=account,
        project=project,
        severity=severity,  # type: ignore[arg-type]
        title=_format_info_title(msg),
        body=_format_info_body(msg),
        source_agent=msg.from_,
        bus_message_id=msg.id,
    )


def build_approval_request(
    msg: BusMessage,
    *,
    account: str,
    project: str,
    timeout_seconds: int = 3 * 60 * 60,
) -> ApprovalRequest:
    """Convert a BusMessage into a transport-neutral ApprovalRequest.

    ``request_id`` is the bus message stem so the daemon's reply dispatch
    can look up which bus file to write the decision against. The
    bus-interactive flow has longer timeouts than tool-call approvals
    (hours, not minutes) because specs don't need sub-second latency.
    """
    priority = msg.priority
    if priority not in ("low", "normal", "high", "urgent"):
        priority = "normal"
    return ApprovalRequest(
        account=account,
        project=project,
        repo=msg.to or "",
        agent=msg.from_,
        tool_name=f"bus:{msg.type}",
        tool_input={
            "id": msg.id,
            "subject": msg.subject,
            "body": msg.body,
            "from": msg.from_,
            "to": msg.to,
            "timestamp": str(msg.frontmatter.get("timestamp", "")),
        },
        reason=msg.subject or f"{msg.type} from {msg.from_}",
        priority=priority,  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
        request_id=msg.stem,   # msg filename stem = unique, lookup key
    )


# ---------------------------------------------------------------------------
# Watcher


class BusWatcher:
    """Polls the bus and dispatches surface decisions to callbacks.

    Wiring: constructed by the daemon with two coroutine callbacks:
      - ``on_info(InfoMessage)``  — for non-interactive surfacing
      - ``on_approval(ApprovalRequest)`` — for interactive (buttons)

    The watcher doesn't know or care how the transport works; it just
    hands off messages that passed the policy check. Daemon wires the
    callbacks to ``transport.send_info`` and ``transport.send_approval``
    respectively, and records interactive ones in a bus-decision registry
    so the callback dispatch can resolve button taps back to bus writes.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        account: str,
        project: str,
        on_info,     # async callable: (InfoMessage) -> ...
        on_approval, # async callable: (ApprovalRequest, BusMessage) -> ...
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.project_root = project_root
        self.account = account
        self.project = project
        self._on_info = on_info
        self._on_approval = on_approval
        self.poll_interval = max(0.1, poll_interval)
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        """Run the poll loop until cancelled / stop() called."""
        _log.info(
            "bus watcher started for %s (project=%s, poll=%.1fs)",
            self.project_root, self.project, self.poll_interval,
        )
        try:
            while not self._stopping.is_set():
                try:
                    await self._scan_once()
                except Exception:  # noqa: BLE001
                    _log.exception("bus watcher: scan failed")
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.poll_interval,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        finally:
            _log.info("bus watcher stopped")

    def stop(self) -> None:
        """Request a graceful stop after the current scan completes."""
        self._stopping.set()

    async def _scan_once(self) -> int:
        """One pass over the bus. Returns number of messages surfaced."""
        overrides = load_surface_overrides(self.project_root)
        state = _load_state(self.project_root)
        now = time.time()
        surfaced = 0

        for msg in iter_bus_messages(self.project_root):
            if msg.stem in state:
                continue  # already surfaced in a previous scan
            decision = decide(msg, overrides=overrides)
            if not decision.surface:
                continue

            try:
                if decision.interactive:
                    req = build_approval_request(
                        msg, account=self.account, project=self.project,
                    )
                    await self._on_approval(req, msg)
                else:
                    info = build_info_message(
                        msg, account=self.account, project=self.project,
                        severity=decision.severity,
                    )
                    await self._on_info(info)
            except Exception:  # noqa: BLE001
                _log.exception(
                    "bus watcher: dispatch failed for %s (%s); "
                    "will retry on next scan",
                    msg.stem, msg.type,
                )
                continue

            state[msg.stem] = now
            surfaced += 1

        if surfaced:
            _save_state(self.project_root, state)
            _log.info("bus watcher: surfaced %d new message(s)", surfaced)
        return surfaced
