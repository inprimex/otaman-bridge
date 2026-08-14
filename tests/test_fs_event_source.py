# Spike tests for FileSystemEventSource -- task 1.2 of auto-session-spawn.
# Validates watchdog catches file-create events reliably and measures latency.
from __future__ import annotations

import statistics
import threading
import time
from pathlib import Path

from otaman_bridge.fs_event_source import BusFileEvent, FileSystemEventSource


def _write_bus_file(active_dir: Path, stem: str) -> Path:
    p = active_dir / f"{stem}.md"
    p.write_text(
        f"---\nid: {stem}\nfrom: a\nto: b\ntype: info\n---\nbody\n",
        encoding="utf-8",
    )
    return p


class TestFileSystemEventSourceBasic:
    def test_single_file_detected(self, tmp_path):
        received: list[BusFileEvent] = []
        src = FileSystemEventSource(tmp_path, received.append)
        src.start()
        time.sleep(0.1)
        try:
            _write_bus_file(tmp_path, "test-001")
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not received:
                time.sleep(0.01)
        finally:
            src.stop()
        assert len(received) == 1
        assert received[0].path.name == "test-001.md"

    def test_non_md_files_ignored(self, tmp_path):
        received: list[BusFileEvent] = []
        src = FileSystemEventSource(tmp_path, received.append)
        src.start()
        time.sleep(0.1)
        try:
            (tmp_path / "lockfile.lock").write_text("x")
            (tmp_path / "metadata.json").write_text("{}")
            (tmp_path / "otaman-message.md").write_text("---\n---\nbody\n")
            time.sleep(0.3)
        finally:
            src.stop()
        md_events = [e for e in received if e.path.suffix == ".md"]
        assert len(md_events) == 1

    def test_start_stop_idempotent(self, tmp_path):
        src = FileSystemEventSource(tmp_path, lambda e: None)
        src.start()
        src.start()  # second start is a no-op
        assert src.running
        src.stop()
        src.stop()  # second stop is a no-op
        assert not src.running


class TestFileSystemEventSourceLatency:
    def test_median_latency_under_50ms(self, tmp_path):
        # Measures detection latency for 20 sequential file creates.
        # Acceptance: median < 50 ms on Linux (inotify backend).
        # Failure means watchdog polling fallback is active.
        write_times: list[float] = []
        detect_times: list[float] = []

        def handler(evt: BusFileEvent) -> None:
            detect_times.append(time.monotonic())

        src = FileSystemEventSource(tmp_path, handler)
        src.start()
        time.sleep(0.1)
        try:
            for i in range(20):
                write_times.append(time.monotonic())
                _write_bus_file(tmp_path, f"latency-{i:03d}")
                time.sleep(0.05)  # 50 ms between writes
            deadline = time.monotonic() + 2.0
            while len(detect_times) < 20 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            src.stop()

        assert len(detect_times) == 20, f"Missed {20 - len(detect_times)} events"
        latencies_ms = [(d - w) * 1000 for w, d in zip(write_times, detect_times, strict=True)]
        median_ms = statistics.median(latencies_ms)
        max_ms = max(latencies_ms)
        print(f"\nLatency: median={median_ms:.1f}ms  max={max_ms:.1f}ms")
        assert median_ms < 50, f"Median {median_ms:.1f}ms >= 50ms threshold"


class TestFileSystemEventSourceRapidCreate:
    def test_burst_50_no_missed_events(self, tmp_path):
        # Burst of 50 rapid file creates -- zero missed events expected.
        received: list[BusFileEvent] = []
        lock = threading.Lock()

        def handler(evt: BusFileEvent) -> None:
            with lock:
                received.append(evt)

        src = FileSystemEventSource(tmp_path, handler)
        src.start()
        time.sleep(0.1)
        try:
            for i in range(50):
                _write_bus_file(tmp_path, f"burst-{i:03d}")
            deadline = time.monotonic() + 3.0
            while len(received) < 50 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            src.stop()
        missed = 50 - len(received)
        print(f"\nBurst 50: received={len(received)} missed={missed}")
        assert missed == 0, f"{missed} events missed in burst of 50"


class TestFileSystemEventSourceAsync:
    def test_async_dispatch_via_schedule_in_loop(self, tmp_path):
        # schedule_in_loop() bridges watchdog thread to asyncio loop.
        import asyncio

        received: list[BusFileEvent] = []

        async def async_handler(evt: BusFileEvent) -> None:
            received.append(evt)

        async def driver():
            loop = asyncio.get_event_loop()
            src = FileSystemEventSource(tmp_path, lambda e: None)
            src.schedule_in_loop(async_handler, loop)
            src.start()
            await asyncio.sleep(0.1)
            try:
                _write_bus_file(tmp_path, "async-001")
                for _ in range(30):
                    await asyncio.sleep(0.05)
                    if received:
                        break
            finally:
                src.stop()

        asyncio.run(driver())
        assert len(received) == 1
        assert received[0].path.name == "async-001.md"
