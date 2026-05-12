"""Tests for bridge/cli.py — maestro bridge run/status/stop.

Tests are run via subprocess (the CLI is not designed for in-process
invocation — it installs signal handlers and owns stdout). Each test
uses a temp HOME so endpoint files don't collide across runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
# bridge cli is now invoked as a package module
BRIDGE_CLI = [sys.executable, "-m", "otaman_bridge.cli"]


def _env_with_home(home: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows
    # Subprocess needs explicit PYTHONPATH (pytest pythonpath does not propagate)
    bridge_src = str(REPO_ROOT / "src")
    core_src = str(REPO_ROOT.parent / "otaman-core" / "src")
    env["PYTHONPATH"] = os.pathsep.join([bridge_src, core_src, env.get("PYTHONPATH", "")])
    return env


@pytest.fixture
def sandbox_home(tmp_path):
    """Give each test its own HOME so endpoint files don't collide."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".maestro").mkdir()
    return home


# ---------------------------------------------------------------------------
# status


class TestStatus:
    def test_status_with_no_accounts(self, sandbox_home):
        """No launch-settings.yaml → friendly empty message."""
        cwd = sandbox_home / "project"
        cwd.mkdir()
        result = subprocess.run(
            BRIDGE_CLI + ["status"],
            capture_output=True, text=True, timeout=10,
            env=_env_with_home(sandbox_home), cwd=cwd,
        )
        assert result.returncode == 0
        assert "No accounts configured" in result.stdout

    def test_status_shows_stopped_for_unbound_account(self, sandbox_home):
        result = subprocess.run(
            BRIDGE_CLI + ["status", "--account", "ghost"],
            capture_output=True, text=True, timeout=10,
            env=_env_with_home(sandbox_home),
        )
        assert result.returncode == 0
        assert "ghost" in result.stdout
        assert "stopped" in result.stdout


# ---------------------------------------------------------------------------
# run + stop (full lifecycle via subprocess)


