"""Tests for scripts/stop_notify.py — Stop-hook question surfacing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
HELPER_INVOKE = [sys.executable, "-m", "otaman_bridge.stop_notify"]


def _bridge_env() -> dict:
    env = os.environ.copy()
    bridge_src = str(REPO_ROOT / "src")
    core_src = str(REPO_ROOT.parent / "otaman-core" / "src")
    env["PYTHONPATH"] = os.pathsep.join([bridge_src, core_src, env.get("PYTHONPATH", "")])
    return env


# afk + stop_notify now importable as package modules
from otaman_bridge import afk, stop_notify

# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def maestro_folder(tmp_path):
    root = tmp_path / "my-maestro"
    root.mkdir()
    (root / "platform.yaml").write_text(
        "project: smoke\nversion: '1.0'\nrepos: []\n",
        encoding="utf-8",
    )
    (root / ".agents").mkdir()
    (root / ".maestro").mkdir()
    return root


def _set_afk_on(root: Path) -> None:
    state = afk.AfkState(
        enabled_at=datetime.now(timezone.utc),
        expires_at=None,
        source="manual",
    )
    afk.write_afk(root, state)


def _write_transcript(
    path: Path,
    assistant_texts: list[str],
    *,
    user_first: bool = True,
) -> None:
    lines: list[str] = []
    if user_first:
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": "some prompt"}]},
                }
            )
        )
    for text in assistant_texts:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit tests — pure functions


class TestLoadLastAssistantText:
    def test_returns_last_assistant(self, tmp_path):
        t = tmp_path / "t.jsonl"
        _write_transcript(t, ["first", "second", "third"])
        assert stop_notify.load_last_assistant_text(t) == "third"

    def test_skips_tool_use_and_thinking(self, tmp_path):
        t = tmp_path / "t.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "q"}]}}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I'll check"},
                            {"type": "tool_use", "name": "Read", "input": {}},
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "…"},
                            {"type": "text", "text": "Done. Which one?"},
                        ]
                    },
                }
            ),
        ]
        t.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert stop_notify.load_last_assistant_text(t) == "Done. Which one?"

    def test_missing_file_returns_none(self, tmp_path):
        assert stop_notify.load_last_assistant_text(tmp_path / "ghost") is None

    def test_empty_file_returns_none(self, tmp_path):
        t = tmp_path / "empty"
        t.write_text("", encoding="utf-8")
        assert stop_notify.load_last_assistant_text(t) is None

    def test_malformed_lines_skipped(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text(
            "not json\n"
            + json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}
            )
            + "\n"
            + "also not json\n",
            encoding="utf-8",
        )
        assert stop_notify.load_last_assistant_text(t) == "hello"


class TestLooksLikeQuestion:
    def test_trailing_question_mark(self):
        assert stop_notify.looks_like_question("Want me to proceed?")

    def test_question_in_middle_of_tail(self):
        assert stop_notify.looks_like_question("Here are options. Which one? " + "x" * 50)

    def test_no_question(self):
        assert not stop_notify.looks_like_question("Done. Here's the result.")

    def test_empty(self):
        assert not stop_notify.looks_like_question("")

    def test_question_beyond_detection_window(self):
        """Question in the first 100 chars of a 10K-char body doesn't count
        — only the tail is checked, matching user-perceived end-of-turn."""
        body = "Are you sure? " + ("x" * 5000)
        assert not stop_notify.looks_like_question(body)


class TestDebounce:
    def test_first_call_allowed(self, tmp_path):
        assert stop_notify.debounce_ok(
            tmp_path / "state.json",
            "sess-1",
            "hash-1",
        )

    def test_duplicate_hash_suppressed(self, tmp_path, monkeypatch):
        state = tmp_path / "state.json"
        # Make MIN_DEBOUNCE_SECONDS=0 so only hash matters.
        monkeypatch.setattr(stop_notify, "MIN_DEBOUNCE_SECONDS", 0)
        assert stop_notify.debounce_ok(state, "sess-1", "hash-1")
        # Same hash immediately after → suppressed
        assert not stop_notify.debounce_ok(state, "sess-1", "hash-1")

    def test_rate_limit_suppresses_even_new_hash(self, tmp_path, monkeypatch):
        state = tmp_path / "state.json"
        monkeypatch.setattr(stop_notify, "MIN_DEBOUNCE_SECONDS", 60)
        assert stop_notify.debounce_ok(state, "sess-1", "hash-1")
        # Different hash but within 60s → still suppressed
        assert not stop_notify.debounce_ok(state, "sess-1", "hash-2")

    def test_different_sessions_independent(self, tmp_path):
        state = tmp_path / "state.json"
        assert stop_notify.debounce_ok(state, "sess-1", "hash-1")
        assert stop_notify.debounce_ok(state, "sess-2", "hash-1")

    def test_prunes_entries_older_than_24h(self, tmp_path, monkeypatch):
        state = tmp_path / "state.json"
        # Seed with an old-looking entry.
        state.write_text(
            json.dumps(
                {
                    "old-session": {"ts": 1, "hash": "old"},  # ts from 1970
                    "fresh": {"ts": int(time.time()), "hash": "new"},
                }
            ),
            encoding="utf-8",
        )
        stop_notify.debounce_ok(state, "another", "hash")
        reloaded = json.loads(state.read_text(encoding="utf-8"))
        assert "old-session" not in reloaded
        assert "another" in reloaded


# ---------------------------------------------------------------------------
# Integration — full helper via subprocess


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


def _run_helper(stdin_payload: dict, *, cwd: Path, home: Path, env_extra=None):
    env = _env_with_home(home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        HELPER_INVOKE,
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
        timeout=15,
        cwd=cwd,
        env=env,
    )


class TestIntegrationAfkOff:
    def test_silent_exit_when_afk_off(self, maestro_folder):
        transcript = maestro_folder / "t.jsonl"
        _write_transcript(transcript, ["Which way? Decide."])
        result = _run_helper(
            {"session_id": "s", "transcript_path": str(transcript)},
            cwd=maestro_folder,
            home=maestro_folder,
        )
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""


class TestIntegrationAfkOnNoDaemon:
    def test_question_afk_on_no_daemon_warns(self, maestro_folder):
        _set_afk_on(maestro_folder)
        transcript = maestro_folder / "t.jsonl"
        _write_transcript(transcript, ["Which one should I pick?"])
        result = _run_helper(
            {"session_id": "s", "transcript_path": str(transcript)},
            cwd=maestro_folder,
            home=maestro_folder,
            env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
        )
        assert result.returncode == 0
        # Goes to stderr — "daemon endpoint missing"
        assert "endpoint missing" in result.stderr.lower() or "daemon" in result.stderr.lower()

    def test_non_question_skips_even_with_afk(self, maestro_folder):
        _set_afk_on(maestro_folder)
        transcript = maestro_folder / "t.jsonl"
        _write_transcript(transcript, ["Done. Results are ready."])
        result = _run_helper(
            {"session_id": "s", "transcript_path": str(transcript)},
            cwd=maestro_folder,
            home=maestro_folder,
            env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
        )
        assert result.returncode == 0
        # Neither the "endpoint missing" warning nor the debounce state
        # write should happen — we exited before reaching daemon code.
        state_file = maestro_folder / ".maestro" / "stop-notify.state"
        assert not state_file.exists()


# ---------------------------------------------------------------------------
# Integration — full loop with live NullTransport daemon


def _start_daemon(account: str, home: Path):
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


class TestIntegrationWithDaemon:
    def test_question_fires_notify(self, maestro_folder):
        _set_afk_on(maestro_folder)
        daemon, transport = _start_daemon("personal", maestro_folder)
        try:
            transcript = maestro_folder / "t.jsonl"
            question = "Should I tackle the auth bug or the specs first?"
            _write_transcript(transcript, [question])
            result = _run_helper(
                {"session_id": "test-sess", "transcript_path": str(transcript)},
                cwd=maestro_folder,
                home=maestro_folder,
                env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
            )
            assert result.returncode == 0

            # Give NullTransport a beat
            for _ in range(40):
                if transport.sent_infos:
                    break
                time.sleep(0.05)
            assert transport.sent_infos, "notify should have reached the transport"
            info = transport.sent_infos[0]
            assert info.severity == "approval"
            assert question in info.body
            assert "Claude is waiting" in info.title
        finally:
            daemon.stop()

    def test_debounce_skips_rapid_repeat(self, maestro_folder):
        _set_afk_on(maestro_folder)
        daemon, transport = _start_daemon("personal", maestro_folder)
        try:
            transcript = maestro_folder / "t.jsonl"
            _write_transcript(transcript, ["Continue?"])
            payload = {"session_id": "rep", "transcript_path": str(transcript)}

            _run_helper(
                payload,
                cwd=maestro_folder,
                home=maestro_folder,
                env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
            )
            for _ in range(40):
                if transport.sent_infos:
                    break
                time.sleep(0.05)
            assert len(transport.sent_infos) == 1

            # Second call immediately after — same content hash → suppressed
            _run_helper(
                payload,
                cwd=maestro_folder,
                home=maestro_folder,
                env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
            )
            time.sleep(0.3)
            assert len(transport.sent_infos) == 1, "debounce should have suppressed the duplicate"
        finally:
            daemon.stop()

    def test_session_id_isolates_debounce(self, maestro_folder):
        """Separate sessions debounce independently — two Claude windows
        can each emit a question without blocking each other."""
        _set_afk_on(maestro_folder)
        daemon, transport = _start_daemon("personal", maestro_folder)
        try:
            transcript = maestro_folder / "t.jsonl"
            _write_transcript(transcript, ["Ready?"])
            for sess in ("sess-A", "sess-B"):
                _run_helper(
                    {"session_id": sess, "transcript_path": str(transcript)},
                    cwd=maestro_folder,
                    home=maestro_folder,
                    env_extra={"MAESTRO_ACTIVE_ACCOUNT": "personal"},
                )
            for _ in range(40):
                if len(transport.sent_infos) >= 2:
                    break
                time.sleep(0.05)
            assert len(transport.sent_infos) == 2
        finally:
            daemon.stop()
