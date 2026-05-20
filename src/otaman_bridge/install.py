"""Service-install helpers for the bridge daemon.

Writes a system-appropriate init file (systemd --user on Linux, launchd
on macOS; Windows stubbed) so the daemon survives logout and auto-
restarts on crash. Called from `maestro bridge install`.

Design §5.7:
    Linux/WSL:  systemd --user — ~/.config/systemd/user/maestro-bridge@.service
    macOS:      launchd agent — ~/Library/LaunchAgents/com.otaman.bridge.<account>.plist
    Windows:    NSSM / Scheduled Task (stubbed for v1)

The generated unit locks the Python interpreter used at install time
(``MAESTRO_PYTHON`` env var) so the service keeps working even if the
user's shell PATH / conda env / nvm state drifts later.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Platform detection + path resolution


def detect_system() -> str:
    """Return ``"linux-systemd"`` | ``"macos-launchd"`` | ``"windows-nssm"``."""
    p = sys.platform
    if p.startswith("linux"):
        return "linux-systemd"
    if p == "darwin":
        return "macos-launchd"
    if p == "win32":
        return "windows-nssm"
    raise RuntimeError(f"unsupported platform: {p}")


def resolve_maestro_cli() -> Path:
    """Locate the maestro/otaman CLI wrapper (``cli/maestro.sh``).

    Resolution chain (post-Step-1 carve):
    1. Sibling otaman-cli checkout: ../otaman-cli/cli/maestro.sh
    2. Sibling legacy maestro-plugin checkout: ../maestro-plugin/cli/maestro.sh
    3. \maestro\ on PATH (fallback)
    """
    here = Path(__file__).resolve()
    # src/otaman_bridge/install.py → otaman-bridge/ → otaman/
    project_root = here.parent.parent.parent
    for sibling, rel in (
        ("otaman-cli", "cli/maestro.sh"),
        ("maestro-plugin", "cli/maestro.sh"),
    ):
        candidate = project_root.parent / sibling / rel
        if candidate.is_file():
            return candidate
        # Also try project_root itself in case bridge sits inside the project parent
        candidate2 = project_root / sibling / rel
        if candidate2.is_file():
            return candidate2
    found = shutil.which("maestro") or shutil.which("otaman")
    if found:
        return Path(found).resolve()
    raise RuntimeError(
        "could not locate maestro/otaman CLI wrapper (expected "
        "otaman-cli/cli/maestro.sh, maestro-plugin/cli/maestro.sh, or `maestro`/`otaman` on PATH)"
    )


def resolve_working_dir(override: Path | str | None = None) -> Path:
    """Working directory for the service — where launch-settings.yaml lives.

    Falls back through:
      1. Explicit ``override`` (from --working-dir).
      2. ``find_maestro_root()`` from the current process's cwd.
      3. Current cwd.
    """
    if override:
        return Path(override).expanduser().resolve()
    try:
        # Lazy import so this module stays usable outside a maestro workspace.
        from otaman_core._resolve import find_maestro_root
        root = find_maestro_root()
        if root is not None:
            return root
    except Exception:  # noqa: BLE001
        pass
    return Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Install target


@dataclass
class InstallTarget:
    """All the inputs an install command needs, resolved at install time.

    Frozen at install time so the service is stable against PATH / venv /
    conda / nvm drift on the user's side.
    """

    account: str
    python: str           # absolute path to the Python interpreter
    maestro_cli: Path     # absolute path to cli/maestro.sh (or `maestro`)
    working_dir: Path     # cwd for the service; should contain launch-settings.yaml
    system: str           # linux-systemd | macos-launchd | windows-nssm
    watch_bus: bool = True  # pass --watch-bus so installed services drain the bus
    idle_auto_afk_minutes: int = 0  # 0 = disabled; positive = auto-enable AFK after N min idle

    def to_dict(self) -> dict:
        d = asdict(self)
        d["maestro_cli"] = str(self.maestro_cli)
        d["working_dir"] = str(self.working_dir)
        return d


def make_install_target(
    account: str,
    *,
    python: str | None = None,
    maestro_cli: Path | None = None,
    working_dir: Path | str | None = None,
    system: str | None = None,
    watch_bus: bool = True,
    idle_auto_afk_minutes: int = 0,
) -> InstallTarget:
    """Build an InstallTarget with sensible defaults resolved at call time."""
    return InstallTarget(
        account=account,
        python=python or sys.executable,
        maestro_cli=maestro_cli or resolve_maestro_cli(),
        working_dir=resolve_working_dir(working_dir),
        system=system or detect_system(),
        watch_bus=watch_bus,
        idle_auto_afk_minutes=idle_auto_afk_minutes,
    )


# ---------------------------------------------------------------------------
# systemd --user (Linux)


# Otaman-native unit name. Legacy "maestro-bridge@.service" units that
# were installed before the rename keep working — their unit files stay
# on disk, systemd keeps managing them — and per-project migration
# (Phase C+) is what removes them. New installs use the otaman name.
SYSTEMD_UNIT_NAME = "otaman-bridge@.service"
LEGACY_SYSTEMD_UNIT_NAME = "maestro-bridge@.service"

SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=Otaman bridge daemon (account %i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
# Lock the Python interpreter used at install time — survives PATH/conda/nvm
# drift. Override with `systemctl --user edit` if needed.
Environment="OTAMAN_PYTHON={python}"
# Point endpoint files at ~/.otaman/ for otaman-native deployments;
# legacy daemons (still using the maestro-bridge@.service unit) keep
# writing to ~/.maestro/ via the default in endpoint_path().
Environment="OTAMAN_BRIDGE_DIR=%h/.otaman"
ExecStart={maestro_cli} bridge run --account %i{watch_bus_flag}{idle_flag}
# Auto-restart on crash, but not too aggressively (exit 0 = intentional stop)
Restart=on-failure
RestartSec=5
# Send logs to the journal (journalctl --user -u otaman-bridge@<account>)
StandardOutput=journal
StandardError=journal
# Don't let a single stuck request take down the daemon forever
TimeoutStopSec=15

[Install]
WantedBy=default.target
"""


