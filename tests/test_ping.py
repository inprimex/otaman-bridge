"""Tests for scripts/ping.py — maestro ping CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent
PING_INVOKE = [sys.executable, "-m", "otaman_bridge.ping"]


@pytest.fixture
def maestro_folder(tmp_path):
    root = tmp_path / "my-maestro"
    root.mkdir()
    (root / "platform.yaml").write_text(
        "project: ping-test\nversion: '1.0'\nrepos: []\n", encoding="utf-8",
    )
    (root / ".agents").mkdir()
    return root


def _env_with_home(home: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("OTAMAN_ACTIVE_ROUTING", None)
    env.pop("OTAMAN_ACTIVE_ACCOUNT", None)
    env.pop("MAESTRO_ACTIVE_ACCOUNT", None)
    bridge_src = str(REPO_ROOT / "src")
    core_src = str(REPO_ROOT.parent / "otaman-core" / "src")
    env["PYTHONPATH"] = os.pathsep.join([bridge_src, core_src, env.get("PYTHONPATH", "")])
    return env


def _run(args: list[str], *, cwd: Path, home: Path, env_extra=None):
    env = _env_with_home(home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        PING_INVOKE + list(args),
        capture_output=True, text=True, timeout=15,
        cwd=cwd, env=env,
    )


def _start_daemon(account: str, home: Path):
    from otaman_bridge.daemon import BridgeDaemon
    from otaman_bridge.transports.null import NullTransport
    transport = NullTransport(allowlist={"*"})
    endpoint = home / ".maestro" / f"bridge-{account}.endpoint"
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    daemon = BridgeDaemon(
        account=account, transport=transport, endpoint_file=endpoint,
    )
    daemon.start()
    return daemon, transport


class TestPingErrors:
    def test_no_message_is_argparse_error(self, maestro_folder):
        result = _run([], cwd=maestro_folder, home=maestro_folder)
        # argparse's own exit code for missing required args is 2
        assert result.returncode != 0

    def test_no_account_resolvable_errors(self, maestro_folder, tmp_path):
        """Run from a non-maestro dir with no account hints — clear error."""
        result = _run(
            ["hello"], cwd=tmp_path, home=maestro_folder,
        )
        assert result.returncode == 1
        assert "account" in result.stderr.lower()

    def test_no_daemon_endpoint_errors(self, maestro_folder):
        result = _run(
            ["hi"], cwd=maestro_folder, home=maestro_folder,
            env_extra={"MAESTRO_ACTIVE_ACCOUNT": "ghost"},
        )
        assert result.returncode == 1
        assert "endpoint" in result.stderr.lower()
        # Should suggest how to start the daemon
        assert "bridge run" in result.stderr

    def test_invalid_severity_errors(self, maestro_folder):
        result = _run(
            ["--severity", "disaster", "test"],
            cwd=maestro_folder, home=maestro_folder,
            env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
        )
        # argparse catches bogus choices
        assert result.returncode != 0


class TestPingHappyPath:
    def test_posts_notify_to_daemon(self, maestro_folder):
        daemon, transport = _start_daemon("personal", maestro_folder)
        try:
            result = _run(
                ["I", "need", "help"],
                cwd=maestro_folder, home=maestro_folder,
                env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
            )
            assert result.returncode == 0, result.stderr

            for _ in range(40):
                if transport.sent_infos:
                    break
                time.sleep(0.05)
            assert transport.sent_infos, "notify should have been received"
            info = transport.sent_infos[0]
            assert info.body == "I need help"
            assert info.account == "personal"
            # Default severity is 'approval'
            assert info.severity == "approval"
        finally:
            daemon.stop()

    def test_custom_title_and_severity(self, maestro_folder):
        daemon, transport = _start_daemon("personal", maestro_folder)
        try:
            result = _run(
                ["--title", "Deploy failed",
                 "--severity", "blocking",
                 "build", "broken"],
                cwd=maestro_folder, home=maestro_folder,
                env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
            )
            assert result.returncode == 0
            for _ in range(40):
                if transport.sent_infos:
                    break
                time.sleep(0.05)
            info = transport.sent_infos[0]
            assert info.title == "Deploy failed"
            assert info.severity == "blocking"
            assert info.body == "build broken"
        finally:
            daemon.stop()

    def test_account_override(self, maestro_folder):
        """Explicit --account wins over env inference."""
        daemon, transport = _start_daemon("riseapps", maestro_folder)
        try:
            result = _run(
                ["--account", "riseapps", "hello"],
                cwd=maestro_folder, home=maestro_folder,
                # Env points elsewhere — flag should override
                env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
            )
            assert result.returncode == 0
            for _ in range(40):
                if transport.sent_infos:
                    break
                time.sleep(0.05)
            assert transport.sent_infos[0].account == "riseapps"
        finally:
            daemon.stop()

    def test_no_debounce_even_for_identical_content(self, maestro_folder):
        """Unlike the Stop hook, ping is explicit — every call delivers."""
        daemon, transport = _start_daemon("personal", maestro_folder)
        try:
            for _ in range(3):
                _run(
                    ["same", "message"],
                    cwd=maestro_folder, home=maestro_folder,
                    env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
                )
            for _ in range(40):
                if len(transport.sent_infos) >= 3:
                    break
                time.sleep(0.05)
            assert len(transport.sent_infos) == 3, (
                "ping should not debounce — user asked for delivery, explicitly"
            )
        finally:
            daemon.stop()


class TestStdoutConfirmation:
    def test_prints_confirmation_on_success(self, maestro_folder):
        daemon, _ = _start_daemon("personal", maestro_folder)
        try:
            result = _run(
                ["hello"], cwd=maestro_folder, home=maestro_folder,
                env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
            )
            assert result.returncode == 0
            assert "Sent ping" in result.stdout
            assert "personal" in result.stdout
        finally:
            daemon.stop()
