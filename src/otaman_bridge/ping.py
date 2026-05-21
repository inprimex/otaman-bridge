"""otaman ping — explicitly surface a message to Telegram via the bridge.

Use when Claude (or the human) wants to proactively get the user's
attention on their phone, outside the normal PreToolUse / Stop-hook
paths. Posts a /notify to the daemon with the provided message.

    otaman ping "something's blocked and I need input"
    otaman ping --account riseapps "urgent: deploy failed"

Behaves like the Stop hook's notification path but:
  - No transcript read; user provides the body directly.
  - No debounce — explicit user-invoked call, respect it.
  - Requires the daemon to be running (no fail-safe silent path).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from otaman_core._resolve import find_maestro_root, read_expected_account, active_routing_env  # legacy: find_maestro_root renamed find_otaman_root at otaman-core 1.0  # noqa: E402


def _derive_account(project_root: Path) -> str | None:
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
    marker_account = read_expected_account(project_root)
    if marker_account:
        return marker_account
    return None


def _read_endpoint(account: str) -> tuple[int, str] | None:
    from otaman_bridge.daemon import endpoint_path  # noqa: PLC0415
    path = endpoint_path(account)  # respects OTAMAN_BRIDGE_DIR / MAESTRO_BRIDGE_DIR
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


def _session_project(project_root: Path | None) -> str:
    if project_root is None:
        return "(no project)"
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
    return project_root.name


def _current_agent(project_root: Path | None) -> str:
    if project_root is None:
        return ""
    agent_file = project_root / ".agents" / "current-agent"
    if not agent_file.is_file():
        return ""
    try:
        return agent_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_SEVERITIES = ("info", "approval", "blocking")


def ping(
    message: str,
    *,
    account: str | None = None,
    title: str | None = None,
    severity: str = "approval",
    project_root: Path | None = None,
) -> int:
    """Send a ping. Returns 0 on success, nonzero on error."""
    if severity not in _SEVERITIES:
        print(
            f"ERROR: severity must be one of {_SEVERITIES}; got {severity!r}",
            file=sys.stderr,
        )
        return 2
    if not message or not message.strip():
        print("ERROR: message is required", file=sys.stderr)
        return 2

    root = project_root or find_maestro_root()  # legacy: renamed find_otaman_root at otaman-core 1.0
    resolved_account = account or _derive_account(root or Path.cwd())
    if not resolved_account:
        print(
            "ERROR: cannot determine account. Pass --account NAME, or run from "
            "an otaman workspace with a .otaman marker.",
            file=sys.stderr,
        )
        return 1

    endpoint = _read_endpoint(resolved_account)
    if endpoint is None:
        print(
            f"ERROR: daemon endpoint missing for account {resolved_account!r}. "
            f"Start it with: otaman bridge run --account {resolved_account}",
            file=sys.stderr,
        )
        return 1
    port, token = endpoint

    agent = _current_agent(root)
    project = _session_project(root)
    effective_title = title or (
        f"Ping from {agent}" if agent else "Ping from Claude"
    )

    payload = {
        "account": resolved_account,
        "project": project,
        "severity": severity,
        "title": effective_title,
        "body": message,
        "source_agent": agent,
    }
    url = f"http://127.0.0.1:{port}/notify"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(req, timeout=5.0).read()
    except (urllib.error.URLError, OSError) as e:
        print(f"ERROR: POST /notify failed: {e}", file=sys.stderr)
        return 1

    print(
        f"Sent ping to {resolved_account} "
        f"({severity}, {len(message)} char body)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="otaman ping",
        description="Post a notification to the bridge (Telegram, etc.)",
    )
    parser.add_argument("message", nargs="+", help="Message body")
    parser.add_argument("--account", help="Account name (default: auto-detected)")
    parser.add_argument("--title", help="Custom title (default: 'Ping from <agent>')")
    parser.add_argument(
        "--severity", default="approval", choices=_SEVERITIES,
        help="Severity level (default: approval = yellow, grabs attention)",
    )
    args = parser.parse_args(argv)

    body = " ".join(args.message)
    return ping(
        body,
        account=args.account,
        title=args.title,
        severity=args.severity,
    )


if __name__ == "__main__":
    sys.exit(main())
