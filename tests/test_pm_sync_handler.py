"""Tests for pm_sync_handler.py — bus-to-PM event handler."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path):
    """Minimal project root with platform.yaml containing a pm-sync block."""
    root = tmp_path / "my-project"
    root.mkdir()
    (root / ".agents" / "bus" / "active").mkdir(parents=True)
    (root / "platform.yaml").write_text(
        """
project: test
version: "1.0"
pm-sync:
  provider: easy8
  base_url: https://pm.example.com
  identity_mode: system_user
  program_name: Test Program
  program_key: TEST
  per_repo: false
  exclude_repos: []
  webhook_target: https://hooks.example.com/pm
  project_map:
    otaman-core: 12
""",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def project_root_no_pm(tmp_path):
    """Project root with platform.yaml but NO pm-sync block."""
    root = tmp_path / "no-pm-project"
    root.mkdir()
    (root / ".agents" / "bus" / "active").mkdir(parents=True)
    (root / "platform.yaml").write_text(
        "project: test\nversion: '1.0'\n",
        encoding="utf-8",
    )
    return root


def _make_mock_adapter():
    """Return a mock that satisfies PmSyncAdapter protocol."""
    adapter = MagicMock()
    adapter.capabilities.issue_comments = True
    return adapter


def _make_handler_with_mock_adapter(project_root):
    """Build a PmSyncHandler with a mocked adapter injected after construction."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    # Patch _load_adapter so no real network calls happen
    mock_adapter = _make_mock_adapter()
    with patch.object(PmSyncHandler, "_load_adapter", return_value=mock_adapter):
        handler = PmSyncHandler(project_root)

    return handler, mock_adapter


# ---------------------------------------------------------------------------
# handle_inbound_webhook — happy path
# ---------------------------------------------------------------------------


def test_handle_inbound_webhook_valid_payload(project_root):
    """handle_inbound_webhook with a valid payload returns ok=True."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    # Build a minimal PmInboundEvent-like object the adapter returns
    fake_event = SimpleNamespace(
        event_type="issue_updated",
        project_id=12,
        issue_id=99,
        new_status="In Progress",
        spec_path=None,
    )
    mock_adapter = _make_mock_adapter()
    mock_adapter.handle_inbound_event.return_value = fake_event

    with patch.object(PmSyncHandler, "_load_adapter", return_value=mock_adapter):
        handler = PmSyncHandler(project_root)

    payload = {"action": "issue_updated", "issue": {"id": 99}, "project": {"id": 12}}
    result = handler.handle_inbound_webhook(payload)

    assert result["ok"] is True
    assert result["event_type"] == "issue_updated"
    mock_adapter.handle_inbound_event.assert_called_once_with(payload)


# ---------------------------------------------------------------------------
# handle_inbound_webhook — pm-sync not configured
# ---------------------------------------------------------------------------


def test_handle_inbound_webhook_not_configured(project_root_no_pm):
    """handle_inbound_webhook returns ok=False when pm-sync is not configured."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    handler = PmSyncHandler(project_root_no_pm)
    assert not handler.enabled

    result = handler.handle_inbound_webhook({"foo": "bar"})

    assert result["ok"] is False
    assert "error" in result


def test_handle_inbound_webhook_no_platform_yaml(tmp_path):
    """handle_inbound_webhook returns ok=False when there is no platform.yaml."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    root = tmp_path / "empty"
    root.mkdir()
    handler = PmSyncHandler(root)
    assert not handler.enabled

    result = handler.handle_inbound_webhook({})
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# _emit_bus_event — issue_updated + Done + spec_path -> spec-update-requested
# ---------------------------------------------------------------------------


def test_emit_bus_event_done_with_spec_path(project_root):
    """_emit_bus_event writes a spec-update-requested message when status is Done."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    handler, mock_adapter = _make_handler_with_mock_adapter(project_root)
    assert handler.enabled

    event = SimpleNamespace(
        event_type="issue_updated",
        project_id=12,
        issue_id=42,
        new_status="Done",
        spec_path="openspec/specs/pm-sync/spec.md",
    )

    handler._emit_bus_event(event)

    active_dir = project_root / ".agents" / "bus" / "active"
    files = list(active_dir.iterdir())
    assert len(files) == 1, f"Expected 1 bus message file, found {len(files)}: {files}"

    content = files[0].read_text(encoding="utf-8")
    assert "type: spec-update-requested" in content
    assert "to: spec-agent" in content
    assert "openspec/specs/pm-sync/spec.md" in content


