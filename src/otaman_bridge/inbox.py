"""Per-user inbox file storage for cross-user messaging.

v0+ team-mode primitive: writes / lists / marks-read messages between
authenticated users. File-based, one directory per recipient, YAML
frontmatter + markdown body. No DB, no daemons -- the bridge process
that handles the MCP tools is also the only writer/reader.

Storage layout (per design doc):

    <root>/                        # default: ~/.otaman/inboxes/
    ├── <user_id_A>/
    │   ├── active/
    │   │   └── <YYYYMMDDTHHMMSSZ>-<from_short>-<subject_slug>.md
    │   └── archive/                # not used in v0+, reserved
    └── <user_id_B>/
        └── ...

Inboxes are created lazily on first write to that user_id (mkdir -p
the dir).

Concurrency: writes use os.O_EXCL with -1, -2, ... suffix retry to
handle rare same-second same-subject collisions between multiple
senders to the same recipient.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_INBOX_ROOT = Path.home() / ".otaman" / "inboxes"
MAX_SUBJECT_LEN = 80
MAX_SLUG_LEN = 32

# Frontmatter delimiter (YAML between two ---). We hand-roll a minimal
# parser rather than depending on PyYAML so this module has zero
# third-party deps.
_FRONTMATTER_DELIM = "---"


@dataclass(frozen=True)
class StoredMessage:
    """A message read back from disk. Mirrors the on-disk frontmatter."""

    id: str
    from_user: str
    from_email: str | None
    to_user: str
    subject: str
    sent_at: str             # ISO-8601 UTC
    read_at: str | None      # ISO-8601 UTC, None = unread
    in_reply_to: str | None
    priority: str            # low | normal | high
    type: str                # chat | review-request | task-handoff | approval-request
    body: str
    path: Path               # absolute path on disk; not serialized


class Inbox:
    """File-backed per-user inbox store."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or DEFAULT_INBOX_ROOT).expanduser()

    # ---- writes ------------------------------------------------------

    def write_message(
        self,
        *,
        from_user: str,
        from_email: str | None,
        to_user: str,
        body: str,
        subject: str | None = None,
        in_reply_to: str | None = None,
        priority: str = "normal",
        msg_type: str = "chat",
    ) -> StoredMessage:
        """Append a message to to_user's inbox. Returns the StoredMessage."""
        if not from_user or not to_user:
            raise ValueError("from_user and to_user are required")
        if priority not in ("low", "normal", "high"):
            raise ValueError(f"priority must be low|normal|high, got {priority!r}")
        if not body or not body.strip():
            raise ValueError("body cannot be empty")

        eff_subject = (subject or _derive_subject(body)).strip()
        if not eff_subject:
            eff_subject = "(no subject)"
        if len(eff_subject) > MAX_SUBJECT_LEN:
            eff_subject = eff_subject[:MAX_SUBJECT_LEN].rstrip()

        sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts = sent_at.replace("-", "").replace(":", "").replace("Z", "Z")
        from_short = _short_user_id(from_user)
        slug = _slugify(eff_subject)[:MAX_SLUG_LEN]
        base_id = f"{ts}-{from_short}-{slug}"

        inbox_dir = self._active_dir(to_user)
        inbox_dir.mkdir(parents=True, exist_ok=True)

        # Atomic write with collision retry
        attempt = 0
        while True:
            candidate_id = base_id if attempt == 0 else f"{base_id}-{attempt}"
            target = inbox_dir / f"{candidate_id}.md"
            try:
                fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                attempt += 1
                if attempt > 99:
                    raise RuntimeError(f"too many collisions on {base_id}")
                continue
            try:
                content = _render_message(
                    id=candidate_id,
                    from_user=from_user, from_email=from_email,
                    to_user=to_user, subject=eff_subject,
                    sent_at=sent_at, in_reply_to=in_reply_to,
                    priority=priority, msg_type=msg_type, body=body,
                )
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
            return StoredMessage(
                id=candidate_id,
                from_user=from_user, from_email=from_email,
                to_user=to_user, subject=eff_subject,
                sent_at=sent_at, read_at=None, in_reply_to=in_reply_to,
                priority=priority, type=msg_type, body=body, path=target,
            )

    # ---- reads -------------------------------------------------------

    def list_messages(
        self,
        user_id: str,
        *,
        unread_only: bool = True,
        from_user: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[StoredMessage]:
        """Return messages from user_id's inbox, filtered + sorted by sent_at desc."""
        if limit < 1 or limit > 200:
            raise ValueError("limit must be 1..200")
        active = self._active_dir(user_id)
        if not active.is_dir():
            return []
        messages = []
        for fp in sorted(active.iterdir(), reverse=True):
            if not fp.is_file() or not fp.name.endswith(".md"):
                continue
            try:
                msg = _read_message(fp)
            except (OSError, ValueError):
                continue
            if unread_only and msg.read_at is not None:
                continue
            if from_user and msg.from_user != from_user:
                continue
            if since and msg.sent_at <= since:
                continue
            messages.append(msg)
            if len(messages) >= limit:
                break
        return messages

    def mark_read(
        self,
        user_id: str,
        message_id: str,
        *,
        mark_all_before: bool = False,
    ) -> int:
        """Set read_at on message(s). Returns count marked."""
        active = self._active_dir(user_id)
        if not active.is_dir():
            return 0
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        target = active / f"{message_id}.md"
        if not target.is_file():
            return 0
        # Find the sent_at of the target message; needed for mark_all_before
        target_msg = _read_message(target)

        count = 0
        if mark_all_before:
            for fp in active.iterdir():
                if not fp.is_file() or not fp.name.endswith(".md"):
                    continue
                try:
                    m = _read_message(fp)
                except (OSError, ValueError):
                    continue
                if m.read_at is None and m.sent_at <= target_msg.sent_at:
                    _atomic_set_read_at(fp, now)
                    count += 1
        else:
            if target_msg.read_at is None:
                _atomic_set_read_at(target, now)
                count = 1
        return count

    # ---- internal ----------------------------------------------------

    def _active_dir(self, user_id: str) -> Path:
        if not user_id or "/" in user_id or ".." in user_id:
            raise ValueError(f"invalid user_id: {user_id!r}")
        return self.root / user_id / "active"


# ---- helpers ----------------------------------------------------------


def _short_user_id(user_id: str) -> str:
    """First 8 chars; readable in filenames without exposing full sub."""
    return user_id[:8] if user_id else "unknown"


def _slugify(text: str) -> str:
    """Filesystem-safe filename slug from arbitrary text."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip().lower())
    return re.sub(r"-+", "-", s).strip("-") or "msg"


def _derive_subject(body: str) -> str:
    """First non-empty line, markdown-stripped, truncated."""
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip basic markdown leaders
        line = re.sub(r"^[#>*\-]+\s*", "", line)
        return line[:MAX_SUBJECT_LEN]
    return ""


def _render_message(*, id, from_user, from_email, to_user, subject,
                    sent_at, in_reply_to, priority, msg_type, body) -> str:
    def yaml_value(v):
        if v is None:
            return "null"
        s = str(v)
        # Quote if YAML-special; v0+ keeps it simple.
        if re.search(r'[":\n#&*!\[\]\{\}\|>%@`]', s) or s.lower() in ("true", "false", "null", "yes", "no", "~"):
            return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
        return s
    lines = [
        _FRONTMATTER_DELIM,
        f"id: {yaml_value(id)}",
        f"from_user: {yaml_value(from_user)}",
        f"from_email: {yaml_value(from_email)}",
        f"to_user: {yaml_value(to_user)}",
        f"subject: {yaml_value(subject)}",
        f"sent_at: {yaml_value(sent_at)}",
        f"read_at: null",
        f"in_reply_to: {yaml_value(in_reply_to)}",
        f"priority: {yaml_value(priority)}",
        f"type: {yaml_value(msg_type)}",
        _FRONTMATTER_DELIM,
        "",
        body.rstrip() + "\n",
    ]
    return "\n".join(lines)


def _read_message(path: Path) -> StoredMessage:
    """Parse a stored message file. Raises ValueError on malformed input."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith(_FRONTMATTER_DELIM):
        raise ValueError(f"missing frontmatter delimiter in {path}")
    rest = text[len(_FRONTMATTER_DELIM):].lstrip("\n")
    fm_end = rest.find(f"\n{_FRONTMATTER_DELIM}")
    if fm_end < 0:
        raise ValueError(f"unclosed frontmatter in {path}")
    fm_text = rest[:fm_end]
    body = rest[fm_end + len(_FRONTMATTER_DELIM) + 1:].lstrip("\n")

    fields: dict[str, str | None] = {}
    for line in fm_text.splitlines():
        if not line or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v == "null" or v == "":
            fields[k] = None
        elif v.startswith('"') and v.endswith('"'):
            fields[k] = v[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        else:
            fields[k] = v

    required = ["id", "from_user", "to_user", "subject", "sent_at"]
    for r in required:
        if not fields.get(r):
            raise ValueError(f"missing required field {r!r} in {path}")
    return StoredMessage(
        id=fields["id"],
        from_user=fields["from_user"],
        from_email=fields.get("from_email"),
        to_user=fields["to_user"],
        subject=fields["subject"],
        sent_at=fields["sent_at"],
        read_at=fields.get("read_at"),
        in_reply_to=fields.get("in_reply_to"),
        priority=fields.get("priority") or "normal",
        type=fields.get("type") or "chat",
        body=body,
        path=path,
    )


def _atomic_set_read_at(path: Path, when: str) -> None:
    """Update the read_at frontmatter line in-place, atomically."""
    text = path.read_text(encoding="utf-8")
    # Replace the read_at line (must be in the frontmatter block).
    new_text = re.sub(
        r"^read_at:.*$",
        f"read_at: {when}",
        text,
        count=1,
        flags=re.M,
    )
    if new_text == text:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(str(tmp), str(path))


__all__ = [
    "DEFAULT_INBOX_ROOT",
    "MAX_SUBJECT_LEN",
    "StoredMessage",
    "Inbox",
]
