"""maestro bridge — daemon lifecycle CLI.

Subcommands:
    run     Start the daemon in the foreground for an account.
    status  Show health for one or all configured accounts.
    stop    Gracefully stop a running daemon (via POST /shutdown).

In T2a only ``null`` transport is available. T2b wires up Telegram;
T2c adds systemd/launchd/NSSM service install.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Ensure bridge/ and scripts/ are importable when run directly.

from otaman_bridge.config import (  # noqa: E402
    list_accounts_from_settings,
    load_account_config,
)
from otaman_bridge.core import get_transport, list_transports  # noqa: E402
from otaman_bridge.daemon import (  # noqa: E402
    BridgeDaemon,
    endpoint_path,
    read_endpoint_file,
)
from otaman_bridge import install as install_mod  # noqa: E402


def _iter_account_names(settings_path: Path) -> list[str]:
    """Read account names from launch-settings.yaml. Empty list if absent."""
    if not settings_path.exists():
        return []
    try:
        import yaml  # type: ignore
    except ImportError:
        return []
    try:
        with open(settings_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return []
    accounts = data.get("accounts") or {}
    return sorted(accounts) if isinstance(accounts, dict) else []


def _resolve_settings_path() -> Path:
    """Find launch-settings.yaml via the maestro-root resolver."""
    from otaman_core._resolve import find_maestro_root  # lazy import — keeps CLI faster
    root = find_maestro_root()
    if root is None:
        return Path.cwd() / "launch-settings.yaml"
    return root / "launch-settings.yaml"


# ---------------------------------------------------------------------------
# run


def cmd_run(args: argparse.Namespace) -> int:
    """Start the daemon in the foreground."""
    # Make sure built-in transports are registered.
    import otaman_bridge.transports  # noqa: F401 — side effect: registers built-ins

    # Resolve transport + config. --transport explicitly overrides config
    # (useful for testing null against an otherwise-telegram account);
    # otherwise the config's `transport:` field wins.
    transport_name = args.transport
    transport_config: dict = {}
    if not args.no_config:
        settings_path = _resolve_settings_path()
        if settings_path.exists():
            try:
                account_cfg = load_account_config(args.account, settings_path)
            except KeyError:
                if transport_name is None:
                    print(
                        f"ERROR: account {args.account!r} not in "
                        f"{settings_path} and no --transport override given",
                        file=sys.stderr,
                    )
                    return 1
                account_cfg = None
            else:
                if transport_name is None:
                    transport_name = account_cfg.transport
                transport_config = dict(account_cfg.transport_config)
                if account_cfg.unresolved_secrets and transport_name != "null":
                    print(
                        f"ERROR: unresolved secrets for account {args.account!r}: "
                        f"{sorted(account_cfg.unresolved_secrets)}. "
                        f"Populate via env / .maestro/secrets.env / keychain.",
                        file=sys.stderr,
                    )
                    return 1
        elif transport_name is None:
            print(
                f"ERROR: no launch-settings.yaml found and no --transport given",
                file=sys.stderr,
            )
            return 1

    if transport_name is None:
        transport_name = "null"

    # Inject account_name so transports can derive per-account file paths
    # (e.g., Telegram's topic cache file).
    transport_config.setdefault("account_name", args.account)

    try:
        transport_cls = get_transport(transport_name)
    except KeyError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        registered = list_transports() or "(none)"
        print(f"hint: registered transports = {registered}", file=sys.stderr)
        return 2

    # NullTransport takes no config; everything else accepts a config dict.
    try:
        if transport_name == "null":
            transport = transport_cls()
        else:
            transport = transport_cls(transport_config)
    except (ValueError, ImportError) as e:
        print(f"ERROR: failed to construct {transport_name!r} transport: {e}",
              file=sys.stderr)
        return 1

    endpoint_file = Path(args.endpoint_file).expanduser() if args.endpoint_file \
        else endpoint_path(args.account)

    bus_watcher_root: Path | None = None
    bus_watcher_project = ""
    if args.watch_bus:
        if isinstance(args.watch_bus, str) and args.watch_bus != "-":
            # Explicit path provided.
            bus_watcher_root = Path(args.watch_bus).expanduser().resolve()
        else:
            # Auto-resolve via the maestro-root resolver.
            from otaman_core._resolve import find_maestro_root  # noqa: PLC0415
            resolved = find_maestro_root()
            if resolved is None:
                print(
                    "ERROR: --watch-bus requested but no maestro folder found. "
                    "Pass --watch-bus /path/to/maestro explicitly or cd into "
                    "a maestro-managed directory.",
                    file=sys.stderr,
                )
                return 1
            bus_watcher_root = resolved
        # Project name: --watch-bus-project explicit override, else read
        # platform.yaml's `project:` (same helper used by the PreToolUse
        # hook so both code paths land on the same Telegram topic).
        if args.watch_bus_project:
            bus_watcher_project = args.watch_bus_project
        else:
            from otaman_bridge.bus_surface import resolve_project_name  # noqa: PLC0415
            bus_watcher_project = resolve_project_name(bus_watcher_root)
        if not (bus_watcher_root / ".agents" / "bus").is_dir():
            print(
                f"WARNING: {bus_watcher_root} has no .agents/bus/ — watcher "
                f"will poll an empty directory.",
                file=sys.stderr,
            )

    daemon = BridgeDaemon(
        account=args.account,
        transport=transport,
        port=args.port,
        endpoint_file=endpoint_file,
        bus_watcher_root=bus_watcher_root,
        bus_watcher_project=bus_watcher_project,
        idle_auto_afk_minutes=args.idle_auto_afk_minutes,
    )
    try:
        daemon.start()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(
        f"maestro bridge: account={args.account} "
        f"transport={transport_name} "
        f"port={daemon.port} "
        f"endpoint={daemon.endpoint_file}"
    )
    print("Press Ctrl-C to stop.")

    stop_event = _install_signal_handlers()
    try:
        # Exit on any of:
        #   - SIGINT / SIGTERM  → stop_event (set by signal handler)
        #   - POST /shutdown    → daemon._shutdown_requested (set by
        #                         handle_shutdown's background thread)
        # Without the second condition, /shutdown tears down the HTTP
        # server + transport but leaves cmd_run idling forever, which
        # looks like a zombie daemon to `maestro bridge stop` callers.
        while not stop_event.is_set() and not daemon._shutdown_requested.is_set():
            stop_event.wait(timeout=0.5)
    finally:
        print("\nShutting down...")
        daemon.stop()  # idempotent via _shutdown_requested guard
    return 0


def _install_signal_handlers():
    """Return a threading.Event set when SIGINT / SIGTERM is received."""
    import threading
    stop_event = threading.Event()

    def handler(signum, frame):  # noqa: ARG001
        stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Setting SIGTERM in threads not allowed on some platforms; skip.
                pass
    return stop_event


# ---------------------------------------------------------------------------
# status


def cmd_status(args: argparse.Namespace) -> int:
    """Show health for one or all configured accounts."""
    if args.account:
        accounts = [args.account]
    else:
        accounts = _iter_account_names(_resolve_settings_path())
        if not accounts:
            print("No accounts configured (launch-settings.yaml is missing "
                  "or has no `accounts:` block).")
            return 0

    rows: list[tuple[str, str, str]] = []
    for account in accounts:
        endpoint_file = endpoint_path(account)
        data = read_endpoint_file(endpoint_file)
        if data is None:
            rows.append((account, "stopped", "—"))
            continue

        port = data.get("port", 0)
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/status", timeout=2.0
            )
            status = json.loads(resp.read().decode("utf-8"))
            rows.append((
                account,
                "running",
                f"pid={status['pid']} "
                f"transport={status['transport']} "
                f"port={status['port']} "
                f"pending={status['pending_approvals']} "
                f"uptime={status['uptime_seconds']}s",
            ))
        except (OSError, urllib.error.URLError, ValueError, KeyError):
            rows.append((
                account, "stale", f"endpoint file but daemon unreachable on port {port}",
            ))

    w_acct = max(len("ACCOUNT"), *(len(r[0]) for r in rows))
    w_state = max(len("STATE"), *(len(r[1]) for r in rows))
    print(f"{'ACCOUNT':<{w_acct}}  {'STATE':<{w_state}}  DETAIL")
    print(f"{'-' * w_acct}  {'-' * w_state}  {'-' * 6}")
    for acct, state, detail in rows:
        print(f"{acct:<{w_acct}}  {state:<{w_state}}  {detail}")
    return 0


# ---------------------------------------------------------------------------
# stop


def cmd_stop(args: argparse.Namespace) -> int:
    """Send /shutdown to a running daemon, or clean up a stale endpoint file.

    A "stale" endpoint file means the prior daemon crashed or was killed
    without getting through its cleanup path (Ctrl-C interrupted too hard,
    OOM, power loss). The next `maestro bridge run` would then fail with
    "endpoint file already exists". This command handles that case by
    detecting the connection-refused → stale signal and cleaning up
    automatically.
    """
    endpoint_file = endpoint_path(args.account)
    data = read_endpoint_file(endpoint_file)
    if data is None:
        print(f"No endpoint file for account '{args.account}' (already stopped)")
        return 0

    port = data.get("port")
    token = data.get("token")
    if not port or not token:
        # Malformed file — nothing we can cleanly call, just remove it.
        print(f"Endpoint file malformed: {endpoint_file}")
        try:
            endpoint_file.unlink()
            print("Removed malformed endpoint file.")
            return 0
        except OSError as e:
            print(f"Failed to remove: {e}", file=sys.stderr)
            return 1

    url = f"http://127.0.0.1:{port}/shutdown"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        urllib.request.urlopen(req, timeout=5.0)
    except urllib.error.URLError as e:
        # Connection refused / timeout typically means the daemon process
        # already died but left the endpoint file behind. Auto-clean so the
        # user can `maestro bridge run` again without manual `rm`.
        reason = str(e).lower()
        if any(s in reason for s in ("refused", "timeout", "timed out", "no route")):
            print(f"Daemon on port {port} not responding — process appears dead, "
                  f"cleaning up stale endpoint file.")
            try:
                endpoint_file.unlink()
                print(f"Stale endpoint removed. Run `maestro bridge run "
                      f"--account {args.account}` to start a fresh daemon.")
                return 0
            except OSError as ue:
                print(f"Failed to remove {endpoint_file}: {ue}", file=sys.stderr)
                return 1
        print(f"Shutdown request failed: {e}", file=sys.stderr)
        return 1

    # Wait for the endpoint file to disappear so callers know the daemon's
    # really gone. daemon.stop() now unlinks the endpoint right after the
    # HTTP server shuts down — well before the async-loop teardown — so
    # this should resolve in under a second. Cap at 5s with a fallback
    # ping below to catch the zombie-daemon case (main loop stuck, file
    # never removed).
    for _ in range(50):
        if not endpoint_file.exists():
            print(f"Stopped: {args.account}")
            return 0
        time.sleep(0.1)

    import urllib.error as _err
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/status", timeout=1.0,
        )
        still_alive = True
    except (_err.URLError, OSError, _err.HTTPError):
        still_alive = False

    if still_alive:
        print(
            f"WARNING: {args.account} is still responding on port {port} "
            f"after 5s — shutdown is hung. Try SIGTERM directly:\n"
            f"  kill -TERM {data.get('pid')}\n"
            f"Or, if installed as a systemd service:\n"
            f"  systemctl --user stop maestro-bridge@{args.account}",
            file=sys.stderr,
        )
        return 1

    # Daemon stopped responding but the endpoint file is stale — clean up.
    print(
        f"Daemon stopped but left a stale endpoint file; removing "
        f"{endpoint_file}",
    )
    try:
        endpoint_file.unlink()
    except OSError as e:
        print(f"Failed to remove: {e}", file=sys.stderr)
        return 1
    print(f"Stopped: {args.account}")
    return 0


# ---------------------------------------------------------------------------
# install / uninstall


def cmd_dcr_cleanup(args: argparse.Namespace) -> int:
    """Delete stale DCR-shim-managed Zitadel apps.

    Reads shim config from the same env vars the daemon uses
    (OTAMAN_DCR_SHIM, OTAMAN_DCR_SHIM_CLIENT_ID, OTAMAN_DCR_SHIM_SECRET,
    OIDC_PROJECT_ID, OIDC_ORG_ID, etc.). Builds an ephemeral mgmt-API
    client and calls sweep_orphans().

    Use this when the daemon hasn't been running and orphans piled up,
    or for one-off cleanup outside the daemon's sweep cadence. The
    daemon's background sweep covers the steady state.
    """
    try:
        from otaman_bridge_ee.dcr_shim import (
            IdpConfig,
            ZitadelMgmtClient,
            ZitadelMgmtError,
            parse_duration_seconds,
            sweep_orphans,
        )
    except ImportError:
        print(
            "DCR shim cleanup requires otaman-bridge-ee (Enterprise Edition).",
            file=sys.stderr,
        )
        return 2

    cfg = IdpConfig.from_env()
    if cfg is None:
        print("DCR shim not enabled (OTAMAN_DCR_SHIM is off). Nothing to clean.",
              file=sys.stderr)
        return 2
    if not (cfg.machine_user_client_id and cfg.machine_user_client_secret and cfg.org_id):
        print(
            "DCR shim mgmt-API credentials incomplete: need "
            "OTAMAN_DCR_SHIM_CLIENT_ID + OTAMAN_DCR_SHIM_SECRET + OIDC_ORG_ID.",
            file=sys.stderr,
        )
        return 2

    if args.ttl:
        try:
            ttl_seconds = parse_duration_seconds(args.ttl)
        except ValueError as exc:
            print(f"invalid --ttl: {exc}", file=sys.stderr)
            return 2
    else:
        ttl_seconds = cfg.cleanup_ttl_seconds

    token_url = f"{cfg.management_base_url}/oauth/v2/token"
    mgmt = ZitadelMgmtClient(
        base_url=cfg.management_base_url,
        token_url=token_url,
        client_id=cfg.machine_user_client_id,
        client_secret=cfg.machine_user_client_secret,
        org_id=cfg.org_id,
        expected_host=cfg.expected_host,
    )

    try:
        report = sweep_orphans(
            mgmt_client=mgmt,
            project_id=cfg.project_id,
            name_prefix=cfg.managed_name_prefix,
            ttl_seconds=ttl_seconds,
            dry_run=bool(args.dry_run),
        )
    except ZitadelMgmtError as exc:
        print(f"sweep failed: {exc}", file=sys.stderr)
        return 1

    verb = "would delete" if args.dry_run else "deleted"
    print(f"DCR cleanup sweep ({cfg.managed_name_prefix}*, ttl={ttl_seconds}s)")
    print(f"  found:    {report.found}")
    print(f"  eligible: {report.eligible}")
    print(f"  {verb}: {report.deleted}")
    if report.failed:
        print(f"  failed:   {report.failed}")
        for fid in report.failed_ids:
            print(f"    - {fid}")
    if args.dry_run and report.deleted:
        print()
        print("Re-run without --dry-run to actually delete.")
    return 0 if report.failed == 0 else 1


def _resolve_accounts(settings_path: Path, cli_account: str | None, all_flag: bool) -> list[str]:
    """Resolve which accounts install/uninstall applies to."""
    if all_flag:
        names = list_accounts_from_settings(settings_path)
        if not names:
            raise RuntimeError(
                f"--all requested but no accounts configured in {settings_path}"
            )
        return names
    if cli_account:
        return [cli_account]
    raise RuntimeError("one of --account NAME or --all is required")


def cmd_install(args: argparse.Namespace) -> int:
    """Install the bridge daemon as a system service."""
    try:
        accounts = _resolve_accounts(
            _resolve_settings_path(), args.account, args.all,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    system = args.system or install_mod.detect_system()
    if system == "windows-nssm":
        print(
            "ERROR: Windows service install is not yet supported.\n"
            "  Run `maestro bridge run` in a dedicated terminal instead.\n"
            "  (See design §5.7 — scoped for a future release.)",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        import json as _json
        for account in accounts:
            target = install_mod.make_install_target(
                account, system=system,
                working_dir=args.working_dir,
                watch_bus=not args.no_watch_bus,
                idle_auto_afk_minutes=args.idle_auto_afk_minutes,
            )
            print(f"--- {account} ---")
            print(_json.dumps(target.to_dict(), indent=2))
            if system == "linux-systemd":
                print("--- unit file ---")
                print(install_mod.render_systemd_unit(target))
            elif system == "macos-launchd":
                print("--- plist ---")
                print(install_mod.render_launchd_plist(target))
        return 0

    enable = not args.no_enable
    start = not args.no_start

    total_msgs: list[str] = []
    for account in accounts:
        print(f"Installing bridge service for account '{account}'...")
        target = install_mod.make_install_target(
            account, system=system,
            working_dir=args.working_dir,
        )
        try:
            if system == "linux-systemd":
                msgs = install_mod.install(
                    target, enable=enable, start=start, linger=args.linger,
                )
            else:  # macos-launchd
                msgs = install_mod.install(target, start=start)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR installing {account}: {e}", file=sys.stderr)
            return 1
        for m in msgs:
            print(f"  {m}")
        total_msgs.extend(msgs)

    print()
    print(f"Installed {len(accounts)} service(s). Check status with:")
    if system == "linux-systemd":
        for a in accounts:
            print(f"  systemctl --user status maestro-bridge@{a}")
            print(f"  journalctl --user -u maestro-bridge@{a} -f")
    elif system == "macos-launchd":
        for a in accounts:
            print(f"  launchctl list | grep com.maestro.bridge.{a}")
            print(f"  tail -f ~/Library/Logs/maestro-bridge/maestro-bridge-{a}.log")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Stop + disable the bridge daemon service (keeps the unit template)."""
    try:
        accounts = _resolve_accounts(
            _resolve_settings_path(), args.account, args.all,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    system = args.system or install_mod.detect_system()
    for account in accounts:
        print(f"Uninstalling bridge service for account '{account}'...")
        try:
            msgs = install_mod.uninstall(account, system=system)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR uninstalling {account}: {e}", file=sys.stderr)
            return 1
        for m in msgs:
            print(f"  {m}")
    return 0


# ---------------------------------------------------------------------------
# argparse


def main(argv: list[str] | None = None) -> int:
    # OTAMAN_BRIDGE_LOG_LEVEL overrides the default. Useful for operators
    # tracing OAuth discovery / MCP request dispatch -- the per-request
    # log_message in the daemon handler is at DEBUG, so DEBUG surfaces
    # the HTTP wire activity.
    _level_name = os.environ.get("OTAMAN_BRIDGE_LOG_LEVEL", "INFO").upper()
    _level = getattr(logging, _level_name, logging.INFO)
    logging.basicConfig(
        level=_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Silence third-party loggers that leak secrets at INFO level.
    # httpx/httpcore print every request URL — and the Telegram Bot API
    # puts the token in the path (`/bot<TOKEN>/getUpdates`), so an INFO
    # log line leaks the whole secret. Drop them to WARNING; only errors
    # surface. Maestro's own `maestro.bridge.*` loggers stay at INFO.
    for noisy in (
        "httpx",
        "httpcore",
        "telegram.ext.Application",
        "telegram.request",
        "telegram.Bot",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        prog="maestro bridge",
        description="Remote-approval bridge daemon lifecycle",
    )
    subs = parser.add_subparsers(dest="subcommand", required=True)

    p_run = subs.add_parser("run", help="Start the daemon in the foreground")
    p_run.add_argument("--account", required=True,
                       help="Account name (e.g. personal, riseapps)")
    p_run.add_argument("--transport",
                       help="Force transport (overrides launch-settings.yaml)")
    p_run.add_argument("--no-config", action="store_true",
                       help="Don't read launch-settings.yaml (uses --transport only)")
    p_run.add_argument("--port", type=int, default=0,
                       help="Bind port (0 = OS-assigned ephemeral)")
    p_run.add_argument("--endpoint-file",
                       help="Override endpoint file path")
    p_run.add_argument(
        "--watch-bus", nargs="?", const="-", default=None,
        help="Poll .agents/bus/active/ and surface new messages to the "
             "transport. Pass a path to the maestro folder to watch, or use "
             "bare --watch-bus to auto-resolve via the maestro-root finder.",
    )
    p_run.add_argument(
        "--watch-bus-project", default="",
        help="Project name used in surfaced message titles (defaults to "
             "the maestro folder name).",
    )
    p_run.add_argument(
        "--idle-auto-afk-minutes", type=int, default=0,
        help="Minutes of UserPromptSubmit inactivity before auto-enabling "
             "AFK (0 = disabled). Needs --watch-bus (shares the same "
             "maestro root). Clears the auto-entry when you come back.",
    )
    p_run.set_defaults(func=cmd_run)

    p_status = subs.add_parser("status", help="Show daemon health")
    p_status.add_argument("--account",
                          help="Only show this account (default: all configured)")
    p_status.set_defaults(func=cmd_status)

    p_stop = subs.add_parser("stop", help="Gracefully stop a running daemon")
    p_stop.add_argument("--account", required=True,
                        help="Account name of the daemon to stop")
    p_stop.set_defaults(func=cmd_stop)

    p_dcr = subs.add_parser(
        "dcr-cleanup",
        help="Delete DCR-shim-managed Zitadel OIDC apps older than TTL "
             "(orphans from stale fingerprints). Reads shim config from env.",
    )
    p_dcr.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be deleted without actually deleting.",
    )
    p_dcr.add_argument(
        "--ttl",
        help="Override OTAMAN_DCR_SHIM_TTL (e.g. 7d / 24h / 1h). "
             "Apps with creation age greater than this are eligible for delete.",
    )
    p_dcr.set_defaults(func=cmd_dcr_cleanup)

    p_install = subs.add_parser(
        "install",
        help="Install the bridge as a system service (systemd on Linux, "
             "launchd on macOS)",
    )
    g_install = p_install.add_mutually_exclusive_group()
    g_install.add_argument("--account", help="Account name to install a service for")
    g_install.add_argument("--all", action="store_true",
                           help="Install services for every account in launch-settings.yaml")
    p_install.add_argument("--system",
                           choices=["linux-systemd", "macos-launchd", "windows-nssm"],
                           help="Override auto-detected platform (mostly for testing)")
    p_install.add_argument("--working-dir",
                           help="Override WorkingDirectory (default: resolved maestro root)")
    p_install.add_argument("--no-enable", action="store_true",
                           help="Linux only — skip `systemctl enable` (won't autostart on boot)")
    p_install.add_argument("--no-start", action="store_true",
                           help="Don't start the service now (install only)")
    p_install.add_argument("--linger", action="store_true",
                           help="Linux only — `loginctl enable-linger` so the "
                                "service survives SSH logout")
    p_install.add_argument(
        "--no-watch-bus", action="store_true",
        help="Omit --watch-bus from the service command. By default the "
             "installed service drains .agents/bus/active/ and surfaces "
             "bus messages (T2d); pass this if you only want the bridge "
             "for PreToolUse approvals.",
    )
    p_install.add_argument(
        "--idle-auto-afk-minutes", type=int, default=0,
        help="Auto-enable AFK after N minutes of UserPromptSubmit inactivity "
             "(0 = disabled). The daemon watches .maestro/last-user-activity "
             "and flips AFK on/off based on human presence.",
    )
    p_install.add_argument("--dry-run", action="store_true",
                           help="Print the resolved target + unit file without writing anything")
    p_install.set_defaults(func=cmd_install)

    p_uninstall = subs.add_parser(
        "uninstall",
        help="Stop + disable the bridge service for an account",
    )
    g_un = p_uninstall.add_mutually_exclusive_group()
    g_un.add_argument("--account", help="Account name to uninstall")
    g_un.add_argument("--all", action="store_true",
                      help="Uninstall services for every configured account")
    p_uninstall.add_argument("--system",
                             choices=["linux-systemd", "macos-launchd", "windows-nssm"],
                             help="Override auto-detected platform")
    p_uninstall.set_defaults(func=cmd_uninstall)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
