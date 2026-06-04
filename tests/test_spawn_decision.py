"""Integration tests for spawn_decision.handle_bus_event (task 1.7).

Scenario A: task-assignment file drop → spawn_decision calls runner once (headless).
Scenario B: duplicate file drop → no second spawn (dedup via warm-session check).
Scenario C: [interactive] task → request-human-review emitted, no runner call.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from otaman_bridge.session_registry import SqliteSessionRegistry
from otaman_bridge.spawn_decision import SpawnOutcome, handle_bus_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OWNED = {"otaman-bridge": "bridge-agent"}


def _write_task_assignment(
    bus_active: Path,
    *,
    stem: str = "20260601T120000-test-assignment",
    to_agent: str = "bridge-agent",
    from_agent: str = "otaman",
    change: str = "test-change",
    mode_annot: str = "[headless]",
    status: str = "pending",
) -> Path:
    """Write a minimal task-assignment .md to bus_active/; return the path."""
    content = textwrap.dedent(f"""\
        ---
        id: {stem}
        from: {from_agent}
        to: {to_agent}
        priority: normal
        type: task-assignment
        timestamp: 2026-06-01T12:00:00Z
        status: {status}
        change: {change}
        ---

        ## Subject: Tasks assigned from "{change}"

        - [ ] 1.1 @otaman-bridge {mode_annot} Implement something.
    """)
    path = bus_active / f"{stem}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bus_dir(tmp_path):
    active = tmp_path / ".agents" / "bus" / "active"
    active.mkdir(parents=True)
    return tmp_path / ".agents" / "bus"


@pytest.fixture
def registry(tmp_path):
    r = SqliteSessionRegistry(db_path=tmp_path / "sessions.db")
    yield r
    r.close()


@pytest.fixture
def runner_mock():
    rc = MagicMock()
    rc.spawn.return_value = "sess-abc123"
    return rc


# ---------------------------------------------------------------------------
# Scenario A — headless task-assignment → single spawn
# ---------------------------------------------------------------------------


class TestHeadlessSpawn:
    def test_calls_runner_spawn_once(self, bus_dir, registry, runner_mock):
        path = _write_task_assignment(bus_dir / "active", mode_annot="[headless]")
        outcome = handle_bus_event(
            path,
            registry=registry,
            runner_client=runner_mock,
            owned_agents=OWNED,
            bus_dir=bus_dir,
        )
        assert outcome is not None
        assert outcome.action == "spawned"
        assert outcome.mode == "headless"
        assert outcome.session_id == "sess-abc123"
        runner_mock.spawn.assert_called_once_with(
            agent="bridge-agent",
            human="otaman",
            mode="headless",
            context={
                "change_id": "test-change",
                "message_path": str(path),
                "trigger_source": "bus-event",
            },
        )

    def test_claims_session_after_spawn(self, bus_dir, registry, runner_mock):
        path = _write_task_assignment(bus_dir / "active", mode_annot="[headless]")
        handle_bus_event(
            path,
            registry=registry,
            runner_client=runner_mock,
            owned_agents=OWNED,
            bus_dir=bus_dir,
        )
        assert registry.is_sessioned("bridge-agent", "otaman")

    def test_trigger_source_in_context(self, bus_dir, registry, runner_mock):
        path = _write_task_assignment(bus_dir / "active", mode_annot="[headless]")
        handle_bus_event(
            path,
            registry=registry,
            runner_client=runner_mock,
            owned_agents=OWNED,
            bus_dir=bus_dir,
            trigger_source="scheduled",
        )
        _, kwargs = runner_mock.spawn.call_args
        assert kwargs["context"]["trigger_source"] == "scheduled"


# ---------------------------------------------------------------------------
# Scenario B — dedup: second identical message → no second spawn
# ---------------------------------------------------------------------------


class TestDedup:
    def test_no_second_spawn_when_sessioned(self, bus_dir, registry, runner_mock):
        path = _write_task_assignment(bus_dir / "active", mode_annot="[headless]")
        # First event spawns
        handle_bus_event(
            path,
            registry=registry,
            runner_client=runner_mock,
            owned_agents=OWNED,
            bus_dir=bus_dir,
        )
        assert runner_mock.spawn.call_count == 1

        # Second event (duplicate) — registry already has the session
        path2 = _write_task_assignment(
            bus_dir / "active",
            stem="20260601T120001-test-assignment-dup",
            mode_annot="[headless]",
        )
        outcome2 = handle_bus_event(
            path2,
            registry=registry,
            runner_client=runner_mock,
            owned_agents=OWNED,
            bus_dir=bus_dir,
        )
        assert runner_mock.spawn.call_count == 1  # still only one spawn
        assert outcome2 is not None
        assert outcome2.action == "warm-session"

    def test_dedup_key_is_deterministic(self, bus_dir, registry, runner_mock):
        p1 = _write_task_assignment(bus_dir / "active", change="my-feature", mode_annot="[headless]")
        p2 = _write_task_assignment(
            bus_dir / "active",
            stem="20260601T120002-second",
            change="my-feature",
            mode_annot="[headless]",
        )
        o1 = handle_bus_event(
            p1, registry=registry, runner_client=runner_mock, owned_agents=OWNED, bus_dir=bus_dir
        )
        # Manually release to allow the second to go through (for key comparison)
        registry.release_session("bridge-agent", "otaman", "sess-abc123")
        o2 = handle_bus_event(
            p2, registry=registry, runner_client=runner_mock, owned_agents=OWNED, bus_dir=bus_dir
        )
        assert o1 is not None and o2 is not None
        assert o1.dedup_key == o2.dedup_key


# ---------------------------------------------------------------------------
# Scenario C — interactive task → request-human-review emitted
# ---------------------------------------------------------------------------


class TestInteractiveReview:
    def test_interactive_emits_review_message(self, bus_dir, registry, runner_mock):
        path = _write_task_assignment(bus_dir / "active", mode_annot="[interactive]")
        outcome = handle_bus_event(
            path,
            registry=registry,
            runner_client=runner_mock,
            owned_agents=OWNED,
            bus_dir=bus_dir,
        )
        assert outcome is not None
        assert outcome.action == "interactive-review"
        assert outcome.mode == "interactive"
        runner_mock.spawn.assert_not_called()

        review_files = list((bus_dir / "active").glob("*review*.md"))
        assert len(review_files) == 1
        review_text = review_files[0].read_text()
        assert "type: request-human-review" in review_text
        assert "bridge-agent" in review_text

    def test_interactive_does_not_call_runner(self, bus_dir, registry, runner_mock):
        path = _write_task_assignment(bus_dir / "active", mode_annot="[interactive]")
        handle_bus_event(
            path, registry=registry, runner_client=runner_mock, owned_agents=OWNED, bus_dir=bus_dir
        )
        runner_mock.spawn.assert_not_called()

    def test_default_mode_is_interactive(self, bus_dir, registry, runner_mock):
        # Task line with no mode annotation → defaults to interactive
        active = bus_dir / "active"
        content = textwrap.dedent("""\
            ---
            id: 20260601T120000-no-mode
            from: otaman
            to: bridge-agent
            priority: normal
            type: task-assignment
            timestamp: 2026-06-01T12:00:00Z
            status: pending
            change: test-change
            ---

            - [ ] 1.1 @otaman-bridge Do something without mode annotation.
        """)
        path = active / "20260601T120000-no-mode.md"
        path.write_text(content, encoding="utf-8")
        outcome = handle_bus_event(
            path, registry=registry, runner_client=runner_mock, owned_agents=OWNED, bus_dir=bus_dir
        )
        assert outcome is not None
        assert outcome.mode == "interactive"
        runner_mock.spawn.assert_not_called()


# ---------------------------------------------------------------------------
# Skip cases
# ---------------------------------------------------------------------------


class TestSkipCases:
    def test_skips_non_task_assignment(self, bus_dir, registry, runner_mock):
        active = bus_dir / "active"
        content = textwrap.dedent("""\
            ---
            id: 20260601T120000-review
            from: otaman
            to: bridge-agent
            type: review-request
            priority: normal
            timestamp: 2026-06-01T12:00:00Z
            status: pending
            ---
            Body.
        """)
        path = active / "review.md"
        path.write_text(content, encoding="utf-8")
        outcome = handle_bus_event(
            path, registry=registry, runner_client=runner_mock, owned_agents=OWNED, bus_dir=bus_dir
        )
        assert outcome is None
        runner_mock.spawn.assert_not_called()

    def test_skips_message_for_other_agent(self, bus_dir, registry, runner_mock):
        path = _write_task_assignment(bus_dir / "active", to_agent="cli-agent")
        outcome = handle_bus_event(
            path, registry=registry, runner_client=runner_mock, owned_agents=OWNED, bus_dir=bus_dir
        )
        assert outcome is None
        runner_mock.spawn.assert_not_called()

    def test_skips_unparseable_file(self, bus_dir, registry, runner_mock):
        path = bus_dir / "active" / "bad.md"
        path.write_text("not a valid frontmatter file\n", encoding="utf-8")
        outcome = handle_bus_event(
            path, registry=registry, runner_client=runner_mock, owned_agents=OWNED, bus_dir=bus_dir
        )
        assert outcome is None

    def test_skips_missing_file(self, bus_dir, registry, runner_mock):
        path = bus_dir / "active" / "nonexistent.md"
        outcome = handle_bus_event(
            path, registry=registry, runner_client=runner_mock, owned_agents=OWNED, bus_dir=bus_dir
        )
        assert outcome is None


# ---------------------------------------------------------------------------
# Spawn failure
# ---------------------------------------------------------------------------


class TestSpawnFailure:
    def test_runner_unreachable_returns_spawn_failed(self, bus_dir, registry):
        from otaman_bridge.runner_client import RunnerUnreachableError
        rc = MagicMock()
        rc.spawn.side_effect = RunnerUnreachableError("no runner")
        path = _write_task_assignment(bus_dir / "active", mode_annot="[headless]")
        outcome = handle_bus_event(
            path, registry=registry, runner_client=rc, owned_agents=OWNED, bus_dir=bus_dir
        )
        assert outcome is not None
        assert outcome.action == "spawn-failed"
        assert not registry.is_sessioned("bridge-agent", "otaman")
