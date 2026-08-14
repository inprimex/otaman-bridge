"""Tests for bridge/config.py — account + transport config loader."""

from __future__ import annotations

import pytest
import yaml

from otaman_bridge.config import (
    list_accounts_from_settings,
    load_account_config,
)


@pytest.fixture
def maestro_folder(tmp_path):
    root = tmp_path / "my-maestro"
    root.mkdir()
    (root / "platform.yaml").write_text("project: test\n", encoding="utf-8")
    return root


def _write_settings(root, data):
    (root / "launch-settings.yaml").write_text(
        yaml.dump(data),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Default (null) transport


class TestNullDefault:
    def test_no_transport_defaults_to_null(self, maestro_folder):
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "personal": {"config_dir": "~/.claude-personal"},
                },
            },
        )
        cfg = load_account_config(
            "personal",
            maestro_folder / "launch-settings.yaml",
        )
        assert cfg.transport == "null"
        assert cfg.transport_config == {}
        assert cfg.config_dir == "~/.claude-personal"

    def test_preserves_config_dir_and_label(self, maestro_folder):
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "personal": {
                        "config_dir": "~/.claude-personal",
                        "label": "Personal",
                    },
                },
            },
        )
        cfg = load_account_config(
            "personal",
            maestro_folder / "launch-settings.yaml",
        )
        assert cfg.label == "Personal"


# ---------------------------------------------------------------------------
# Long-form transport config


class TestLongForm:
    def test_explicit_transport_and_config(self, maestro_folder, monkeypatch):
        monkeypatch.setenv("MY_BOT_TOKEN", "secret-token-123")
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "personal": {
                        "config_dir": "~/.claude-personal",
                        "transport": "telegram",
                        "transport_config": {
                            "group_id": -1001111,
                            "allowed_user_ids": [12345],
                            "bot_token": {
                                "sources": [
                                    {"type": "env", "name": "MY_BOT_TOKEN"},
                                ]
                            },
                        },
                    },
                },
            },
        )
        cfg = load_account_config(
            "personal",
            maestro_folder / "launch-settings.yaml",
        )
        assert cfg.transport == "telegram"
        assert cfg.transport_config["group_id"] == -1001111
        assert cfg.transport_config["allowed_user_ids"] == [12345]
        assert cfg.transport_config["bot_token"] == "secret-token-123"

    def test_unresolved_secret_dropped_from_config(self, maestro_folder, monkeypatch):
        """If the chain yields no value, key is dropped AND reported."""
        monkeypatch.delenv("MISSING_TOKEN", raising=False)
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "personal": {
                        "config_dir": "~/.claude-personal",
                        "transport": "telegram",
                        "transport_config": {
                            "group_id": -1001111,
                            "bot_token": {
                                "sources": [
                                    {"type": "env", "name": "MISSING_TOKEN"},
                                ]
                            },
                        },
                    },
                },
            },
        )
        cfg = load_account_config(
            "personal",
            maestro_folder / "launch-settings.yaml",
        )
        assert "bot_token" not in cfg.transport_config
        assert "bot_token" in cfg.unresolved_secrets

    def test_resolve_secrets_false_keeps_raw(self, maestro_folder, monkeypatch):
        """Tests don't want secrets resolved; verify the opt-out."""
        monkeypatch.setenv("MY_BOT_TOKEN", "real-secret")
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "personal": {
                        "transport": "telegram",
                        "transport_config": {
                            "bot_token": "MY_BOT_TOKEN",  # short form
                        },
                    },
                },
            },
        )
        cfg = load_account_config(
            "personal",
            maestro_folder / "launch-settings.yaml",
            resolve_secrets=False,
        )
        # With resolve_secrets=False, the raw string stays in config
        assert cfg.transport_config["bot_token"] == "MY_BOT_TOKEN"
        assert "bot_token" in cfg.unresolved_secrets


# ---------------------------------------------------------------------------
# Short-form (legacy) sugar


