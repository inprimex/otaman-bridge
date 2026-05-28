"""Tests for scripts/afk.py — AFK flag, duration parser, CLI."""

from __future__ import annotations

import importlib.util
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _load():
    """Reload afk per-test for fresh globals (the dataclass module-cache trick still applies)."""
    import importlib
    from otaman_bridge import afk as _afk
    return importlib.reload(_afk)


afk = _load()


@pytest.fixture(autouse=True)
def _suppress_notifications(monkeypatch):
    """Existing AFK tests don't want a real daemon round-trip on cmd_on/off.
    The notify path is exercised by TestNotify below; this autouse keeps
    the older tests purely about state."""
    monkeypatch.setenv("MAESTRO_AFK_NO_NOTIFY", "1")


@pytest.fixture
def maestro_folder(tmp_path):
    root = tmp_path / "my-maestro"
    root.mkdir()
    (root / "platform.yaml").write_text("project: test\n", encoding="utf-8")
    (root / ".agents").mkdir()
    return root


# ---------------------------------------------------------------------------
# Duration parser


class TestParseDuration:
    def test_seconds(self):
        assert afk.parse_duration("30s") == timedelta(seconds=30)

    def test_minutes(self):
        assert afk.parse_duration("15m") == timedelta(minutes=15)

    def test_hours(self):
        assert afk.parse_duration("8h") == timedelta(hours=8)

    def test_days(self):
        assert afk.parse_duration("2d") == timedelta(days=2)

    def test_weeks(self):
        assert afk.parse_duration("1w") == timedelta(weeks=1)

    def test_compound_hours_minutes(self):
        assert afk.parse_duration("1h30m") == timedelta(hours=1, minutes=30)

    def test_compound_days_hours(self):
        assert afk.parse_duration("2d4h") == timedelta(days=2, hours=4)

    def test_compound_three_units(self):
        assert afk.parse_duration("1w3d12h") == timedelta(weeks=1, days=3, hours=12)

    def test_uppercase_normalized(self):
        assert afk.parse_duration("30S") == timedelta(seconds=30)

    def test_whitespace_tolerated(self):
        assert afk.parse_duration("  15m  ") == timedelta(minutes=15)

    def test_bare_number_rejected(self):
        with pytest.raises(ValueError):
            afk.parse_duration("30")

    def test_unknown_unit_rejected(self):
        """`y` (year) isn't in the grammar — reject loudly."""
        with pytest.raises(ValueError):
            afk.parse_duration("1y")
        with pytest.raises(ValueError):
            afk.parse_duration("3mo")

    def test_uppercase_M_treated_as_minutes(self):
        """Intentional: we lowercase the whole string, so `M` (month in some
        systems) collapses onto `m` (minutes). Document the behavior rather
        than rejecting, so `1H30M` works the same as `1h30m`."""
        assert afk.parse_duration("1H30M") == timedelta(hours=1, minutes=30)

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            afk.parse_duration("-1h")

    def test_zero_rejected(self):
        with pytest.raises(ValueError):
            afk.parse_duration("0s")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            afk.parse_duration("")
        with pytest.raises(ValueError):
            afk.parse_duration("   ")

    def test_none_rejected(self):
        with pytest.raises(ValueError):
            afk.parse_duration(None)  # type: ignore[arg-type]

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            afk.parse_duration("eight hours")
        with pytest.raises(ValueError):
            afk.parse_duration("h8")


class TestFormatRemaining:
    def test_compact_multi_unit(self):
        td = timedelta(days=1, hours=2, minutes=3)
        assert afk.format_remaining(td) == "1d 2h 3m"

    def test_hours_minutes(self):
        assert afk.format_remaining(timedelta(minutes=90)) == "1h 30m"

    def test_seconds_shown(self):
        assert afk.format_remaining(timedelta(seconds=45)) == "45s"

    def test_zero_or_negative(self):
        assert afk.format_remaining(timedelta(seconds=0)) == "0s"
        assert afk.format_remaining(timedelta(seconds=-5)) == "0s"


# ---------------------------------------------------------------------------
# AfkState serialization


