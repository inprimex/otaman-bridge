from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers import Observer

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BusFileEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BusFileEvent:
    "Emitted when a new .md file appears in the bus active directory."
    path: Path
    detected_at: float = field(default_factory=time.monotonic)  # seconds since epoch (monotonic)


# ---------------------------------------------------------------------------
# FileSystemEventSource
# ---------------------------------------------------------------------------


class FileSystemEventSource:
    """Watches a directory with watchdog and dispatches BusFileEvents.

    Designed as a drop-in replacement for polling (BusWatcher) with lower latency.
    The dispatcher callback is invoked from a watchdog OS-thread; callers that
    need asyncio should use schedule_in_loop().

    Thread safety: start() / stop() are thread-safe. The dispatcher callback MAY
    be called from a non-main thread; callers are responsible for any needed locking.

    Event source abstraction (per design.md Q4):
      - Mode 1: FileSystemEventSource (this class, backed by watchdog/inotify)
      - Mode 2+: NatsEventSource (future, backed by NATS subject subscription)
    """

    def __init__(
        self,
        watch_dir: Path,
        dispatcher: Callable[[BusFileEvent], None],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._watch_dir = watch_dir
        self._dispatcher = dispatcher
        self._loop = loop
        self._observer: Observer | None = None
        self._lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        "Start the OS-level file watcher. Safe to call once."
        with self._lock:
            if self._started:
                return
            observer = Observer()
            observer.schedule(
                _BusHandler(self._dispatch_raw),
                str(self._watch_dir),
                recursive=False,
            )
            observer.start()
            self._observer = observer
            self._started = True
        _log.info(
            "FileSystemEventSource started on %s (backend: %s)",
            self._watch_dir,
            observer.__class__.__name__,
        )

    def stop(self) -> None:
        "Stop the watcher and join the observer thread."
        with self._lock:
            if not self._started:
                return
            obs = self._observer
            self._started = False
            self._observer = None
        if obs is not None:
            obs.stop()
            obs.join(timeout=3.0)
        _log.info("FileSystemEventSource stopped")

    @property
    def running(self) -> bool:
        with self._lock:
            return self._started

    # ------------------------------------------------------------------
    # Async helper
    # ------------------------------------------------------------------

    def schedule_in_loop(
        self,
        async_handler: Callable[[BusFileEvent], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Wire an async handler to receive events via asyncio.run_coroutine_threadsafe.

        Watchdog callbacks run in a non-asyncio thread. This method bridges the
        threading gap: events are posted to the given loop from the watchdog thread.
        """
        def sync_bridge(evt: BusFileEvent) -> None:
            asyncio.run_coroutine_threadsafe(async_handler(evt), loop)

        self._dispatcher = sync_bridge
        self._loop = loop

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _dispatch_raw(self, path: Path) -> None:
        evt = BusFileEvent(path=path)
        try:
            self._dispatcher(evt)
        except Exception:
            _log.exception("dispatcher raised unexpectedly for %s", path)


# ---------------------------------------------------------------------------
# Watchdog handler (private)
# ---------------------------------------------------------------------------


class _BusHandler(FileSystemEventHandler):
    "Translate watchdog events to BusFileEvent callbacks."

    def __init__(self, callback: Callable[[Path], None]) -> None:
        super().__init__()
        self._callback = callback

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if path.suffix == ".md":
            self._callback(path)

