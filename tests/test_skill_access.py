"""Tests for tasks 3.1–3.3: Mode 2+ cofounder-only skill enforcement.

Scenarios:
  3.3(a) Cofounder token passes — role: cofounder in CallContext.roles
  3.3(b) Non-cofounder token blocked — structured error returned, not 500
  3.3(c) Mode 1 (no Zitadel) — bridge defers to plugin; no check performed
"""

from __future__ import annotations

from pathlib import Path

import pytest

from otaman_bridge.mcp_server import CallContext
from otaman_bridge.skill_access import check_skill_access, is_zitadel_mode

INVESTOR_SKILL = "tech-startup:investor-targeting-strategist"
FINANCIAL_SKILL = "tech-startup:financial-modeling-analyst"
PUBLIC_SKILL = "tech-startup:pitch-deck-composer"
COFOUNDER_ONLY = "cofounder-only"
PUBLIC = "public"


def _platform_yaml(tmp_path: Path, *, zitadel: bool) -> Path:
    content = "identity:\n  provider: zitadel\n" if zitadel else "project: test\n"
    p = tmp_path / "platform.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _ctx(*roles: str) -> CallContext:
    return CallContext(user_id="u1", user_email=None, roles=roles)


# ---------------------------------------------------------------------------
# is_zitadel_mode
# ---------------------------------------------------------------------------


class TestIsZitadelMode:
    def test_none_path_returns_false(self):
        assert is_zitadel_mode(None) is False

    def test_missing_file_returns_false(self, tmp_path):
        assert is_zitadel_mode(tmp_path / "missing.yaml") is False

    def test_zitadel_provider_returns_true(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=True)
        assert is_zitadel_mode(p) is True

    def test_absent_identity_returns_false(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=False)
        assert is_zitadel_mode(p) is False

    def test_other_provider_returns_false(self, tmp_path):
        p = tmp_path / "platform.yaml"
        p.write_text("identity:\n  provider: keycloak\n", encoding="utf-8")
        assert is_zitadel_mode(p) is False

    def test_empty_yaml_returns_false(self, tmp_path):
        p = tmp_path / "platform.yaml"
        p.write_text("", encoding="utf-8")
        assert is_zitadel_mode(p) is False


# ---------------------------------------------------------------------------
# Scenario 3.3(a): cofounder token passes in Mode 2+
# ---------------------------------------------------------------------------


class TestCofounterTokenPasses:
    def test_cofounder_role_allowed(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=True)
        result = check_skill_access(INVESTOR_SKILL, COFOUNDER_ONLY, _ctx("cofounder"), platform_yaml_path=p)
        assert result is None

    def test_cofounder_among_multiple_roles_passes(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=True)
        result = check_skill_access(INVESTOR_SKILL, COFOUNDER_ONLY, _ctx("cofounder", "member", "admin"), platform_yaml_path=p)
        assert result is None

    def test_cofounder_allowed_for_financial_skill(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=True)
        result = check_skill_access(FINANCIAL_SKILL, COFOUNDER_ONLY, _ctx("cofounder"), platform_yaml_path=p)
        assert result is None


# ---------------------------------------------------------------------------
# Scenario 3.3(b): non-cofounder token blocked in Mode 2+
# ---------------------------------------------------------------------------


class TestNonCofounterBlocked:
    def test_empty_roles_blocked(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=True)
        result = check_skill_access(INVESTOR_SKILL, COFOUNDER_ONLY, _ctx(), platform_yaml_path=p)
        assert result is not None
        assert result["error"] == "skill_access_denied"
        assert result["skill"] == INVESTOR_SKILL
        assert result["required_role"] == "cofounder"

    def test_member_role_blocked(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=True)
        result = check_skill_access(INVESTOR_SKILL, COFOUNDER_ONLY, _ctx("member"), platform_yaml_path=p)
        assert result is not None
        assert result["error"] == "skill_access_denied"

    def test_admin_without_cofounder_blocked(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=True)
        result = check_skill_access(INVESTOR_SKILL, COFOUNDER_ONLY, _ctx("admin"), platform_yaml_path=p)
        assert result is not None
        assert result["error"] == "skill_access_denied"

    def test_public_skill_passes_even_without_cofounder_role(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=True)
        result = check_skill_access(PUBLIC_SKILL, PUBLIC, _ctx(), platform_yaml_path=p)
        assert result is None

    def test_error_shape_matches_spec(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=True)
        result = check_skill_access(FINANCIAL_SKILL, COFOUNDER_ONLY, _ctx("member"), platform_yaml_path=p)
        assert result == {
            "error": "skill_access_denied",
            "skill": FINANCIAL_SKILL,
            "required_role": "cofounder",
        }


# ---------------------------------------------------------------------------
# Scenario 3.3(c): Mode 1 — bridge defers, no enforcement
# ---------------------------------------------------------------------------


class TestMode1Defers:
    def test_no_platform_yaml_no_check(self):
        result = check_skill_access(INVESTOR_SKILL, COFOUNDER_ONLY, _ctx(), platform_yaml_path=None)
        assert result is None

    def test_no_zitadel_provider_no_check(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=False)
        result = check_skill_access(INVESTOR_SKILL, COFOUNDER_ONLY, _ctx(), platform_yaml_path=p)
        assert result is None

    def test_non_cofounder_passes_in_mode1(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=False)
        result = check_skill_access(INVESTOR_SKILL, COFOUNDER_ONLY, _ctx("member"), platform_yaml_path=p)
        assert result is None

    def test_both_cofounder_only_skills_pass_in_mode1(self, tmp_path):
        p = _platform_yaml(tmp_path, zitadel=False)
        ctx = _ctx()
        r1 = check_skill_access(INVESTOR_SKILL, COFOUNDER_ONLY, ctx, platform_yaml_path=p)
        r2 = check_skill_access(FINANCIAL_SKILL, COFOUNDER_ONLY, ctx, platform_yaml_path=p)
        assert r1 is None and r2 is None
