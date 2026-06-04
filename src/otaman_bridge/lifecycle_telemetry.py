"""Lifecycle telemetry for auto-session-spawn (task 1.5).

Emits structured bus messages for the following events:
- spawn-start: headless session spawn initiated
- spawn-complete: headless session ended cleanly (called by runner/daemon on exit)
- spawn-failed: spawn attempt failed — delivered to human inbox
- session-released: session removed from registry (linger expired or explicit release)

OTel span hooks are included as no-ops; wire them to ADR-007 OTel provider when
that infrastructure lands. The bus-message path is always active.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _ts_prefix() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")


def _write_bus_message(
    bus_dir: Path,
    *,
    msg_type: str,
    from_agent: str,
    to: str,
    priority: str,
    change_id: str,
    subject: str,
    body: str,
) -> Path:
    ts = _now_iso()
    prefix = _ts_prefix()
    slug = f"{prefix}-{from_agent}-to-{to}-{msg_type.replace('-', '_')}"
    filename = f"{slug}.md"
    content = (
        f"---\n"
        f"id: {slug}\n"
        f"from: {from_agent}\n"
        f"to: {to}\n"
        f"priority: {priority}\n"
        f"type: {msg_type}\n"
        f"timestamp: {ts}\n"
        f"status: pending\n"
        f"change: {change_id}\n"
        f"---\n"
        f"\n"
        f"## Subject: {subject}\n"
        f"\n"
        f"{body}\n"
    )
    out = bus_dir / "active" / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    _log.debug("Emitted lifecycle event %s: %s", msg_type, filename)
    return out


# ---------------------------------------------------------------------------
# Public event emitters
# ---------------------------------------------------------------------------


def emit_spawn_start(
    *,
    bus_dir: Path,
    from_agent: str,
    agent_id: str,
    human_id: str,
    session_id: str,
    change_id: str,
    mode: str,
    trigger_source: str,
) -> Path:
    """Emit a spawn-start lifecycle event to the human inbox."""
    subject = f"Auto-spawn started: {agent_id} ({mode})"
    body = (
        f"**Agent**: {agent_id}  \n"
        f"**Human**: {human_id}  \n"
        f"**Session**: {session_id}  \n"
        f"**Mode**: {mode}  \n"
        f"**Change**: {change_id}  \n"
        f"**Trigger**: {trigger_source}  \n"
    )
    return _write_bus_message(
        bus_dir,
        msg_type="spawn-start",
        from_agent=from_agent,
        to=human_id,
        priority="normal",
        change_id=change_id,
        subject=subject,
        body=body,
    )


def emit_spawn_complete(
    *,
    bus_dir: Path,
    from_agent: str,
    agent_id: str,
    human_id: str,
    session_id: str,
    change_id: str,
) -> Path:
    """Emit a spawn-complete lifecycle event (called when headless session exits 0)."""
    subject = f"Auto-spawn complete: {agent_id}"
    body = (
        f"**Agent**: {agent_id}  \n"
        f"**Human**: {human_id}  \n"
        f"**Session**: {session_id}  \n"
        f"**Change**: {change_id}  \n"
        f"The headless session exited cleanly.\n"
    )
    return _write_bus_message(
        bus_dir,
        msg_type="spawn-complete",
        from_agent=from_agent,
        to=human_id,
        priority="normal",
        change_id=change_id,
        subject=subject,
        body=body,
    )


def emit_spawn_failed(
    *,
    bus_dir: Path,
    from_agent: str,
    agent_id: str,
    human_id: str,
    change_id: str,
    error: str,
) -> Path:
    """Emit a spawn-failed event to the human inbox (high priority — human must see it)."""
    subject = f"Auto-spawn FAILED: {agent_id} — {error[:60]}"
    body = (
        f"**Agent**: {agent_id}  \n"
        f"**Human**: {human_id}  \n"
        f"**Change**: {change_id}  \n"
        f"**Error**: {error}  \n"
        f"\n"
        f"The headless session could not be started. The task-assignment remains pending\n"
        f"on the bus. Check runner logs and retry when the runner is reachable.\n"
    )
    return _write_bus_message(
        bus_dir,
        msg_type="spawn-failed",
        from_agent=from_agent,
        to=human_id,
        priority="high",
        change_id=change_id,
        subject=subject,
        body=body,
    )


def emit_session_released(
    *,
    bus_dir: Path,
    from_agent: str,
    agent_id: str,
    human_id: str,
    session_id: str,
    change_id: str,
    reason: str = "linger-expired",
) -> Path:
    """Emit a session-released lifecycle event."""
    subject = f"Session released: {agent_id} ({reason})"
    body = (
        f"**Agent**: {agent_id}  \n"
        f"**Human**: {human_id}  \n"
        f"**Session**: {session_id}  \n"
        f"**Change**: {change_id}  \n"
        f"**Reason**: {reason}  \n"
    )
    return _write_bus_message(
        bus_dir,
        msg_type="session-released",
        from_agent=from_agent,
        to=human_id,
        priority="normal",
        change_id=change_id,
        subject=subject,
        body=body,
    )


# ---------------------------------------------------------------------------
# OTel stub — wire to ADR-007 provider when available
# ---------------------------------------------------------------------------


def otel_spawn_span(
    event_type: str,
    *,
    agent_id: str,
    session_id: str,
    change_id: str,
    **attrs: object,
) -> None:
    """No-op OTel span emission. Replace with real provider per ADR-007."""
    # Future: use opentelemetry-sdk trace API here
    pass


__all__ = [
    "emit_spawn_start",
    "emit_spawn_complete",
    "emit_spawn_failed",
    "emit_session_released",
    "otel_spawn_span",
]
