"""Tests for experimental_mode detection and banner helpers.

Covers multi-tenant-org-runtime task 2.2 and containerized-agent-execution
task 4.3.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from otaman_bridge.experimental_mode import (
    BANNER_BLOCK,
    BANNER_LABEL,
    BANNER_ONELINE,
    RUNTIME_MODE_EXPERIMENTAL_MULTI_TENANT,
    RUNTIME_MODE_SINGLE,
    detect_runtime_mode,
    emit_startup_banner,
    healthz_extras,
    is_experimental,
    prefix_approval_body,
)


# ---------------------------------------------------------------------------
# Detection helpers


class TestDetectRuntimeMode:
    def test_returns_none_when_no_platform_yaml(self, tmp_path):
        assert detect_runtime_mode(tmp_path) is None

    def test_reads_flat_layout(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: experimental_multi_tenant\n",
            encoding="utf-8",
        )
        assert detect_runtime_mode(tmp_path) == "experimental_multi_tenant"

    def test_reads_multi_tenant_layout(self, tmp_path):
        platform_dir = tmp_path / "_platform"
        platform_dir.mkdir()
        (platform_dir / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: single\n",
            encoding="utf-8",
        )
        assert detect_runtime_mode(tmp_path) == "single"

    def test_multi_tenant_layout_takes_precedence_over_flat(self, tmp_path):
        platform_dir = tmp_path / "_platform"
        platform_dir.mkdir()
        (platform_dir / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: experimental_multi_tenant\n",
            encoding="utf-8",
        )
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: single\n",
            encoding="utf-8",
        )
        assert detect_runtime_mode(tmp_path) == "experimental_multi_tenant"

    def test_returns_none_when_runtime_key_absent(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "project: test\nversion: '1.0'\n",
            encoding="utf-8",
        )
        assert detect_runtime_mode(tmp_path) is None

    def test_returns_none_on_corrupt_yaml(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "runtime: [not a dict]\n", encoding="utf-8"
        )
        assert detect_runtime_mode(tmp_path) is None

    def test_returns_none_on_parse_error(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant: {unclosed",
            encoding="utf-8",
        )
        assert detect_runtime_mode(tmp_path) is None


class TestIsExperimental:
    def test_true_for_experimental_multi_tenant(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: experimental_multi_tenant\n",
            encoding="utf-8",
        )
        assert is_experimental(tmp_path) is True

    def test_false_for_single_mode(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: single\n",
            encoding="utf-8",
        )
        assert is_experimental(tmp_path) is False

    def test_false_when_no_file(self, tmp_path):
        assert is_experimental(tmp_path) is False


# ---------------------------------------------------------------------------
# Banner constants — contract stability


class TestBannerConstants:
    def test_oneline_contains_experimental_warning_cue(self):
        assert "EXPERIMENTAL" in BANNER_ONELINE
        assert "⚠️" in BANNER_ONELINE

    def test_block_is_multiline(self):
        assert "\n" in BANNER_BLOCK
        assert "EXPERIMENTAL" in BANNER_BLOCK

    def test_block_contains_key_safety_messages(self):
        assert "NOT audited" in BANNER_BLOCK or "not audited" in BANNER_BLOCK.lower()
        assert "non-production" in BANNER_BLOCK.lower()

    def test_label_is_machine_readable(self):
        assert " " not in BANNER_LABEL
        assert BANNER_LABEL == RUNTIME_MODE_EXPERIMENTAL_MULTI_TENANT


# ---------------------------------------------------------------------------
# emit_startup_banner


class TestEmitStartupBanner:
    def test_emits_block_when_experimental(self, tmp_path, caplog):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: experimental_multi_tenant\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            result = emit_startup_banner(tmp_path)
        assert result is True
        assert "EXPERIMENTAL" in caplog.text

    def test_no_emission_when_single(self, tmp_path, caplog):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: single\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            result = emit_startup_banner(tmp_path)
        assert result is False
        assert "EXPERIMENTAL" not in caplog.text

    def test_no_emission_when_no_file(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            result = emit_startup_banner(tmp_path)
        assert result is False


# ---------------------------------------------------------------------------
# prefix_approval_body


class TestPrefixApprovalBody:
    def test_prepends_oneline_when_experimental(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: experimental_multi_tenant\n",
            encoding="utf-8",
        )
        result = prefix_approval_body("please approve this", tmp_path)
        assert result.startswith(BANNER_ONELINE)
        assert "please approve this" in result

    def test_no_prefix_when_single_mode(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: single\n",
            encoding="utf-8",
        )
        body = "please approve this"
        result = prefix_approval_body(body, tmp_path)
        assert result == body

    def test_no_prefix_when_no_file(self, tmp_path):
        body = "please approve"
        assert prefix_approval_body(body, tmp_path) == body


# ---------------------------------------------------------------------------
# healthz_extras


class TestHealthzExtras:
    def test_empty_when_no_root(self):
        assert healthz_extras(None) == {}

    def test_includes_runtime_mode_when_set(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: experimental_multi_tenant\n",
            encoding="utf-8",
        )
        extras = healthz_extras(tmp_path)
        assert extras["runtime_mode"] == "experimental_multi_tenant"
        assert "experimental_warning" in extras

    def test_no_experimental_warning_for_single_mode(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "runtime:\n  multi_tenant:\n    mode: single\n",
            encoding="utf-8",
        )
        extras = healthz_extras(tmp_path)
        assert extras.get("runtime_mode") == "single"
        assert "experimental_warning" not in extras

    def test_empty_when_no_platform_yaml(self, tmp_path):
        assert healthz_extras(tmp_path) == {}
