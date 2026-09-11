"""Acting-human identity + attribution for multi-human sessions (team-mode 2.1 / B1).

Bridge-owned writer half of B1 (``team-mode-registers-and-sessions``,
``specs/multi-human-sessions`` requirement 1, design D3-B1). Given a session's
authenticated human — the ``sub`` of the attach JWT the bridge mints via
``ce_web_auth`` — this resolves the human's ``human-roster`` entry and exposes
the acting-human context to the session as ``<workspace>/.otaman/acting-human.json``.

The plugin's ``SessionStart`` hook and its commit / bus-message / registry-transition
stamping read that file (the same ``.otaman/`` state-file convention the
``last-user-activity`` / ``afk`` hooks already use); see the bridge↔plugin seam
agreed on the bus (bridge writes, plugin reads).

**Hook-point (runner-mediated).** The bridge only *mints* the attach JWT; the
WS-attach that validates it and spawns the per-human session lives in the runner
(EE). So the runner invokes :func:`write_acting_human` with the validated ``sub``
BEFORE it spawns the session (task 2.2, step 2) — this module is the mechanism,
not the call site.

**Inert on single-human (CE / founder mode).** When the ``human-roster`` has
fewer than two entries there is no multi-human tenant, so nothing is written and
any stale file is removed. The plugin reader treats an absent file as
"single-human, no attribution" — so CE behavior is unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger("maestro.bridge.acting_human")  # legacy: logger renamed at otaman-core 1.0

#: Session-scoped acting-human state file, under the workspace ``.otaman/`` dir.
ACTING_HUMAN_FILENAME = "acting-human.json"

#: Provenance marker recorded in the file — the identity came from the attach JWT.
SOURCE_ATTACH_JWT = "attach-jwt"


def default_acting_human_path(project_root: Path) -> Path:
    """Path to the acting-human state file for a session workspace."""
    return Path(project_root) / ".otaman" / ACTING_HUMAN_FILENAME


def _load_roster(project_root: Path) -> list:
    """Read the ``human-roster`` list from the program's platform.yaml ([] on any failure).

    Accepts platform.yaml either directly under ``project_root`` or under
    ``project_root/otaman-meta`` (the canonical program layout), mirroring the
    lookup used elsewhere in the bridge.
    """
    root = Path(project_root)
    candidates = (root / "platform.yaml", root / "otaman-meta" / "platform.yaml")
    for platform_yaml in candidates:
        if not platform_yaml.is_file():
            continue
        try:
            import yaml  # noqa: PLC0415 — optional dep, avoid top-level

            data = yaml.safe_load(platform_yaml.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — any failure -> no roster
            return []
        roster = data.get("human-roster", [])
        return roster if isinstance(roster, list) else []
    return []


def is_multi_human(roster: list) -> bool:
    """True when the roster defines a multi-human tenant (two or more people).

    Single-human (0 or 1) is CE / founder mode — the B1 mechanism is inert there.
    """
    return sum(1 for p in roster if isinstance(p, dict)) >= 2


def resolve_acting_human(sub: str, roster: list) -> dict | None:
    """Resolve the acting-human context for ``sub`` on this tenant, or None if inert.

    Returns ``None`` (no attribution) when the tenant is single-human (CE) or
    ``sub`` is empty. On a multi-human tenant, returns
    ``{email, name, roles, source}``: ``name``/``roles`` come from the matching
    ``human-roster`` entry (matched case-insensitively on ``email``); a human who
    authenticated but is not on the roster is still attributed by email with empty
    roles, so the acting-human stamp is never silently dropped.
    """
    sub = (sub or "").strip()
    if not sub or not is_multi_human(roster):
        return None
    for person in roster:
        if not isinstance(person, dict):
            continue
        if str(person.get("email", "")).strip().lower() == sub.lower():
            roles = person.get("roles", [])
            return {
                "email": sub,
                "name": str(person.get("name", "") or ""),
                "roles": [str(r) for r in roles] if isinstance(roles, list) else [],
                "source": SOURCE_ATTACH_JWT,
            }
    # Authenticated but not rostered: attribute the human, no roles to inject.
    return {"email": sub, "name": "", "roles": [], "source": SOURCE_ATTACH_JWT}


def write_acting_human(project_root: Path, sub: str) -> Path | None:
    """Expose the acting-human for ``sub`` to the session workspace; inert-safe.

    Loads the program roster, resolves the acting-human, and atomically writes
    ``<project_root>/.otaman/acting-human.json``. Returns the path written, or
    ``None`` when the tenant is single-human / ``sub`` is unusable — in which case
    any stale file is removed so a resumed workspace never carries a prior human's
    attribution. This is the mechanism the runner invokes at session spawn.
    """
    context = resolve_acting_human(sub, _load_roster(project_root))
    if context is None:
        clear_acting_human(project_root)
        return None
    path = default_acting_human_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic; matches the .otaman/ state-file convention
    _log.debug("acting-human exposed for %s (%d roles)", context["email"], len(context["roles"]))
    return path


def clear_acting_human(project_root: Path) -> None:
    """Remove the acting-human state file if present (session teardown / inert)."""
    try:
        default_acting_human_path(project_root).unlink()
    except FileNotFoundError:
        pass
    except OSError:  # noqa: BLE001 — best-effort cleanup, never raise
        _log.debug("could not remove acting-human file", exc_info=True)


__all__ = [
    "ACTING_HUMAN_FILENAME",
    "SOURCE_ATTACH_JWT",
    "clear_acting_human",
    "default_acting_human_path",
    "is_multi_human",
    "resolve_acting_human",
    "write_acting_human",
]