class TestAfkState:
    def test_roundtrip_indefinite(self):
        state = afk.AfkState(
            enabled_at=datetime(2026, 4, 23, 19, 30, tzinfo=timezone.utc),
            expires_at=None,
            source="manual",
            enabled_by="roman",
        )
        d = state.to_dict()
        # No expires_at key when indefinite
        assert "expires_at" not in d
        restored = afk.AfkState.from_dict(d)
        assert restored == state

    def test_roundtrip_with_expiry(self):
        state = afk.AfkState(
            enabled_at=datetime(2026, 4, 23, 19, 30, tzinfo=timezone.utc),
            expires_at=datetime(2026, 4, 23, 20, 30, tzinfo=timezone.utc),
            source="ssh-auto",
            enabled_by="",
        )
        restored = afk.AfkState.from_dict(state.to_dict())
        assert restored == state

    def test_invalid_source_rejected(self):
        with pytest.raises(ValueError):
            afk.AfkState.from_dict({
                "enabled_at": "2026-04-23T19:30:00Z",
                "source": "invalid",
            })

    def test_unattended_source_accepted(self):
        """Hooks/ssh-auto-afk.sh writes ``source: unattended``; the state
        loader must parse it (was a silent-fail bug — files appeared off)."""
        state = afk.AfkState.from_dict({
            "enabled_at": "2026-04-23T19:30:00Z",
            "source": "unattended",
        })
        assert state.source == "unattended"

    def test_idle_auto_source_accepted(self):
        state = afk.AfkState.from_dict({
            "enabled_at": "2026-04-23T19:30:00Z",
            "source": "idle-auto",
        })
        assert state.source == "idle-auto"

    def test_missing_enabled_at_rejected(self):
        with pytest.raises(ValueError):
            afk.AfkState.from_dict({"source": "manual"})

    def test_z_suffix_parsed(self):
        """ISO 8601 with Z (UTC shorthand) must parse cleanly."""
        state = afk.AfkState.from_dict({
            "enabled_at": "2026-04-23T19:30:00Z",
            "source": "manual",
        })
        assert state.enabled_at.tzinfo is not None

    def test_expiry_in_past_marks_expired(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        state = afk.AfkState(
            enabled_at=past - timedelta(hours=2),
            expires_at=past,
            source="manual",
        )
        assert state.is_expired()

    def test_expiry_in_future_not_expired(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        state = afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=future,
            source="manual",
        )
        assert not state.is_expired()

    def test_indefinite_never_expired(self):
        state = afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None,
            source="manual",
        )
        assert not state.is_expired()


# ---------------------------------------------------------------------------
# File I/O + lazy expiry


class TestReadWrite:
    def test_write_then_read_roundtrip(self, maestro_folder):
        state = afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None,
            source="manual",
            enabled_by="test",
        )
        afk.write_afk(maestro_folder, state)
        restored = afk.read_afk(maestro_folder)
        assert restored is not None
        assert restored.source == state.source
        assert restored.enabled_by == state.enabled_by

    def test_read_absent_returns_none(self, maestro_folder):
        assert afk.read_afk(maestro_folder) is None

    def test_read_corrupt_returns_none(self, maestro_folder):
        (maestro_folder / ".maestro").mkdir()
        (maestro_folder / ".maestro" / "afk").write_text(
            "not: valid: yaml: [", encoding="utf-8",
        )
        assert afk.read_afk(maestro_folder) is None

    def test_read_missing_enabled_at_returns_none(self, maestro_folder):
        (maestro_folder / ".maestro").mkdir()
        (maestro_folder / ".maestro" / "afk").write_text(
            "source: manual\n", encoding="utf-8",
        )
        assert afk.read_afk(maestro_folder) is None

    def test_expired_is_deleted_lazily(self, maestro_folder):
        """Reading an expired AFK file deletes it and returns None."""
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        state = afk.AfkState(
            enabled_at=past - timedelta(hours=2),
            expires_at=past,
            source="manual",
        )
        afk.write_afk(maestro_folder, state)
        assert afk.afk_path(maestro_folder).is_file()
        assert afk.read_afk(maestro_folder) is None
        assert not afk.afk_path(maestro_folder).is_file()

    def test_clear_returns_true_when_existed(self, maestro_folder):
        state = afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None,
            source="manual",
        )
        afk.write_afk(maestro_folder, state)
        assert afk.clear_afk(maestro_folder) is True
        assert not afk.afk_path(maestro_folder).exists()

    def test_clear_returns_false_when_absent(self, maestro_folder):
        assert afk.clear_afk(maestro_folder) is False


# ---------------------------------------------------------------------------
# CLI


