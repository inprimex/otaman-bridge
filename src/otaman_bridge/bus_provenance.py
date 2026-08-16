"""Ledger-provenance verification + quarantine for privileged bus messages.

Implements the consumer side of the ``bus-write-integrity`` capability
(bus-test-isolation task 3.1): privileged-type bus files (``human-decision``,
``spec-change-approved``, ``spec-change-rejected``, ``emergency-halt``) are
only acted on if a matching record exists in the host-local confirmation
ledger written by the TTY-gated producer commands. A raw-written privileged
file — test fixture, script, or attacker — cannot forge the ledger entry, so
it is moved to ``.agents/bus/quarantine/`` (never acted on, never silently
deleted — forensics) and a non-privileged ``info`` alert is emitted.

The ledger primitives live in :mod:`otaman_core.confirmations`; the hash is
over the file's exact on-disk bytes, so verification re-reads the file rather
than re-serializing the parsed message.
"""

from __future__ import annotations

import logging
from pathlib import Path

from otaman_core.confirmations import (
    PRIVILEGED_TYPES,
    hash_message,
    verify_confirmation,
)

from otaman_bridge.bus_surface import BusMessage
from otaman_bridge.core import InfoMessage

_log = logging.getLogger("maestro.bridge.bus_provenance")  # legacy: renamed at core 1.0


def is_privileged_type(msg: BusMessage) -> bool:
    """True when the message's type requires ledger provenance."""
    return msg.type in PRIVILEGED_TYPES


def verify_provenance(msg: BusMessage, *, ledger_path: Path | None = None) -> bool:
    """True iff the ledger holds a record matching this file's exact bytes.

    The file is re-read so the digest covers the on-disk content, byte-exact.
    The record may be keyed by either the filename stem (the bus-wide ack
    convention) or the frontmatter ``id`` — both are accepted; the content
    hash binds the record to the bytes either way, so the key choice carries
    no trust weight. Unreadable file -> False (fail closed).
    """
    try:
        raw = msg.path.read_text(encoding="utf-8")
    except OSError:
        _log.warning("provenance: cannot read %s; treating as unverified", msg.path)
        return False
    digest = hash_message(raw)
    if verify_confirmation(message_id=msg.stem, content_hash=digest, path=ledger_path):
        return True
    return msg.id != msg.stem and verify_confirmation(
        message_id=msg.id, content_hash=digest, path=ledger_path
    )


def quarantine_message(project_root: Path, msg: BusMessage) -> Path:
    """Move the message file to ``.agents/bus/quarantine/``; return new path.

    The quarantine dir is created on demand. Existing quarantined files are
    never overwritten — collisions get a numeric suffix so repeated attacks
    (or repeated scans racing) preserve every distinct artifact.
    """
    qdir = project_root / ".agents" / "bus" / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    target = qdir / msg.path.name
    n = 1
    while target.exists():
        target = qdir / f"{msg.path.stem}.{n}{msg.path.suffix}"
        n += 1
    msg.path.rename(target)
    return target


def build_quarantine_alert(
    msg: BusMessage,
    quarantined_to: Path,
    *,
    account: str,
    project: str,
) -> InfoMessage:
    """Non-privileged ``info`` alert naming the quarantined file."""
    return InfoMessage(
        account=account,
        project=project,
        severity="info",
        title=f"⛔ Quarantined unverified {msg.type} message",
        body=(
            f"Privileged-type bus file has NO confirmation-ledger record and "
            f"was NOT acted on.\n"
            f"File: {msg.path.name}\n"
            f"Claimed sender: {msg.from_ or '(none)'}\n"
            f"Moved to: {quarantined_to}\n"
            f"If this was a genuine human-gated action, re-issue it via the "
            f"gated otaman command; otherwise inspect the file and report "
            f"per the bus-write-integrity runbook."
        ),
        source_agent="bridge-watcher",
        bus_message_id=msg.id,
    )
