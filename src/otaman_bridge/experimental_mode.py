"""Experimental multi-tenant mode detection and banner emission.

ADR-012 gate 2 requires that any bridge running in ``experimental_multi_tenant``
mode makes this unambiguous to operators and surfaces it in all approval
prompts sent to the human.

Canonical banner text lives here: this module is the single source of truth
for the actual strings so all code paths import from one place rather than
duplicating text.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger("maestro.bridge.experimental_mode")  # legacy: renamed at core 1.0

# ---------------------------------------------------------------------------
# Runtime-mode constants

RUNTIME_MODE_SINGLE = "single"
RUNTIME_MODE_EXPERIMENTAL_MULTI_TENANT = "experimental_multi_tenant"

# ---------------------------------------------------------------------------
# Canonical banner text — this module is the single source of truth.

#: One-line prefix for Telegram messages and log lines.
BANNER_ONELINE = (
    "⚠️ EXPERIMENTAL MULTI-TENANT MODE — not validated for production; data isolation not audited"
)

#: Multi-line block for startup log and CLI status output.
BANNER_BLOCK = (
    "╔══════════════════════════════════════════════════════════════════╗\n"
    "║  ⚠️  EXPERIMENTAL MULTI-TENANT MODE                              ║\n"
    "║                                                                  ║\n"
    "║  This bridge is running in experimental_multi_tenant mode.       ║\n"
    "║  Data isolation between Organisations is NOT audited.            ║\n"
    "║  Upgrade from this mode is manual (no automated migration).      ║\n"
    "║  Use only on non-production workspaces.                          ║\n"
    "╚══════════════════════════════════════════════════════════════════╝"
)

#: Short label for web UI footer / status payloads.
BANNER_LABEL = "experimental_multi_tenant"


# ---------------------------------------------------------------------------
# Detection


def detect_runtime_mode(project_root: Path) -> str | None:
    """Return the ``runtime.multi_tenant.mode`` value from platform.yaml.

    Search order (mirrors ``find_otaman_root`` workspace layouts):
      1. ``<project_root>/_platform/platform.yaml``  — multi-tenant layout
      2. ``<project_root>/platform.yaml``             — flat layout

    Returns ``None`` when no platform.yaml is found or the field is absent.
    """
    candidates = [
        project_root / "_platform" / "platform.yaml",
        project_root / "platform.yaml",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            import yaml as _yaml  # noqa: PLC0415 — optional dep, avoid top-level

            data: Any = _yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        runtime = data.get("runtime")
        if isinstance(runtime, dict):
            mt = runtime.get("multi_tenant")
            if isinstance(mt, dict):
                mode = mt.get("mode")
                if mode is not None:
                    return str(mode)
    return None


def is_experimental(project_root: Path) -> bool:
    """Return True when the workspace declares ``experimental_multi_tenant`` mode."""
    return detect_runtime_mode(project_root) == RUNTIME_MODE_EXPERIMENTAL_MULTI_TENANT


# ---------------------------------------------------------------------------
# Emission helpers


def emit_startup_banner(
    project_root: Path,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Emit the experimental-mode banner to the logger if mode is experimental.

    Called once at daemon startup (from ``cmd_run``).  Returns True when the
    banner was emitted, False when the workspace is in normal single mode.
    """
    if not is_experimental(project_root):
        return False
    log = logger or _log
    for line in BANNER_BLOCK.splitlines():
        log.warning("%s", line)
    return True


def prefix_approval_body(body: str, project_root: Path) -> str:
    """Prepend the one-line experimental banner to a Telegram approval body.

    Called by the daemon's transport layer when sending approval requests so
    the human sees the experimental-mode warning on every prompt, not just at
    startup.  No-ops when the workspace is in normal single mode.
    """
    if not is_experimental(project_root):
        return body
    return f"{BANNER_ONELINE}\n\n{body}"


def healthz_extras(project_root: Path | None) -> dict[str, Any]:
    """Return experimental-mode fields for the ``/healthz`` response.

    Included in the health payload so monitoring can detect mode without
    parsing logs.
    """
    if project_root is None:
        return {}
    mode = detect_runtime_mode(project_root)
    if mode is None:
        return {}
    result: dict[str, Any] = {"runtime_mode": mode}
    if mode == RUNTIME_MODE_EXPERIMENTAL_MULTI_TENANT:
        result["experimental_warning"] = BANNER_ONELINE
    return result
