"""PreToolUse helper — read AFK, POST /approval to daemon, translate response.

Invoked by ``hooks/bridge-approval.sh`` only when the AFK flag file is
present (cheap fast-path stays in bash). This script does the Python-ish
work: parse AFK, resolve the daemon endpoint, craft a JSON request,
post it, translate the daemon response into the Claude Code hook
output format, and exit with the right code.

**Fail-safe contract** (see §5.5 of the design doc):
Any failure path — AFK expired, no account resolvable, no endpoint
file, daemon unreachable, HTTP error, malformed response — exits 0
with no opinion. The user's native Claude Code prompt then takes
over. The bridge must never make Claude harder to use than stock.

stdin (from Claude Code hook):
    { "tool_name": "Bash", "tool_input": {...}, "session_id": "...", ... }

stdout on block / explicit allow / ask (per Claude Code hook protocol):
    { "hookSpecificOutput": { "permissionDecision": "allow|deny|ask" },
      "systemMessage": "..." }

Exit codes:
    0 — allow or ask (no opinion)
    2 — deny (permission blocked)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from otaman_core._resolve import find_maestro_root, read_expected_account, active_routing_env  # legacy: find_maestro_root renamed find_otaman_root at otaman-core 1.0  # noqa: E402
from otaman_bridge.afk import read_afk


def _log_warn(msg: str) -> None:
    """Surface a warning to the user without blocking the prompt."""
    print(f"otaman bridge: {msg}", file=sys.stderr)


def _emit_allow_and_exit() -> None:
    """No opinion — Claude proceeds with its default (shows native prompt)."""
    sys.exit(0)


def _emit_decision(decision: str, reason: str = "") -> None:
    """Emit a hookSpecificOutput and exit with the right code for `decision`.

    Claude Code's hook protocol requires ``hookEventName`` inside
    ``hookSpecificOutput`` — without it the output validation fails
    and the hook's decision is silently ignored (treated as no-opinion).
    """
    payload: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        },
    }
    if reason:
        payload["systemMessage"] = reason
    print(json.dumps(payload))
    sys.exit(2 if decision == "deny" else 0)


def _derive_account(project_root: Path) -> str | None:
    """Figure out which account this session belongs to.

    Priority:
      1. ``$OTAMAN_ACTIVE_ROUTING`` — set by the launcher (most reliable).
      2. ``CLAUDE_CONFIG_DIR`` basename — ``~/.claude-<name>`` → ``<name>``.
      3. The managed repo's ``.otaman`` marker ``expected_account`` field.

    Returns the account name, or ``None`` if nothing resolves — in which
    case the hook short-circuits to fail-safe (native prompt).
    """
    env_account = active_routing_env()
    if env_account:
        return env_account

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if config_dir:
        base = os.path.basename(config_dir.rstrip("/\\"))
        if base.startswith(".claude-"):
            return base[len(".claude-"):]
        if base in ("", ".claude"):
            return "default"

    marker_account = read_expected_account(Path.cwd())
    if marker_account:
        return marker_account

    return None


def _endpoint_file(account: str) -> Path:
    """Standard endpoint file path — delegates to the shared resolver so
    ``OTAMAN_BRIDGE_DIR`` override applies here too.
    """
    from otaman_bridge.daemon import endpoint_path
    return endpoint_path(account)


def _read_endpoint(account: str) -> tuple[int, str] | None:
    """Return (port, token) from the endpoint file, or None if unavailable."""
    path = _endpoint_file(account)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        port = int(data.get("port") or 0)
        token = str(data.get("token") or "")
        if port > 0 and token:
            return port, token
    except (OSError, ValueError):
        return None
    return None


def _session_project(project_root: Path) -> str:
    """Best-effort project name (from platform.yaml).

    Single source of truth lives in bridge.bus_surface.resolve_project_name
    so hook-side (this file) and daemon-side (BusWatcher via bridge/cli.py)
    agree on the Telegram topic name. Falls back to a local scan if the
    bridge module isn't importable (e.g. partial install).
    """
    try:
        from otaman_bridge.bus_surface import resolve_project_name  # noqa: PLC0415
        return resolve_project_name(project_root)
    except ImportError:
        # Minimal fallback — mirrors resolve_project_name's first pass.
        platform_yaml = project_root / "platform.yaml"
        if not platform_yaml.is_file():
            return project_root.name
        try:
            text = platform_yaml.read_text(encoding="utf-8")
        except OSError:
            return project_root.name
        for line in text.splitlines():
            if line.startswith("project:"):
                _, _, value = line.partition(":")
                value = value.strip().strip("'").strip('"')
                if value:
                    return value
                break
        return project_root.name


def _session_repo(project_root: Path) -> str:
    """Best-effort repo name (the cwd if inside a managed repo; else '')."""
    try:
        cwd = Path.cwd().resolve()
        # If cwd is a direct child of project_root's parent (sibling repo layout)
        # or any descendant of one, take its top-level name.
        rel = cwd.relative_to(project_root.parent)
        return rel.parts[0] if rel.parts else ""
    except ValueError:
        return Path.cwd().name


def main() -> int:
    # --- Read the Claude Code hook input ---
    try:
        raw = sys.stdin.read()
        hook_input: dict[str, Any] = json.loads(raw) if raw else {}
    except (OSError, ValueError):
        _emit_allow_and_exit()
        return 0  # unreachable

    tool_name = str(hook_input.get("tool_name", ""))
    tool_input = hook_input.get("tool_input", {}) or {}
    if not tool_name:
        _emit_allow_and_exit()

    # --- Resolve otaman workspace root + AFK state ---
    project_root = find_maestro_root()
    if project_root is None:
        _emit_allow_and_exit()
        return 0  # unreachable

    state = read_afk(project_root)
    if state is None:
        # AFK off or expired — native prompt handles it.
        _emit_allow_and_exit()

    # --- Which account owns this session? ---
    account = _derive_account(project_root)
    if account is None:
        _log_warn("AFK is on but cannot determine account — falling back to native prompt")
        _emit_allow_and_exit()

    # --- Daemon endpoint ---
    endpoint = _read_endpoint(account)
    if endpoint is None:
        _log_warn(
            f"AFK is on but daemon endpoint missing for account {account!r}. "
            f"Run `otaman bridge run --account {account}` to start it."
        )
        _emit_allow_and_exit()
    port, token = endpoint

    # --- Figure out a reasonable agent name (current-agent file > cwd) ---
    agent = ""
    agent_file = project_root / ".agents" / "current-agent"
    if agent_file.is_file():
        try:
            agent = agent_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    # --- Build the ApprovalRequest payload ---
    # Timeout: default 9 minutes to stay under Claude Code's 10-minute hook cap.
    timeout_seconds = int(os.environ.get("MAESTRO_BRIDGE_TIMEOUT", "540"))
    priority = str(tool_input.get("priority", "normal")) if isinstance(tool_input, dict) else "normal"

    payload = {
        "account": account,
        "project": _session_project(project_root),
        "repo": _session_repo(project_root),
        "agent": agent,
        "tool_name": tool_name,
        "tool_input": tool_input if isinstance(tool_input, dict) else {"value": tool_input},
        "reason": "",
        "priority": priority,
        "timeout_seconds": timeout_seconds,
    }

    # --- POST /approval ---
    url = f"http://127.0.0.1:{port}/approval"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")

    try:
        # Give curl-level timeout a bit more room than the approval timeout.
        with urllib.request.urlopen(req, timeout=timeout_seconds + 15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log_warn(
            f"daemon unreachable (account={account}): {e}. "
            f"Falling back to native prompt."
        )
        _emit_allow_and_exit()
        return 0  # unreachable

    # --- Translate daemon response ---
    decision = str(body.get("decision", "ask"))
    message = str(body.get("message") or body.get("responder") or "")
    if decision not in ("allow", "deny", "ask", "timeout"):
        _log_warn(f"daemon returned unknown decision {decision!r}")
        _emit_allow_and_exit()

    # "timeout" is effectively "no decision" — fall back to native prompt.
    if decision == "timeout":
        _emit_allow_and_exit()

    if decision == "allow":
        # Claude Code accepts {"decision": "allow"} OR exit 0 with no output.
        # Emitting explicit allow is more auditable + lets us pass a reason.
        _emit_decision("allow", message)

    if decision == "deny":
        reason = body.get("message") or f"denied by {body.get('responder', 'reviewer')}"
        _emit_decision("deny", str(reason))

    # decision == "ask"
    _emit_decision("ask", message)
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
