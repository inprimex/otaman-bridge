"""maestro afk — toggle the remote-approval "Away From Keyboard" mode.

When AFK is on, the PreToolUse bridge hook (``hooks/bridge-approval.sh``)
forwards permission prompts to the daemon instead of blocking on the
terminal. When off (default on local sessions), Claude Code's native
prompt shows and the daemon receives a fire-and-forget notification only.

State is persisted to ``<maestro-root>/.maestro/afk`` — a tiny YAML file
with TTL support so it survives restarts, sleep/wake, and reconnects
without needing a background timer. Expired entries are deleted lazily
on read.

Duration grammar (``maestro afk on [DURATION]``):
    30s 15m 8h 2d 1w                # single unit
    1h30m 2d4h 1w3d                 # compound (sum of units)
    (no arg)                        # indefinite — clear with `afk off`

Sources (``source:`` field in the file):
    manual     — set by ``maestro afk on``
    unattended — auto-set by SessionStart when MAESTRO_UNATTENDED=1
    ssh-auto   — legacy alias for ``unattended`` (older AFK files)
    idle-auto  — set by the daemon's IdleAFKMonitor after N min of no input

Notifications (cmd_on / cmd_off):
    Best-effort POST to the bridge daemon's ``/notify`` endpoint so the
    user gets a Telegram heads-up when AFK changes. Silent on any
    failure (daemon down, no account resolvable, etc.) — the CLI must
    work without the daemon. Set ``MAESTRO_AFK_NO_NOTIFY=1`` to suppress
    in tests / scripted batches.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML is required. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

from otaman_core._resolve import find_maestro_root, active_profile_env  # noqa: E402


AFK_FILENAME = "afk"
# "ssh-auto" kept for backwards-compat with files written before the
# 2026-04 rename; "unattended" is the current name written by both the
# CLI and hooks/ssh-auto-afk.sh.
VALID_SOURCES = ("manual", "unattended", "ssh-auto", "idle-auto")


# ---------------------------------------------------------------------------
# Duration parsing


_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}

# Matches a single "NNunit" chunk. Grammar is run repeatedly over the input.
_DURATION_CHUNK = re.compile(r"(\d+)([smhdw])")


def parse_duration(text: str) -> timedelta:
    """Parse a duration string into a timedelta.

    Accepts:
      - Single unit: ``30s``, ``15m``, ``8h``, ``2d``, ``1w``
      - Compound: ``1h30m``, ``2d4h``, ``1w3d12h``

    Raises ``ValueError`` on empty, negative, or unrecognized input.
    The grammar rejects bare numbers (``30`` without a unit) and
    unknown units (``3M`` for month).
    """
    if text is None:
        raise ValueError("duration is required")
    stripped = text.strip().lower()
    if not stripped:
        raise ValueError("duration is empty")

    total = 0
    idx = 0
    while idx < len(stripped):
        m = _DURATION_CHUNK.match(stripped, idx)
        if not m or m.start() != idx:
            raise ValueError(
                f"invalid duration {text!r}: expected NN{{s|m|h|d|w}} "
                f"(e.g. 30s, 15m, 1h30m)"
            )
        count = int(m.group(1))
        unit = m.group(2)
        total += count * _DURATION_UNITS[unit]
        idx = m.end()

    if total <= 0:
        raise ValueError(f"duration must be positive: {text!r}")
    return timedelta(seconds=total)


def format_remaining(delta: timedelta) -> str:
    """Render a timedelta as a compact ``1d 2h 3m`` string."""
    total = int(delta.total_seconds())
    if total <= 0:
        return "0s"
    parts: list[str] = []
    for unit, seconds in (
        ("w", _DURATION_UNITS["w"]),
        ("d", _DURATION_UNITS["d"]),
        ("h", _DURATION_UNITS["h"]),
        ("m", _DURATION_UNITS["m"]),
    ):
        count, total = divmod(total, seconds)
        if count:
            parts.append(f"{count}{unit}")
    if total:
        parts.append(f"{total}s")
    return " ".join(parts) if parts else "0s"


# ---------------------------------------------------------------------------
# State


@dataclass
class AfkState:
    """Parsed contents of ``.maestro/afk``."""

    enabled_at: datetime
    expires_at: datetime | None
    source: str = "manual"
    enabled_by: str = ""

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        return current >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled_at": self.enabled_at.isoformat(),
            "source": self.source,
        }
        if self.expires_at is not None:
            out["expires_at"] = self.expires_at.isoformat()
        if self.enabled_by:
            out["enabled_by"] = self.enabled_by
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AfkState":
        enabled_at = _parse_iso(data.get("enabled_at"))
        if enabled_at is None:
            raise ValueError("afk state missing enabled_at")
        source = str(data.get("source", "manual"))
        if source not in VALID_SOURCES:
            raise ValueError(f"invalid source {source!r}; expected one of {VALID_SOURCES}")
        return cls(
            enabled_at=enabled_at,
            expires_at=_parse_iso(data.get("expires_at")),
            source=source,
            enabled_by=str(data.get("enabled_by", "") or ""),
        )


def _parse_iso(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        # Accept trailing Z for UTC (fromisoformat supports it on 3.11+,
        # but normalize explicitly for older Pythons).
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# File I/O


def afk_path(maestro_root: Path) -> Path:
    return maestro_root / ".maestro" / AFK_FILENAME


def read_afk(maestro_root: Path) -> AfkState | None:
    """Read the AFK file. Returns None if absent, unreadable, or expired.

    Expired entries are deleted lazily — this is why the SessionStart
    hook doesn't need a background timer.
    """
    path = afk_path(maestro_root)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        state = AfkState.from_dict(data)
    except ValueError:
        return None
    if state.is_expired():
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return state


def write_afk(maestro_root: Path, state: AfkState) -> Path:
    """Write the AFK file, creating ``.maestro/`` if needed."""
    path = afk_path(maestro_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(state.to_dict(), sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return path


def clear_afk(maestro_root: Path) -> bool:
    """Remove the AFK file. Returns True if something was deleted."""
    path = afk_path(maestro_root)
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


# ---------------------------------------------------------------------------
# Bridge daemon notification (best-effort)
#
# When AFK toggles, post an InfoMessage to the daemon's /notify endpoint
# so Telegram (or whatever transport is configured) sends the user a
# heads-up. The whole thing is fail-safe: any missing piece (no account
# resolvable, no endpoint file, daemon unreachable) silently skips. The
# CLI must keep working when the daemon isn't running.


def _resolve_account_for_notify() -> str | None:
    """Mirror of ``bridge_approval.py:_derive_account``.

    Priority: ``$OTAMAN_ACTIVE_PROFILE`` → ``CLAUDE_CONFIG_DIR`` basename →
    ``.maestro`` marker's ``expected_account`` field. Returns None if
    nothing resolves (caller should skip notifying).
    """
    env_account = active_profile_env()
    if env_account:
        return env_account

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if config_dir:
        base = os.path.basename(config_dir.rstrip("/\\"))
        if base.startswith(".claude-"):
            return base[len(".claude-"):]
        if base in ("", ".claude"):
            return "default"

    try:
        from otaman_core._resolve import read_expected_account  # noqa: PLC0415
        marker = read_expected_account(Path.cwd())
        if marker:
            return marker
    except Exception:  # noqa: BLE001
        pass
    return None


def _resolve_project_for_notify(maestro_root: Path) -> str:
    """Project name from platform.yaml, falling back to the folder name.

    Reuses ``bridge.bus_surface.resolve_project_name`` when importable so
    PreToolUse approvals and these notifications land on the same
    Telegram topic.
    """
    try:
        from otaman_bridge.bus_surface import resolve_project_name  # noqa: PLC0415
        return resolve_project_name(maestro_root)
    except Exception:  # noqa: BLE001
        try:
            text = (maestro_root / "platform.yaml").read_text(encoding="utf-8")
        except OSError:
            return maestro_root.name
        for line in text.splitlines():
            if line.startswith("project:"):
                _, _, value = line.partition(":")
                value = value.strip().strip("'").strip('"')
                if value:
                    return value
                break
        return maestro_root.name


def _post_info_to_daemon(
    maestro_root: Path, *, title: str, body: str, severity: str = "info",
) -> bool:
    """POST an InfoMessage to the local daemon. Returns True on success."""
    if os.environ.get("MAESTRO_AFK_NO_NOTIFY") == "1":
        return False
    account = _resolve_account_for_notify()
    if not account:
        return False
    endpoint_file = Path.home() / ".maestro" / f"bridge-{account}.endpoint"
    if not endpoint_file.is_file():
        return False
    try:
        ep = json.loads(endpoint_file.read_text(encoding="utf-8"))
        port = int(ep.get("port") or 0)
        token = str(ep.get("token") or "")
    except (OSError, ValueError):
        return False
    if not port or not token:
        return False

    payload = {
        "account": account,
        "project": _resolve_project_for_notify(maestro_root),
        "severity": severity,
        "title": title,
        "body": body,
        "source_agent": "maestro-afk",
        "bus_message_id": "",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/notify", data=data, method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(req, timeout=2.0)
        return True
    except (urllib.error.URLError, OSError):
        return False


def notify_afk_enabled(
    maestro_root: Path, state: AfkState, *, reason: str = "",
) -> bool:
    """Send 'AFK enabled' Telegram notification. Best-effort."""
    parts = [f"Source: {state.source}"]
    if state.expires_at is not None:
        delta = state.expires_at - datetime.now(timezone.utc)
        parts.append(
            f"Expires in {format_remaining(delta)} "
            f"(at {state.expires_at.astimezone().strftime('%H:%M %Z')})."
        )
    else:
        parts.append("No expiry — clear with `maestro afk off` or by "
                     "starting a new Claude session.")
    if reason:
        parts.append(f"Note: {reason}")
    parts.append("")
    parts.append("Approvals will route to this chat until cleared.")
    return _post_info_to_daemon(
        maestro_root,
        title="🌙 AFK enabled",
        body="\n".join(parts),
    )


def notify_afk_cleared(
    maestro_root: Path, *, prior_source: str = "", reason: str = "",
) -> bool:
    """Send 'AFK cleared' Telegram notification. Best-effort."""
    parts: list[str] = []
    if prior_source:
        parts.append(f"Cleared {prior_source} AFK.")
    if reason:
        parts.append(f"Reason: {reason}")
    parts.append("Back to local prompts.")
    return _post_info_to_daemon(
        maestro_root,
        title="☀️ AFK cleared",
        body="\n".join(parts),
    )


# ---------------------------------------------------------------------------
# CLI


def _current_user() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or ""


def cmd_on(args: argparse.Namespace) -> int:
    root = _require_root()
    now = datetime.now(timezone.utc)
    expires_at: datetime | None = None
    if args.duration:
        try:
            delta = parse_duration(args.duration)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        expires_at = now + delta

    state = AfkState(
        enabled_at=now,
        expires_at=expires_at,
        source=args.source,
        enabled_by=args.as_user or _current_user(),
    )
    path = write_afk(root, state)
    if expires_at:
        print(f"AFK: on  (source: {state.source}, expires in {format_remaining(expires_at - now)})")
    else:
        print(f"AFK: on  (source: {state.source}, no expiry)")
    print(f"  file: {path}")
    notify_afk_enabled(root, state, reason=getattr(args, "reason", "") or "")
    return 0


def cmd_off(args: argparse.Namespace) -> int:
    root = _require_root()
    prior = read_afk(root)
    prior_source = prior.source if prior is not None else ""
    if clear_afk(root):
        print("AFK: off")
        notify_afk_cleared(
            root,
            prior_source=prior_source,
            reason=getattr(args, "reason", "") or "",
        )
    else:
        print("AFK: off  (already off)")
    return 0


def cmd_send_event(args: argparse.Namespace) -> int:
    """Hidden subcommand — fire the daemon notification without touching state.

    Used by ``hooks/ssh-auto-afk.sh`` after it writes the AFK file
    directly (the bash hook keeps the diagnostic ``signal:`` line that
    cmd_on doesn't emit). Always exits 0; a failed notify is fine.
    """
    root = find_maestro_root()
    if root is None:
        return 0
    if args.event == "enabled":
        state = read_afk(root)
        if state is None:
            return 0
        notify_afk_enabled(root, state, reason=args.reason or "")
    elif args.event == "cleared":
        notify_afk_cleared(
            root,
            prior_source=args.source or "",
            reason=args.reason or "",
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    root = _require_root()
    state = read_afk(root)
    if state is None:
        print("AFK: off")
        return 0
    if state.expires_at is None:
        remaining = "no expiry"
    else:
        now = datetime.now(timezone.utc)
        remaining = f"{format_remaining(state.expires_at - now)} remaining"
    who = f", by {state.enabled_by}" if state.enabled_by else ""
    print(f"AFK: on  (source: {state.source}, {remaining}{who})")
    print(f"  enabled_at: {state.enabled_at.isoformat()}")
    if state.expires_at is not None:
        print(f"  expires_at: {state.expires_at.isoformat()}")
    return 0


def _require_root() -> Path:
    root = find_maestro_root()
    if root is None:
        print(
            "ERROR: no maestro folder found. Run from inside a managed repo, "
            "set MAESTRO_ROOT, or create a .maestro marker.",
            file=sys.stderr,
        )
        sys.exit(1)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="maestro afk",
        description="Toggle remote-approval AFK mode",
    )
    subs = parser.add_subparsers(dest="subcommand", required=True)

    p_on = subs.add_parser("on", help="Enable AFK (optionally with duration)")
    p_on.add_argument(
        "duration", nargs="?", default=None,
        help="Duration (e.g. 30s, 15m, 8h, 2d, 1w, 1h30m). Omit for indefinite.",
    )
    p_on.add_argument(
        "--source", default="manual",
        choices=list(VALID_SOURCES),
        help="Why AFK is being enabled (default: manual)",
    )
    p_on.add_argument(
        "--as-user", default=None,
        help="Override enabled_by (defaults to $USER / $USERNAME)",
    )
    p_on.add_argument(
        "--reason", default="",
        help="Free-text reason included in the Telegram notification",
    )
    p_on.set_defaults(func=cmd_on)

    p_off = subs.add_parser("off", help="Disable AFK")
    p_off.add_argument(
        "--reason", default="",
        help="Free-text reason included in the Telegram notification",
    )
    p_off.set_defaults(func=cmd_off)

    # Hidden: hooks call this to fire a notification without rewriting the
    # AFK file. Underscore prefix keeps it out of casual help output even
    # though argparse still lists it.
    p_evt = subs.add_parser(
        "_send-event",
        help=argparse.SUPPRESS,
    )
    p_evt.add_argument("event", choices=("enabled", "cleared"))
    p_evt.add_argument("--source", default="",
                       help="Prior source (cleared events) for the message body")
    p_evt.add_argument("--reason", default="",
                       help="Free-text reason included in the notification")
    p_evt.set_defaults(func=cmd_send_event)

    p_status = subs.add_parser("status", help="Show current AFK state")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
