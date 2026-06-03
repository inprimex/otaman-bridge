"""Warm-session linger manager (task 1.6).

When a [headless] session's task queue drains, ``SessionLingerManager`` starts a
linger timer (default 30 min, configurable per agent via platform.yaml
``agents.<slug>.linger_minutes``). A new task-assignment arriving within the linger
window resets the timer. After expiry the registry entry is released and a
session-released lifecycle event is emitted.

Thread-safe. All timer callbacks run on daemon threads (won't block process exit).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

DEFAULT_LINGER_SECONDS = 30 * 60.0  # 30 minutes


@dataclass
class _LingerEntry:
    agent_id: str
    human_id: str
    session_id: str
    change_id: str
    timer: threading.Timer | None = field(default=None, compare=False, repr=False)


class SessionLingerManager:
    """Manages linger timers for warm headless sessions.

    Typical usage:
    1. After a headless spawn succeeds: ``linger.start(agent_id, human_id, session_id, change_id)``
    2. When a task-assignment arrives for an existing warm session:
       ``linger.reset(agent_id, human_id)`` to extend the window.
    3. When a session is explicitly released: ``linger.cancel(agent_id, human_id)``

    The on_expire callback receives (agent_id, human_id, session_id, change_id) and
    is responsible for calling registry.release_session() and emitting lifecycle events.
    """

    def __init__(
        self,
        on_expire: Callable[[str, str, str, str], None],
        *,
        linger_seconds: float = DEFAULT_LINGER_SECONDS,
        linger_override: dict[str, float] | None = None,
    ) -> None:
        """
        Args:
            on_expire: Called on linger expiry with (agent_id, human_id, session_id, change_id).
            linger_seconds: Default linger duration in seconds.
            linger_override: Per-agent overrides keyed by agent_id, values in seconds.
        """
        self._on_expire = on_expire
        self._default_linger = linger_seconds
        self._overrides: dict[str, float] = linger_override or {}
        self._entries: dict[tuple[str, str], _LingerEntry] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        agent_id: str,
        human_id: str,
        session_id: str,
        change_id: str,
    ) -> None:
        """Start (or reset) the linger timer for (agent_id, human_id)."""
        key = (agent_id, human_id)
        delay = self._overrides.get(agent_id, self._default_linger)
        with self._lock:
            self._cancel_existing(key)
            entry = _LingerEntry(
                agent_id=agent_id,
                human_id=human_id,
                session_id=session_id,
                change_id=change_id,
            )
            t = threading.Timer(delay, self._fire, args=[key])
            t.daemon = True
            t.start()
            entry.timer = t
            self._entries[key] = entry
        _log.debug(
            "Linger timer started: (%s, %s) session=%s delay=%.0fs",
            agent_id, human_id, session_id, delay,
        )

    def reset(self, agent_id: str, human_id: str) -> bool:
        """Reset the linger timer if one exists. Returns True if a timer was found."""
        key = (agent_id, human_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            self._cancel_existing(key)
            delay = self._overrides.get(agent_id, self._default_linger)
            t = threading.Timer(delay, self._fire, args=[key])
            t.daemon = True
            t.start()
            entry.timer = t
        _log.debug("Linger timer reset: (%s, %s)", agent_id, human_id)
        return True

    def cancel(self, agent_id: str, human_id: str) -> bool:
        """Cancel the linger timer if one exists. Returns True if cancelled."""
        key = (agent_id, human_id)
        with self._lock:
            if key not in self._entries:
                return False
            self._cancel_existing(key)
            del self._entries[key]
        _log.debug("Linger timer cancelled: (%s, %s)", agent_id, human_id)
        return True

    def active_keys(self) -> list[tuple[str, str]]:
        """Return (agent_id, human_id) pairs with active linger timers."""
        with self._lock:
            return list(self._entries.keys())

    def shutdown(self) -> None:
        """Cancel all active timers. Call on daemon shutdown."""
        with self._lock:
            keys = list(self._entries.keys())
        for key in keys:
            self.cancel(*key)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cancel_existing(self, key: tuple[str, str]) -> None:
        entry = self._entries.get(key)
        if entry and entry.timer:
            entry.timer.cancel()
            entry.timer = None

    def _fire(self, key: tuple[str, str]) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is None:
            return  # was cancelled between timer fire and lock acquisition
        _log.info(
            "Linger expired for (%s, %s) session=%s",
            entry.agent_id, entry.human_id, entry.session_id,
        )
        try:
            self._on_expire(
                entry.agent_id,
                entry.human_id,
                entry.session_id,
                entry.change_id,
            )
        except Exception:
            _log.exception(
                "on_expire callback raised for (%s, %s)", entry.agent_id, entry.human_id
            )


# ---------------------------------------------------------------------------
# Platform.yaml linger_minutes loader
# ---------------------------------------------------------------------------


def load_linger_overrides(platform_yaml_path: Path) -> dict[str, float]:
    """Read agents.<slug>.linger_minutes from platform.yaml; return {agent_id: seconds}.

    Returns an empty dict if the file is missing, unreadable, or has no linger overrides.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    if not platform_yaml_path.is_file():
        return {}
    try:
        text = platform_yaml_path.read_text(encoding="utf-8")
        cfg = yaml.safe_load(text)
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}
    agents_block = cfg.get("agents", {})
    if not isinstance(agents_block, dict):
        return {}
    overrides: dict[str, float] = {}
    for agent_id, agent_cfg in agents_block.items():
        if not isinstance(agent_cfg, dict):
            continue
        linger_m = agent_cfg.get("linger_minutes")
        if isinstance(linger_m, (int, float)) and linger_m > 0:
            overrides[str(agent_id)] = float(linger_m) * 60.0
    return overrides


__all__ = [
    "DEFAULT_LINGER_SECONDS",
    "SessionLingerManager",
    "load_linger_overrides",
]
