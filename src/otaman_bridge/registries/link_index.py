"""In-memory link index for outcome↔solution relationships.

Reads outcomes.yaml + solutions.yaml on startup and on file-change events.
Builds three maps used by ``otaman outcome show --with-solutions`` and
``otaman solution show --tasks-status``:

    outcome-id  → [solution-ids]
    solution-id → outcome-id
    solution-id → [dep-refs]        (outcome/solution kind deps only)

Thread-safe: all mutations hold ``_lock``; reads take a snapshot.
File-watching is optional — gracefully disabled when watchdog is absent.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore[assignment]

try:
    from watchdog.events import FileModifiedEvent, FileSystemEventHandler
    from watchdog.observers import Observer as _Observer

    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Index data
# ---------------------------------------------------------------------------


@dataclass
class _IndexData:
    outcome_to_solutions: dict[str, list[str]] = field(default_factory=dict)
    solution_to_outcome: dict[str, str] = field(default_factory=dict)
    solution_to_deps: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class RegistryLinkIndex:
    """In-memory index of outcome↔solution cross-references.

    Instantiate via ``RegistryLinkIndex(outcomes_path, solutions_path)`` with
    known paths, or via the convenience factory ``from_project_root(root)``
    which resolves paths from ``platform.yaml``.

    Call ``start()`` to enable live file-watching (requires watchdog).
    Call ``stop()`` to release the watcher thread.
    """

    def __init__(self, outcomes_path: Path, solutions_path: Path) -> None:
        self._outcomes_path = outcomes_path
        self._solutions_path = solutions_path
        self._lock = threading.RLock()
        self._data = _IndexData()
        self._observer: Any = None
        self._rebuild()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_project_root(
        cls,
        project_root: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> RegistryLinkIndex:
        """Resolve registry paths from *project_root* (otaman-meta directory).

        Path resolution:
          1. ``OTAMAN_BUSINESS_DIR`` env var (absolute path override)
          2. Repo with ``owner: cpo-agent`` in ``platform.yaml``
          3. Repo with ``owner: main-agent`` in ``platform.yaml``

        Raises ``FileNotFoundError`` if business dir cannot be resolved.
        """
        if env is None:
            env = dict(os.environ)
        business = _find_business_dir(project_root, env)
        if business is None:
            raise FileNotFoundError(
                f"Cannot locate business repo from {project_root}. "
                "Set OTAMAN_BUSINESS_DIR or add owner: cpo-agent to platform.yaml."
            )
        return cls(business / "outcomes.yaml", business / "solutions.yaml")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start file-watching. No-op if watchdog is not installed."""
        if not _WATCHDOG_AVAILABLE:
            _log.warning("watchdog not installed — RegistryLinkIndex will not auto-reload")
            return
        with self._lock:
            if self._observer is not None:
                return
            watched_dirs: set[Path] = {
                self._outcomes_path.parent,
                self._solutions_path.parent,
            }
            observer = _Observer()
            handler = _ReloadHandler(self._outcomes_path, self._solutions_path, self._rebuild)
            for d in watched_dirs:
                observer.schedule(handler, str(d), recursive=False)
            observer.start()
            self._observer = observer
        _log.info(
            "RegistryLinkIndex watching %s and %s",
            self._outcomes_path,
            self._solutions_path,
        )

    def stop(self) -> None:
        """Stop file-watching and release the observer thread."""
        with self._lock:
            obs = self._observer
            self._observer = None
        if obs is not None:
            obs.stop()
            obs.join(timeout=3.0)
            _log.info("RegistryLinkIndex stopped")

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def solutions_for_outcome(self, outcome_id: str) -> list[str]:
        """Return all solution ids linked to *outcome_id* (empty if none)."""
        with self._lock:
            return list(self._data.outcome_to_solutions.get(outcome_id, []))

    def outcome_for_solution(self, solution_id: str) -> str | None:
        """Return the outcome id for *solution_id*, or ``None``."""
        with self._lock:
            return self._data.solution_to_outcome.get(solution_id)

    def deps_for_solution(self, solution_id: str) -> list[str]:
        """Return dependency refs for *solution_id* (outcome/solution kinds only)."""
        with self._lock:
            return list(self._data.solution_to_deps.get(solution_id, []))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        data = _build_index(self._outcomes_path, self._solutions_path)
        with self._lock:
            self._data = data
        _log.debug(
            "RegistryLinkIndex rebuilt: %d outcomes, %d solutions",
            len(data.outcome_to_solutions),
            len(data.solution_to_outcome),
        )


# ---------------------------------------------------------------------------
# Watchdog handler (private)
# ---------------------------------------------------------------------------


if _WATCHDOG_AVAILABLE:

    class _ReloadHandler(FileSystemEventHandler):
        def __init__(
            self,
            outcomes_path: Path,
            solutions_path: Path,
            rebuild: Any,
        ) -> None:
            super().__init__()
            self._watched = {str(outcomes_path), str(solutions_path)}
            self._rebuild = rebuild

        def on_modified(self, event: FileModifiedEvent) -> None:
            if event.is_directory:
                return
            if str(event.src_path) in self._watched:
                _log.debug("Registry file changed: %s — rebuilding index", event.src_path)
                try:
                    self._rebuild()
                except Exception:
                    _log.exception("RegistryLinkIndex rebuild failed after file change")


# ---------------------------------------------------------------------------
# Index builder (pure function — easy to test)
# ---------------------------------------------------------------------------


def _build_index(outcomes_path: Path, solutions_path: Path) -> _IndexData:
    data = _IndexData()

    solutions_raw = _load_yaml_list(solutions_path, "solutions")
    for sol in solutions_raw:
        sol_id = sol.get("id")
        if not sol_id:
            continue
        outcome_id = sol.get("outcome-id")
        if outcome_id:
            data.solution_to_outcome[sol_id] = outcome_id
            data.outcome_to_solutions.setdefault(outcome_id, []).append(sol_id)

        deps: list[str] = []
        for dep in sol.get("dependencies") or []:
            if not isinstance(dep, dict):
                continue
            kind = dep.get("kind", "")
            ref = dep.get("ref")
            if kind in ("outcome", "solution") and ref:
                deps.append(ref)
        if deps:
            data.solution_to_deps[sol_id] = deps

    return data


def _load_yaml_list(path: Path, key: str) -> list[dict]:
    if _yaml is None:
        _log.error("PyYAML not installed — cannot load %s", path)
        return []
    if not path.exists():
        _log.debug("Registry file not found (index will be empty): %s", path)
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = _yaml.safe_load(f) or {}
        items = raw.get(key) or []
        return [i for i in items if isinstance(i, dict)]
    except Exception:
        _log.exception("Failed to parse %s", path)
        return []


# ---------------------------------------------------------------------------
# Path resolution helper (mirrors otaman-cli loader, no cross-package import)
# ---------------------------------------------------------------------------


def _find_business_dir(project_root: Path, env: dict[str, str]) -> Path | None:
    override = env.get("OTAMAN_BUSINESS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    platform_yaml = project_root / "platform.yaml"
    if not platform_yaml.exists() or _yaml is None:
        return None

    try:
        with open(platform_yaml, encoding="utf-8") as f:
            cfg = _yaml.safe_load(f) or {}
    except Exception:
        _log.exception("Failed to parse platform.yaml at %s", platform_yaml)
        return None

    repos = cfg.get("repos") or []
    if not isinstance(repos, list):
        return None

    for owner_hint in ("cpo-agent", "main-agent"):
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            if repo.get("owner") == owner_hint:
                rel = repo.get("path") or ""
                if rel:
                    return (project_root / rel).expanduser().resolve()
    return None