def systemd_unit_path() -> Path:
    """User-level systemd unit directory (otaman-bridge@.service)."""
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def legacy_systemd_unit_path() -> Path:
    """Path of the pre-rename maestro-bridge@.service unit file.

    Used by Phase D cleanup tooling to confirm legacy units before
    archiving + removing.
    """
    return Path.home() / ".config" / "systemd" / "user" / LEGACY_SYSTEMD_UNIT_NAME


def render_systemd_unit(target: InstallTarget) -> str:
    idle_flag = (
        f" --idle-auto-afk-minutes {target.idle_auto_afk_minutes}"
        if target.idle_auto_afk_minutes > 0 else ""
    )
    return SYSTEMD_UNIT_TEMPLATE.format(
        workdir=target.working_dir,
        python=target.python,
        maestro_cli=target.maestro_cli,
        watch_bus_flag=" --watch-bus" if target.watch_bus else "",
        idle_flag=idle_flag,
    )


def install_systemd(
    target: InstallTarget,
    *,
    enable: bool = True,
    start: bool = True,
    linger: bool = False,
    runner=subprocess.run,
) -> list[str]:
    """Write the unit file (if missing or content changed) + apply state.

    Args:
        enable: ``systemctl --user enable`` so the service starts at boot.
        start: ``systemctl --user start`` (or ``enable --now``) right now.
        linger: ``loginctl enable-linger`` so the user's services keep
            running after logout (important for SSH-only boxes where
            session end would otherwise kill the daemon).
        runner: seam for test injection; defaults to ``subprocess.run``.
    """
    results: list[str] = []
    unit_path = systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    new_content = render_systemd_unit(target)
    old_content = unit_path.read_text(encoding="utf-8") if unit_path.exists() else ""
    if new_content != old_content:
        unit_path.write_text(new_content, encoding="utf-8")
        results.append(f"{'Updated' if old_content else 'Wrote'}: {unit_path}")
    else:
        results.append(f"Unit file unchanged: {unit_path}")

    # Reload systemd so it picks up the (possibly new) unit.
    runner(["systemctl", "--user", "daemon-reload"], check=True)
    results.append("Ran: systemctl --user daemon-reload")

    service = f"otaman-bridge@{target.account}.service"
    if enable and start:
        runner(["systemctl", "--user", "enable", "--now", service], check=True)
        results.append(f"Enabled + started: {service}")
    elif enable:
        runner(["systemctl", "--user", "enable", service], check=True)
        results.append(f"Enabled: {service}")
    elif start:
        runner(["systemctl", "--user", "start", service], check=True)
        results.append(f"Started: {service}")

    if linger:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
        if not user:
            results.append("Warning: could not determine username for enable-linger")
        else:
            runner(["loginctl", "enable-linger", user], check=True)
            results.append(f"Enabled linger for user: {user} "
                           f"(service survives logout)")

    return results


def uninstall_systemd(
    account: str,
    *,
    runner=subprocess.run,
) -> list[str]:
    """Stop + disable the service; leave the unit file in place.

    The unit template is shared across all accounts (via ``@``), so
    removing it would break other configured accounts. Re-install to
    update, ``bridge stop`` for a one-shot stop.
    """
    results: list[str] = []
    # Phase B-0a-3 of the CE/EE migration: stop both the new otaman-bridge
    # unit AND the legacy maestro-bridge unit (if either was installed).
    # `check=False` because either may not exist — best-effort cleanup.
    for prefix in ("otaman-bridge", "maestro-bridge"):
        service = f"{prefix}@{account}.service"
        runner(["systemctl", "--user", "stop", service], check=False)
        results.append(f"Stopped: {service}")
        runner(["systemctl", "--user", "disable", service], check=False)
        results.append(f"Disabled: {service}")
    return results


