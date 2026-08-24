"""Tests for edition.py + the 3.1 CE gates (ce-ee-release-channels)."""

from __future__ import annotations

import logging
from pathlib import Path

import otaman_bridge.edition as edition_mod
import otaman_bridge.spawn_decision as spawn_mod
from otaman_bridge.edition import (
    CE_SPAWN_NOTICE,
    edition_identity,
    edition_status,
    emit_ce_notice_once,
    mismatch_diagnostic,
    runner_feature_unavailable_text,
)


def _write_edition(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "edition.yaml"
    p.write_text(content, encoding="utf-8")
    return p


class TestEditionIdentity:
    def test_ce(self, tmp_path):
        p = _write_edition(tmp_path, "edition: ce\nchannel: ce\n")
        assert edition_identity(p) == "ce"

    def test_ee(self, tmp_path):
        p = _write_edition(tmp_path, "edition: ee\n")
        assert edition_identity(p) == "ee"

    def test_missing_file_is_unknown(self, tmp_path):
        assert edition_identity(tmp_path / "nope.yaml") == "unknown"

    def test_unparseable_is_unknown(self, tmp_path):
        p = _write_edition(tmp_path, ":: not: [yaml")
        assert edition_identity(p) == "unknown"

    def test_non_mapping_is_unknown(self, tmp_path):
        p = _write_edition(tmp_path, "- just\n- a list\n")
        assert edition_identity(p) == "unknown"

    def test_weird_edition_value_is_unknown(self, tmp_path):
        p = _write_edition(tmp_path, "edition: enterprise-plus\n")
        assert edition_identity(p) == "unknown"

    def test_unknown_keys_ignored(self, tmp_path):
        """Q3a forward-compat: readers MUST ignore unknown keys."""
        p = _write_edition(
            tmp_path,
            "edition: ce\nfuture_phase_b_field: whatever\nsubscription:\n  tenant_id: x\n",
        )
        assert edition_identity(p) == "ce"


class TestMismatchDiagnostic:
    def test_file_ee_probe_ce(self, tmp_path, monkeypatch):
        p = _write_edition(tmp_path, "edition: ee\n")
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: False)
        assert "says 'ee'" in mismatch_diagnostic(p)

    def test_file_ce_probe_ee(self, tmp_path, monkeypatch):
        p = _write_edition(tmp_path, "edition: ce\n")
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: True)
        assert "says 'ce'" in mismatch_diagnostic(p)

    def test_agreement_none(self, tmp_path, monkeypatch):
        p = _write_edition(tmp_path, "edition: ce\n")
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: False)
        assert mismatch_diagnostic(p) is None

    def test_unknown_none(self, tmp_path):
        assert mismatch_diagnostic(tmp_path / "nope.yaml") is None


class TestEditionStatus:
    def test_ce_shape(self, tmp_path, monkeypatch):
        p = _write_edition(tmp_path, "edition: ce\n")
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: False)
        s = edition_status(p)
        assert s["edition"] == "ce"
        assert s["auto_session_spawn"] == "unavailable (EE)"
        assert "edition_diagnostic" not in s

    def test_mismatch_includes_diagnostic(self, tmp_path, monkeypatch):
        p = _write_edition(tmp_path, "edition: ee\n")
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: False)
        assert "edition_diagnostic" in edition_status(p)


class TestCeNotice:
    def test_emitted_once_and_only_when_unavailable(self, monkeypatch, caplog):
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: False)
        monkeypatch.setattr(edition_mod, "_ce_notice_emitted", False)
        logger = logging.getLogger("test.edition.notice")
        with caplog.at_level(logging.INFO, logger=logger.name):
            assert emit_ce_notice_once(logger) is True
            assert emit_ce_notice_once(logger) is False  # once per process
        assert sum(CE_SPAWN_NOTICE in r.message for r in caplog.records) == 1

    def test_not_emitted_when_available(self, monkeypatch):
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: True)
        monkeypatch.setattr(edition_mod, "_ce_notice_emitted", False)
        assert emit_ce_notice_once() is False


