"""Integration tests for pm_sync_handler (task 6.2 — pm-sync-adapter spec change).

Tests use a mocked adapter so no live Easy8 instance is required.

Scenarios covered:
  - spec-change-approved → adapter.create_issue() called; issue_id persisted
  - task-assignment → adapter.update_issue(in_progress) + add_comment()
  - task-complete → adapter.update_issue(done) + add_comment()
  - Capability-gated: no add_comment() when capabilities.issue_comments == False
  - Inbound webhook: Issue→Done + spec-path → spec-update-requested bus event
  - Inbound webhook: Issue create (external) → pm-issue-created bus event
  - Inbound webhook: @spec-agent comment → spec-update-requested bus event
  - Agent dispatch (task 4.7): project_id resolved to agent from project-map
  - MCP Tier 2 (task 9.3): call_mcp_complex_query falls back gracefully when unavailable
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from otaman_bridge.pm_sync_handler import PmSyncHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLATFORM_YAML_CONTENT = """\
project: otaman
pm-sync:
  provider: easy8
  base-url: https://es.example.com
  identity-mode: system_user
  program-name: Otaman Platform
  program-key: otaman
  per-repo: true
  status-map:
    declared: New
    in_progress: In Progress
    done: Closed
  tracker: Task
  project-map:
    _root: 1
    otaman-specs: 2
    otaman-bridge: 3
repos:
  - name: otaman-specs
    owner: spec-agent
  - name: otaman-bridge
    owner: bridge-agent
