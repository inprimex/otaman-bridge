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

import dataclasses
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
human-roster:
  - name: Roman Starikov
    email: roman@example.com
    roles: [cofounder, cto, cpo]
    pm-user-id: 1
  - name: Alice Dev
    email: alice@example.com
    roles: [developer]
    pm-user-id: 7
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
            "spec-change-approved",
            "spec-agent",
            "human",
            "pm-sync-adapter: PmSyncAdapter protocol",
            "openspec/changes/pm-sync-adapter/tasks.md",
            "pm-sync-adapter",
        )
        handler.adapter.create_issue.assert_called_once()

    def test_persists_issue_id(self, handler: PmSyncHandler) -> None:
        handler.adapter.create_issue.return_value = _make_issue(99)
        handler.handle_bus_event(
            "spec-change-approved",
            "spec-agent",
            "human",
            "Some spec change",
            None,
            "my-change",
        )
        assert handler._load_issue_id("my-change") == 99

    def test_posts_comment_if_issue_comments_enabled(self, handler: PmSyncHandler) -> None:
        handler.handle_bus_event(
            "spec-change-approved",
            "spec-agent",
            "human",
            "My spec",
            None,
            "my-change",
        )
        handler.adapter.add_comment.assert_called_once()
        comment = handler.adapter.add_comment.call_args[0][1]
        assert "Spec approved" in comment

    def test_no_comment_if_issue_comments_disabled(self, handler: PmSyncHandler) -> None:
        handler.adapter = _make_adapter(issue_comments=False)
        handler.handle_bus_event(
            "spec-change-approved",
            "spec-agent",
            "human",
            "My spec",
            None,
            "my-change",
        )
        handler.adapter.add_comment.assert_not_called()


# ---------------------------------------------------------------------------
# Outbound: task-assignment → update_issue(in_progress) + comment (task 4.3)
# ---------------------------------------------------------------------------


class TestTaskAssignment:
    def test_updates_issue_to_in_progress(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-assignment",
            "otaman",
            "bridge-agent",
            "Implement pm_sync_handler",
            None,
            "my-change",
        )
        handler.adapter.update_issue.assert_called_once()
        call_args = handler.adapter.update_issue.call_args
        assert call_args[0][0] == 42
        state = call_args[0][1]
        assert "in_progress" in str(getattr(state, "status", state)).lower()

    def test_posts_comment_with_correct_format(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-assignment",
            "roman",
            "bridge-agent",
            "Do something",
            None,
            "my-change",
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
            "task-assignment",
            "otaman",
            "bridge-agent",
            "Do something",
            None,
            "my-change",
        )
        handler.adapter.add_comment.assert_not_called()

    def test_skips_when_no_issue_resolved(self, handler: PmSyncHandler) -> None:
        handler.handle_bus_event(
            "task-assignment",
            "otaman",
            "bridge-agent",
            "unknown change",
            None,
            "nonexistent-change",
        )
        handler.adapter.update_issue.assert_not_called()


# ---------------------------------------------------------------------------
# Outbound: task-complete → update_issue(done) + comment (task 4.4)
# ---------------------------------------------------------------------------


