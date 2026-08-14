"""Enforce the messenger-transport abstraction boundary.

Runs scripts/check_transport_boundary.py against the current tree. Fails
if any file outside ``bridge/transports/`` imports a transport-specific
library. Catches leaks early in CI.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def _load_check_module():
    import importlib

    from otaman_bridge import check_transport_boundary as ctb

    return importlib.reload(ctb)


check_mod = _load_check_module()


class TestBoundary:
    def test_current_tree_is_clean(self):
        """Every PR must pass this. If it fails, move the offending
        import inside bridge/transports/ or mediate via Transport."""
        exit_code = check_mod.main()
        assert exit_code == 0, (
            "Transport-specific imports leaked outside bridge/transports/. "
            "Run `python scripts/check_transport_boundary.py` locally for details."
        )


class TestDetection:
    """Lint must actually catch violations — not just happy-path green."""

    def test_catches_forbidden_top_level_import(self, tmp_path, monkeypatch):
        leaky = tmp_path / "bridge" / "leaky.py"
        leaky.parent.mkdir(parents=True)
        leaky.write_text("import telegram\n", encoding="utf-8")
        monkeypatch.setattr(check_mod, "REPO_ROOT", tmp_path)
        exit_code = check_mod.main()
        assert exit_code == 1

    def test_catches_forbidden_from_import(self, tmp_path, monkeypatch):
        leaky = tmp_path / "bridge" / "daemon.py"
        leaky.parent.mkdir(parents=True)
        leaky.write_text(
            "from slack_sdk import WebClient\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(check_mod, "REPO_ROOT", tmp_path)
        exit_code = check_mod.main()
        assert exit_code == 1

    def test_catches_dotted_submodule(self, tmp_path, monkeypatch):
        """`from telegram.ext import ...` must be caught too."""
        leaky = tmp_path / "scripts" / "x.py"
        leaky.parent.mkdir(parents=True)
        leaky.write_text(
            "from telegram.ext import Application\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(check_mod, "REPO_ROOT", tmp_path)
        exit_code = check_mod.main()
        assert exit_code == 1

    def test_allows_imports_inside_transports_dir(self, tmp_path, monkeypatch):
        """Files under bridge/transports/ are allowed to import transport libs."""
        allowed = tmp_path / "bridge" / "transports" / "telegram.py"
        allowed.parent.mkdir(parents=True)
        allowed.write_text("import telegram\n", encoding="utf-8")
        # Provide a non-leaky sibling to make sure the scan runs over something
        (tmp_path / "bridge" / "core.py").write_text(
            "from typing import Protocol\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(check_mod, "REPO_ROOT", tmp_path)
        exit_code = check_mod.main()
        assert exit_code == 0

    def test_clean_tree_returns_zero(self, tmp_path, monkeypatch):
        (tmp_path / "some.py").write_text(
            "from pathlib import Path\nimport json\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(check_mod, "REPO_ROOT", tmp_path)
        exit_code = check_mod.main()
        assert exit_code == 0


class TestForbiddenList:
    """Documents the forbidden prefixes so adding a new transport
    requires touching this test — deliberate friction."""

    def test_all_forbidden_prefixes_present(self):
        expected = {"telegram", "slack_sdk", "discord", "nio", "matrix_nio"}
        assert expected.issubset(set(check_mod.FORBIDDEN_PREFIXES)), (
            f"check_transport_boundary.FORBIDDEN_PREFIXES is missing some "
            f"expected entries. Got: {check_mod.FORBIDDEN_PREFIXES}"
        )
