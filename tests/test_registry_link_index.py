from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from otaman_bridge.registries.link_index import (
    RegistryLinkIndex,
    _build_index,
    _find_business_dir,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OUTCOMES_YAML = """
outcomes:
  - id: JTBD-1-create-account
    status: Done
    chosen-solution: SOL-1-email-password
  - id: JTBD-2-sign-up-sso
    status: Backlog
    chosen-solution: null
  - id: JTBD-3-invite-colleagues
    status: Done
    chosen-solution: SOL-5-invite-token
"""

SOLUTIONS_YAML = """
solutions:
  - id: SOL-1-email-password
    outcome-id: JTBD-1-create-account
    dependencies: []

  - id: SOL-2-magic-link
    outcome-id: JTBD-1-create-account
    dependencies:
      - kind: external
        name: Email service

  - id: SOL-5-invite-token
    outcome-id: JTBD-3-invite-colleagues
    dependencies:
      - kind: external
        name: Email service

  - id: SOL-10-assignee-dropdown
    outcome-id: JTBD-2-sign-up-sso
    dependencies:
      - kind: outcome
        ref: JTBD-3-invite-colleagues
      - kind: solution
        ref: SOL-5-invite-token
      - kind: external
        name: Notification service
"""


@pytest.fixture
def registry_dir(tmp_path):
    (tmp_path / "outcomes.yaml").write_text(OUTCOMES_YAML, encoding="utf-8")
    (tmp_path / "solutions.yaml").write_text(SOLUTIONS_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture
def index(registry_dir):
    return RegistryLinkIndex(
        registry_dir / "outcomes.yaml",
        registry_dir / "solutions.yaml",
    )


# ---------------------------------------------------------------------------
# _build_index (pure function)
# ---------------------------------------------------------------------------


class TestBuildIndex:
    def test_outcome_to_solutions(self, registry_dir):
        data = _build_index(
            registry_dir / "outcomes.yaml",
            registry_dir / "solutions.yaml",
        )
        assert sorted(data.outcome_to_solutions["JTBD-1-create-account"]) == [
            "SOL-1-email-password",
            "SOL-2-magic-link",
        ]
        assert data.outcome_to_solutions["JTBD-3-invite-colleagues"] == ["SOL-5-invite-token"]
        assert data.outcome_to_solutions["JTBD-2-sign-up-sso"] == ["SOL-10-assignee-dropdown"]

    def test_solution_to_outcome(self, registry_dir):
        data = _build_index(
            registry_dir / "outcomes.yaml",
            registry_dir / "solutions.yaml",
        )
        assert data.solution_to_outcome["SOL-1-email-password"] == "JTBD-1-create-account"
        assert data.solution_to_outcome["SOL-10-assignee-dropdown"] == "JTBD-2-sign-up-sso"

    def test_solution_to_deps_only_internal_kinds(self, registry_dir):
        data = _build_index(
            registry_dir / "outcomes.yaml",
            registry_dir / "solutions.yaml",
        )
        # SOL-10 has outcome + solution + external deps; only internal refs survive
        assert sorted(data.solution_to_deps["SOL-10-assignee-dropdown"]) == [
            "JTBD-3-invite-colleagues",
            "SOL-5-invite-token",
        ]
        # SOL-2 has only external deps — not in index
        assert "SOL-2-magic-link" not in data.solution_to_deps

    def test_no_deps_entry_when_empty(self, registry_dir):
        data = _build_index(
            registry_dir / "outcomes.yaml",
            registry_dir / "solutions.yaml",
        )
        assert "SOL-1-email-password" not in data.solution_to_deps

    def test_missing_solutions_file_returns_empty(self, tmp_path):
        outcomes = tmp_path / "outcomes.yaml"
        outcomes.write_text(OUTCOMES_YAML, encoding="utf-8")
        data = _build_index(outcomes, tmp_path / "solutions.yaml")
        assert data.outcome_to_solutions == {}
        assert data.solution_to_outcome == {}

    def test_missing_outcomes_file_still_builds_solution_maps(self, tmp_path):
        solutions = tmp_path / "solutions.yaml"
        solutions.write_text(SOLUTIONS_YAML, encoding="utf-8")
        data = _build_index(tmp_path / "outcomes.yaml", solutions)
        # solution→outcome maps are driven by solutions.yaml alone
        assert data.solution_to_outcome["SOL-1-email-password"] == "JTBD-1-create-account"

    def test_solution_without_outcome_id_skipped(self, tmp_path):
        (tmp_path / "outcomes.yaml").write_text("outcomes: []\n", encoding="utf-8")
        (tmp_path / "solutions.yaml").write_text(
            "solutions:\n  - id: SOL-orphan\n    dependencies: []\n",
            encoding="utf-8",
        )
        data = _build_index(tmp_path / "outcomes.yaml", tmp_path / "solutions.yaml")
        assert "SOL-orphan" not in data.solution_to_outcome
        assert data.outcome_to_solutions == {}


# ---------------------------------------------------------------------------
# RegistryLinkIndex query methods
# ---------------------------------------------------------------------------


class TestRegistryLinkIndexQueries:
    def test_solutions_for_outcome(self, index):
        sols = index.solutions_for_outcome("JTBD-1-create-account")
        assert set(sols) == {"SOL-1-email-password", "SOL-2-magic-link"}

    def test_solutions_for_unknown_outcome_empty(self, index):
        assert index.solutions_for_outcome("JTBD-99-nonexistent") == []

    def test_outcome_for_solution(self, index):
        assert index.outcome_for_solution("SOL-5-invite-token") == "JTBD-3-invite-colleagues"

    def test_outcome_for_unknown_solution_none(self, index):
        assert index.outcome_for_solution("SOL-99-nonexistent") is None

    def test_deps_for_solution(self, index):
        deps = index.deps_for_solution("SOL-10-assignee-dropdown")
        assert set(deps) == {"JTBD-3-invite-colleagues", "SOL-5-invite-token"}

    def test_deps_for_no_internal_deps_empty(self, index):
        assert index.deps_for_solution("SOL-2-magic-link") == []

    def test_deps_for_unknown_solution_empty(self, index):
        assert index.deps_for_solution("SOL-99-nonexistent") == []

    def test_query_returns_copy_not_reference(self, index):
        sols = index.solutions_for_outcome("JTBD-1-create-account")
        sols.clear()
        assert len(index.solutions_for_outcome("JTBD-1-create-account")) == 2


# ---------------------------------------------------------------------------
# Live reload on file change
# ---------------------------------------------------------------------------


class TestLiveReload:
    def test_index_updates_after_solutions_file_change(self, registry_dir):
        idx = RegistryLinkIndex(
            registry_dir / "outcomes.yaml",
            registry_dir / "solutions.yaml",
        )
        assert "SOL-1-email-password" in idx.solutions_for_outcome("JTBD-1-create-account")

        # Overwrite solutions.yaml with a stripped version
        (registry_dir / "solutions.yaml").write_text(
            "solutions:\n  - id: SOL-99-new\n"
            "    outcome-id: JTBD-1-create-account\n    dependencies: []\n",
            encoding="utf-8",
        )
        idx._rebuild()  # simulate fswatch event

        assert idx.solutions_for_outcome("JTBD-1-create-account") == ["SOL-99-new"]
        assert idx.outcome_for_solution("SOL-1-email-password") is None


# ---------------------------------------------------------------------------
# from_project_root factory
# ---------------------------------------------------------------------------


class TestFromProjectRoot:
    def _write_platform(self, root: Path, repos: list[dict]) -> None:
        (root / "platform.yaml").write_text(yaml.dump({"repos": repos}), encoding="utf-8")

    def _write_registries(self, business: Path) -> None:
        business.mkdir(parents=True, exist_ok=True)
        (business / "outcomes.yaml").write_text(OUTCOMES_YAML, encoding="utf-8")
        (business / "solutions.yaml").write_text(SOLUTIONS_YAML, encoding="utf-8")

    def test_resolves_cpo_agent_repo(self, tmp_path):
        biz = tmp_path / "otaman-biz"
        self._write_registries(biz)
        self._write_platform(tmp_path, [{"owner": "cpo-agent", "path": "otaman-biz"}])
        idx = RegistryLinkIndex.from_project_root(tmp_path)
        assert idx.outcome_for_solution("SOL-1-email-password") == "JTBD-1-create-account"

    def test_resolves_main_agent_repo_fallback(self, tmp_path):
        biz = tmp_path / "otaman-main"
        self._write_registries(biz)
        self._write_platform(tmp_path, [{"owner": "main-agent", "path": "otaman-main"}])
        idx = RegistryLinkIndex.from_project_root(tmp_path)
        assert idx.outcome_for_solution("SOL-1-email-password") == "JTBD-1-create-account"

    def test_env_override_takes_precedence(self, tmp_path):
        biz = tmp_path / "env-biz"
        self._write_registries(biz)
        self._write_platform(tmp_path, [{"owner": "cpo-agent", "path": "wrong-dir"}])
        idx = RegistryLinkIndex.from_project_root(tmp_path, env={"OTAMAN_BUSINESS_DIR": str(biz)})
        assert idx.outcome_for_solution("SOL-1-email-password") == "JTBD-1-create-account"

    def test_raises_when_no_business_repo(self, tmp_path):
        self._write_platform(tmp_path, [{"owner": "bridge-agent", "path": "otaman-bridge"}])
        with pytest.raises(FileNotFoundError, match="Cannot locate business repo"):
            RegistryLinkIndex.from_project_root(tmp_path)

    def test_raises_when_no_platform_yaml(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            RegistryLinkIndex.from_project_root(tmp_path)


# ---------------------------------------------------------------------------
# _find_business_dir
# ---------------------------------------------------------------------------


class TestFindBusinessDir:
    def test_env_var_overrides_platform(self, tmp_path):
        # Use a real absolute path: _find_business_dir resolves the override,
        # and a bare "/some/biz" resolves to a drive-qualified path on Windows.
        biz = tmp_path / "biz"
        result = _find_business_dir(tmp_path, {"OTAMAN_BUSINESS_DIR": str(biz)})
        assert result == biz.resolve()

    def test_returns_none_without_platform_yaml(self, tmp_path):
        assert _find_business_dir(tmp_path, {}) is None
