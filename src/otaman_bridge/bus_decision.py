"""Bus decision writer — persists Approve/Reject taps from Telegram.

When the user taps Approve or Reject on a bus ``spec-change-request``
card, the daemon has to record the decision in the same places a local
``/maestro:approve`` run would:

1. Write the per-agent ack file at
   ``.agents/bus/active/acks/{msg-stem}.human.ack`` with body
   ``approved`` or ``rejected``.
2. Broadcast a ``spec-change-approved`` (to: all) or
   ``spec-change-rejected`` (to: original proposer) message into the
   active bus so all agents pick it up on their next ``/maestro:check``.

This module is deliberately self-contained — no transport, no asyncio,
just file I/O — so it can also be reused by a future ``maestro bus
decide`` CLI or by tests.

**Scope note**: T2d-3 does NOT invoke the OpenSpec CLI from the daemon
itself. The broadcast message points humans at ``/maestro:approve`` /
``openspec new change`` for the actual spec creation. The daemon's
job is to durably record the *decision*; executing it stays in
``cli/maestro.py`` (which has the OpenSpec-detection code path).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from otaman_bridge.bus_surface import BusMessage

_log = logging.getLogger("maestro.bridge.bus_decision")


def _now_ts() -> tuple[str, str]:
    """Return (YYYYMMDDTHHMMSS, ISO-8601-UTC) for filename + frontmatter."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S"), now.isoformat()


def _slug(text: str) -> str:
    """Lowercase slug for filenames — same shape as cli/maestro.py uses."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "decision"


def write_approval_ack(
    project_root: Path,
    msg_stem: str,
    *,
    decision: str,
) -> Path:
    """Write the ``human.ack`` file recording the decision.

    decision: ``"approved"`` or ``"rejected"`` — anything else raises.
    Returns the path that was written.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be 'approved' or 'rejected', got {decision!r}")
    acks_dir = project_root / ".agents" / "bus" / "active" / "acks"
    acks_dir.mkdir(parents=True, exist_ok=True)
    ack_file = acks_dir / f"{msg_stem}.human.ack"
    ack_file.write_text(f"{decision}\n", encoding="utf-8")
    return ack_file


def broadcast_decision(
    project_root: Path,
    msg: BusMessage,
    *,
    decision: str,
    responder: str = "",
    comment: str = "",
) -> Path:
    """Write a ``spec-change-{approved,rejected}`` message to the active bus.

    Mirrors the format emitted by ``/maestro:approve`` locally (see
    ``cli/maestro.py``), so downstream agents running
    ``/maestro:check`` can't tell whether the decision came from
    Telegram or the terminal.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be 'approved' or 'rejected', got {decision!r}")
    active = project_root / ".agents" / "bus" / "active"
    active.mkdir(parents=True, exist_ok=True)

    ts_file, ts_iso = _now_ts()
    subject = msg.subject or f"{msg.type} from {msg.from_}"
    slug = _slug(subject)
    proposer = msg.from_ or "all"
    comment_section = f"\n### Human comments\n{comment}\n" if comment else ""
    responder_line = f"\n**Decided by**: {responder}" if responder else ""

    if decision == "approved":
        out_file = active / f"{ts_file}-human-to-all-spec-change-approved.md"
        body = f"""---
id: {ts_file}-approved-{slug}
from: human
to: all
priority: high
type: spec-change-approved
timestamp: {ts_iso}
status: pending
---

## Subject: Approved: {subject}

The spec-change-request from **{proposer}** has been **approved** via the remote bridge.{responder_line}

**Original proposal**: {msg.stem}
{comment_section}
### Next steps
1. Specs will be created/updated in the specs repo (via OpenSpec or manually)
2. All agents will be notified when specs are committed (via post-commit hook)
3. Affected agents should review updated specs and adapt implementation

Use `/maestro:check` to track updates.
"""
    else:  # rejected
        out_file = active / f"{ts_file}-human-to-{proposer}-spec-change-rejected.md"
        reason = comment or "No reason provided."
        body = f"""---
