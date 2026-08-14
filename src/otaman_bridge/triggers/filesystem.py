"""Production FileSystemEventSource — inotify/FSEvents-backed bus trigger.

Implements the ``EventSource`` protocol so ``NatsEventSource`` can be swapped
in for Mode 2+ without changing spawn-decision callers.

Startup recovery: ``process_pending()`` should be called after ``start()`` to
drain any files that arrived while the bridge was offline.

Partial-write safety: new .md files are re-read with exponential backoff until
YAML frontmatter parses cleanly (or the retry budget is exhausted).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

try:
    from watchdog.events import (
        FileCreatedEvent,
    )
    from watchdog.events import (
        FileSystemEventHandler as _WDHandler,
    )
    from watchdog.observers import Observer as _Observer

    _WATCHDOG = True
except ImportError:  # pragma: no cover
    _WATCHDOG = False

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

_RETRY_DELAYS = (0.05, 0.1, 0.25, 0.5)  # seconds between read retries


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EventSource(Protocol):
    """Abstract event source — filesystem today, NATS in Mode 2+.

    Implementors call ``handler(path)`` for each new bus message file.
    The bridge's spawn-decision module registers one handler and never
    cares which backend delivers the event.
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def process_pending(self) -> None: ...

    @property
    def running(self) -> bool: ...


# ---------------------------------------------------------------------------
# BusFileEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BusFileEvent:
    """Emitted for each new valid .md file appearing in the watched directory."""

    path: Path
    detected_at: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Production FileSystemEventSource
# ---------------------------------------------------------------------------


class FileSystemEventSource:
    """Watches a bus active/ directory via watchdog (inotify on Linux, FSEvents on macOS).

    - Delivers events only for .md files with parseable YAML frontmatter.
    - Retries reads for partial writes (file created before write completes).
    - ``process_pending()`` drains files present at startup (restart recovery).
    - Thread-safe: ``start()``/``stop()`` are safe to call from any thread.
    """

    def __init__(
        self,
        watch_dir: Path,
        handler: Callable[[BusFileEvent], None],
        *,
        seen: set[Path] | None = None,
    ) -> None:
        self._watch_dir = watch_dir
        self._handler = handler
        self._seen: set[Path] = seen if seen is not None else set()
        self._lock = threading.Lock()
        self._observer: object | None = None
        self._started = False

    # ------------------------------------------------------------------
    # EventSource protocol
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the OS-level file watcher. Safe to call once."""
        if not _WATCHDOG:
            _log.warning("watchdog not installed — FileSystemEventSource is inactive")
            return
        with self._lock:
            if self._started:
                return
            obs = _Observer()
            obs.schedule(_Handler(self._dispatch), str(self._watch_dir), recursive=False)
            obs.start()
            self._observer = obs
            self._started = True
        _log.info("FileSystemEventSource started on %s", self._watch_dir)

    def stop(self) -> None:
        """Stop the watcher and join the observer thread."""
        with self._lock:
            obs = self._observer
            self._started = False
            self._observer = None
        if obs is not None:
            obs.stop()  # type: ignore[union-attr]
            obs.join(timeout=3.0)  # type: ignore[union-attr]
        _log.info("FileSystemEventSource stopped")

    def process_pending(self) -> None:
        """Dispatch any .md files in watch_dir that haven't been seen yet.

        Call this after ``start()`` to recover messages that arrived while
        the bridge was offline.
        """
        active = self._watch_dir
        if not active.is_dir():
            return
        for path in sorted(active.glob("*.md")):
            self._dispatch(path)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._started

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _dispatch(self, path: Path) -> None:
        with self._lock:
            if path in self._seen:
                return
            self._seen.add(path)

        if not _read_with_retry(path):
            _log.debug("Dropping unparseable bus file: %s", path)
            return

        evt = BusFileEvent(path=path)
        try:
            self._handler(evt)
        except Exception:
            _log.exception("EventSource handler raised for %s", path)


def _read_with_retry(path: Path) -> bool:
    """Return True if the file has parseable YAML frontmatter; retry for partial writes."""
    if _yaml is None:
        return path.exists()
    for i, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        if "---" in text and _parse_frontmatter(text) is not None:
            return True
        if delay is None:
            break
        _log.debug("Bus file not ready yet, retry %d: %s", i + 1, path)
        time.sleep(delay)
    return False


def _parse_frontmatter(text: str) -> dict | None:
    if _yaml is None:
        return None
    try:
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        fm = _yaml.safe_load(parts[1])
        return fm if isinstance(fm, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Watchdog handler (private)
# ---------------------------------------------------------------------------

if _WATCHDOG:

    class _Handler(_WDHandler):
        def __init__(self, callback: Callable[[Path], None]) -> None:
            super().__init__()
            self._cb = callback

        def on_created(self, event: FileCreatedEvent) -> None:
            if not event.is_directory and str(event.src_path).endswith(".md"):
                self._cb(Path(str(event.src_path)))
else:

    class _Handler:  # type: ignore[no-redef]
        def __init__(self, *a, **kw) -> None:
            pass
