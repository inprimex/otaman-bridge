"""Tests for bridge/idle_afk.py — IdleAFKMonitor state transitions."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest


from otaman_bridge.idle_afk import (
    AFK_FILENAME,
    ACTIVITY_FILENAME,
    IdleAFKMonitor,
    _read_afk_source,
)


@pytest.fixture
def maestro_root(tmp_path):
    """Maestro folder with a .maestro/ state dir."""
    root = tmp_path / "maestro"
    (root / ".maestro").mkdir(parents=True)
    return root


def _write_activity(root: Path, *, age_seconds: float = 0.0) -> Path:
    """Write the activity file; mtime = now - age_seconds."""
    path = root / ".maestro" / ACTIVITY_FILENAME
    path.write_text("2026-04-24T12:00:00+00:00\n", encoding="utf-8")
    if age_seconds > 0:
        t = time.time() - age_seconds
        os.utime(path, (t, t))
    return path


def _write_afk(root: Path, source: str = "manual") -> Path:
    path = root / ".maestro" / AFK_FILENAME
    path.write_text(
        f"enabled_at: 2026-04-24T12:00:00+00:00\n"
        f"source: {source}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# _check_once state machine


class TestCheckOnce:
    def test_no_activity_file_is_noop(self, maestro_root):
        """Before the first UserPromptSubmit, the monitor has no baseline
        so it must not make decisions."""
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=1)
        asyncio.run(monitor._check_once())
        assert not (maestro_root / ".maestro" / AFK_FILENAME).exists()

    def test_fresh_activity_does_not_enable(self, maestro_root):
        """Activity younger than the threshold is 'user is here' — no AFK."""
        _write_activity(maestro_root, age_seconds=5)  # 5s ago, threshold 60s
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=1)
        asyncio.run(monitor._check_once())
        assert not (maestro_root / ".maestro" / AFK_FILENAME).exists()

    def test_stale_activity_enables_idle_afk(self, maestro_root):
        """Activity older than threshold flips AFK on with source=idle-auto."""
        _write_activity(maestro_root, age_seconds=120)  # 2 min idle
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=1)
        asyncio.run(monitor._check_once())
        afk = maestro_root / ".otaman" / AFK_FILENAME
        assert afk.is_file()
        content = afk.read_text(encoding="utf-8")
        assert "source: idle-auto" in content
        assert "signal: idle-minutes=" in content

    def test_fresh_activity_clears_our_afk(self, maestro_root):
        """User comes back → we clear our own idle-auto AFK."""
        _write_activity(maestro_root, age_seconds=5)
        _write_afk(maestro_root, source="idle-auto")
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=1)
        asyncio.run(monitor._check_once())
        assert not (maestro_root / ".maestro" / AFK_FILENAME).exists()

    def test_does_not_clear_manual_afk(self, maestro_root):
        """Manual AFK survives — user intent beats our heuristic."""
        _write_activity(maestro_root, age_seconds=5)
        _write_afk(maestro_root, source="manual")
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=1)
        asyncio.run(monitor._check_once())
        assert (maestro_root / ".maestro" / AFK_FILENAME).is_file()
        assert _read_afk_source(
            maestro_root / ".maestro" / AFK_FILENAME
        ) == "manual"

    def test_does_not_clear_unattended_afk(self, maestro_root):
        """Unattended session AFK also survives."""
        _write_activity(maestro_root, age_seconds=5)
        _write_afk(maestro_root, source="unattended")
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=1)
        asyncio.run(monitor._check_once())
        assert _read_afk_source(
            maestro_root / ".maestro" / AFK_FILENAME
        ) == "unattended"

    def test_does_not_enable_if_manual_already_set(self, maestro_root):
        """Stale activity + existing manual AFK → do nothing (don't overwrite)."""
        _write_activity(maestro_root, age_seconds=120)
        _write_afk(maestro_root, source="manual")
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=1)
        asyncio.run(monitor._check_once())
        assert _read_afk_source(
            maestro_root / ".maestro" / AFK_FILENAME
        ) == "manual"


