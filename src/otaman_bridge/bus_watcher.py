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
``<workspace-root>/.otaman/bus-surfaced.state`` — JSON dict of
``{msg_stem: timestamp}``. Pruned after 7 days so the file can't grow
unbounded (archived messages stop appearing on disk anyway).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from otaman_bridge.bus_provenance import (
    build_quarantine_alert,
    is_privileged_type,
    quarantine_message,
    verify_provenance,
)
from otaman_bridge.bus_surface import (
    BusMessage,
    decide,
    iter_bus_messages,
    load_surface_overrides,
)
from otaman_bridge.core import ApprovalRequest, InfoMessage
from otaman_bridge.lifecycle_gate import is_inert, program_lifecycle_state

_log = logging.getLogger("maestro.bridge.bus_watcher")  # legacy: logger renamed at otaman-core 1.0


POLL_INTERVAL_SECONDS = 2.0  # how often we re-scan the bus
PRUNE_OLDER_THAN_SECONDS = 7 * 24 * 60 * 60  # drop state entries older than this
STATE_FILENAME = "bus-surfaced.state"

_warned_legacy_state: bool = False


def _state_path(project_root: Path) -> Path:
    return project_root / ".otaman" / STATE_FILENAME


def _state_path_legacy(project_root: Path) -> Path:
    return project_root / ".maestro" / STATE_FILENAME  # legacy: read-fallback until otaman-core 1.0


def _load_state(project_root: Path) -> dict[str, float]:
    """Return ``{msg_stem: surfaced_at_unix_ts}``. Empty dict if absent/corrupt."""
    global _warned_legacy_state
    path = _state_path(project_root)
    migrating_from_legacy = False
    if not path.is_file():
        legacy = _state_path_legacy(project_root)
        if legacy.is_file():
            if not _warned_legacy_state:
                _log.warning(
                    "legacy: found bus-surfaced.state under .maestro/; "
                    "migration: writing the .otaman/ copy now — delete the old one once confirmed"
                )
                _warned_legacy_state = True
            path = legacy
            migrating_from_legacy = True
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
    # One-time migration: write .otaman/ immediately so the canonical path
    # exists after restart even if no new messages surface this scan cycle.
    # legacy: the .maestro/ file is left in place (operator deletes it once
    # the .otaman/ copy is confirmed present).
    if migrating_from_legacy:
        _save_state(project_root, out)
        _log.info(
            "legacy: migrated bus-surfaced.state from .maestro/ to .otaman/ (%d entries); "
            "the old copy is safe to delete",
            len(out),
        )
    return out


def load_surfaced_state(project_root: Path) -> dict[str, float]:
    """Public accessor for the surfaced-state dedup map.

    Used by ``BusSurfaceService``'s restart-recovery pass to tell which
    already-surfaced bus cards still lack a decision. See ``_load_state``
    for the on-disk format.
    """
    return _load_state(project_root)