# ---------------------------------------------------------------------------
# launchd (macOS)


LAUNCHD_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.otaman.bridge.{account}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{maestro_cli}</string>
    <string>bridge</string>
    <string>run</string>
    <string>--account</string>
    <string>{account}</string>{watch_bus_args}{idle_args}
  </array>
  <key>WorkingDirectory</key>
  <string>{workdir}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OTAMAN_PYTHON</key>
    <string>{python}</string>
    <key>OTAMAN_BRIDGE_DIR</key>
    <string>{home}/.otaman</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>{log_dir}/otaman-bridge-{account}.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/otaman-bridge-{account}.err</string>
</dict>
</plist>
"""


def launchd_plist_path(account: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"com.otaman.bridge.{account}.plist"


def launchd_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "otaman-bridge"


def render_launchd_plist(target: InstallTarget) -> str:
    # Each extra ProgramArgument is a <string> element on its own line; keep
    # indentation consistent with the surrounding array block.
    watch_bus_args = "\n    <string>--watch-bus</string>" if target.watch_bus else ""
    idle_args = ""
    if target.idle_auto_afk_minutes > 0:
        idle_args = (
            "\n    <string>--idle-auto-afk-minutes</string>"
            f"\n    <string>{target.idle_auto_afk_minutes}</string>"
        )
    return LAUNCHD_PLIST_TEMPLATE.format(
        account=target.account,
        maestro_cli=target.maestro_cli,
        workdir=target.working_dir,
        python=target.python,
        home=str(Path.home()),
        log_dir=launchd_log_dir(),
        watch_bus_args=watch_bus_args,
        idle_args=idle_args,
    )


def install_launchd(
    target: InstallTarget,
    *,
    start: bool = True,
    runner=subprocess.run,
) -> list[str]:
    """Write the plist + load it with launchctl."""
    results: list[str] = []
    plist_path = launchd_plist_path(target.account)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    launchd_log_dir().mkdir(parents=True, exist_ok=True)

    plist_path.write_text(render_launchd_plist(target), encoding="utf-8")
    results.append(f"Wrote: {plist_path}")

    # Idempotent: unload (ignore failure if not loaded), then load.
    runner(["launchctl", "unload", str(plist_path)], check=False)
    runner(["launchctl", "load", str(plist_path)], check=True)
    results.append(f"Loaded: com.otaman.bridge.{target.account}")

    if start:
        runner(["launchctl", "start", f"com.otaman.bridge.{target.account}"],
               check=False)
        results.append(f"Started: com.otaman.bridge.{target.account}")

    return results


def uninstall_launchd(
    account: str,
    *,
    runner=subprocess.run,
) -> list[str]:
    """Unload + delete the plist."""
    results: list[str] = []
    plist_path = launchd_plist_path(account)
    runner(["launchctl", "unload", str(plist_path)], check=False)
    results.append(f"Unloaded: com.otaman.bridge.{account}")
    if plist_path.exists():
        plist_path.unlink()
        results.append(f"Removed: {plist_path}")
    return results


# ---------------------------------------------------------------------------
# Windows (NSSM / Scheduled Task) — stubbed


class WindowsInstallNotSupported(RuntimeError):
    """Placeholder until NSSM / scheduled-task install lands."""


def install_windows(target: InstallTarget, **_kwargs) -> list[str]:  # noqa: ARG001
    raise WindowsInstallNotSupported(
        "Windows service install is not yet implemented. Options:\n"
        "  - Run `maestro bridge run --account <name>` in a dedicated terminal\n"
        "  - Install NSSM (https://nssm.cc/) and wrap the daemon manually\n"
        "  - Use Task Scheduler with trigger At log on, action = maestro.sh\n"
        "Tracking: design §5.7 scopes this for a future release."
    )


def uninstall_windows(account: str, **_kwargs) -> list[str]:  # noqa: ARG001
    raise WindowsInstallNotSupported(
        "Windows service uninstall not supported (no install to undo)."
    )


# ---------------------------------------------------------------------------
# Dispatcher


def install(
    target: InstallTarget,
    **options,
) -> list[str]:
    """Dispatch to the right installer for the target's system."""
    if target.system == "linux-systemd":
        return install_systemd(target, **options)
    if target.system == "macos-launchd":
        return install_launchd(target, **options)
    if target.system == "windows-nssm":
        return install_windows(target, **options)
    raise RuntimeError(f"unknown system: {target.system}")


def uninstall(
    account: str,
    *,
    system: str | None = None,
    **options,
) -> list[str]:
    system = system or detect_system()
    if system == "linux-systemd":
        return uninstall_systemd(account, **options)
    if system == "macos-launchd":
        return uninstall_launchd(account, **options)
    if system == "windows-nssm":
        return uninstall_windows(account, **options)
    raise RuntimeError(f"unknown system: {system}")