class TestTaskComplete:
    def test_updates_issue_to_done(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-complete",
            "bridge-agent",
            "human",
            "pm_sync_handler implemented",
            None,
            "my-change",
        )
        handler.adapter.update_issue.assert_called_once()
        call_args = handler.adapter.update_issue.call_args
        assert call_args[0][0] == 42
        state = call_args[0][1]
        assert "done" in str(getattr(state, "status", state)).lower()

    def test_posts_comment_with_correct_format(self, handler: PmSyncHandler) -> None:
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-complete",
            "bridge-agent",
            "human",
            "pm_sync_handler done",
            None,
            "my-change",
        )
        handler.adapter.add_comment.assert_called_once()
        comment = handler.adapter.add_comment.call_args[0][1]
        assert "✅" in comment
        assert "task complete" in comment

    def test_no_comment_when_issue_comments_disabled(self, handler: PmSyncHandler) -> None:
        handler.adapter = _make_adapter(issue_comments=False)
        handler._save_issue_id("my-change", 42)
        handler.handle_bus_event(
            "task-complete",
            "bridge-agent",
            "human",
            "done",
            None,
            "my-change",
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
            event_type="update",
            issue_id=7,
            new_status="Done",
            spec_path="openspec/changes/foo/tasks.md",
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
    def test_returns_none_when_mcp_client_unavailable(self, handler: PmSyncHandler) -> None:
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


# ---------------------------------------------------------------------------
# Human-roster assignee resolution (human-roster spec tasks 3.1–3.3)
# ---------------------------------------------------------------------------

from otaman_bridge.pm_sync_handler import resolve_assignee  # noqa: E402

ROSTER = [
    {"name": "Roman", "roles": ["cofounder", "cto"], "pm-user-id": 1},
    {"name": "Alice", "roles": ["developer"], "pm-user-id": 7},
]


class TestResolveAssignee:
    def test_agent_role_match(self) -> None:
        assert resolve_assignee("cofounder-agent", ROSTER) == 1

    def test_human_resolves_to_cofounder_first(self) -> None:
        assert resolve_assignee("human", ROSTER) == 1

    def test_developer_agent_resolves(self) -> None:
        assert resolve_assignee("developer-agent", ROSTER) == 7

    def test_no_match_returns_none(self) -> None:
        assert (
            resolve_assignee("cpo-agent", [{"name": "X", "roles": ["developer"], "pm-user-id": 5}])
            is None
        )

    def test_missing_pm_user_id_returns_none(self) -> None:
        roster = [{"name": "X", "roles": ["cofounder"]}]
        assert resolve_assignee("cofounder-agent", roster) is None

    def test_empty_roster_returns_none(self) -> None:
        assert resolve_assignee("human", []) is None


class TestSpecsRepoName:
    def test_derives_repo_name_from_specs_root(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        # workspace/openspec/changes → workspace is the "repo" dir, name = tmp dir name
        # The fallback when _specs_root returns an in-workspace path is just the basename
        name = handler_with_openspec._specs_repo_name()
        # Should be non-empty string (may be the tmp dir name); key property: not empty
        assert isinstance(name, str) and len(name) > 0

    def test_falls_back_to_otaman_specs_when_unresolvable(self, handler: PmSyncHandler) -> None:
        # handler has no openspec dir → _specs_root returns None → fallback
        assert handler._specs_repo_name() == "otaman-specs"

    def test_spec_change_approved_uses_specs_repo_not_msg_from(
        self, handler_with_openspec: PmSyncHandler
    ) -> None:
        """agent_name in SpecChange must be the specs repo name, not the message sender."""
        handler_with_openspec.handle_bus_event(
            "spec-change-approved",
            "plugin-agent",
            "spec-agent",
            "My spec",
            None,
            "my-change",
        )
        call_args = handler_with_openspec.adapter.create_issue.call_args[0][0]
        assert getattr(call_args, "agent_name", None) != "plugin-agent"


class TestSpecChangeApprovedWithRoster:
    def test_resolve_assignee_called_and_passed(self, handler: PmSyncHandler) -> None:
        # handler has ROSTER from PLATFORM_YAML_CONTENT; to=human → pm-user-id=1
        handler.handle_bus_event(
            "spec-change-approved",
            "spec-agent",
            "human",
            "My spec",
            None,
            "my-change",
        )
        call_args = handler.adapter.create_issue.call_args[0][0]
        # _SpecChangeWithAssignee wraps SpecChange and exposes assigned_to_id
        assert getattr(call_args, "assigned_to_id", None) == 1


# ---------------------------------------------------------------------------
# .pm-sync.yaml persistence (pm-sync-issue-id-on-spec tasks 2.1–2.5)
# ---------------------------------------------------------------------------


@pytest.fixture
def handler_with_openspec(workspace: Path) -> PmSyncHandler:
    """Handler with openspec/changes/ dir in workspace for .pm-sync.yaml tests."""
    changes_dir = workspace / "openspec" / "changes" / "my-change"
    changes_dir.mkdir(parents=True)
    h = PmSyncHandler(workspace)
    h.adapter = _make_adapter()
    h.enabled = True
    h._project_id_to_repo = {1: "_root", 2: "otaman-specs", 3: "otaman-bridge"}
    return h


class TestPmSyncYaml:
    def test_spec_change_approved_writes_pm_sync_yaml(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        handler_with_openspec.adapter.create_issue.return_value = _make_issue(42)
        handler_with_openspec.handle_bus_event(
            "spec-change-approved",
            "spec-agent",
            "human",
            "My spec",
            None,
            "my-change",
        )
        pm_sync = workspace / "openspec" / "changes" / "my-change" / ".pm-sync.yaml"
        assert pm_sync.is_file()
        import yaml

        data = yaml.safe_load(pm_sync.read_text())
        assert data["change_issue_id"] == 42

    def test_resolve_issue_id_reads_pm_sync_yaml_first(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        # Pre-write .pm-sync.yaml
        pm_sync = workspace / "openspec" / "changes" / "my-change" / ".pm-sync.yaml"
        pm_sync.write_text("change_issue_id: 99\n", encoding="utf-8")
        result = handler_with_openspec._resolve_issue_id("my-change", "anything")
        assert result == 99
        # Should NOT have called list_issues (no API call needed)
        handler_with_openspec.adapter.list_issues.assert_not_called()

    def test_resolve_falls_through_to_issue_map_when_no_pm_sync_yaml(
        self, handler: PmSyncHandler
    ) -> None:
        handler._save_issue_id("my-change", 55)
        # handler has no openspec dir → _pm_sync_file returns None → falls to issue-map
        result = handler._resolve_issue_id("my-change", "")
        assert result == 55

    def test_write_failure_logs_warning_and_does_not_raise(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        # Make the directory read-only so write fails
        pm_dir = workspace / "openspec" / "changes" / "my-change"
        pm_dir.chmod(0o555)
        try:
            # Should log WARNING but not raise
            handler_with_openspec._write_pm_sync_yaml("my-change", 77)
        finally:
            pm_dir.chmod(0o755)

    def test_malformed_pm_sync_yaml_returns_none(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        pm_sync = workspace / "openspec" / "changes" / "my-change" / ".pm-sync.yaml"
        pm_sync.write_text(": invalid: yaml: [[[", encoding="utf-8")
        result = handler_with_openspec._read_pm_sync_yaml("my-change")
        assert result is None

    def test_existing_tasks_preserved_on_write(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        pm_sync = workspace / "openspec" / "changes" / "my-change" / ".pm-sync.yaml"
        pm_sync.write_text("change_issue_id: 10\ntasks:\n  '2.1': 43\n", encoding="utf-8")
        handler_with_openspec._write_pm_sync_yaml("my-change", 10)
        import yaml

        data = yaml.safe_load(pm_sync.read_text())
        assert data.get("tasks", {}).get("2.1") == 43


# ---------------------------------------------------------------------------
# Proposal helpers (tasks 3.1–3.3)
# ---------------------------------------------------------------------------


class TestProposalHelpers:
    def test_read_proposal_description_reads_file(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        proposal = workspace / "openspec" / "changes" / "my-change" / "proposal.md"
        proposal.write_text("# My Proposal\n\nSome content.", encoding="utf-8")
        result = handler_with_openspec._read_proposal_description("my-change")
        assert result == "# My Proposal\n\nSome content."

    def test_read_proposal_description_returns_empty_on_missing(
        self, handler_with_openspec: PmSyncHandler
    ) -> None:
        result = handler_with_openspec._read_proposal_description("nonexistent-change")
        assert result == ""

    def test_read_proposal_description_returns_empty_when_no_specs_root(
        self, handler: PmSyncHandler
    ) -> None:
        result = handler._read_proposal_description("any-change")
        assert result == ""

    def test_extract_proposal_title_finds_heading(self, handler: PmSyncHandler) -> None:
        result = handler._extract_proposal_title("# My Title\n\nBody text.")
        assert result == "My Title"

    def test_extract_proposal_title_skips_subheadings(self, handler: PmSyncHandler) -> None:
        result = handler._extract_proposal_title("## Subheading\nBody.\n# Real Title")
        assert result == "Real Title"

    def test_extract_proposal_title_returns_none_when_absent(self, handler: PmSyncHandler) -> None:
        assert handler._extract_proposal_title("No headings here.") is None

    def test_build_issue_title_with_proposal_title(self, handler: PmSyncHandler) -> None:
        assert handler._build_issue_title("my-change", "My Title") == "[my-change] My Title"

    def test_build_issue_title_falls_back_to_change_name(self, handler: PmSyncHandler) -> None:
        assert handler._build_issue_title("my-change", None) == "[my-change] my-change"

    # pm-agent-ident (B5): subject composition per agent_identification mode.
    def test_build_issue_title_both_includes_agent(self, handler: PmSyncHandler) -> None:
        assert (
            handler._build_issue_title("my-change", "My Title", "spec-agent", "both")
            == "[my-change][spec-agent] My Title"
        )

    def test_build_issue_title_subject_prefix_includes_agent(self, handler: PmSyncHandler) -> None:
        assert (
            handler._build_issue_title("my-change", "My Title", "spec-agent", "subject-prefix")
            == "[my-change][spec-agent] My Title"
        )

    def test_build_issue_title_custom_field_omits_agent(self, handler: PmSyncHandler) -> None:
        assert (
            handler._build_issue_title("my-change", "My Title", "spec-agent", "custom-field")
            == "[my-change] My Title"
        )

    def test_build_issue_title_both_fallback_to_change_name(self, handler: PmSyncHandler) -> None:
        assert (
            handler._build_issue_title("my-change", None, "spec-agent", "both")
            == "[my-change][spec-agent] my-change"
        )

    def test_build_issue_title_empty_agent_never_emits_empty_segment(
        self, handler: PmSyncHandler
    ) -> None:
        assert handler._build_issue_title("my-change", "T", "", "both") == "[my-change] T"


# ---------------------------------------------------------------------------
# spec-change-approved rich title + description (task 3.4)
# ---------------------------------------------------------------------------


class TestSpecChangeApprovedRichTitle:
    def test_title_uses_proposal_heading(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        proposal = workspace / "openspec" / "changes" / "my-change" / "proposal.md"
        proposal.write_text("# Rich Issue Title\n\nBody.", encoding="utf-8")
        handler_with_openspec.handle_bus_event(
            "spec-change-approved",
            "spec-agent",
            "human",
            "Approved: my-change: some description",
            None,
            "my-change",
        )
        call_sc = handler_with_openspec.adapter.create_issue.call_args[0][0]
        # Default mode is `both`, so the subject carries the agent segment after
        # the change prefix (agent value = the resolved specs-repo name).
        agent = handler_with_openspec._specs_repo_name()
        assert getattr(call_sc, "title", "") == f"[my-change][{agent}] Rich Issue Title"

    def test_title_falls_back_to_change_name_when_no_proposal(
        self, handler_with_openspec: PmSyncHandler
    ) -> None:
        # No proposal.md written — _read_proposal_description returns ""
        handler_with_openspec.handle_bus_event(
            "spec-change-approved",
            "spec-agent",
            "human",
            "Approved: my-change",
            None,
            "my-change",
        )
        call_sc = handler_with_openspec.adapter.create_issue.call_args[0][0]
        agent = handler_with_openspec._specs_repo_name()
        assert getattr(call_sc, "title", "") == f"[my-change][{agent}] my-change"

    def test_description_populated_from_proposal(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        proposal = workspace / "openspec" / "changes" / "my-change" / "proposal.md"
        proposal.write_text("# Title\n\nFull description body.", encoding="utf-8")
        handler_with_openspec.handle_bus_event(
            "spec-change-approved",
            "spec-agent",
            "human",
            "Approved: my-change",
            None,
            "my-change",
        )
        call_sc = handler_with_openspec.adapter.create_issue.call_args[0][0]
        assert "Full description body." in getattr(call_sc, "description", "")

    def test_jtbd_id_threaded_from_handle_event(self, handler_with_openspec: PmSyncHandler) -> None:
        class _FakeMsg:
            type = "spec-change-approved"
            from_ = "spec-agent"
            to = "human"
            subject = "Approved: my-change"
            frontmatter = {"change": "my-change", "jtbd-id": "JTBD-42", "spec-path": ""}

        handler_with_openspec.handle_event(_FakeMsg())
        call_sc = handler_with_openspec.adapter.create_issue.call_args[0][0]
        assert getattr(call_sc, "jtbd_id", None) == "JTBD-42"

    def test_jtbd_id_none_when_absent_from_frontmatter(
        self, handler_with_openspec: PmSyncHandler
    ) -> None:
        class _FakeMsg:
            type = "spec-change-approved"
            from_ = "spec-agent"
            to = "human"
            subject = "Approved: my-change"
            frontmatter: dict = {"change": "my-change"}

        handler_with_openspec.handle_event(_FakeMsg())
        call_sc = handler_with_openspec.adapter.create_issue.call_args[0][0]
        assert getattr(call_sc, "jtbd_id", "sentinel") is None


# ---------------------------------------------------------------------------
# Garbage-directory bug fix (2026-07-04 GAP audit finding)
# ---------------------------------------------------------------------------


class TestNoChangeFrontmatterSkipsRatherThanGuesses:
    """handle_event() must not derive change_name from the free-text
    subject for spec-change-approved events -- that produced garbage
    openspec/changes/ directories (e.g. "Subject", or a full sentence
    scraped from the body) whenever the subject didn't match the
    expected "Approved: <change-name>" shape."""

    def test_missing_change_frontmatter_skips_create_issue(
        self, handler_with_openspec: PmSyncHandler, caplog
    ) -> None:
        class _FakeMsg:
            type = "spec-change-approved"
            from_ = "spec-agent"
            to = "human"
            subject = "Subject: Approved: pluggable-secret-backend"
            frontmatter: dict = {}

        handler_with_openspec.handle_event(_FakeMsg())
        handler_with_openspec.adapter.create_issue.assert_not_called()
        assert any("no 'change:' frontmatter field" in rec.message for rec in caplog.records)

    def test_valid_change_frontmatter_still_works(
        self, handler_with_openspec: PmSyncHandler
    ) -> None:
        class _FakeMsg:
            type = "spec-change-approved"
            from_ = "spec-agent"
            to = "human"
            subject = "Subject: some unrelated free text"
            frontmatter: dict = {"change": "my-change"}

        handler_with_openspec.handle_event(_FakeMsg())
        handler_with_openspec.adapter.create_issue.assert_called_once()


class TestWriteNeverCreatesChangeDirectory:
    """_write_pm_sync_yaml must never mkdir a change directory that
    doesn't already exist -- that's the write site that turned any bad
    change_name into a real garbage directory on disk."""

    def test_nonexistent_change_dir_is_skipped_not_created(
        self, handler: PmSyncHandler, workspace: Path, caplog
    ) -> None:
        # openspec/changes/ (the root) exists so _specs_root() resolves,
        # but the specific change directory does not -- this is the
        # exact shape of the bug: a bad/unexpected change_name.
        (workspace / "openspec" / "changes").mkdir(parents=True)
        target_dir = workspace / "openspec" / "changes" / "does-not-exist-yet"
        assert not target_dir.exists()

        handler._write_pm_sync_yaml("does-not-exist-yet", 99)

        assert not target_dir.exists(), "_write_pm_sync_yaml must not create a new change directory"
        assert any("refusing to create it" in rec.message for rec in caplog.records)

    def test_existing_change_dir_still_gets_written(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        handler_with_openspec._write_pm_sync_yaml("my-change", 123)
        pm_sync = workspace / "openspec" / "changes" / "my-change" / ".pm-sync.yaml"
        assert pm_sync.is_file()


# ---------------------------------------------------------------------------
# platform_custom_fields injection (task 3.5)
# ---------------------------------------------------------------------------


_PLATFORM_YAML_WITH_CUSTOM_FIELDS = """\
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
  custom-fields:
    jtbd-id: 4
    otaman-agent: 5
repos:
  - name: otaman-specs
    owner: spec-agent
"""


class TestLoadAdapterCustomFields:
    def test_custom_fields_passed_to_adapter_constructor(self, workspace: Path) -> None:
        """_load_adapter injects platform_custom_fields from config into the adapter."""
        from unittest.mock import patch

        (workspace / "platform.yaml").write_text(
            _PLATFORM_YAML_WITH_CUSTOM_FIELDS, encoding="utf-8"
        )

        mock_cls = MagicMock()
        mock_cls.return_value.capabilities = _make_capabilities()

        # get_pm_adapter is called inline inside _load_adapter; patch it to return
        # mock_cls so we can inspect constructor kwargs.
        with patch("otaman_core.pm_sync.get_pm_adapter", return_value=mock_cls):
            PmSyncHandler(workspace)

        assert mock_cls.called
        passed_cf = mock_cls.call_args.kwargs.get("platform_custom_fields")
        assert passed_cf == {"jtbd-id": 4, "otaman-agent": 5}

    def test_empty_custom_fields_passed_when_absent_from_config(self, workspace: Path) -> None:
        """When custom-fields is absent from platform.yaml, pass empty dict."""
        from unittest.mock import patch

        mock_cls = MagicMock()
        mock_cls.return_value.capabilities = _make_capabilities()

        with patch("otaman_core.pm_sync.get_pm_adapter", return_value=mock_cls):
            PmSyncHandler(workspace)

        assert mock_cls.called
        passed_cf = mock_cls.call_args.kwargs.get("platform_custom_fields")
        assert passed_cf == {}


# ---------------------------------------------------------------------------
# pm-agent-ident (B5 ruling): configurable agent identification
# ---------------------------------------------------------------------------


class TestAgentIdentification:
    """Bridge half of pm-agent-ident: compose the subject per config + pass the
    mode to the adapter (which gates the otaman-agent custom-field write)."""

    def _fire(self, handler: PmSyncHandler) -> object:
        handler.handle_bus_event(
            "spec-change-approved", "spec-agent", "human", "Approved: my-change", None, "my-change"
        )
        return handler.adapter.create_issue.call_args[0][0]

    def _write_proposal(self, workspace: Path) -> None:
        (workspace / "openspec" / "changes" / "my-change" / "proposal.md").write_text(
            "# Rich Title\n\nBody.", encoding="utf-8"
        )

    def test_default_both_composes_agent_segment(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        self._write_proposal(workspace)
        sc = self._fire(handler_with_openspec)  # config default → both
        agent = handler_with_openspec._specs_repo_name()
        assert getattr(sc, "title", "") == f"[my-change][{agent}] Rich Title"

    def test_custom_field_mode_omits_agent_from_subject(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        self._write_proposal(workspace)
        handler_with_openspec.config = dataclasses.replace(
            handler_with_openspec.config, agent_identification="custom-field"
        )
        sc = self._fire(handler_with_openspec)
        assert getattr(sc, "title", "") == "[my-change] Rich Title"

    def test_subject_prefix_mode_includes_agent(
        self, handler_with_openspec: PmSyncHandler, workspace: Path
    ) -> None:
        self._write_proposal(workspace)
        handler_with_openspec.config = dataclasses.replace(
            handler_with_openspec.config, agent_identification="subject-prefix"
        )
        sc = self._fire(handler_with_openspec)
        agent = handler_with_openspec._specs_repo_name()
        assert getattr(sc, "title", "") == f"[my-change][{agent}] Rich Title"

    def test_mode_passed_to_adapter_setter(self, workspace: Path) -> None:
        """_load_adapter hands the mode to the adapter's set_agent_identification."""
        (workspace / "platform.yaml").write_text(
            _PLATFORM_YAML_WITH_CUSTOM_FIELDS.replace(
                "  custom-fields:",
                "  agent-identification: custom-field\n  custom-fields:",
            ),
            encoding="utf-8",
        )
        mock_cls = MagicMock()
        mock_cls.return_value.capabilities = _make_capabilities()
        with patch("otaman_core.pm_sync.get_pm_adapter", return_value=mock_cls):
            PmSyncHandler(workspace)
        mock_cls.return_value.set_agent_identification.assert_called_once_with("custom-field")

    def test_default_mode_passed_to_adapter_setter(self, workspace: Path) -> None:
        mock_cls = MagicMock()
        mock_cls.return_value.capabilities = _make_capabilities()
        with patch("otaman_core.pm_sync.get_pm_adapter", return_value=mock_cls):
            PmSyncHandler(workspace)
        mock_cls.return_value.set_agent_identification.assert_called_once_with("both")
