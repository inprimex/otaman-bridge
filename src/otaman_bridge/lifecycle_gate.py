"""Program-lifecycle runtime enforcement for the bridge (program-lifecycle-states 2.2).

The bridge is a per-program service (``otaman-bridge@<program>``). Its runtime
behavior is gated on the program's lifecycle state, resolved through core's
single read point (design D1): ``otaman_core.lifecycle.read_program_state``.

Per design D2:

- **active / limited** → normal operation. ``limited`` only gates *new* spawns,
  which is the runner's concern; the bridge is unaffected.
- **suspended** → the bridge stays up but goes **inert**: it stops surfacing bus
  cards and stops its AFK/watch behavior for the program. A later resume
  (→ active) restores it with no restart.
- **archived** → **inert** as well. Deploy's step 3 also stops + disables the
  per-program unit and moves the folder; this in-process gate is the
  belt-and-suspenders so a still-running unit does nothing. ``unarchive``
  (→ active) restores it.

Only the per-program bridge is affected — never shared daemons.

**Fail-safe**: any resolution/read failure, or an absent registry, resolves to
``active``. The bridge never wrongly goes inert (worst case is a missed
enforcement, which the state re-read on the next scan corrects once resolvable).
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger("maestro.bridge.lifecycle_gate")  # legacy: renamed at core 1.0

ACTIVE = "active"

#: States in which the per-program bridge goes inert (no surfacing, no AFK/watch).
INERT_STATES = frozenset({"suspended", "archived"})


def resolve_org_and_program(project_root: Path) -> tuple[Path, str] | None:
    """Resolve ``(org_root, program)`` from the watched program root.

    Canonical layout: ``<org_root>/programs/<program>/otaman-meta``. The org root
    holds ``config/lifecycle.yaml``; ``<program>`` is the program folder name (the
    same identifier used by the ``otaman-bridge@<program>`` unit and the archive
    path). Returns None when the path has no ``programs`` ancestor (caller then
    treats the state as active).
    """
    parts = Path(project_root).resolve().parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "programs" and i + 1 < len(parts):
            return Path(*parts[:i]), parts[i + 1]
    return None


def program_lifecycle_state(project_root: Path) -> str:
    """Return the watched program's lifecycle state; ``active`` on any failure."""
    try:
        resolved = resolve_org_and_program(project_root)
        if resolved is None:
            return ACTIVE
        org_root, program = resolved
        from otaman_core.lifecycle import read_program_state  # noqa: PLC0415

        return read_program_state(org_root, program)
    except Exception:  # noqa: BLE001 — never let lifecycle wiring break the watch loop
        _log.debug("lifecycle state resolution failed; defaulting to active", exc_info=True)
        return ACTIVE


def is_inert(state: str) -> bool:
    """True when the per-program bridge should suspend its work for this state."""
    return state in INERT_STATES


__all__ = [
    "ACTIVE",
    "INERT_STATES",
    "is_inert",
    "program_lifecycle_state",
    "resolve_org_and_program",
]