class TestRunStopLifecycle:
    @pytest.mark.skipif(
        sys.platform == "darwin",
        reason="macOS CI runners (GitHub Actions) have loopback-bind/daemon-startup "
               "timing that exceeds reasonable subprocess timeouts; tests pass on "
               "local macOS and on ubuntu/windows CI. Backlog: investigate exact "
               "runner constraint and re-enable."
    )
    def test_run_then_stop_roundtrip(self, sandbox_home):
        endpoint_file = sandbox_home / ".maestro" / "bridge-test.endpoint"
        proc = subprocess.Popen(
            BRIDGE_CLI + ["run",
             "--account", "test",
             "--transport", "null"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env_with_home(sandbox_home),
        )
        try:
            # Wait for endpoint file to appear
            for _ in range(60):
                if endpoint_file.is_file():
                    break
                time.sleep(0.1)
            else:
                out, err = proc.communicate(timeout=10)
                pytest.fail(
                    f"daemon failed to write endpoint file\nstdout={out}\nstderr={err}"
                )

            data = json.loads(endpoint_file.read_text(encoding="utf-8"))
            assert data["account"] == "test"
            assert data["transport"] == "null"
            assert data["port"] > 0

            # Call status to confirm running
            status = subprocess.run(
                BRIDGE_CLI + ["status", "--account", "test"],
                capture_output=True, text=True, timeout=10,
                env=_env_with_home(sandbox_home),
            )
            assert status.returncode == 0
            assert "running" in status.stdout

            # Stop it
            stop = subprocess.run(
                BRIDGE_CLI + ["stop", "--account", "test"],
                capture_output=True, text=True, timeout=10,
                env=_env_with_home(sandbox_home),
            )
            assert stop.returncode == 0, stop.stderr
            assert "Stopped" in stop.stdout

            # Endpoint file should be gone
            assert not endpoint_file.exists()
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def test_run_rejects_unknown_transport(self, sandbox_home):
        result = subprocess.run(
            BRIDGE_CLI + ["run",
             "--account", "test", "--transport", "nonexistent"],
            capture_output=True, text=True, timeout=10,
            env=_env_with_home(sandbox_home),
        )
        assert result.returncode == 2
        assert "Unknown transport" in result.stderr

    @pytest.mark.skipif(
        sys.platform == "darwin",
        reason="macOS CI runners (GitHub Actions) have loopback-bind/daemon-startup "
               "timing that exceeds reasonable subprocess timeouts; tests pass on "
               "local macOS and on ubuntu/windows CI. Backlog: investigate exact "
               "runner constraint and re-enable."
    )
    def test_run_exits_on_shutdown_request(self, sandbox_home):
        """POST /shutdown must terminate the `bridge run` process, not just
        the HTTP server. Previously cmd_run only woke on SIGINT/SIGTERM,
        so /shutdown left a zombie process with an unlinked endpoint —
        the next `bridge run` then saw it as a live daemon on the old port
        and refused."""
        endpoint_file = sandbox_home / ".maestro" / "bridge-ex.endpoint"
        proc = subprocess.Popen(
            BRIDGE_CLI + ["run",
             "--account", "ex",
             "--transport", "null"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env_with_home(sandbox_home),
        )
        try:
            for _ in range(60):
                if endpoint_file.is_file():
                    break
                time.sleep(0.1)
            assert endpoint_file.is_file()

            # Call `stop`, which POSTs /shutdown. If cmd_run is wired
            # correctly, the daemon exits cleanly.
            stop = subprocess.run(
                BRIDGE_CLI + ["stop", "--account", "ex"],
                capture_output=True, text=True, timeout=10,
                env=_env_with_home(sandbox_home),
            )
            assert stop.returncode == 0, stop.stderr

            # The main process MUST exit — not just the HTTP thread.
            try:
                rc = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail(
                    "bridge run process did NOT exit after /shutdown — "
                    "cmd_run is probably still idling on stop_event only."
                )
            assert rc == 0, f"expected clean exit, got rc={rc}"
            assert not endpoint_file.exists()
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def test_stop_without_running_daemon(self, sandbox_home):
        """`stop` on an already-stopped account is a no-op success, not an error."""
        result = subprocess.run(
            BRIDGE_CLI + ["stop", "--account", "ghost"],
            capture_output=True, text=True, timeout=10,
            env=_env_with_home(sandbox_home),
        )
        assert result.returncode == 0
        assert "already stopped" in result.stdout.lower()

    def test_stop_cleans_stale_endpoint_file(self, sandbox_home):
        """Stale endpoint (daemon gone, file left) → `stop` cleans it up."""
        endpoint = sandbox_home / ".maestro" / "bridge-ghost.endpoint"
        # Port 1 is never listening locally — guaranteed connection refused.
        endpoint.write_text(json.dumps({
            "port": 1, "token": "stale", "pid": 99999,
            "account": "ghost", "transport": "null",
            "started_at": "2026-04-24T00:00:00+00:00",
        }), encoding="utf-8")

        result = subprocess.run(
            BRIDGE_CLI + ["stop", "--account", "ghost"],
            capture_output=True, text=True, timeout=10,
            env=_env_with_home(sandbox_home),
        )
        assert result.returncode == 0
        assert not endpoint.exists(), "stale endpoint file should be removed"
        assert "stale" in result.stdout.lower()


class TestInvalidArgs:
    def test_run_requires_account(self, sandbox_home):
        result = subprocess.run(
            BRIDGE_CLI + ["run"],
            capture_output=True, text=True, timeout=10,
            env=_env_with_home(sandbox_home),
        )
        assert result.returncode != 0
        assert "account" in result.stderr.lower()

    def test_no_subcommand_errors(self):
        result = subprocess.run(
            BRIDGE_CLI,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0

    def test_help_works(self, sandbox_home):
        result = subprocess.run(
            BRIDGE_CLI + ["--help"],
            capture_output=True, text=True, timeout=10,
            env=_env_with_home(sandbox_home),
        )
        assert result.returncode == 0
        assert "run" in result.stdout
        assert "status" in result.stdout
        assert "stop" in result.stdout


class TestConfigDrivenRun:
    """`maestro bridge run` reads launch-settings.yaml when --transport absent."""

    @pytest.mark.skipif(
        sys.platform == "darwin",
        reason="macOS CI runners (GitHub Actions) have loopback-bind/daemon-startup "
               "timing that exceeds reasonable subprocess timeouts; tests pass on "
               "local macOS and on ubuntu/windows CI. Backlog: investigate exact "
               "runner constraint and re-enable."
    )
    def test_picks_transport_from_settings(self, sandbox_home, tmp_path):
        """No --transport flag → daemon loads transport from launch-settings.yaml."""
        maestro = tmp_path / "my-maestro"
        maestro.mkdir()
        (maestro / "platform.yaml").write_text("project: x\n", encoding="utf-8")
        (maestro / ".agents").mkdir()
        (maestro / "launch-settings.yaml").write_text(
            "accounts:\n"
            "  demo:\n"
            "    config_dir: ~/.claude-demo\n"
            "    transport: null\n",  # explicit null; no secret needed
            encoding="utf-8",
        )

        endpoint_file = sandbox_home / ".maestro" / "bridge-demo.endpoint"
        proc = subprocess.Popen(
            BRIDGE_CLI + ["run", "--account", "demo"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env_with_home(sandbox_home), cwd=maestro,
        )
        try:
            for _ in range(60):
                if endpoint_file.is_file():
                    break
                time.sleep(0.1)
            else:
                out, err = proc.communicate(timeout=10)
                pytest.fail(
                    f"daemon failed to start\nstdout={out}\nstderr={err}"
                )
            data = json.loads(endpoint_file.read_text(encoding="utf-8"))
            assert data["transport"] == "null"
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=15)

    def test_missing_account_with_no_transport_errors(self, sandbox_home, tmp_path):
        """Account not in settings AND no --transport → clear error."""
        maestro = tmp_path / "my-maestro"
        maestro.mkdir()
        (maestro / "platform.yaml").write_text("project: x\n", encoding="utf-8")
        (maestro / ".agents").mkdir()
        (maestro / "launch-settings.yaml").write_text(
            "accounts:\n  other: {config_dir: ~/.claude-other}\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            BRIDGE_CLI + ["run", "--account", "missing"],
            capture_output=True, text=True, timeout=10,
            env=_env_with_home(sandbox_home), cwd=maestro,
        )
        assert result.returncode == 1
        assert "missing" in result.stderr
        assert "not in" in result.stderr
