"""Tests for bridge/install.py — systemd + launchd service installers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from otaman_bridge import install as install_mod
from otaman_bridge.install import (
    InstallTarget,
    WindowsInstallNotSupported,
    install_launchd,
    install_systemd,
    make_install_target,
    render_launchd_plist,
    render_systemd_unit,
    uninstall_launchd,
    uninstall_systemd,
)


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """Redirect Path.home() so unit files land in tmp."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def target(sandbox_home, tmp_path):
    """A resolved InstallTarget pointing at the plugin repo."""
    maestro_cli = tmp_path / "cli" / "maestro.sh"
    maestro_cli.parent.mkdir(parents=True)
    maestro_cli.write_text("#!/bin/bash\n", encoding="utf-8")
    return InstallTarget(
        account="personal",
        python="/home/user/anaconda3/bin/python",
        maestro_cli=maestro_cli,
        working_dir=tmp_path / "maestro-folder",
        system="linux-systemd",
    )


@pytest.fixture
def fake_runner():
    """Collect subprocess.run calls without executing them."""
    calls: list[list] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        return result

    _run.calls = calls
    return _run


# ---------------------------------------------------------------------------
# Platform detection


class TestDetectSystem:
    def test_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert install_mod.detect_system() == "linux-systemd"

    def test_macos(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert install_mod.detect_system() == "macos-launchd"

    def test_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert install_mod.detect_system() == "windows-nssm"

    def test_unknown_raises(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "haiku")
        with pytest.raises(RuntimeError, match="unsupported"):
            install_mod.detect_system()


# ---------------------------------------------------------------------------
# systemd unit rendering + install


class TestSystemdUnit:
    def test_render_contains_all_fields(self, target):
        unit = render_systemd_unit(target)
        assert "Description=Otaman bridge daemon" in unit
        assert "After=network-online.target" in unit
        assert f"WorkingDirectory={target.working_dir}" in unit
        assert f"OTAMAN_PYTHON={target.python}" in unit
        # Endpoint files land in ~/.otaman/ via the OTAMAN_BRIDGE_DIR env
        # var the unit exports; legacy daemons keep using ~/.maestro/.
        assert "OTAMAN_BRIDGE_DIR=%h/.otaman" in unit
        # watch_bus defaults to True, so the ExecStart ends with --watch-bus.
        assert f"ExecStart={target.maestro_cli} bridge run --account %i --watch-bus" in unit
        assert "Restart=on-failure" in unit
        assert "WantedBy=default.target" in unit

    def test_render_omits_watch_bus_when_disabled(self, target):
        target.watch_bus = False
        unit = render_systemd_unit(target)
        assert f"ExecStart={target.maestro_cli} bridge run --account %i" in unit
        assert "--watch-bus" not in unit

    def test_render_includes_idle_flag_when_set(self, target):
        target.idle_auto_afk_minutes = 30
        unit = render_systemd_unit(target)
        assert "--idle-auto-afk-minutes 30" in unit

    def test_render_omits_idle_flag_when_zero(self, target):
        target.idle_auto_afk_minutes = 0
        unit = render_systemd_unit(target)
        assert "--idle-auto-afk-minutes" not in unit

    def test_template_is_account_instance(self, target):
        """Template uses %i so one unit file serves any --account."""
        unit = render_systemd_unit(target)
        # The ExecStart and Description reference %i (systemd instance marker)
        # rather than the literal account name, so a single unit file
        # handles every account (otaman-bridge@personal, @riseapps, etc.).
        assert "%i" in unit


class TestInstallSystemd:
    def test_writes_unit_file_if_missing(self, sandbox_home, target, fake_runner):
        msgs = install_systemd(target, runner=fake_runner)
        unit = sandbox_home / ".config" / "systemd" / "user" / "otaman-bridge@.service"
        assert unit.is_file()
        assert "OTAMAN_PYTHON=" in unit.read_text(encoding="utf-8")
        assert any("Wrote:" in m for m in msgs)

    def test_skips_write_when_content_identical(
        self,
        sandbox_home,
        target,
        fake_runner,
    ):
        install_systemd(target, runner=fake_runner)
        msgs = install_systemd(target, runner=fake_runner)
        # Second run shouldn't report "Wrote:" — content is unchanged
        assert any("unchanged" in m.lower() for m in msgs)
        assert not any("Wrote:" in m for m in msgs)

    def test_runs_daemon_reload(self, sandbox_home, target, fake_runner):
        install_systemd(target, runner=fake_runner)
        cmds = fake_runner.calls
        assert any(cmd == ["systemctl", "--user", "daemon-reload"] for cmd in cmds)

    def test_enable_and_start_together(self, sandbox_home, target, fake_runner):
        """Default path: `systemctl --user enable --now`."""
        install_systemd(target, enable=True, start=True, runner=fake_runner)
        cmds = fake_runner.calls
        service = "otaman-bridge@personal.service"
        assert any(cmd == ["systemctl", "--user", "enable", "--now", service] for cmd in cmds)

    def test_enable_only(self, sandbox_home, target, fake_runner):
        install_systemd(target, enable=True, start=False, runner=fake_runner)
        cmds = fake_runner.calls
        assert any(
            cmd == ["systemctl", "--user", "enable", "otaman-bridge@personal.service"]
            for cmd in cmds
        )
        assert not any("start" in cmd for cmd in cmds if "--user" in cmd)

    def test_start_only(self, sandbox_home, target, fake_runner):
        install_systemd(target, enable=False, start=True, runner=fake_runner)
        cmds = fake_runner.calls
        assert any(
            cmd == ["systemctl", "--user", "start", "otaman-bridge@personal.service"]
            for cmd in cmds
        )
        # Shouldn't have called enable
        assert not any("enable" in cmd for cmd in cmds if "--user" in cmd)

    def test_no_enable_no_start(self, sandbox_home, target, fake_runner):
        """Both disabled → unit file written + daemon-reload, nothing else."""
        install_systemd(target, enable=False, start=False, runner=fake_runner)
        cmds = fake_runner.calls
        # Only daemon-reload should have run
        assert len(cmds) == 1
        assert cmds[0] == ["systemctl", "--user", "daemon-reload"]

    def test_linger_enabled(self, sandbox_home, target, fake_runner, monkeypatch):
        monkeypatch.setenv("USER", "romans")
        install_systemd(target, linger=True, runner=fake_runner)
        cmds = fake_runner.calls
        assert any(cmd == ["loginctl", "enable-linger", "romans"] for cmd in cmds)


class TestUninstallSystemd:
    def test_stops_and_disables_without_failing(self, fake_runner):
        msgs = uninstall_systemd("personal", runner=fake_runner)
        cmds = fake_runner.calls
        assert any(
            cmd == ["systemctl", "--user", "stop", "otaman-bridge@personal.service"] for cmd in cmds
        )
        assert any(
            cmd == ["systemctl", "--user", "disable", "otaman-bridge@personal.service"]
            for cmd in cmds
        )
        assert any("Stopped" in m for m in msgs)

    def test_also_stops_legacy_maestro_bridge_unit(self, fake_runner):
        """Phase B-0a-3: uninstall covers BOTH otaman-bridge@ and the
        legacy maestro-bridge@ unit so a clean migration removes the
        pre-rename systemd state for a given account."""
        uninstall_systemd("personal", runner=fake_runner)
        cmds = fake_runner.calls
        assert any(
            cmd == ["systemctl", "--user", "stop", "maestro-bridge@personal.service"]
            for cmd in cmds
        )
        assert any(
            cmd == ["systemctl", "--user", "disable", "maestro-bridge@personal.service"]
            for cmd in cmds
        )


# ---------------------------------------------------------------------------
# launchd plist rendering + install


class TestLaunchdPlist:
    def test_render_contains_all_fields(self, target):
        target.system = "macos-launchd"
        plist = render_launchd_plist(target)
        assert "<string>com.otaman.bridge.personal</string>" in plist
        assert f"<string>{target.maestro_cli}</string>" in plist
        assert "<string>bridge</string>" in plist
        assert "<string>run</string>" in plist
        assert "<string>--account</string>" in plist
        assert "<string>personal</string>" in plist
        assert f"<string>{target.workdir if False else target.working_dir}</string>" in plist
        assert target.python in plist
        assert "RunAtLoad" in plist and "KeepAlive" in plist
        # watch_bus defaults to True → plist includes the --watch-bus arg.
        assert "<string>--watch-bus</string>" in plist

    def test_render_omits_watch_bus_when_disabled(self, target):
        target.system = "macos-launchd"
        target.watch_bus = False
        plist = render_launchd_plist(target)
        assert "--watch-bus" not in plist

    def test_render_includes_idle_flag_when_set(self, target):
        target.system = "macos-launchd"
        target.idle_auto_afk_minutes = 45
        plist = render_launchd_plist(target)
        assert "<string>--idle-auto-afk-minutes</string>" in plist
        assert "<string>45</string>" in plist

    def test_render_omits_idle_flag_when_zero(self, target):
        target.system = "macos-launchd"
        target.idle_auto_afk_minutes = 0
        plist = render_launchd_plist(target)
        assert "--idle-auto-afk-minutes" not in plist

    def test_plist_valid_xml(self, target):
        target.system = "macos-launchd"
        plist = render_launchd_plist(target)
        # Basic XML shape checks
        assert plist.startswith("<?xml")
        assert '<plist version="1.0">' in plist
        assert plist.rstrip().endswith("</plist>")


class TestInstallLaunchd:
    def test_writes_plist_and_loads(self, sandbox_home, target, fake_runner):
        target.system = "macos-launchd"
        msgs = install_launchd(target, runner=fake_runner)
        plist = sandbox_home / "Library" / "LaunchAgents" / "com.otaman.bridge.personal.plist"
        assert plist.is_file()
        content = plist.read_text(encoding="utf-8")
        assert "com.otaman.bridge.personal" in content

        cmds = fake_runner.calls
        # Should unload (even if not loaded) then load, then start
        assert any(cmd[:2] == ["launchctl", "unload"] for cmd in cmds)
        assert any(cmd[:2] == ["launchctl", "load"] for cmd in cmds)
        assert any(cmd == ["launchctl", "start", "com.otaman.bridge.personal"] for cmd in cmds)
        assert any("Wrote:" in m for m in msgs)
        assert any("Loaded:" in m for m in msgs)

    def test_no_start(self, sandbox_home, target, fake_runner):
        target.system = "macos-launchd"
        install_launchd(target, start=False, runner=fake_runner)
        # launchctl start NOT issued when start=False (but load still happens)
        cmds = fake_runner.calls
        assert any(cmd[:2] == ["launchctl", "load"] for cmd in cmds)
        assert not any(cmd == ["launchctl", "start", "com.otaman.bridge.personal"] for cmd in cmds)


class TestUninstallLaunchd:
    def test_unloads_and_removes_plist(self, sandbox_home, target, fake_runner):
        target.system = "macos-launchd"
        install_launchd(target, runner=fake_runner)
        plist = sandbox_home / "Library" / "LaunchAgents" / "com.otaman.bridge.personal.plist"
        assert plist.is_file()

        msgs = uninstall_launchd("personal", runner=fake_runner)
        assert not plist.exists(), "plist should have been removed"
        assert any("Removed:" in m for m in msgs)


# ---------------------------------------------------------------------------
# Windows stub


class TestWindowsStub:
    def test_install_raises_not_supported(self, target):
        target.system = "windows-nssm"
        with pytest.raises(WindowsInstallNotSupported, match="Windows"):
            install_mod.install_windows(target)

    def test_error_message_helpful(self, target):
        target.system = "windows-nssm"
        try:
            install_mod.install_windows(target)
        except WindowsInstallNotSupported as e:
            msg = str(e)
            assert "NSSM" in msg
            assert "run" in msg.lower()  # hint at running manually
            assert "Task Scheduler" in msg or "scheduler" in msg.lower()


# ---------------------------------------------------------------------------
# Dispatcher


class TestDispatcher:
    def test_install_routes_to_systemd(self, sandbox_home, target, fake_runner):
        msgs = install_mod.install(target, runner=fake_runner)
        # systemd-specific message
        assert any("daemon-reload" in m.lower() for m in msgs)

    def test_install_routes_to_launchd(self, sandbox_home, target, fake_runner):
        target.system = "macos-launchd"
        msgs = install_mod.install(target, runner=fake_runner)
        assert any("Loaded:" in m for m in msgs)

    def test_install_windows_raises(self, target):
        target.system = "windows-nssm"
        with pytest.raises(WindowsInstallNotSupported):
            install_mod.install(target)


# ---------------------------------------------------------------------------
# Target resolution


class TestMakeInstallTarget:
    def test_defaults_to_sys_executable(self, sandbox_home, target, monkeypatch, tmp_path):
        """Python interpreter defaults to sys.executable (freezes what pip
        installed into)."""
        monkeypatch.setattr(sys, "platform", "linux")
        # Pass maestro_cli explicitly — avoids dependency on actual filesystem
        # layout (which differs between local dev and CI editable installs).
        stub_cli = tmp_path / "cli" / "maestro.sh"
        stub_cli.parent.mkdir(parents=True, exist_ok=True)
        stub_cli.write_text("#!/bin/bash\n", encoding="utf-8")
        t = make_install_target("personal", maestro_cli=stub_cli)
        assert t.python == sys.executable

    def test_explicit_system_honored(self, tmp_path):
        stub_cli = tmp_path / "cli" / "maestro.sh"
        stub_cli.parent.mkdir(parents=True, exist_ok=True)
        stub_cli.write_text("#!/bin/bash\n", encoding="utf-8")
        t = make_install_target(
            "x", system="macos-launchd", python="/p", working_dir="/w", maestro_cli=stub_cli
        )
        assert t.system == "macos-launchd"

    def test_to_dict_json_safe(self, target):
        """InstallTarget.to_dict produces JSON-compatible types."""
        import json

        s = json.dumps(target.to_dict())
        assert "personal" in s
        assert "linux-systemd" in s