id: {ts_file}-rejected-{slug}
from: human
to: {proposer}
priority: normal
type: spec-change-rejected
timestamp: {ts_iso}
status: pending
---

## Subject: Rejected: {subject}

The spec-change-request from **{proposer}** has been **rejected** via the remote bridge.{responder_line}

**Original proposal**: {msg.stem}

### Reason
{reason}

No spec changes will be made. Adjust your approach and re-propose if needed.
"""

    out_file.write_text(body, encoding="utf-8")
    _log.info(
        "bus decision: wrote %s (%s) for %s",
        out_file.name, decision, msg.stem,
    )
    return out_file


def record_decision(
    project_root: Path,
    msg: BusMessage,
    *,
    decision: str,
    responder: str = "",
    comment: str = "",
) -> tuple[Path, Path]:
    """Convenience: ack + broadcast in one call. Returns (ack_path, broadcast_path)."""
    ack = write_approval_ack(project_root, msg.stem, decision=decision)
    broadcast = broadcast_decision(
        project_root, msg,
        decision=decision, responder=responder, comment=comment,
    )
    return ack, broadcast


def write_reply_message(
    project_root: Path,
    msg: BusMessage,
    *,
    text: str,
    responder: str = "",
) -> Path:
    """Write an ``info`` bus message carrying a free-text human reply.

    Used when the user replies via Telegram (or an equivalent
    channel) to a bus card. The reply surfaces on the bus as a
    regular message ``from: human, to: <original proposer>`` so
    the addressed agent picks it up on its next ``/maestro:check``.

    Unlike approve/reject, a reply does NOT resolve a
    spec-change-request's decision — the user may still follow up
    with an Approve or Reject tap. For ``to: human`` cards (which
    use Acknowledge instead of Approve/Reject), a reply is usually
    paired with a separate Acknowledge to close the thread.
    """
    active = project_root / ".agents" / "bus" / "active"
    active.mkdir(parents=True, exist_ok=True)

    ts_file, ts_iso = _now_ts()
    proposer = msg.from_ or "all"
    slug = _slug(text or msg.subject or "reply")
    out_file = active / f"{ts_file}-human-to-{proposer}-reply.md"
    responder_line = f"\n**Via**: {responder}" if responder else ""

    body = f"""---
id: {ts_file}-reply-{slug}
from: human
to: {proposer}
priority: normal
type: info
timestamp: {ts_iso}
status: pending
in_reply_to: {msg.stem}
---

## Subject: Re: {msg.subject or msg.type}

{text}
{responder_line}

---
*This is a human reply delivered via the remote bridge to message {msg.stem}.*
"""
    out_file.write_text(body, encoding="utf-8")
    _log.info(
        "bus reply: wrote %s (in_reply_to=%s)", out_file.name, msg.stem,
    )
    return out_file


def write_acknowledge(
    project_root: Path,
    msg: BusMessage,
    *,
    responder: str = "",
    comment: str = "",
) -> tuple[Path, Path | None]:
    """Record an Acknowledge tap on a ``to: human`` card.

    Writes the human.ack file (body = ``acknowledged``) and, when
    a comment accompanies the ack, also emits a free-text reply
    message. Returns ``(ack_path, reply_path or None)``.

    Semantics diverge from Approve/Reject: acknowledge does not
    broadcast a decision — ``to: human`` messages aren't
    spec-change proposals, they're attention-requiring notes.
    The ack tells the sender "seen"; any commentary rides on the
    reply message.
    """
    acks_dir = project_root / ".agents" / "bus" / "active" / "acks"
    acks_dir.mkdir(parents=True, exist_ok=True)
    ack_file = acks_dir / f"{msg.stem}.human.ack"
    ack_file.write_text("acknowledged\n", encoding="utf-8")

    reply_file: Path | None = None
    if comment:
        reply_file = write_reply_message(
            project_root, msg, text=comment, responder=responder,
        )
    return ack_file, reply_file
