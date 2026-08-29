"""Tests for the bridge program-lifecycle gate (program-lifecycle-states 2.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from otaman_core.lifecycle import lifecycle_registry_path, record_transition

from otaman_bridge.bus_watcher import BusWatcher
from otaman_bridge.lifecycle_gate import (
    is_inert,
    program_lifecycle_state,
    resolve_org_and_program,
)


def _layout(tmp_path: Path, org: str = "acme", program: str = "proj") -> tuple[Path, Path]:
    """Build the canonical <org_root>/programs/<program>/otaman-meta tree.

    Returns (project_root, org_root).
    """
    project_root = tmp_path / "orgs" / org / "programs" / program / "otaman-meta"
    project_root.mkdir(parents=True)
    return project_root, tmp_path / "orgs" / org


def _set_state(org_root: Path, program: str, state: str) -> None:
    record_transition(lifecycle_registry_path(org_root), program, state, by="roman@acme")


# ---------------------------------------------------------------------------
# org/program resolution
# ---------------------------------------------------------------------------


class TestResolve:
    def test_canonical_layout(self, tmp_path):
        project_root, org_root = _layout(tmp_path, org="acme", program="proj")
        assert resolve_org_and_program(project_root) == (org_root, "proj")

    def test_no_programs_ancestor_returns_none(self, tmp_path):
        # A path with no 'programs' component can't be resolved -> None.
        odd = tmp_path / "somewhere" / "otaman-meta"
        odd.mkdir(parents=True)
        assert resolve_org_and_program(odd) is None

    def test_nested_programs_uses_innermost(self, tmp_path):
        # If 'programs' appears twice, the innermost (nearest the leaf) wins.
        project_root = tmp_path / "programs" / "outer" / "programs" / "inner" / "otaman-meta"
        project_root.mkdir(parents=True)
        org_root, program = resolve_org_and_program(project_root)
        assert program == "inner"
        assert org_root == tmp_path / "programs" / "outer"


# ---------------------------------------------------------------------------
# state reading + inert classification
# ---------------------------------------------------------------------------


class TestState:
    def test_absent_registry_is_active(self, tmp_path):
        project_root, _ = _layout(tmp_path)
        assert program_lifecycle_state(project_root) == "active"
        assert is_inert("active") is False

    def test_unresolvable_path_is_active(self, tmp_path):
        odd = tmp_path / "no-programs-here"
        odd.mkdir(parents=True)
        assert program_lifecycle_state(odd) == "active"

    def test_reads_suspended_and_is_inert(self, tmp_path):
        project_root, org_root = _layout(tmp_path)
        _set_state(org_root, "proj", "suspended")
        assert program_lifecycle_state(project_root) == "suspended"
        assert is_inert("suspended") is True

    def test_reads_archived_and_is_inert(self, tmp_path):
        project_root, org_root = _layout(tmp_path)
        _set_state(org_root, "proj", "archived")
        assert program_lifecycle_state(project_root) == "archived"
        assert is_inert("archived") is True

    def test_limited_reads_but_not_inert(self, tmp_path):
        project_root, org_root = _layout(tmp_path)
        _set_state(org_root, "proj", "limited")
        assert program_lifecycle_state(project_root) == "limited"
        assert is_inert("limited") is False  # limited is a runner concern, bridge normal

    def test_resume_restores_active(self, tmp_path):
        project_root, org_root = _layout(tmp_path)
        _set_state(org_root, "proj", "suspended")
        assert is_inert(program_lifecycle_state(project_root)) is True
        _set_state(org_root, "proj", "active")  # resume / unarchive
        assert is_inert(program_lifecycle_state(project_root)) is False

    def test_other_program_state_does_not_leak(self, tmp_path):
        # A sibling program being suspended must not affect this one.
        project_root, org_root = _layout(tmp_path, program="proj")
        _set_state(org_root, "other", "archived")
        assert program_lifecycle_state(project_root) == "active"


# ---------------------------------------------------------------------------
# BusWatcher enforcement — the gate actually pauses/resumes surfacing at runtime
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.infos: list = []
        self.approvals: list = []

    async def on_info(self, info):
        self.infos.append(info)

    async def on_approval(self, approval, msg):
        self.approvals.append((approval, msg))


def _program_project(
    tmp_path: Path, *, program: str = "proj", org: str = "acme"
) -> tuple[Path, Path]:
    """Canonical layout with a bus dir + platform.yaml. Returns (project_root, org_root)."""
    project_root = tmp_path / "orgs" / org / "programs" / program / "otaman-meta"
    (project_root / ".agents" / "bus" / "active").mkdir(parents=True)
    (project_root / "platform.yaml").write_text(
        f"project: {program}\nversion: '1.0'\nrepos: []\n", encoding="utf-8"
    )
    return project_root, tmp_path / "orgs" / org


def _write_scr(project_root: Path, stem: str) -> Path:
    """A spec-change-request to human — surfaces as one approval in a normal scan."""
    p = project_root / ".agents" / "bus" / "active" / f"{stem}.md"
    p.write_text(
        f"---\nid: {stem}\nfrom: agent-a\nto: human\npriority: normal\n"
        f"type: spec-change-request\ntimestamp: 2026-04-24T10:00:00Z\n---\n\n"
        f"## Subject: t\n\nbody\n",
        encoding="utf-8",
    )
    return p


def _watcher(project_root: Path, rec: _Recorder) -> BusWatcher:
    return BusWatcher(
        project_root,
        account="acct",
        project="proj",
        on_info=rec.on_info,
        on_approval=rec.on_approval,
        poll_interval=0.05,
    )


class TestWatcherGate:
    def test_active_bridge_surfaces_normally(self, tmp_path):
        project_root, _ = _program_project(tmp_path)  # absent registry -> active
        _write_scr(project_root, "20260101T000000-scr")
        rec = _Recorder()
        n = asyncio.run(_watcher(project_root, rec)._scan_once())
        assert n == 1
        assert len(rec.approvals) == 1

    def test_suspended_bridge_is_inert(self, tmp_path):
        project_root, org_root = _program_project(tmp_path)
        _write_scr(project_root, "20260101T000000-scr")
        _set_state(org_root, "proj", "suspended")
        rec = _Recorder()
        n = asyncio.run(_watcher(project_root, rec)._scan_once())
        assert n == 0  # inert: nothing surfaced
        assert rec.approvals == []

    def test_archived_bridge_is_inert(self, tmp_path):
        project_root, org_root = _program_project(tmp_path)
        _write_scr(project_root, "20260101T000000-scr")
        _set_state(org_root, "proj", "archived")
        rec = _Recorder()
        assert asyncio.run(_watcher(project_root, rec)._scan_once()) == 0
        assert rec.approvals == []

    def test_suspend_then_resume_restores_surfacing(self, tmp_path):
        """Runtime transition: a card pending during suspend surfaces on resume."""
        project_root, org_root = _program_project(tmp_path)
        _write_scr(project_root, "20260101T000000-scr")
        _set_state(org_root, "proj", "suspended")
        rec = _Recorder()
        w = _watcher(project_root, rec)
        assert asyncio.run(w._scan_once()) == 0  # inert, not surfaced
        assert rec.approvals == []
        _set_state(org_root, "proj", "active")  # resume — no restart
        assert asyncio.run(w._scan_once()) == 1  # the pending card now surfaces
        assert len(rec.approvals) == 1