def test_emit_bus_event_created_goes_to_human(project_root):
    """_emit_bus_event writes pm-issue-created and routes to human."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    handler, _ = _make_handler_with_mock_adapter(project_root)

    event = SimpleNamespace(
        event_type="issue_created",
        project_id=12,
        issue_id=7,
        new_status=None,
        spec_path=None,
    )

    handler._emit_bus_event(event)

    active_dir = project_root / ".agents" / "bus" / "active"
    files = list(active_dir.iterdir())
    assert len(files) == 1

    content = files[0].read_text(encoding="utf-8")
    assert "type: pm-issue-created" in content
    assert "to: human" in content


def test_emit_bus_event_updated_no_spec_path_goes_to_human(project_root):
    """_emit_bus_event with issue_updated but no spec_path routes to human."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    handler, _ = _make_handler_with_mock_adapter(project_root)

    event = SimpleNamespace(
        event_type="issue_updated",
        project_id=12,
        issue_id=8,
        new_status="Done",
        spec_path=None,
    )

    handler._emit_bus_event(event)

    active_dir = project_root / ".agents" / "bus" / "active"
    files = list(active_dir.iterdir())
    content = files[0].read_text(encoding="utf-8")
    assert "type: pm-issue-updated" in content
    assert "to: human" in content


# ---------------------------------------------------------------------------
# handle_bus_event — outbound
# ---------------------------------------------------------------------------


def test_handle_bus_event_spec_change_approved(project_root):
    """handle_bus_event with spec-change-approved calls adapter.create_issue."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    handler, mock_adapter = _make_handler_with_mock_adapter(project_root)
    mock_issue = SimpleNamespace(id=55)
    mock_adapter.create_issue.return_value = mock_issue

    handler.handle_bus_event(
        msg_type="spec-change-approved",
        msg_from="spec-agent",
        msg_to="bridge-agent",
        subject="PM sync integration approved",
        spec_path="openspec/specs/pm-sync/spec.md",
        change_name="pm-sync-integration",
    )

    mock_adapter.create_issue.assert_called_once()
    call_args = mock_adapter.create_issue.call_args[0][0]
    assert call_args.change_name == "pm-sync-integration"


def test_handle_bus_event_disabled_is_noop(project_root_no_pm):
    """handle_bus_event is a no-op when pm-sync is not configured."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    handler = PmSyncHandler(project_root_no_pm)
    # Should not raise
    handler.handle_bus_event(
        msg_type="spec-change-approved",
        msg_from="spec-agent",
        msg_to="bridge-agent",
        subject="should be ignored",
        spec_path=None,
        change_name="some-change",
    )


# ---------------------------------------------------------------------------
# _load_adapter — fallback to direct Easy8Adapter import
# ---------------------------------------------------------------------------


def test_load_adapter_falls_back_to_easy8(project_root):
    """_load_adapter resolves Easy8Adapter when otaman_core registry misses it."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    # Simulate otaman_core registry raising KeyError
    def fake_get_pm_adapter(name):
        raise KeyError(name)

    mock_easy8_cls = MagicMock()
    mock_instance = _make_mock_adapter()
    mock_easy8_cls.return_value = mock_instance

    with (
        patch("otaman_core.pm_sync.get_pm_adapter", fake_get_pm_adapter),
        patch.dict(sys.modules, {"otaman_adapters.easy8": MagicMock(Easy8Adapter=mock_easy8_cls)}),
    ):
        handler = PmSyncHandler(project_root)

    # If the adapter loaded, enabled should be True
    assert handler.enabled


def test_load_adapter_sets_project_map(project_root):
    """_load_adapter calls set_project_map when the adapter supports it."""
    from otaman_bridge.pm_sync_handler import PmSyncHandler

    mock_adapter = _make_mock_adapter()
    mock_cls = MagicMock(return_value=mock_adapter)

    with patch("otaman_core.pm_sync.get_pm_adapter", return_value=mock_cls):
        handler = PmSyncHandler(project_root)

    mock_adapter.set_project_map.assert_called_once()
    call_kwargs = mock_adapter.set_project_map.call_args[0][0]
    assert "otaman-core" in call_kwargs
