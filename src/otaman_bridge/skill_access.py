"""Mode 2+ cofounder-only skill enforcement gate (tasks 3.1–3.2).

Enforcement model (design.md Q3):
  Mode 1 (identity.provider absent or not "zitadel"):
      No bridge check — plugin-side honor enforcement applies.
  Mode 2+ (identity.provider: zitadel):
      Bridge is the hard gate. CallContext.roles must contain "cofounder"
      for skills with access: cofounder-only. Skill body never reaches a
      non-cofounder session in Mode 2+.

Usage::
    result = check_skill_access(
        "tech-startup:investor-targeting-strategist",
        "cofounder-only",
        call_context,
        platform_yaml_path=path,
    )
    if result is not None:
        # return structured error to caller — do not forward skill body
        ...
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from otaman_bridge.mcp_server import CallContext

__all__ = [
    "is_zitadel_mode",
    "check_skill_access",
]


def is_zitadel_mode(platform_yaml_path: Path | str | None) -> bool:
    """Return True when platform.yaml declares ``identity.provider: zitadel``."""
    if platform_yaml_path is None or _yaml is None:
        return False
    try:
        text = Path(platform_yaml_path).read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = _yaml.safe_load(text) or {}
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    identity = data.get("identity")
    return isinstance(identity, dict) and identity.get("provider") == "zitadel"


def check_skill_access(
    skill_id: str,
    skill_access: str,
    call_context: CallContext,
    *,
    platform_yaml_path: Path | str | None = None,
) -> dict | None:
    """Gate cofounder-only skills in Mode 2+ (Zitadel).

    Args:
        skill_id: Fully-qualified skill ID (e.g. "tech-startup:investor-targeting-strategist").
        skill_access: Access level from pack.yaml (e.g. "public", "cofounder-only").
        call_context: Caller identity; roles come from Zitadel JWT in Mode 2+.
        platform_yaml_path: Path to platform.yaml. None → Mode 1 (no check).

    Returns:
        None if access is granted (skill body may be forwarded).
        dict with the task 3.2 error shape if access is denied::

            {"error": "skill_access_denied", "skill": "<id>", "required_role": "cofounder"}
    """
    if skill_access != "cofounder-only":
        return None

    if not is_zitadel_mode(platform_yaml_path):
        return None

    if "cofounder" in call_context.roles:
        return None

    return {
        "error": "skill_access_denied",
        "skill": skill_id,
        "required_role": "cofounder",
    }