"""


def _make_capabilities(*, issue_comments: bool = True) -> MagicMock:
    caps = MagicMock()
    caps.issue_comments = issue_comments
    caps.mcp_support = True
    return caps


def _make_issue(issue_id: int = 42, subject: str = "test issue") -> MagicMock:
    issue = MagicMock()
    issue.id = issue_id
    issue.subject = subject
    return issue


def _make_adapter(*, issue_id: int = 42, issue_comments: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.capabilities = _make_capabilities(issue_comments=issue_comments)
    adapter.create_issue.return_value = _make_issue(issue_id)
    adapter.update_issue.return_value = _make_issue(issue_id)
    adapter.list_issues.return_value = []
    return adapter


def _make_inbound_event(
    *,
    event_type: str = "update",
    project_id: int = 2,
    issue_id: int = 7,
    new_status: str | None = None,
    spec_path: str | None = None,
    issue_subject: str | None = None,
) -> MagicMock:
    evt = MagicMock()
    evt.event_type = event_type
    evt.project_id = project_id
    evt.issue_id = issue_id
    evt.new_status = new_status
    evt.spec_path = spec_path
    evt.issue_subject = issue_subject
    return evt


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "platform.yaml").write_text(PLATFORM_YAML_CONTENT, encoding="utf-8")
    (tmp_path / ".agents" / "bus" / "active").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def handler(workspace: Path) -> PmSyncHandler:
    h = PmSyncHandler(workspace)
    h.adapter = _make_adapter()
    h.enabled = True
    h._project_id_to_repo = {1: "_root", 2: "otaman-specs", 3: "otaman-bridge"}
    return h


# ---------------------------------------------------------------------------
# Outbound: spec-change-approved → create_issue (task 4.2)
# ---------------------------------------------------------------------------


class TestSpecChangeApproved:
    def test_calls_create_issue(self, handler: PmSyncHandler) -> None:
        handler.handle_bus_event(
            "spec-change-approved", "spec-agent", "human",
            "pm-sync-adapter: PmSyncAdapter protocol", "openspec/changes/pm-sync-adapter/tasks.md",
            "pm-sync-adapter",
        )
        handler.adapter.create_issue.assert_called_once()

    def test_persists_issue_id(self, handler: PmSyncHandler) -> None:
        handler.adapter.create_issue.return_value = _make_issue(99)
        handler.handle_bus_event(
            "spec-change-approved", "spec-agent", "human",
            "Some spec change", None, "my-change",
        )
        assert handler._load_issue_id("my-change") == 99

    def test_posts_comment_if_issue_comments_enabled(self, handler: PmSyncHandler) -> None:
        handler.handle_bus_event(
            "spec-change-approved", "spec-agent", "human",
            "My spec", None, "my-change",
        )
        handler.adapter.add_comment.assert_called_once()
        comment = handler.adapter.add_comment.call_args[0][1]
        assert "Spec approved" in comment

    def test_no_comment_if_issue_comments_disabled(self, handler: PmSyncHandler) -> None:
        handler.adapter = _make_adapter(issue_comments=False)
        handler.handle_bus_event(
            "spec-change-approved", "spec-agent", "human",
            "My spec", None, "my-change",
        )
        handler.adapter.add_comment.assert_not_called()


# ---------------------------------------------------------------------------
# Outbound: task-assignment → update_issue(in_progress) + comment (task 4.3)
# ---------------------------------------------------------------------------


class TestTaskAssignment:
    def test_updates_issue_to_in_progress(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-assignment", "otaman", "bridge-agent",
            "Implement pm_sync_handler", None, "my-change",
        )
        handler.adapter.update_issue.assert_called_once()
        call_args = handler.adapter.update_issue.call_args
        assert call_args[0][0] == 42
        state = call_args[0][1]
        assert "in_progress" in str(getattr(state, "status", state)).lower()

    def test_posts_comment_with_correct_format(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-assignment", "roman", "bridge-agent",
            "Do something", None, "my-change",
        )
        handler.adapter.add_comment.assert_called_once()
        comment = handler.adapter.add_comment.call_args[0][1]
        assert "🤖" in comment
        assert "roman" in comment
        assert "bridge-agent" in comment

    def test_no_comment_when_issue_comments_disabled(self, handler: PmSyncHandler) -> None:
        handler.adapter = _make_adapter(issue_comments=False)
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-assignment", "otaman", "bridge-agent",
            "Do something", None, "my-change",
        )
        handler.adapter.add_comment.assert_not_called()

    def test_skips_when_no_issue_resolved(self, handler: PmSyncHandler) -> None:
        handler.handle_bus_event(
            "task-assignment", "otaman", "bridge-agent",
            "unknown change", None, "nonexistent-change",
        )
        handler.adapter.update_issue.assert_not_called()


# ---------------------------------------------------------------------------
# Outbound: task-complete → update_issue(done) + comment (task 4.4)
# ---------------------------------------------------------------------------


class TestTaskComplete:
    def test_updates_issue_to_done(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-complete", "bridge-agent", "human",
            "pm_sync_handler implemented", None, "my-change",
        )
        handler.adapter.update_issue.assert_called_once()
        call_args = handler.adapter.update_issue.call_args
        assert call_args[0][0] == 42
        state = call_args[0][1]
        assert "done" in str(getattr(state, "status", state)).lower()

    def test_posts_comment_with_correct_format(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-complete", "bridge-agent", "human",
            "pm_sync_handler done", None, "my-change",
        )
        handler.adapter.add_comment.assert_called_once()
        comment = handler.adapter.add_comment.call_args[0][1]
        assert "✅" in comment
        assert "task complete" in comment

    def test_no_comment_when_issue_comments_disabled(self, handler: PmSyncHandler) -> None:
        handler.adapter = _make_adapter(issue_comments=False)
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-complete", "bridge-agent", "human",
            "done", None, "my-change",
        )
        handler.adapter.add_comment.assert_not_called()


# ---------------------------------------------------------------------------
# Inbound: PM webhook → bus event (task 4.5)
# ---------------------------------------------------------------------------


class TestInboundWebhook:
    def _active_messages(self, workspace: Path) -> list[Path]:
        return sorted((workspace / ".agents" / "bus" / "active").glob("*.md"))

    def test_issue_done_with_spec_path_emits_spec_update_requested(
        self, handler: PmSyncHandler, workspace: Path
    ) -> None:
        evt = _make_inbound_event(
            event_type="update", issue_id=7,
            new_status="Done", spec_path="openspec/changes/foo/tasks.md",
        )
        handler.adapter.handle_inbound_event.return_value = evt
        result = handler.handle_inbound_webhook({"action": "update", "issue": {}})
        assert result["ok"] is True
        msgs = self._active_messages(workspace)
        assert len(msgs) == 1
        content = msgs[0].read_text()
        assert "spec-update-requested" in content
        assert "spec-agent" in content

    def test_issue_create_emits_pm_issue_created(
        self, handler: PmSyncHandler, workspace: Path
    ) -> None:
        evt = _make_inbound_event(event_type="create", issue_id=8)
        handler.adapter.handle_inbound_event.return_value = evt
        handler.handle_inbound_webhook({"action": "create"})
        msgs = self._active_messages(workspace)
        assert len(msgs) == 1
        assert "pm-issue-created" in msgs[0].read_text()

    def test_issue_update_other_emits_pm_issue_updated(
        self, handler: PmSyncHandler, workspace: Path
    ) -> None:
        evt = _make_inbound_event(event_type="update", issue_id=9)
        handler.adapter.handle_inbound_event.return_value = evt
        handler.handle_inbound_webhook({"action": "update"})
        msgs = self._active_messages(workspace)
        assert len(msgs) == 1
        assert "pm-issue-updated" in msgs[0].read_text()

    def test_spec_agent_comment_emits_spec_update_requested(
        self, handler: PmSyncHandler, workspace: Path
    ) -> None:
        evt = _make_inbound_event(event_type="update", issue_id=10)
        handler.adapter.handle_inbound_event.return_value = evt
        payload = {
            "action": "update",
            "issue": {"id": 10},
            "journals": [{"notes": "Hey @spec-agent please update the task status"}],
        }
        handler.handle_inbound_webhook(payload)
        msgs = self._active_messages(workspace)
        assert len(msgs) == 1
        content = msgs[0].read_text()
        assert "spec-update-requested" in content
        assert "spec-agent" in content

    def test_disabled_handler_returns_error(self, workspace: Path) -> None:
        h = PmSyncHandler(workspace)
        h.enabled = False
        result = h.handle_inbound_webhook({"action": "update"})
        assert result["ok"] is False

    def test_project_id_maps_to_agent_in_bus_to(
        self, handler: PmSyncHandler, workspace: Path
    ) -> None:
        # project_id=2 → otaman-specs → spec-agent (from repos in platform.yaml)
        evt = _make_inbound_event(event_type="create", project_id=2, issue_id=11)
        handler.adapter.handle_inbound_event.return_value = evt
        handler.handle_inbound_webhook({"action": "create"})
        msgs = self._active_messages(workspace)
        assert len(msgs) == 1
        assert "spec-agent" in msgs[0].read_text()


# ---------------------------------------------------------------------------
# MCP Tier 2 (task 9.3)
# ---------------------------------------------------------------------------


class TestMcpTier2:
    def test_returns_none_when_mcp_client_unavailable(
        self, handler: PmSyncHandler
    ) -> None:
        import otaman_bridge.pm_sync_handler as _mod
        original = _mod._MCP_CLIENT_CLS
        _mod._MCP_CLIENT_CLS = None
        try:
            result = handler.call_mcp_complex_query("easy8_issues_list", {})
            assert result is None
        finally:
            _mod._MCP_CLIENT_CLS = original

    def test_returns_none_when_disabled(self, workspace: Path) -> None:
        h = PmSyncHandler(workspace)
        h.enabled = False
        result = h.call_mcp_complex_query("easy8_issues_list", {})
        assert result is None

    def test_calls_mcp_client_when_available(self, handler: PmSyncHandler) -> None:
        import otaman_bridge.pm_sync_handler as _mod
        original = _mod._MCP_CLIENT_CLS

        mock_mcp_cls = MagicMock()
        mock_mcp_instance = MagicMock()
        mock_mcp_instance.call_tool.return_value = {"issues": []}
        mock_mcp_cls.return_value = mock_mcp_instance

        _mod._MCP_CLIENT_CLS = mock_mcp_cls
        try:
            with patch.dict(os.environ, {"OTAMAN_PM_EASY8_API_KEY": "test-key"}):
                handler.config = MagicMock()
                handler.config.provider = "easy8"
                handler.config.base_url = "https://es.example.com"
                result = handler.call_mcp_complex_query("easy8_issues_list", {"project_id": 1})
            assert result == {"issues": []}
            mock_mcp_instance.call_tool.assert_called_once_with(
                "easy8_issues_list", {"project_id": 1}
            )
        finally:
            _mod._MCP_CLIENT_CLS = original


# ---------------------------------------------------------------------------
# Issue map persistence
# ---------------------------------------------------------------------------


class TestIssueMapPersistence:
    def test_save_and_load_issue_id(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("my-feature", 77)
        assert handler._load_issue_id("my-feature") == 77

    def test_load_returns_none_for_unknown_change(self, handler: PmSyncHandler) -> None:
        assert handler._load_issue_id("nonexistent") is None

    def test_update_existing_entry(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("change-a", 1)
        handler._save_issue_id("change-a", 2)
        assert handler._load_issue_id("change-a") == 2

    def test_multiple_changes_tracked_independently(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("change-a", 10)
        handler._save_issue_id("change-b", 20)
        assert handler._load_issue_id("change-a") == 10
        assert handler._load_issue_id("change-b") == 20