def _save_state(project_root: Path, state: dict[str, float]) -> None:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Prune old entries so the file doesn't grow unbounded.
    cutoff = time.time() - PRUNE_OLDER_THAN_SECONDS
    pruned = {k: v for k, v in state.items() if v >= cutoff}
    # Atomic write (tmp + rename): a plain write_text truncates first, so a
    # concurrent reader (restart-recovery pass, tests) can observe an empty
    # or partial file — and a crash mid-write would corrupt dedup state.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(pruned, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
        request_id=msg.stem,  # msg filename stem = unique, lookup key
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
        on_info,  # async callable: (InfoMessage) -> ...
        on_approval,  # async callable: (ApprovalRequest, BusMessage) -> ...
        on_event=None,  # sync callable: (BusMessage) -> None; called for every surfaced msg
        poll_interval: float = POLL_INTERVAL_SECONDS,
        ledger_path: Path | None = None,  # confirmation ledger override (tests); None = canonical
    ) -> None:
        self.project_root = project_root
        self.account = account
        self.project = project
        self._on_info = on_info
        self._on_approval = on_approval
        self._on_event = on_event
        self.poll_interval = max(0.1, poll_interval)
        self.ledger_path = ledger_path
        self._stopping = asyncio.Event()
        # program-lifecycle-states 2.2: tracks the current inert state (suspended
        # /archived) so transitions log once, not every scan. None = active/normal.
        self._lifecycle_inert: str | None = None

    async def run(self) -> None:
        """Run the poll loop until cancelled / stop() called."""
        _log.info(
            "bus watcher started for %s (project=%s, poll=%.1fs)",
            self.project_root,
            self.project,
            self.poll_interval,
        )
        try:
            while not self._stopping.is_set():
                try:
                    await self._scan_once()
                except Exception:  # noqa: BLE001
                    _log.exception("bus watcher: scan failed")
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self.poll_interval,
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
        """One pass over the bus. Returns number of messages surfaced to transport."""
        # program-lifecycle-states 2.2: gate on the program's lifecycle state
        # (design D1 read point). suspended/archived → this per-program bridge is
        # inert: no surfacing, no AFK/watch. active/limited → normal. Transitions
        # (suspend/resume, archive/unarchive) are picked up here at runtime.
        lifecycle_state = program_lifecycle_state(self.project_root)
        if is_inert(lifecycle_state):
            if self._lifecycle_inert != lifecycle_state:
                _log.info(
                    "program lifecycle=%s → bridge inert (surfacing + AFK/watch paused)",
                    lifecycle_state,
                )
                self._lifecycle_inert = lifecycle_state
            return 0
        if self._lifecycle_inert is not None:
            _log.info("program lifecycle=%s → bridge resumed", lifecycle_state)
            self._lifecycle_inert = None

        overrides = load_surface_overrides(self.project_root)
        state = _load_state(self.project_root)
        now = time.time()
        surfaced = 0
        state_changed = False  # tracks whether _save_state is needed

        for msg in iter_bus_messages(self.project_root):
            if msg.stem in state:
                continue  # already processed in a previous scan

            # Privileged types require ledger provenance (bus-write-integrity
            # 3.1). Unverified -> quarantine + non-privileged info alert;
            # NEVER act (a forged halt must not halt anything), never delete.
            if is_privileged_type(msg) and not verify_provenance(msg, ledger_path=self.ledger_path):
                quarantined_to = quarantine_message(self.project_root, msg)
                _log.warning(
                    "quarantined unverified privileged message %s (type=%s) -> %s",
                    msg.stem,
                    msg.type,
                    quarantined_to,
                )
                try:
                    await self._on_info(
                        build_quarantine_alert(
                            msg, quarantined_to, account=self.account, project=self.project
                        )
                    )
                except Exception:  # noqa: BLE001
                    _log.exception("quarantine alert dispatch failed for %s", msg.stem)
                continue

            decision = decide(msg, overrides=overrides)

            if not decision.surface:
                # Not surfaced to transport (e.g. task-assignment at normal priority),
                # but on_event (PM sync) must still fire — PM sync is independent of
                # the transport notification layer.
                if self._on_event is not None:
                    try:
                        self._on_event(msg)
                    except Exception:  # noqa: BLE001
                        _log.exception(
                            "pm sync: handle_event failed for %s; continuing",
                            msg.stem,
                        )
                state[msg.stem] = now
                state_changed = True
                continue

            try:
                if decision.interactive:
                    req = build_approval_request(
                        msg,
                        account=self.account,
                        project=self.project,
                    )
                    await self._on_approval(req, msg)
                else:
                    info = build_info_message(
                        msg,
                        account=self.account,
                        project=self.project,
                        severity=decision.severity,
                    )
                    await self._on_info(info)
            except Exception:  # noqa: BLE001
                _log.exception(
                    "bus watcher: dispatch failed for %s (%s); will retry on next scan",
                    msg.stem,
                    msg.type,
                )
                continue

            if self._on_event is not None:
                try:
                    self._on_event(msg)
                except Exception:  # noqa: BLE001
                    _log.exception(
                        "pm sync: handle_event failed for %s; continuing",
                        msg.stem,
                    )

            state[msg.stem] = now
            state_changed = True
            surfaced += 1

        if state_changed:
            _save_state(self.project_root, state)
        if surfaced:
            _log.info("bus watcher: surfaced %d new message(s)", surfaced)
        return surfaced
