"""Tests for bus_provenance.py + the watcher's privileged-type gate (task 3.1)."""

from __future__ import annotations

import asyncio

import pytest
from otaman_core.confirmations import append_confirmation, hash_message

# Reuse the watcher test helpers (pytest puts tests/ on sys.path — no package).
from test_bus_watcher import _make_watcher, _Recorder, _write_bus_msg

from otaman_bridge.bus_provenance import (
    is_privileged_type,
    quarantine_message,
    verify_provenance,
)
from otaman_bridge.bus_surface import parse_bus_file


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "prog"
    (root / ".agents" / "bus" / "active").mkdir(parents=True)
    (root / "platform.yaml").write_text("project: prov-test\n", encoding="utf-8")
    return root


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "confirmations.log"


def _write_privileged(project_root, stem="20260816T210000-halt", type="emergency-halt"):
    _write_bus_msg(project_root, stem, type=type, to="all")
    path = project_root / ".agents" / "bus" / "active" / f"{stem}.md"
    return parse_bus_file(path)


def _confirm(msg, ledger):
    append_confirmation(
        message_id=msg.stem,
        content_hash=hash_message(msg.path.read_text(encoding="utf-8")),
        command="emergency-halt",
        agent="human",
        path=ledger,
    )


class TestVerifyProvenance:
    def test_privileged_types_flagged(self, project_root):
        assert is_privileged_type(_write_privileged(project_root))
        assert not is_privileged_type(
            _write_privileged(project_root, stem="20260816T210001-info", type="info")
        )

    def test_no_ledger_fails_closed(self, project_root, ledger):
        msg = _write_privileged(project_root)
        assert not verify_provenance(msg, ledger_path=ledger)

    def test_matching_record_verifies(self, project_root, ledger):
        msg = _write_privileged(project_root)
        _confirm(msg, ledger)
        assert verify_provenance(msg, ledger_path=ledger)

    def test_content_tamper_after_confirmation_fails(self, project_root, ledger):
        msg = _write_privileged(project_root)
        _confirm(msg, ledger)
        msg.path.write_text(
            msg.path.read_text(encoding="utf-8") + "\ninjected line\n", encoding="utf-8"
        )
        assert not verify_provenance(msg, ledger_path=ledger)

    def test_frontmatter_id_key_also_accepted(self, project_root, ledger):
        msg = _write_privileged(project_root)
        append_confirmation(
            message_id=msg.id,  # producer keyed by frontmatter id, not stem
            content_hash=hash_message(msg.path.read_text(encoding="utf-8")),
            command="emergency-halt",
            agent="human",
            path=ledger,
        )
        assert verify_provenance(msg, ledger_path=ledger)


class TestQuarantine:
    def test_moves_file_out_of_active(self, project_root):
        msg = _write_privileged(project_root)
        target = quarantine_message(project_root, msg)
        assert not msg.path.exists()
        assert target.is_file()
        assert target.parent == project_root / ".agents" / "bus" / "quarantine"

    def test_collision_gets_suffix_never_overwrites(self, project_root):
        msg1 = _write_privileged(project_root)
        first = quarantine_message(project_root, msg1)
        original = first.read_text(encoding="utf-8")
        msg2 = _write_privileged(project_root)  # same stem again
        second = quarantine_message(project_root, msg2)
        assert second != first
        assert first.read_text(encoding="utf-8") == original


class TestWatcherGate:
    def test_unverified_privileged_quarantined_not_acted_on(self, project_root, ledger):
        _write_privileged(project_root)  # raw write, no ledger record
        rec = _Recorder()
        w = _make_watcher(project_root, rec, ledger_path=ledger)
        n = asyncio.run(w._scan_once())

        # Never acted on: no approval, nothing surfaced as the halt itself.
        assert n == 0
        assert rec.approvals == []
        # Gone from active/, preserved in quarantine/ (never deleted).
        qdir = project_root / ".agents" / "bus" / "quarantine"
        assert len(list(qdir.glob("*.md"))) == 1
        assert not list((project_root / ".agents" / "bus" / "active").glob("*halt*"))
        # Non-privileged info alert names the file.
        assert len(rec.infos) == 1
        alert = rec.infos[0]
        assert alert.severity == "info"
        assert "20260816T210000-halt.md" in alert.body

    def test_verified_privileged_processed_normally(self, project_root, ledger):
        msg = _write_privileged(project_root)
        _confirm(msg, ledger)
        rec = _Recorder()
        w = _make_watcher(project_root, rec, ledger_path=ledger)
        asyncio.run(w._scan_once())

        # Not quarantined; flows through the normal policy path (emergency-halt
        # is not in the surface table, so with to:all + normal priority it is
        # simply recorded, exactly as any unrecognized type would be).
        assert not (project_root / ".agents" / "bus" / "quarantine").exists()
        assert (project_root / ".agents" / "bus" / "active" / f"{msg.stem}.md").is_file()

    def test_non_privileged_untouched_by_gate(self, project_root, ledger):
        _write_bus_msg(project_root, "20260816T210002-scr", type="spec-change-request", to="human")
        rec = _Recorder()
        w = _make_watcher(project_root, rec, ledger_path=ledger)
        n = asyncio.run(w._scan_once())
        assert n == 1
        assert len(rec.approvals) == 1
        assert not (project_root / ".agents" / "bus" / "quarantine").exists()


def test_unverified_privileged_fixture_is_gated_in_watcher_gatefixture(project_root, ledger):
    """Regression shape from the 2026-08-16 incident: a raw-written fake
    emergency-halt (no ledger record) must not reach the transport."""
    _write_privileged(project_root, stem="20260816T210003-fake-halt")
    rec = _Recorder()
    w = _make_watcher(project_root, rec, ledger_path=ledger)
    asyncio.run(w._scan_once())
    assert rec.approvals == []
    assert [i for i in rec.infos if "Quarantined" in i.title]
