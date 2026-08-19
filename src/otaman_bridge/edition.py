"""Edition identity + capability gating (ce-ee-release-channels 3.1).

Honesty split (design Q3/Q3a):

- **Enforcement is package presence** (import probe): EE features live in
  EE-only packages the CE channel never delivers. A user-editable YAML must
  never gate a capability (F185 hardcoded-trust doctrine).
- **``~/.otaman/edition.yaml`` is identity, not enforcement**: it tells
  humans and UX surfaces what the install IS. Missing/unparseable file ==>
  edition UNKNOWN; probes still fully decide behavior. Readers MUST ignore
  unknown keys (forward-compat, per the co-signed Q3a schema record).
- Org-scoped by construction: one OS user == one org == one home, so the
  single unkeyed file is correct; the bridge runs as its org user and reads
  its own home's file.

Probe-vs-file mismatch is surfaced as a one-line diagnostic in ``/status``
— honest, cheap, and with no enforcement pretension.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger("maestro.bridge.edition")  # legacy: renamed at core 1.0

EDITION_CE = "ce"
EDITION_EE = "ee"
EDITION_UNKNOWN = "unknown"

#: One-line edition-boundary notice — tier information, not a failure.
CE_SPAWN_NOTICE = (
    "auto-session-spawn is part of the hosted/EE tier; the bridge runs without it "
    "(manual and direct-SSH session flows are unaffected)"
)

_ce_notice_emitted = False


def default_edition_path() -> Path:
    return Path.home() / ".otaman" / "edition.yaml"


def edition_identity(path: Path | None = None) -> str:
    """Return ``ce`` / ``ee`` / ``unknown`` from edition.yaml (identity only).

    Missing or unparseable file, or an unrecognized ``edition`` value, means
    UNKNOWN — the file is never load-bearing; probes decide behavior.
    """
    p = path or default_edition_path()
    try:
        import yaml  # noqa: PLC0415 — optional dep, avoid top-level

        data: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — any failure -> UNKNOWN, by contract
        return EDITION_UNKNOWN
    if not isinstance(data, dict):
        return EDITION_UNKNOWN
    edition = str(data.get("edition", "")).strip().lower()
    return edition if edition in (EDITION_CE, EDITION_EE) else EDITION_UNKNOWN


def ee_features_present() -> bool:
    """Import-probe for the EE package — the ENFORCEMENT signal (Q3)."""
    try:
        return importlib.util.find_spec("otaman_bridge_ee") is not None
    except (ImportError, ValueError):
        return False


def auto_session_spawn_available() -> bool:
    """Probe-gated availability of the auto-session-spawn subsystem."""
    return ee_features_present()


def mismatch_diagnostic(path: Path | None = None) -> str | None:
    """One-line honesty diagnostic when edition.yaml disagrees with the probe.

    Returns None when the file is absent/unknown or agrees with reality.
    """
    identity = edition_identity(path)
    if identity == EDITION_UNKNOWN:
        return None
    probed_ee = ee_features_present()
    if identity == EDITION_EE and not probed_ee:
        return "edition file says 'ee' but EE packages are not installed"
    if identity == EDITION_CE and probed_ee:
        return "edition file says 'ce' but EE packages are installed"
    return None


def edition_status(path: Path | None = None) -> dict[str, Any]:
    """Edition fields for ``/status`` — identity + probe-derived capability."""
    status: dict[str, Any] = {
        "edition": edition_identity(path),
        "auto_session_spawn": (
            "available" if auto_session_spawn_available() else "unavailable (EE)"
        ),
    }
    diag = mismatch_diagnostic(path)
    if diag is not None:
        status["edition_diagnostic"] = diag
    return status


def emit_ce_notice_once(logger: logging.Logger | None = None) -> bool:
    """Log the CE edition-boundary notice once per process; True if emitted."""
    global _ce_notice_emitted
    if auto_session_spawn_available() or _ce_notice_emitted:
        return False
    (logger or _log).info("%s", CE_SPAWN_NOTICE)
    _ce_notice_emitted = True
    return True


def runner_feature_unavailable_text(feature: str) -> str | None:
    """Edition-boundary wording for runner-backed features in CE.

    Returns None when EE packages are present (a runner SHOULD exist —
    callers keep their existing fault-style error). In CE the absence of a
    runner is an edition fact, not a failure, and the message says so.
    """
    if ee_features_present():
        return None
    return (
        f"{feature} is part of the hosted/EE tier and is not available in "
        "this Community Edition install. This is an edition boundary, not "
        "an error — see https://otaman.ai for the hosted tier."
    )