# ---------------------------------------------------------------------------
# Callbacks (notify on transitions)


class TestCallbacks:
    def test_on_enabled_called_when_flipping_idle(self, maestro_root):
        calls = []
        async def on_enabled(reason):
            calls.append(reason)
        _write_activity(maestro_root, age_seconds=180)
        monitor = IdleAFKMonitor(
            maestro_root, idle_minutes=1, on_enabled=on_enabled,
        )
        asyncio.run(monitor._check_once())
        assert len(calls) == 1
        assert "min of inactivity" in calls[0]

    def test_on_enabled_not_called_twice(self, maestro_root):
        """Multiple idle ticks with AFK already set should NOT re-notify."""
        calls = []
        async def on_enabled(reason):
            calls.append(reason)
        _write_activity(maestro_root, age_seconds=180)
        monitor = IdleAFKMonitor(
            maestro_root, idle_minutes=1, on_enabled=on_enabled,
        )
        asyncio.run(monitor._check_once())
        asyncio.run(monitor._check_once())
        asyncio.run(monitor._check_once())
        assert len(calls) == 1

    def test_on_cleared_called_when_user_returns(self, maestro_root):
        cleared = []
        async def on_cleared():
            cleared.append(True)
        _write_activity(maestro_root, age_seconds=5)
        _write_afk(maestro_root, source="idle-auto")
        monitor = IdleAFKMonitor(
            maestro_root, idle_minutes=1, on_cleared=on_cleared,
        )
        asyncio.run(monitor._check_once())
        assert len(cleared) == 1

    def test_callback_failure_does_not_crash_monitor(self, maestro_root):
        async def bad_cb(*_args):
            raise RuntimeError("boom")
        _write_activity(maestro_root, age_seconds=180)
        monitor = IdleAFKMonitor(
            maestro_root, idle_minutes=1, on_enabled=bad_cb,
        )
        # Should not raise.
        asyncio.run(monitor._check_once())
        # AFK should still be written despite the callback failing.
        assert (maestro_root / ".otaman" / AFK_FILENAME).is_file()


# ---------------------------------------------------------------------------
# Round-trip: idle → enabled → active → cleared


class TestRoundTrip:
    def test_full_cycle(self, maestro_root):
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=1)
        afk = maestro_root / ".otaman" / AFK_FILENAME

        # 1. User active, no AFK
        _write_activity(maestro_root, age_seconds=5)
        asyncio.run(monitor._check_once())
        assert not afk.exists()

        # 2. User walks away — age exceeds threshold
        _write_activity(maestro_root, age_seconds=180)
        asyncio.run(monitor._check_once())
        assert afk.is_file()
        assert _read_afk_source(afk) == "idle-auto"

        # 3. User returns (fresh activity)
        _write_activity(maestro_root, age_seconds=1)
        asyncio.run(monitor._check_once())
        assert not afk.exists()


# ---------------------------------------------------------------------------
# run / stop


class TestRunStop:
    def test_run_stops_cleanly(self, maestro_root):
        monitor = IdleAFKMonitor(
            maestro_root, idle_minutes=1, poll_interval=5.0,
        )

        async def driver():
            task = asyncio.create_task(monitor.run())
            await asyncio.sleep(0.1)
            monitor.stop()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(driver())


# ---------------------------------------------------------------------------
# Config validation


class TestConfig:
    def test_idle_minutes_floored_to_one(self, maestro_root):
        """Zero/negative idle_minutes could DoS the filesystem — floor to 1."""
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=0)
        assert monitor.idle_minutes >= 1
        monitor = IdleAFKMonitor(maestro_root, idle_minutes=-5)
        assert monitor.idle_minutes >= 1

    def test_poll_interval_floored(self, maestro_root):
        """Poll interval under 5s would hammer the filesystem needlessly."""
        monitor = IdleAFKMonitor(
            maestro_root, idle_minutes=10, poll_interval=0.1,
        )
        assert monitor.poll_interval >= 5.0
