#!/usr/bin/env python3
"""Enforce the messenger-transport abstraction boundary (§10 design doc).

All transport-specific imports must live inside files in ``bridge/transports/``.
Anywhere else is a bug — the abstraction is leaking. This lint fails the
build on any forbidden import.

Usage::

    python3 scripts/check_transport_boundary.py           # runs, exits 0 / 1
    pytest tests/test_transport_boundary.py               # also runs the check

Forbidden libraries (add here as new transports are introduced):
    telegram, python-telegram-bot, telethon, pyrogram,
    slack_sdk, slack-bolt, slack-sdk,
    discord, discord.py, disnake, nextcord,
    nio, matrix-nio, matrix-client.

Allowed locations: any file directly under ``bridge/transports/``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# Layout: src/otaman_bridge/check_transport_boundary.py — repo root is parent.parent.parent
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PKG_DIR = Path(__file__).resolve().parent  # otaman_bridge/
TRANSPORTS_DIR = PKG_DIR / "transports"

FORBIDDEN_PREFIXES = (
    # Telegram
    "telegram",
    "telethon",
    "pyrogram",
    # Slack
    "slack_sdk",
    "slack_bolt",
    "slack",
    # Discord
    "discord",
    "disnake",
    "nextcord",
    # Matrix
    "nio",
    "matrix_client",
    "matrix_nio",
)

# Paths to skip entirely. Relative to REPO_ROOT.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".claude",
}


def _is_forbidden_module(module: str) -> bool:
    """Return True if an import like ``import <module>`` is forbidden.

    Matches exact name or dotted prefix (``telegram.ext`` matches ``telegram``).
    """
    if not module:
        return False
    head = module.split(".")[0]
    return head in FORBIDDEN_PREFIXES


def _collect_imports(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, module_name) for every import in ``path``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append((node.lineno, node.module))
    return out


def _should_skip(path: Path) -> bool:
    """Skip files in known-uninteresting directories and the transports package."""
    rel = path.relative_to(REPO_ROOT)
    parts = rel.parts
    for skip in SKIP_DIRS:
        if skip in parts:
            return True
    # Files inside any *transports/ directory are allowed transport libs (legacy bridge/transports/ + new otaman_bridge/transports/).
    if "transports" in parts:
        return True
    return False


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    violations: list[tuple[Path, int, str]] = []

    for py in REPO_ROOT.rglob("*.py"):
        if _should_skip(py):
            continue
        for lineno, module in _collect_imports(py):
            if _is_forbidden_module(module):
                violations.append((py, lineno, module))

    if not violations:
        print("transport boundary OK "
              f"(scanned .py files outside bridge/transports/)")
        return 0

    print("ERROR: transport-specific imports found outside bridge/transports/:")
    print()
    for path, lineno, module in violations:
        rel = path.relative_to(REPO_ROOT)
        print(f"  {rel}:{lineno}  imports {module!r}")
    print()
    print(
        "These imports break the Transport abstraction (design §10). "
        "Move the code into bridge/transports/ or mediate it through "
        "the Transport Protocol instead.",
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