class TestRunnerFeatureText:
    def test_none_when_ee(self, monkeypatch):
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: True)
        assert runner_feature_unavailable_text("X") is None

    def test_edition_boundary_wording_in_ce(self, monkeypatch):
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: False)
        text = runner_feature_unavailable_text("Team session listing")
        assert "hosted/EE tier" in text
        assert "not an error" in text


class _ExplodingRunner:
    """Any call means the CE gate failed — spawn paths must not touch it."""

    def __getattr__(self, name):
        raise AssertionError(f"runner_client.{name} called despite CE gate")


class TestSpawnGate:
    def test_ce_gate_skips_without_touching_runner(self, tmp_path, monkeypatch):
        monkeypatch.setattr(spawn_mod, "auto_session_spawn_available", lambda: False)
        # Reset the notice latch so this test observes the emit path too.
        monkeypatch.setattr(edition_mod, "_ce_notice_emitted", False)
        msg = tmp_path / "20260819T000000-task.md"
        msg.write_text(
            "---\ntype: task-assignment\nto: bridge-agent\n---\n\n## Subject: x\n",
            encoding="utf-8",
        )
        outcome = spawn_mod.handle_bus_event(
            msg,
            registry=None,  # gate fires before any of these are touched
            runner_client=_ExplodingRunner(),
            owned_agents={"otaman-bridge": "bridge-agent"},
            bus_dir=tmp_path,
            project_root=str(tmp_path),
        )
        assert outcome is None

    def test_ee_path_unchanged(self, tmp_path, monkeypatch):
        """With the gate open, non-task messages still return None via the
        normal parse path (proves the gate does not swallow EE behavior)."""
        monkeypatch.setattr(spawn_mod, "auto_session_spawn_available", lambda: True)
        msg = tmp_path / "20260819T000001-info.md"
        msg.write_text("---\ntype: info\nto: all\n---\n\nbody\n", encoding="utf-8")
        outcome = spawn_mod.handle_bus_event(
            msg,
            registry=None,
            runner_client=_ExplodingRunner(),
            owned_agents={"otaman-bridge": "bridge-agent"},
            bus_dir=tmp_path,
            project_root=str(tmp_path),
        )
        assert outcome is None

    def test_ce_gate_ignores_stale_runner_endpoint(self, tmp_path, monkeypatch, caplog):
        """1.3 boundary case 4: a leftover runner.endpoint from a prior EE run
        is irrelevant under CE. The probe gates first, so the endpoint file is
        never read and no connection/retry is attempted — one honest notice,
        clean logs (no spawn-failed noise), graceful skip."""
        from otaman_bridge.runner_client import RunnerClient

        # A well-formed endpoint file, exactly as a prior EE run would leave.
        stale = tmp_path / "runner.endpoint"
        stale.write_text("host=127.0.0.1\nport=8091\ntoken=TKN\npid=1234\n", encoding="utf-8")

        def _explode(*_a, **_k):
            raise AssertionError("CE bridge touched a stale runner endpoint")

        client = RunnerClient(endpoint_file=stale, opener=_explode)
        # Reaching the endpoint read (or any connection) at all is a CE-gate failure.
        monkeypatch.setattr(client, "_read_endpoint", _explode)

        # CE = EE package absent; probe drives both the gate and the notice.
        monkeypatch.setattr(edition_mod, "ee_features_present", lambda: False)
        monkeypatch.setattr(edition_mod, "_ce_notice_emitted", False)

        msg = tmp_path / "20260824T000000-task.md"
        msg.write_text(
            "---\ntype: task-assignment\nto: bridge-agent\n---\n\n## Subject: x\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.INFO):
            outcome = spawn_mod.handle_bus_event(
                msg,
                registry=None,
                runner_client=client,
                owned_agents={"otaman-bridge": "bridge-agent"},
                bus_dir=tmp_path,
                project_root=str(tmp_path),
            )
        assert outcome is None  # graceful skip, no crash
        assert stale.exists()  # leftover file left untouched on disk
        # Exactly one honest edition notice; zero runner/spawn-failed noise.
        assert sum(CE_SPAWN_NOTICE in r.message for r in caplog.records) == 1
        assert not any(
            "spawn" in r.message.lower() and "fail" in r.message.lower() for r in caplog.records
        )
