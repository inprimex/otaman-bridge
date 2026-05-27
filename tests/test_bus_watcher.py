"""Tests for bridge/bus_watcher.py — polling loop + dedup state."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest


from otaman_bridge.bus_watcher import (
    POLL_INTERVAL_SECONDS,
    BusWatcher,
    build_approval_request,
    build_info_message,
)
from otaman_bridge.bus_surface import BusMessage


@pytest.fixture
def project_root(tmp_path):
    """Maestro folder with an empty .agents/bus/active directory."""
    root = tmp_path / "my-maestro"
    root.mkdir()
    (root / ".agents" / "bus" / "active").mkdir(parents=True)
    (root / "platform.yaml").write_text(
        "project: test\nversion: '1.0'\nrepos: []\n", encoding="utf-8",
    )
    return root


def _write_bus_msg(project_root: Path, stem: str, *, type: str = "info",
                   from_: str = "agent-a", to: str = "agent-b",
                   priority: str = "normal",
                   subject: str = "test") -> Path:
    """Create a minimal bus message file and return its path."""
    bus = project_root / ".agents" / "bus" / "active"
    p = bus / f"{stem}.md"
    p.write_text(
        f"---\n"
        f"id: {stem}\n"
        f"from: {from_}\n"
        f"to: {to}\n"
        f"priority: {priority}\n"
        f"type: {type}\n"
        f"timestamp: 2026-04-24T10:00:00Z\n"
        f"---\n\n"
        f"## Subject: {subject}\n\nbody of the message\n",
        encoding="utf-8",
    )
    return p


class _Recorder:
    """Collects on_info / on_approval calls for assertions."""

    def __init__(self):
        self.infos = []
        self.approvals = []

    async def on_info(self, info):
        self.infos.append(info)

    async def on_approval(self, approval, msg):
        self.approvals.append((approval, msg))


@pytest.fixture
def recorder():
    return _Recorder()


def _make_watcher(project_root, recorder, **kwargs):
    return BusWatcher(
        project_root=project_root,
        account="personal",
        project="test",
        on_info=recorder.on_info,
        on_approval=recorder.on_approval,
        poll_interval=0.05,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# _scan_once — single pass


class TestScanOnce:
    def test_empty_bus_surfaces_nothing(self, project_root, recorder):
        w = _make_watcher(project_root, recorder)
        n = asyncio.run(w._scan_once())
        assert n == 0
        assert recorder.infos == [] and recorder.approvals == []

    def test_info_broadcast_does_not_surface(self, project_root, recorder):
        _write_bus_msg(project_root, "20260424T100000-a-to-all-info",
                       type="info", to="all")
        n = asyncio.run(_make_watcher(project_root, recorder)._scan_once())
        assert n == 0
        assert recorder.infos == []

    def test_task_assignment_does_not_surface(self, project_root, recorder):
        _write_bus_msg(project_root, "20260424T100000-a-to-b-task",
                       type="task-assignment")
        n = asyncio.run(_make_watcher(project_root, recorder)._scan_once())
        assert n == 0

    def test_spec_change_request_surfaces_interactive(self, project_root, recorder):
        _write_bus_msg(
            project_root,
            "20260424T100000-agent-to-human-scr",
            type="spec-change-request", to="human",
            subject="please approve endpoint v2",
        )
        n = asyncio.run(_make_watcher(project_root, recorder)._scan_once())
        assert n == 1
        assert len(recorder.approvals) == 1
        assert len(recorder.infos) == 0
        req, orig = recorder.approvals[0]
        assert req.tool_name == "bus:spec-change-request"
        assert req.request_id == "20260424T100000-agent-to-human-scr"
        assert "endpoint v2" in req.reason
        assert orig.type == "spec-change-request"

    def test_urgent_surfaces_info(self, project_root, recorder):
        _write_bus_msg(
            project_root,
            "20260424T100000-a-to-b-alert",
            type="review-request", priority="urgent",
        )
        n = asyncio.run(_make_watcher(project_root, recorder)._scan_once())
        assert n == 1
        # Urgent without to:human → blocking non-interactive info
        assert len(recorder.infos) == 1
        assert recorder.infos[0].severity == "blocking"


# ---------------------------------------------------------------------------
# Dedup state


class TestDedup:
    def test_message_only_surfaces_once_across_scans(
        self, project_root, recorder,
    ):
        _write_bus_msg(project_root, "dup-1",
                       type="spec-change-request", to="human")
        w = _make_watcher(project_root, recorder)
        asyncio.run(w._scan_once())
        asyncio.run(w._scan_once())
        assert len(recorder.approvals) == 1

    def test_new_message_added_between_scans_surfaces(
        self, project_root, recorder,
    ):
        _write_bus_msg(project_root, "first",
                       type="spec-change-request", to="human")
        w = _make_watcher(project_root, recorder)
        asyncio.run(w._scan_once())
        assert len(recorder.approvals) == 1

        _write_bus_msg(project_root, "second",
                       type="spec-change-request", to="human")
        asyncio.run(w._scan_once())
        assert len(recorder.approvals) == 2

    def test_state_file_persists_across_watcher_instances(
        self, project_root, recorder,
    ):
        _write_bus_msg(project_root, "persistent",
                       type="spec-change-request", to="human")
        # Watcher 1 surfaces
        asyncio.run(_make_watcher(project_root, recorder)._scan_once())
        assert len(recorder.approvals) == 1

        # Fresh watcher, fresh recorder, same state file → skip
        r2 = _Recorder()
        asyncio.run(_make_watcher(project_root, r2)._scan_once())
        assert len(r2.approvals) == 0

    def test_state_file_format_is_json(self, project_root, recorder):
        _write_bus_msg(project_root, "stateful",
                       type="spec-change-request", to="human")
        asyncio.run(_make_watcher(project_root, recorder)._scan_once())
        # State file is written under .otaman/ (migrated from .maestro/ in
        # finish-maestro-to-otaman-migration).
        state_file = project_root / ".otaman" / "bus-surfaced.state"
        assert state_file.is_file()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert "stateful" in data
        assert isinstance(data["stateful"], (int, float))


# ---------------------------------------------------------------------------
# Overrides are read per-scan


class TestOverridesApplied:
    def test_platform_yaml_turn_on_review_request(
        self, project_root, recorder,
    ):
        (project_root / "platform.yaml").write_text(
            "project: test\nversion: '1.0'\nrepos: []\n"
            "surface:\n  review_request: true\n",
            encoding="utf-8",
        )
        _write_bus_msg(project_root, "rev",
                       type="review-request", from_="cto-reviewer")
        n = asyncio.run(_make_watcher(project_root, recorder)._scan_once())
        assert n == 1
        assert recorder.infos[0].severity == "info"


# ---------------------------------------------------------------------------
# Dispatcher failure isolates


class TestDispatchFailureIsolates:
    def test_one_bad_dispatch_doesnt_block_others(
        self, project_root,
    ):
        """If on_info throws on one message, other messages still surface."""
        attempts = []

        async def flaky_on_info(info):
            attempts.append(info.bus_message_id)
            if "bad" in info.bus_message_id:
                raise RuntimeError("simulated transport failure")

        async def on_approval(req, msg):
            pass

        # Override surface so all three types surface as info
        (project_root / "platform.yaml").write_text(
            "project: test\nversion: '1.0'\nrepos: []\n"
            "surface:\n  review_request: true\n",
            encoding="utf-8",
        )

        _write_bus_msg(project_root, "good-1", type="review-request",
                       from_="a", subject="ok 1")
        _write_bus_msg(project_root, "bad-fail", type="review-request",
                       from_="b", subject="boom")
        _write_bus_msg(project_root, "good-2", type="review-request",
                       from_="c", subject="ok 2")

        w = BusWatcher(
            project_root=project_root, account="personal", project="test",
            on_info=flaky_on_info,
            on_approval=on_approval,
            poll_interval=0.05,
        )
        asyncio.run(w._scan_once())

        # All three were attempted
        assert len(attempts) == 3
        # The failed one should NOT be in state (will retry next scan)
        state = json.loads(
            (project_root / ".otaman" / "bus-surfaced.state").read_text()
        )
        assert "good-1" in state
        assert "good-2" in state
        assert "bad-fail" not in state


# ---------------------------------------------------------------------------
# run / stop


class TestRunStop:
    def test_run_surfaces_and_exits_on_stop(self, project_root, recorder):
        _write_bus_msg(project_root, "run-test",
                       type="spec-change-request", to="human")
        w = _make_watcher(project_root, recorder)

        async def driver():
            task = asyncio.create_task(w.run())
            # Give it time to do a scan or two
            for _ in range(20):
                if recorder.approvals:
                    break
                await asyncio.sleep(0.05)
            w.stop()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(driver())
        assert len(recorder.approvals) == 1


# ---------------------------------------------------------------------------
# Payload builders


class TestBuildInfoMessage:
    def test_includes_type_and_parties_in_title(self):
        msg = BusMessage(
            path=Path("x.md"), stem="x",
            frontmatter={"id": "x", "from": "a", "to": "b", "type": "review-request"},
            body="## Subject: s\n\nbody",
        )
        info = build_info_message(msg, account="personal", project="p")
        assert "a" in info.title and "b" in info.title
        assert "review-request" in info.title

    def test_long_body_truncated(self):
        long = "x" * 2000
        msg = BusMessage(
            path=Path("x.md"), stem="x",
            frontmatter={"from": "a", "to": "b", "type": "info"},
            body=f"## Subject: s\n\n{long}",
        )
        info = build_info_message(msg, account="p", project="p")
        assert len(info.body) < 1500
        assert "truncated" in info.body


class TestBuildApprovalRequest:
    def test_request_id_is_message_stem(self):
        msg = BusMessage(
            path=Path("x.md"), stem="20260424T100000-unique-id",
            frontmatter={"from": "a", "to": "human",
                         "type": "spec-change-request"},
            body="## Subject: change\n\nbody",
        )
        req = build_approval_request(msg, account="p", project="p")
        assert req.request_id == "20260424T100000-unique-id"
        assert req.tool_name == "bus:spec-change-request"
        # Full body available in tool_input for downstream details handling
        assert "body" in req.tool_input