class TestShortForm:
    def test_telegram_short_form_expands(self, maestro_folder, monkeypatch):
        """`telegram:` block auto-promotes when `transport:` isn't set."""
        monkeypatch.setenv("MY_TG_BOT", "token-xyz")
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "personal": {
                        "config_dir": "~/.claude-personal",
                        "telegram": {
                            "group_id": -1001111,
                            "bot_token_env": "MY_TG_BOT",
                        },
                    },
                },
            },
        )
        cfg = load_account_config(
            "personal",
            maestro_folder / "launch-settings.yaml",
        )
        assert cfg.transport == "telegram"
        assert cfg.transport_config["group_id"] == -1001111
        assert cfg.transport_config["bot_token"] == "token-xyz"

    def test_explicit_transport_wins_over_short_form(self, maestro_folder):
        """If both transport: and telegram: appear, explicit wins."""
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "personal": {
                        "transport": "null",
                        "telegram": {"group_id": -42},  # should be ignored
                    },
                },
            },
        )
        cfg = load_account_config(
            "personal",
            maestro_folder / "launch-settings.yaml",
        )
        assert cfg.transport == "null"
        assert cfg.transport_config == {}

    def test_bot_token_env_translates_to_bot_token_string(
        self,
        maestro_folder,
        monkeypatch,
    ):
        """Short ``bot_token_env: NAME`` normalizes to ``bot_token: NAME``,
        which then resolves via the env source chain."""
        monkeypatch.setenv("X_BOT", "val")
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "p": {
                        "telegram": {
                            "group_id": 1,
                            "bot_token_env": "X_BOT",
                        },
                    },
                },
            },
        )
        cfg = load_account_config(
            "p",
            maestro_folder / "launch-settings.yaml",
        )
        assert cfg.transport_config["bot_token"] == "val"


# ---------------------------------------------------------------------------
# Error paths


class TestErrors:
    def test_unknown_account_raises(self, maestro_folder):
        _write_settings(maestro_folder, {"accounts": {"personal": {}}})
        with pytest.raises(KeyError, match="ghost"):
            load_account_config(
                "ghost",
                maestro_folder / "launch-settings.yaml",
            )

    def test_no_settings_file_raises(self, maestro_folder):
        with pytest.raises(KeyError):
            load_account_config(
                "personal",
                maestro_folder / "launch-settings.yaml",
            )

    def test_non_mapping_account_raises(self, maestro_folder):
        _write_settings(
            maestro_folder,
            {
                "accounts": {"personal": "not-a-dict"},
            },
        )
        with pytest.raises(ValueError, match="mapping"):
            load_account_config(
                "personal",
                maestro_folder / "launch-settings.yaml",
            )


# ---------------------------------------------------------------------------
# Dotenv fallback


class TestDotenvResolution:
    def test_bot_token_from_dotenv(self, maestro_folder, monkeypatch):
        monkeypatch.delenv("MY_BOT", raising=False)
        (maestro_folder / ".maestro").mkdir()
        (maestro_folder / ".maestro" / "secrets.env").write_text(
            "MY_BOT=from-dotenv\n",
            encoding="utf-8",
        )
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "p": {
                        "transport": "telegram",
                        "transport_config": {
                            "bot_token": {
                                "sources": [
                                    {"type": "env", "name": "MY_BOT"},
                                    {"type": "dotenv", "name": "MY_BOT"},
                                ]
                            },
                        },
                    },
                },
            },
        )
        cfg = load_account_config(
            "p",
            maestro_folder / "launch-settings.yaml",
        )
        assert cfg.transport_config["bot_token"] == "from-dotenv"

    def test_env_beats_dotenv(self, maestro_folder, monkeypatch):
        monkeypatch.setenv("MY_BOT", "from-env")
        (maestro_folder / ".maestro").mkdir()
        (maestro_folder / ".maestro" / "secrets.env").write_text(
            "MY_BOT=from-dotenv\n",
            encoding="utf-8",
        )
        _write_settings(
            maestro_folder,
            {
                "accounts": {
                    "p": {
                        "transport": "telegram",
                        "transport_config": {
                            "bot_token": {
                                "sources": [
                                    {"type": "env", "name": "MY_BOT"},
                                    {"type": "dotenv", "name": "MY_BOT"},
                                ]
                            },
                        },
                    },
                },
            },
        )
        cfg = load_account_config(
            "p",
            maestro_folder / "launch-settings.yaml",
        )
        assert cfg.transport_config["bot_token"] == "from-env"


# ---------------------------------------------------------------------------
# Helpers


class TestListAccounts:
    def test_returns_sorted_names(self, maestro_folder):
        _write_settings(
            maestro_folder,
            {
                "accounts": {"zulu": {}, "alpha": {}, "mike": {}},
            },
        )
        assert list_accounts_from_settings(maestro_folder / "launch-settings.yaml") == [
            "alpha",
            "mike",
            "zulu",
        ]

    def test_empty_when_no_file(self, tmp_path):
        assert list_accounts_from_settings(tmp_path / "nope.yaml") == []

    def test_empty_when_no_accounts_block(self, tmp_path):
        (tmp_path / "s.yaml").write_text("active_connection: local\n")
        assert list_accounts_from_settings(tmp_path / "s.yaml") == []