class TestCli:
    def _invoke(self, maestro_folder, *args, monkeypatch=None):
        """Run the CLI via afk.main(), capturing stdout/stderr."""
        if monkeypatch is not None:
            monkeypatch.chdir(maestro_folder)
        return afk.main(list(args))

    def test_on_without_duration_is_indefinite(self, maestro_folder, monkeypatch, capsys):
        monkeypatch.chdir(maestro_folder)
        assert self._invoke(maestro_folder, "on") == 0
        state = afk.read_afk(maestro_folder)
        assert state is not None
        assert state.expires_at is None
        assert state.source == "manual"
        out = capsys.readouterr().out
        assert "no expiry" in out

    def test_on_with_duration_sets_expires_at(self, maestro_folder, monkeypatch):
        monkeypatch.chdir(maestro_folder)
        assert self._invoke(maestro_folder, "on", "1h") == 0
        state = afk.read_afk(maestro_folder)
        assert state is not None
        assert state.expires_at is not None
        delta = state.expires_at - state.enabled_at
        # Allow a bit of slack for test execution time
        assert timedelta(minutes=59) < delta < timedelta(hours=1, minutes=1)

    def test_on_invalid_duration_exits_nonzero(self, maestro_folder, monkeypatch, capsys):
        monkeypatch.chdir(maestro_folder)
        assert self._invoke(maestro_folder, "on", "bogus") == 2
        err = capsys.readouterr().err
        assert "invalid duration" in err.lower()

    def test_off_when_on_clears(self, maestro_folder, monkeypatch):
        monkeypatch.chdir(maestro_folder)
        self._invoke(maestro_folder, "on", "1h")
        assert self._invoke(maestro_folder, "off") == 0
        assert afk.read_afk(maestro_folder) is None

    def test_off_when_off_is_noop(self, maestro_folder, monkeypatch, capsys):
        monkeypatch.chdir(maestro_folder)
        assert self._invoke(maestro_folder, "off") == 0
        out = capsys.readouterr().out
        assert "off" in out.lower()

    def test_status_when_off(self, maestro_folder, monkeypatch, capsys):
        monkeypatch.chdir(maestro_folder)
        assert self._invoke(maestro_folder, "status") == 0
        assert "off" in capsys.readouterr().out.lower()

    def test_status_when_on_indefinite(self, maestro_folder, monkeypatch, capsys):
        monkeypatch.chdir(maestro_folder)
        self._invoke(maestro_folder, "on")
        assert self._invoke(maestro_folder, "status") == 0
        out = capsys.readouterr().out
        assert "on" in out.lower()
        assert "no expiry" in out.lower()

    def test_status_when_on_with_duration_shows_remaining(
        self, maestro_folder, monkeypatch, capsys,
    ):
        monkeypatch.chdir(maestro_folder)
        self._invoke(maestro_folder, "on", "1h30m")
        assert self._invoke(maestro_folder, "status") == 0
        out = capsys.readouterr().out
        assert "remaining" in out.lower()

    def test_source_flag_persists(self, maestro_folder, monkeypatch):
        monkeypatch.chdir(maestro_folder)
        self._invoke(maestro_folder, "on", "--source", "ssh-auto")
        state = afk.read_afk(maestro_folder)
        assert state is not None
        assert state.source == "ssh-auto"

    def test_invalid_source_rejected(self, maestro_folder, monkeypatch):
        monkeypatch.chdir(maestro_folder)
        with pytest.raises(SystemExit):
            self._invoke(maestro_folder, "on", "--source", "garbage")

    def test_no_maestro_root_errors(self, tmp_path, monkeypatch):
        """CLI needs a discoverable maestro root."""
        orphan = tmp_path / "orphan"
        orphan.mkdir()
        monkeypatch.chdir(orphan)
        monkeypatch.delenv("MAESTRO_ROOT", raising=False)
        with pytest.raises(SystemExit):
            afk.main(["on"])


class TestAfkPath:
    def test_path_format(self, tmp_path):
        assert afk.afk_path(tmp_path).name == "afk"
        assert afk.afk_path(tmp_path).parent.name == ".otaman"


# ---------------------------------------------------------------------------
# Telegram notification path (via daemon /notify)
#
# Stubs the HTTP layer so we can verify the right payload is built without
# spinning up a real daemon. The function is fail-safe: any missing piece
# (no account, no endpoint file, urlopen error) returns False quietly.


