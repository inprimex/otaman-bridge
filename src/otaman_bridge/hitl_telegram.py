"""Telegram HUMAN-DECISION confirmation mechanism (hitl-confirmation-adapters 2.1).

This is the bridge-side *mechanism* for the messenger confirmation adapter. It
drives the bridge's existing ``/approval`` surface (Telegram transport, inline
approve/reject buttons, timeout) so a HUMAN-DECISION command can require a live
human confirmation from the human's authenticated account.

**Ownership split (agreed with cli-agent, mirrors the PR #13 TOTP seam).**
``otaman-cli`` depends on ``otaman-bridge`` (not the reverse) and its adapter
registry has no entry-point discovery, so:

- This module is **cli-import-free** — it never imports ``otaman_cli``.
- ``otaman-cli`` owns the thin ``TelegramAdapter`` wrapper in
  ``otaman_cli.hitl.adapters``: its methods lazily/guarded-import
  :func:`is_enrolled` / :func:`confirm_via_telegram` and build the cli-owned
  ``ConfirmationResult``. A bridge without the ``telegram`` extra makes those
  imports/probes fail soft, so the messenger tier is a transparent no-op.

**Enrollment** is read from the tenant-scope ``~/.otaman/hitl.yaml`` map, using
the canonical structured field from otaman-core's ``hitl-schema.yaml`` (task
3.1) — distinct from TOTP's ``totp_secret_ref`` so one human can hold both::

    enrollment:
      <email>:
        messenger:
          adapter: telegram
          address_ref: <secret-backend key>   # a REFERENCE, values-never-exposed

**Fail-closed.** Unlike the AFK hook (which fails *open* to a native prompt when
the daemon is unreachable), a HUMAN-DECISION guard must never self-approve: an
unreachable daemon, an unenrolled human, or any ambiguous decision resolves to
``approved=False``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("maestro.bridge.hitl_telegram")  # legacy: renamed at core 1.0

MESSENGER_ADAPTER_ID = "telegram"
_TOOL_NAME = "hitl:human-decision"


def default_hitl_path() -> Path:
    """Tenant-scope HITL config path — alongside ``edition.yaml`` in ``~/.otaman``."""
    return Path.home() / ".otaman" / "hitl.yaml"


@dataclass(frozen=True)
class TelegramConfirmResult:
    """Outcome of a Telegram confirmation.

    ``human_id`` records WHICH enrolled human approved (their roster email), so
    the cli audit trail can name the approver; it is None when no human decided
    (timeout / unreachable / not enrolled).
    """

    approved: bool
    human_id: str | None = None


# ---------------------------------------------------------------------------
# Enrollment (is_configured signal)
# ---------------------------------------------------------------------------


def _telegram_extra_available() -> bool:
    """True if the ``telegram`` extra (python-telegram-bot) is importable.

    Enrollment without the transport installed is not a usable adapter, so this
    gates ``is_enrolled`` — keeping an under-provisioned install a no-op rather
    than a confirmation that can never deliver.
    """
    import importlib.util  # noqa: PLC0415 — cheap, local

    try:
        return importlib.util.find_spec("telegram") is not None
    except (ImportError, ValueError):
        return False


def _load_enrollment(path: Path | None = None) -> dict:
    """Return the ``enrollment`` map from ``~/.otaman/hitl.yaml`` ({} on any failure).

    Missing/unparseable file, or a non-mapping shape, yields an empty map —
    the file is never load-bearing for anything but identity/enrollment, and a
    malformed one simply means "no messenger enrollment". Unknown keys are
    ignored (forward-compat, per the core schema's additive posture).
    """
    p = path or default_hitl_path()
    try:
        import yaml  # noqa: PLC0415 — optional dep, avoid top-level

        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — any failure -> no enrollment, by contract
        return {}
    if not isinstance(data, dict):
        return {}
    enrollment = data.get("enrollment")
    return enrollment if isinstance(enrollment, dict) else {}


def _has_telegram_binding(entry: object) -> bool:
    """True if one human's enrollment entry carries a Telegram messenger binding."""
    if not isinstance(entry, dict):
        return False
    messenger = entry.get("messenger")
    if not isinstance(messenger, dict):
        return False
    return str(messenger.get("adapter", "")).strip().lower() == MESSENGER_ADAPTER_ID and bool(
        str(messenger.get("address_ref", "")).strip()
    )


def telegram_enrolled_emails(path: Path | None = None) -> list[str]:
    """Roster emails with a valid Telegram messenger enrollment (sorted, stable)."""
    enrollment = _load_enrollment(path)
    return sorted(
        email
        for email, entry in enrollment.items()
        if isinstance(email, str) and _has_telegram_binding(entry)
    )


def is_enrolled(email: str | None = None, *, path: Path | None = None) -> bool:
    """Is the Telegram messenger adapter configured for ``email`` (or anyone)?

    Requires BOTH the ``telegram`` transport extra AND an
    ``enrollment[<email>].messenger.adapter == "telegram"`` binding with an
    ``address_ref``. With ``email=None``, True iff ANY roster human is enrolled.
    Returns False (transparent no-op) when the extra is absent or nothing is
    enrolled — this is the cli adapter's ``is_configured`` signal.
    """
    if not _telegram_extra_available():
        return False
    emails = telegram_enrolled_emails(path)
    if email is None:
        return bool(emails)
    return email in emails


def _sole_enrolled_email(path: Path | None = None) -> str | None:
    """The enrolled email when exactly one human is enrolled, else None."""
    emails = telegram_enrolled_emails(path)
    return emails[0] if len(emails) == 1 else None


# ---------------------------------------------------------------------------
# Confirmation (drives the existing /approval surface)
# ---------------------------------------------------------------------------


def _resolve_account_and_endpoint() -> tuple[str, int, str] | None:
    """Resolve (account, port, token) for the running bridge daemon, or None.

    Reuses the same account-derivation + endpoint-read the AFK approval client
    uses, so a confirmation targets the exact daemon (and thus the exact
    authenticated Telegram account) the session belongs to.
    """
    from otaman_bridge import bridge_approval  # noqa: PLC0415 — same package, avoid cycle

    account = bridge_approval._derive_account(Path.cwd())
    if not account:
        return None
    endpoint = bridge_approval._read_endpoint(account)
    if endpoint is None:
        return None
    port, token = endpoint
    return account, port, token


def confirm_via_telegram(
    description: str,
    *,
    email: str | None = None,
    expected_phrase: str = "CONFIRM",
    timeout_seconds: int = 540,
) -> TelegramConfirmResult:
    """Ask an enrolled human to confirm a HUMAN-DECISION command over Telegram.

    Emits a confirmation request through the bridge's ``/approval`` surface,
    which sends a buttoned message to the human's authenticated account and
    blocks until they approve/reject or the request times out.

    Mapping (fail-closed): ``allow`` -> (True, human), ``deny`` -> (False,
    human), ``timeout``/``ask``/unknown -> (False, None). A daemon that cannot
    be reached, or no enrolled human, also yields (False, None) — a
    HUMAN-DECISION guard must never self-approve.
    """
    if not is_enrolled(email):
        _log.info("hitl telegram: no enrolled human (email=%s) — not confirmed", email)
        return TelegramConfirmResult(approved=False, human_id=None)

    resolved = _resolve_account_and_endpoint()
    if resolved is None:
        _log.warning("hitl telegram: bridge daemon unreachable — failing closed (not confirmed)")
        return TelegramConfirmResult(approved=False, human_id=None)
    account, port, token = resolved

    who = email or _sole_enrolled_email()
    payload = {
        "account": account,
        "project": "",
        "repo": "",
        "agent": "",
        "tool_name": _TOOL_NAME,
        "tool_input": {"description": description, "expected_phrase": expected_phrase},
        "reason": description,
        "priority": "high",
        "timeout_seconds": timeout_seconds,
    }

    url = f"http://127.0.0.1:{port}/approval"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds + 15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log.warning("hitl telegram: /approval call failed (%s) — not confirmed", exc)
        return TelegramConfirmResult(approved=False, human_id=None)

    decision = str(body.get("decision", "")).strip().lower()
    if decision == "allow":
        return TelegramConfirmResult(approved=True, human_id=who)
    if decision == "deny":
        # Explicit decline — name who declined for the audit trail.
        return TelegramConfirmResult(approved=False, human_id=who)
    # timeout / ask / unknown — no human decision landed.
    return TelegramConfirmResult(approved=False, human_id=None)


__all__ = [
    "MESSENGER_ADAPTER_ID",
    "TelegramConfirmResult",
    "confirm_via_telegram",
    "default_hitl_path",
    "is_enrolled",
    "telegram_enrolled_emails",
]
