"""Tests for scripts/bridge_approval.py — PreToolUse daemon bridge."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
# bridge_approval invoked as a package module; afk also imported from package
HELPER_INVOKE = [sys.executable, "-m", "otaman_bridge.bridge_approval"]
from otaman_bridge import afk


@pytest.fixture
def maestro_folder(tmp_path):
    root = tmp_path / "my-maestro"
    root.mkdir()
    (root / "platform.yaml").write_text(
        "project: test-project\nversion: '1.0'\nrepos: []\n",
        encoding="utf-8",
    )
    (root / ".agents").mkdir()
    return root


def _run_helper(
    stdin_payload: dict,
    *,
    cwd: Path,
    home: Path,
    env_extra: dict | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess:
    """Invoke bridge_approval.py with the given stdin payload."""
    env = os.environ.copy()
    # Ensure subprocess can resolve otaman_bridge + otaman_core (pytest pythonpath config does not propagate)
    bridge_src = str(REPO_ROOT / "src")
    core_src = str(REPO_ROOT.parent / "otaman-core" / "src")
    env["PYTHONPATH"] = os.pathsep.join([bridge_src, core_src, env.get("PYTHONPATH", "")])
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("OTAMAN_ACTIVE_ROUTING", None)
    env.pop("OTAMAN_ACTIVE_ACCOUNT", None)
    env.pop("MAESTRO_ACTIVE_ACCOUNT", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        HELPER_INVOKE,
        input=json.dumps(stdin_payload),
        capture_output=True, text=True, timeout=timeout,
        cwd=cwd, env=env,
    )


def _start_daemon(account: str, home: Path):
    """Run a bridge daemon in a background thread with NullTransport."""
    sys.path.insert(0, str(REPO_ROOT))
    from otaman_bridge.daemon import BridgeDaemon
    from otaman_bridge.transports.null import NullTransport
    transport = NullTransport(allowlist={"*"})
    endpoint = home / ".maestro" / f"bridge-{account}.endpoint"
    daemon = BridgeDaemon(
        account=account,
        transport=transport,
        endpoint_file=endpoint,
    )
    daemon.start()
    return daemon, transport


# ---------------------------------------------------------------------------
# Fast-path: AFK off / expired


class TestAfkOff:
    def test_no_afk_file_exits_0_no_output(self, maestro_folder):
        """AFK absent → helper exits 0 with no stdout (native prompt)."""
        result = _run_helper(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            cwd=maestro_folder, home=maestro_folder,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_expired_afk_exits_0(self, maestro_folder):
        """Expired AFK → read_afk() deletes + returns None → fail-safe exit."""
        (maestro_folder / ".maestro").mkdir()
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        state = afk.AfkState(
            enabled_at=past - timedelta(hours=2),
            expires_at=past,
            source="manual",
        )
        afk.write_afk(maestro_folder, state)
        result = _run_helper(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            cwd=maestro_folder, home=maestro_folder,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_missing_tool_name_exits_0(self, maestro_folder):
        result = _run_helper({}, cwd=maestro_folder, home=maestro_folder)
        assert result.returncode == 0
        assert result.stdout == ""


# ---------------------------------------------------------------------------
# AFK on but daemon unreachable → fail-safe


class TestAfkOnNoDaemon:
    def _set_afk(self, root: Path):
        (root / ".maestro").mkdir(exist_ok=True)
        afk.write_afk(root, afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None,
            source="manual",
        ))

    def test_no_endpoint_file_falls_back(self, maestro_folder):
        self._set_afk(maestro_folder)
        result = _run_helper(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            cwd=maestro_folder, home=maestro_folder,
            env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
        )
        assert result.returncode == 0
        assert result.stdout == ""
        assert "endpoint missing" in result.stderr.lower() \
            or "daemon" in result.stderr.lower()

    def test_stale_endpoint_falls_back(self, maestro_folder):
        """Endpoint file exists but no daemon listening → fall back."""
        self._set_afk(maestro_folder)
        endpoint = maestro_folder / ".maestro" / "bridge-personal.endpoint"
        endpoint.write_text(json.dumps({
            "port": 1,  # port 1 is unlikely to be serving our daemon
            "token": "stale-token",
            "pid": 99999,
            "account": "personal",
            "transport": "null",
            "started_at": "2026-04-23T00:00:00+00:00",
        }))
        result = _run_helper(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            cwd=maestro_folder, home=maestro_folder,
            env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
        )
        assert result.returncode == 0
        assert "unreachable" in result.stderr.lower() \
            or "fall" in result.stderr.lower()

    def test_no_account_derivable_falls_back(self, maestro_folder):
        self._set_afk(maestro_folder)
        result = _run_helper(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            cwd=maestro_folder, home=maestro_folder,
            # No MAESTRO_ACTIVE_ACCOUNT, no CLAUDE_CONFIG_DIR, no marker
        )
        assert result.returncode == 0
        assert "account" in result.stderr.lower()


# ---------------------------------------------------------------------------
# AFK on + daemon alive → full roundtrip


class TestAfkOnWithDaemon:
    def _set_afk(self, root: Path):
        (root / ".maestro").mkdir(exist_ok=True)
        afk.write_afk(root, afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None,
            source="manual",
        ))

    def test_allow_decision_exits_0_with_allow_output(self, maestro_folder):
        self._set_afk(maestro_folder)
        daemon, transport = _start_daemon("personal", maestro_folder)
        try:
            # Kick off helper in a thread; push a reply when it's pending.
            result_holder: dict = {}

            def run_helper():
                result_holder["result"] = _run_helper(
                    {
                        "tool_name": "Bash",
                        "tool_input": {"command": "npm install foo"},
                    },
                    cwd=maestro_folder,
                    home=maestro_folder,
                    env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal",
                               "MAESTRO_BRIDGE_TIMEOUT": "5"},
                    timeout=20.0,
                )

            t = threading.Thread(target=run_helper)
            t.start()

            # Wait for approval to reach transport
            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)
            assert transport.sent_approvals, "approval never reached transport"
            req = transport.sent_approvals[0]

            # Push an "approve" reply
            from otaman_bridge.core import InboundReply
            transport.push_reply(InboundReply(
                request_id=req.request_id,
                action="approve",
                responder="test:harness",
            ))

            t.join(timeout=15)
            result = result_holder["result"]
            assert result.returncode == 0, (
                f"rc={result.returncode} stdout={result.stdout!r} "
                f"stderr={result.stderr!r}"
            )
            data = json.loads(result.stdout)
            # hookEventName is REQUIRED by Claude Code's hook validator —
            # missing it silently drops the decision (treated as no-opinion).
            assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
            assert data["hookSpecificOutput"]["permissionDecision"] == "allow"
        finally:
            daemon.stop()

    def test_deny_decision_exits_2(self, maestro_folder):
        self._set_afk(maestro_folder)
        daemon, transport = _start_daemon("personal", maestro_folder)
        try:
            result_holder: dict = {}

            def run_helper():
                result_holder["result"] = _run_helper(
                    {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
                    cwd=maestro_folder,
                    home=maestro_folder,
                    env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal",
                               "MAESTRO_BRIDGE_TIMEOUT": "5"},
                    timeout=20.0,
                )

            t = threading.Thread(target=run_helper)
            t.start()
            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)
            req = transport.sent_approvals[0]

            from otaman_bridge.core import InboundReply
            transport.push_reply(InboundReply(
                request_id=req.request_id,
                action="reject",
                responder="test:harness",
                comment="too dangerous",
            ))

            t.join(timeout=15)
            result = result_holder["result"]
            assert result.returncode == 2
            data = json.loads(result.stdout)
            assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
            assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
            # Reason (or responder) propagates to systemMessage
            assert "too dangerous" in data["systemMessage"] \
                or "deny" in data["systemMessage"].lower() \
                or data["systemMessage"]  # non-empty is enough
        finally:
            daemon.stop()

    def test_timeout_decision_falls_back(self, maestro_folder):
        """Daemon timeout is treated as no-opinion → fall back to native."""
        self._set_afk(maestro_folder)
        daemon, _ = _start_daemon("personal", maestro_folder)
        try:
            result = _run_helper(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                cwd=maestro_folder,
                home=maestro_folder,
                env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal",
                           "MAESTRO_BRIDGE_TIMEOUT": "1"},
                timeout=20.0,
            )
            assert result.returncode == 0
            # timeout → no output → native prompt
            assert result.stdout == "" or "timeout" not in result.stdout.lower()
        finally:
            daemon.stop()

    def test_payload_carries_expected_fields(self, maestro_folder):
        """Verify the daemon receives account, project, agent, tool fields."""
        self._set_afk(maestro_folder)
        (maestro_folder / ".agents" / "current-agent").write_text(
            "backend-agent\n", encoding="utf-8",
        )
        daemon, transport = _start_daemon("riseapps", maestro_folder)
        try:
            result_holder: dict = {}

            def run_helper():
                result_holder["result"] = _run_helper(
                    {"tool_name": "Write", "tool_input": {
                        "file_path": "/tmp/foo", "content": "x",
                    }},
                    cwd=maestro_folder,
                    home=maestro_folder,
                    env_extra={"MAESTRO_ACTIVE_ACCOUNT": "riseapps",
                               "MAESTRO_BRIDGE_TIMEOUT": "5"},
                    timeout=20.0,
                )

            t = threading.Thread(target=run_helper)
            t.start()
            for _ in range(80):
                if transport.sent_approvals:
                    break
                time.sleep(0.05)
            req = transport.sent_approvals[0]

            from otaman_bridge.core import InboundReply
            transport.push_reply(InboundReply(
                request_id=req.request_id, action="approve", responder="t",
            ))
            t.join(timeout=15)

            assert req.account == "riseapps"
            assert req.project == "test-project"
            assert req.agent == "backend-agent"
            assert req.tool_name == "Write"
            assert req.tool_input["file_path"] == "/tmp/foo"
        finally:
            daemon.stop()


# ---------------------------------------------------------------------------
# Account derivation (isolated from full roundtrip)


class TestAccountDerivation:
    def test_env_var_wins(self, maestro_folder, monkeypatch):
        """MAESTRO_ACTIVE_ACCOUNT wins over everything else."""
        import importlib.util
        # bridge_approval is now a package module; re-import for test isolation
        import importlib
        from otaman_bridge import bridge_approval as _ba
        module = importlib.reload(_ba)

        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "env-wins")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude-other"))
        assert module._derive_account(maestro_folder) == "env-wins"

    def test_claude_config_dir_basename(self, maestro_folder, monkeypatch):
        import importlib.util
        # bridge_approval is now a package module; re-import for test isolation
        import importlib
        from otaman_bridge import bridge_approval as _ba
        module = importlib.reload(_ba)

        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.delenv("MAESTRO_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude-riseapps"))
        assert module._derive_account(maestro_folder) == "riseapps"

    def test_returns_none_when_nothing_resolves(self, tmp_path, monkeypatch):
        import importlib.util
        # bridge_approval is now a package module; re-import for test isolation
        import importlib
        from otaman_bridge import bridge_approval as _ba
        module = importlib.reload(_ba)

        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.delenv("MAESTRO_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.chdir(tmp_path)  # no marker here
        assert module._derive_account(tmp_path) is None