class _Captured:
    """Captures a single ``urllib.request.urlopen(req, ...)`` call."""

    def __init__(self):
        self.url = None
        self.headers = {}
        self.body = None
        self.raise_exc = None

    def __call__(self, req, timeout=None):  # noqa: ARG002
        self.url = req.full_url
        self.headers = dict(req.header_items())
        self.body = req.data.decode("utf-8") if req.data else None
        if self.raise_exc is not None:
            raise self.raise_exc
        class _Resp:
            def __enter__(self_): return self_  # noqa: N805
            def __exit__(self_, *a): return False  # noqa: N805
            def read(self_): return b"{}"  # noqa: N805
        return _Resp()


def _setup_endpoint(home: Path, account: str, *, port: int = 12345,
                    token: str = "tok-abc") -> Path:
    """Create a fake ~/.maestro/bridge-<account>.endpoint."""
    import json as _json
    base = home / ".maestro"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"bridge-{account}.endpoint"
    path.write_text(
        _json.dumps({"port": port, "token": token, "pid": 1, "account": account,
                     "transport": "null"}),
        encoding="utf-8",
    )
    return path


class TestNotify:
    @pytest.fixture
    def home_dir(self, tmp_path, monkeypatch):
        """Redirect Path.home() so endpoint files land in tmp_path."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        return home

    @pytest.fixture
    def captured(self, monkeypatch):
        cap = _Captured()
        monkeypatch.setattr(afk.urllib.request, "urlopen", cap)
        return cap

    def test_notify_skipped_when_kill_switch(
        self, maestro_folder, home_dir, monkeypatch, captured,
    ):
        """MAESTRO_AFK_NO_NOTIFY=1 must short-circuit before any I/O."""
        monkeypatch.setenv("MAESTRO_AFK_NO_NOTIFY", "1")
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "personal")
        _setup_endpoint(home_dir, "personal")
        state = afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None, source="manual",
        )
        assert afk.notify_afk_enabled(maestro_folder, state) is False
        assert captured.url is None

    def test_notify_skipped_without_account(
        self, maestro_folder, home_dir, monkeypatch, captured,
    ):
        """No env hints → no .maestro marker → silent skip."""
        monkeypatch.delenv("MAESTRO_AFK_NO_NOTIFY", raising=False)
        monkeypatch.delenv("MAESTRO_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.chdir(maestro_folder)
        state = afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None, source="manual",
        )
        assert afk.notify_afk_enabled(maestro_folder, state) is False
        assert captured.url is None

    def test_notify_skipped_without_endpoint_file(
        self, maestro_folder, home_dir, monkeypatch, captured,
    ):
        """Account resolves but no daemon → silent skip (no daemon running)."""
        monkeypatch.delenv("MAESTRO_AFK_NO_NOTIFY", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "personal")
        # No endpoint file written.
        state = afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None, source="manual",
        )
        assert afk.notify_afk_enabled(maestro_folder, state) is False
        assert captured.url is None

    def test_notify_posts_to_daemon_when_running(
        self, maestro_folder, home_dir, monkeypatch, captured,
    ):
        monkeypatch.delenv("MAESTRO_AFK_NO_NOTIFY", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "personal")
        _setup_endpoint(home_dir, "personal", port=54321, token="tok-xyz")

        state = afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            source="manual",
        )
        assert afk.notify_afk_enabled(maestro_folder, state, reason="going to bed") is True

        assert captured.url == "http://127.0.0.1:54321/notify"
        # Headers are returned with capitalized names by urllib's header_items().
        auth = {k.lower(): v for k, v in captured.headers.items()}
        assert auth["authorization"] == "Bearer tok-xyz"
        import json as _json
        body = _json.loads(captured.body)
        assert body["account"] == "personal"
        assert body["title"] == "🌙 AFK enabled"
        assert "Source: manual" in body["body"]
        assert "Expires in" in body["body"]
        assert "going to bed" in body["body"]

    def test_notify_swallows_urlopen_errors(
        self, maestro_folder, home_dir, monkeypatch, captured,
    ):
        """Daemon endpoint exists but is unreachable → False, no exception."""
        import urllib.error as _err
        monkeypatch.delenv("MAESTRO_AFK_NO_NOTIFY", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "personal")
        _setup_endpoint(home_dir, "personal")
        captured.raise_exc = _err.URLError("connection refused")
        state = afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None, source="manual",
        )
        # Must not raise.
        assert afk.notify_afk_enabled(maestro_folder, state) is False

    def test_cleared_notification_includes_prior_source(
        self, maestro_folder, home_dir, monkeypatch, captured,
    ):
        monkeypatch.delenv("MAESTRO_AFK_NO_NOTIFY", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "personal")
        _setup_endpoint(home_dir, "personal")
        assert afk.notify_afk_cleared(
            maestro_folder, prior_source="idle-auto",
            reason="new Claude session",
        ) is True
        import json as _json
        body = _json.loads(captured.body)
        assert body["title"] == "☀️ AFK cleared"
        assert "idle-auto" in body["body"]
        assert "new Claude session" in body["body"]

    def test_account_resolution_priority(
        self, maestro_folder, home_dir, monkeypatch,
    ):
        """MAESTRO_ACTIVE_ACCOUNT wins over CLAUDE_CONFIG_DIR basename."""
        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "primary")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home_dir / ".claude-secondary"))
        assert afk._resolve_account_for_notify() == "primary"

        monkeypatch.delenv("MAESTRO_ACTIVE_ACCOUNT")
        assert afk._resolve_account_for_notify() == "secondary"

    def test_send_event_subcommand_enabled(
        self, maestro_folder, home_dir, monkeypatch, capsys, captured,
    ):
        """The hidden ``_send-event enabled`` subcommand reads the live AFK
        state and notifies — used by ssh-auto-afk.sh after it writes the file
        with its custom ``signal:`` line."""
        monkeypatch.delenv("MAESTRO_AFK_NO_NOTIFY", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "personal")
        _setup_endpoint(home_dir, "personal")
        monkeypatch.chdir(maestro_folder)

        # Pre-write the AFK file (mimics ssh-auto-afk.sh's cat <<EOF).
        afk.write_afk(maestro_folder, afk.AfkState(
            enabled_at=datetime.now(timezone.utc),
            expires_at=None, source="unattended",
            enabled_by="root",
        ))

        rc = afk.main([
            "_send-event", "enabled",
            "--reason", "launcher flagged this connection as unattended",
        ])
        assert rc == 0
        import json as _json
        body = _json.loads(captured.body)
        assert body["title"] == "🌙 AFK enabled"
        assert "Source: unattended" in body["body"]
        assert "launcher flagged" in body["body"]

    def test_send_event_subcommand_cleared(
        self, maestro_folder, home_dir, monkeypatch, captured,
    ):
        monkeypatch.delenv("MAESTRO_AFK_NO_NOTIFY", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "personal")
        _setup_endpoint(home_dir, "personal")
        monkeypatch.chdir(maestro_folder)

        rc = afk.main([
            "_send-event", "cleared",
            "--source", "manual",
            "--reason", "new Claude session started",
        ])
        assert rc == 0
        import json as _json
        body = _json.loads(captured.body)
        assert body["title"] == "☀️ AFK cleared"
        assert "manual" in body["body"]
        assert "new Claude session" in body["body"]

    def test_cmd_on_triggers_notify(
        self, maestro_folder, home_dir, monkeypatch, captured,
    ):
        """End-to-end: ``maestro afk on`` writes file AND notifies."""
        monkeypatch.delenv("MAESTRO_AFK_NO_NOTIFY", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "personal")
        _setup_endpoint(home_dir, "personal")
        monkeypatch.chdir(maestro_folder)

        assert afk.main(["on", "1h", "--reason", "lunch break"]) == 0
        # File written
        assert afk.read_afk(maestro_folder) is not None
        # Notification sent
        assert captured.url and captured.url.endswith("/notify")
        import json as _json
        body = _json.loads(captured.body)
        assert "lunch break" in body["body"]

    def test_cmd_off_triggers_notify_with_prior_source(
        self, maestro_folder, home_dir, monkeypatch, captured,
    ):
        monkeypatch.delenv("MAESTRO_AFK_NO_NOTIFY", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ROUTING", raising=False)
        monkeypatch.delenv("OTAMAN_ACTIVE_ACCOUNT", raising=False)
        monkeypatch.setenv("MAESTRO_ACTIVE_ACCOUNT", "personal")
        _setup_endpoint(home_dir, "personal")
        monkeypatch.chdir(maestro_folder)

        # Set first (this also fires the enabled-notification, capturing
        # overwrites with each call). Then off — captured will hold the off.
        afk.main(["on"])
        captured.body = None
        captured.url = None
        assert afk.main(["off"]) == 0
        assert captured.url and captured.url.endswith("/notify")
        import json as _json
        body = _json.loads(captured.body)
        assert body["title"] == "☀️ AFK cleared"
        assert "manual" in body["body"]
