"""Bridge daemon — HTTP on 127.0.0.1:<ephemeral> with bearer-token auth.

Design rationale (§5.3 / §9):
- Loopback HTTP everywhere (not unix sockets / named pipes) so one
  implementation works on Linux, macOS, WSL, and Windows, and a future
  local UI reuses the same endpoint.
- Bearer token stored in ``~/.otaman/bridge-<account>.endpoint`` (mode
  0600). Token rotates on every daemon restart.
- Same-user processes can impersonate — accepted, matches unix-socket
  trust boundary.
- Sync HTTP server bridges to an async asyncio loop running in a
  background thread; Transport methods are all async.

Routes:
    POST /approval   — blocks until reply or timeout, returns ApprovalResponse
    POST /notify     — fire-and-forget, returns 202
    GET  /status     — daemon health, returns summary dict
    POST /reply      — internal: deliver a decision for a pending approval
    POST /shutdown   — graceful stop, removes endpoint file

All routes require ``Authorization: Bearer <token>`` except ``GET /status``
which returns sanitized info without auth (intentional: lets `otaman bridge
status` introspect a daemon it doesn't own the token for).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets as _secrets
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from otaman_bridge.afk_service import AfkService
from otaman_bridge.approval_service import ApprovalService
from otaman_bridge.auth_stack import AuthStack
from otaman_bridge.bus_surface_service import BusSurfaceService, _PendingBusDecision
from otaman_bridge.bus_watcher import BusWatcher
from otaman_bridge.core import (
    ApprovalResponse,
    InboundReply,
    InfoMessage,
    Transport,
)
from otaman_bridge.edition import edition_status, emit_ce_notice_once
from otaman_bridge.experimental_mode import healthz_extras
from otaman_bridge.http_handler import _make_handler
from otaman_bridge.mcp_dispatch_service import McpDispatchService

# Map from transport-neutral Action verbs → Decision verbs.
# Decisions resolve the pending approval; non-decision actions (details,
# snooze, comment) do their own thing and leave the approval pending.
_ACTION_TO_DECISION: dict[str, str] = {
    "approve": "allow",
    "reject": "deny",
}

# How long Snooze defers an approval by. The deadline extends by this
# amount + a 30s buffer so the hook doesn't time out mid-snooze. Exposed
# as a module-level name so tests can monkeypatch it down to sub-second.
SNOOZE_SECONDS = 15 * 60

_log = logging.getLogger("maestro.bridge.daemon")  # legacy: logger renamed at otaman-core 1.0


# ---------------------------------------------------------------------------
# Endpoint file


def endpoint_path(account: str, *, home: Path | None = None) -> Path:
    """Standard location for the endpoint file.

    Resolution order:
      1. ``$OTAMAN_BRIDGE_DIR`` env var — absolute path to the directory
         holding endpoint files. Use this for otaman-native deployments
         pointing at ``~/.otaman/``.
      2. ``$MAESTRO_BRIDGE_DIR`` env var — legacy alias.
      3. ``<home>/.maestro/`` — legacy: default kept for back-compat with
         running daemons + tooling that doesn't know about the new path.
         Will be changed to ``~/.otaman/`` as the default at otaman-core 1.0.

    Per the CE/EE workspace migration, otaman-native deployments should
    set ``OTAMAN_BRIDGE_DIR=$HOME/.otaman`` so endpoint files land
    alongside the runner's ``~/.otaman/runner.endpoint``. Legacy
    deployments (greenbin, personal, manual-test) continue to write to
    ``~/.maestro/`` until each is migrated.  # legacy: remove default at otaman-core 1.0
    """
    h = home or Path.home()
    override = os.environ.get("OTAMAN_BRIDGE_DIR") or os.environ.get("MAESTRO_BRIDGE_DIR")
    if override:
        base = Path(override)
        if not base.is_absolute():
            base = h / base
    else:
        base = h / ".maestro"  # legacy: default changes to ~/.otaman/ at otaman-core 1.0
    return base / f"bridge-{account}.endpoint"


def write_endpoint_file(
    path: Path,
    *,
    port: int,
    token: str,
    pid: int,
    account: str,
    transport: str,
) -> None:
    """Write the endpoint descriptor and tighten permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "port": port,
        "token": token,
        "pid": pid,
        "account": account,
        "transport": transport,
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError:
            pass


def read_endpoint_file(path: Path) -> dict[str, Any] | None:
    """Read and parse an endpoint file; None if absent or malformed."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _build_ce_web_auth(bus_watcher_root: Path | None, state_dir: Path):
    """Construct the CE web-auth service, or None (ce-refresh-token 1.2).

    Returns None — so the ``/api/auth/*`` + ``/api/terminal/attach-token``
    routes 404 — when there is no workspace, the ``otaman-core[web-auth]`` extra
    is absent, or ``terminal.local_auth`` is not configured/enabled. This is the
    runner-free CE surface; EE serves the equivalent from the runner.
    """
    if bus_watcher_root is None:
        return None
    try:
        from otaman_core.web_auth import CeAuthManager

        from otaman_bridge.ce_web_auth import CeWebAuthService, RefreshTokenStore
    except ImportError:
        _log.debug("CE web-auth unavailable (otaman-core[web-auth] extra not installed)")
        return None
    platform_yaml = Path(bus_watcher_root) / "platform.yaml"
    try:
        manager = CeAuthManager.from_platform_yaml(platform_yaml, Path(state_dir))
    except Exception:  # noqa: BLE001 — never let auth wiring crash daemon startup
        _log.exception("CE web-auth: failed to build auth manager from %s", platform_yaml)
        return None
    if not manager.enabled:
        return None
    _log.info("CE web-auth enabled (runner-free): /api/auth/login, /refresh, attach-token")
    return CeWebAuthService(manager, RefreshTokenStore(Path(state_dir)))


# ---------------------------------------------------------------------------
# Async loop helper — runs in a dedicated thread so sync HTTP handlers
# can submit coroutines via run_coroutine_threadsafe.


class _AsyncLoopThread:
    """Background asyncio event loop, shut down cleanly on ``stop()``."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="bridge-asyncio",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        self._started.wait()

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._started.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def submit(self, coro):
        """Schedule a coroutine; return a ``concurrent.futures.Future``."""
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        if self.loop and self.loop.is_running():
            # Cancel all pending tasks and AWAIT them before stopping the loop —
            # cancelling alone leaves them in "cancelling" state when the loop
            # closes, which triggers "Task was destroyed but it is pending".
            async def _drain():
                assert self.loop is not None
                current = asyncio.current_task()
                tasks = [t for t in asyncio.all_tasks(self.loop) if t is not current]
                for t in tasks:
                    t.cancel()
                for t in tasks:
                    try:
                        await t
                    except BaseException:  # noqa: BLE001, PERF203
                        # Cancellation is expected; other errors at shutdown
                        # are non-fatal.
                        pass

            fut = asyncio.run_coroutine_threadsafe(_drain(), self.loop)
            try:
                fut.result(timeout=3.0)
            except Exception:  # noqa: BLE001
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Daemon


def _build_oidc_validator_from_env():
    """Build an OIDCValidator from environment if configured, else None.

    Reads:
        OTAMAN_AUTH_MODE       — must be ``oidc`` to enable
        OIDC_ISSUER            — the OIDC provider's issuer URL
        OIDC_AUDIENCE_BRIDGE   — this bridge's Zitadel client id
        OIDC_JWKS_URI          — optional; derived from issuer if absent
        OIDC_REQUIRED_ROLE     — optional role gate

    Returns ``OIDCValidator`` instance or ``None`` (auth falls back
    to loopback bearer only — same-host CLI introspection).
    """
    if os.environ.get("OTAMAN_AUTH_MODE", "").lower() != "oidc":
        return None
    issuer = os.environ.get("OIDC_ISSUER", "").strip()
    audience = os.environ.get("OIDC_AUDIENCE_BRIDGE", "").strip()
    if not issuer or not audience:
        _log.warning(
            "OTAMAN_AUTH_MODE=oidc but OIDC_ISSUER/OIDC_AUDIENCE_BRIDGE unset; "
            "falling back to loopback bearer only"
        )
        return None
    from otaman_core.auth_oidc import OIDCConfig, OIDCValidator

    cfg = OIDCConfig(
        issuer=issuer,
        audience=audience,
        jwks_uri=os.environ.get("OIDC_JWKS_URI") or None,
        required_role=os.environ.get("OIDC_REQUIRED_ROLE") or None,
    )
    _log.info("OIDC validator enabled (issuer=%s aud=%s)", issuer, audience)
    return OIDCValidator(cfg)


def _build_protected_resource_metadata(
    *,
    issuer: str,
    resource: str,
    scopes: tuple[str, ...] = ("openid", "profile", "email"),
) -> dict[str, Any]:
    """RFC 9728 Protected Resource Metadata payload.

    Returned at ``/.well-known/oauth-protected-resource`` so MCP clients
    can discover this bridge's authorization server. The MCP authorization
    spec then has the client fetch ``<issuer>/.well-known/oauth-authorization-server``
    directly from the issuer — we do not host AS metadata here.
    """
    return {
        "resource": resource,
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": list(scopes),
    }


def _resolve_public_resource_url(host_header: str) -> str:
    """Derive the resource-server identifier URL from a request Host header.

    Precedence:
        1. ``OTAMAN_BRIDGE_PUBLIC_URL`` env var (set when bridge sits
           behind a reverse proxy with a public hostname / TLS).
        2. ``http://<Host header>`` (loopback dev case).

    The fallback is intentionally http; production deployments behind
    TLS must set the env override so the resource identifier matches
    what clients actually reach.
    """
    override = os.environ.get("OTAMAN_BRIDGE_PUBLIC_URL", "").strip()
    if override:
        return override.rstrip("/")
    host = (host_header or "127.0.0.1").strip()
    return f"http://{host}"


def _build_web_login_flow_from_env():
    """Build a (LoginFlow, PendingLoginStore) pair from environment, or None.

    Reads:
        OTAMAN_AUTH_MODE              -- must be ``oidc`` to enable
        OIDC_ISSUER                   -- Zitadel issuer URL
        OIDC_AUDIENCE_BRIDGE          -- bridge's client_id in Zitadel
        OIDC_BRIDGE_REDIRECT_URI      -- public ``/auth/callback`` URL we
                                         registered with Zitadel
        OIDC_PROJECT_ID               -- optional; adds project-aud scope

    Returns ``(LoginFlow, PendingLoginStore)`` when fully configured,
    else ``None``. The daemon stores this on ``self.web_login_flow``;
    when ``None`` the ``/auth/login`` route returns 503 (web login is
    not enabled — clients should use a Bearer token directly).
    """
    if os.environ.get("OTAMAN_AUTH_MODE", "").lower() != "oidc":
        return None
    issuer = os.environ.get("OIDC_ISSUER", "").strip()
    # Prefer the dedicated web-client id (created by zitadel-bootstrap.py
    # as otaman-bridge-web). Fall back to OIDC_AUDIENCE_BRIDGE for backward
    # compat with deployments that have one combined client id.
    client_id = (
        os.environ.get("OIDC_BRIDGE_WEB_CLIENT_ID", "").strip()
        or os.environ.get("OIDC_AUDIENCE_BRIDGE", "").strip()
    )
    redirect_uri = os.environ.get("OIDC_BRIDGE_REDIRECT_URI", "").strip()
    if not issuer or not client_id or not redirect_uri:
        _log.warning(
            "OTAMAN_AUTH_MODE=oidc but web-login env incomplete; "
            "/auth/login disabled (issuer=%s client=%s redirect=%s)",
            bool(issuer),
            bool(client_id),
            bool(redirect_uri),
        )
        return None
    try:
        from otaman_bridge_ee.web_auth import LoginFlow, PendingLoginStore, WebAuthConfig
    except ImportError:
        _log.info("EE package absent; web-login flow disabled")
        return None
    cfg = WebAuthConfig(
        issuer=issuer,
        client_id=client_id,
        redirect_uri=redirect_uri,
        project_id=os.environ.get("OIDC_PROJECT_ID") or None,
    )
    store = PendingLoginStore()
    _log.info("web-login flow enabled (redirect_uri=%s)", redirect_uri)
    return LoginFlow(cfg, store), store


class BridgeDaemon:
    """Owns the HTTP server, the transport, and the pending-approval table."""

    def __init__(
        self,
        *,
        account: str,
        transport: Transport,
        host: str = "127.0.0.1",
        port: int = 0,  # 0 = OS-assigned ephemeral
        endpoint_file: Path | None = None,
        bus_watcher_root: Path | None = None,
        bus_watcher_project: str = "",
        idle_auto_afk_minutes: int = 0,
    ) -> None:
        if not account.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Invalid account name: {account!r}")
        self.account = account
        self.transport = transport
        self.host = host
        self.requested_port = port
        self.endpoint_file = endpoint_file or endpoint_path(account)
        # T2d: optional bus watcher. When provided, the daemon drains
        # .agents/bus/active/ at a polling interval and surfaces matching
        # messages via the transport. bus_watcher_root is the otaman
        # workspace to watch; bus_watcher_project is the project name for
        # Telegram titles (defaults to root.name if empty).
        self.bus_watcher_root = bus_watcher_root
        self.bus_watcher_project = bus_watcher_project
        # Idle-auto-AFK: when positive, the daemon spawns an IdleAFKMonitor
        # that flips AFK on after N minutes of no UserPromptSubmit activity
        # and clears it (if self-set) when the user returns. 0 disables.
        self.idle_auto_afk_minutes = int(idle_auto_afk_minutes)

        self.token = _secrets.token_hex(24)  # 48 hex chars
        self.pid = os.getpid()
        self.started_at = time.monotonic()

        # CE/EE auth wiring (OIDC validator, DCR shim, web-login stack,
        # composite auth_provider chain). Extracted to AuthStack (F040
        # phase 4) — every attribute it builds stays a frozen forwarding
        # property below (auth_stack.py's module docstring explains why).
        # project_root=bus_watcher_root lets the DCR shim's trust level
        # (F185) read platform.yaml's terminal.dcr_shim_trust when a
        # workspace is configured; None in env-only/--no-config mode.
        self._auth_stack = AuthStack(token=self.token, project_root=self.bus_watcher_root)

        # ce-refresh-token 1.2: the bridge is the runner-free CE web-auth host.
        # Mounts otaman-core's shared AuthService + a refresh layer when
        # local_auth is configured and the otaman-core[web-auth] extra is
        # present; None otherwise (the /api/auth/* routes then 404).
        self.ce_web_auth = _build_ce_web_auth(self.bus_watcher_root, self.endpoint_file.parent)

        # MCP tool registry + RunnerClient + Inbox. Extracted to
        # McpDispatchService (F040 phase 5) — mcp_server, _runner_client,
        # and inbox stay frozen forwarding properties below.
        self._mcp_service = McpDispatchService(session_store=self.session_store)

        self._async = _AsyncLoopThread()
        # Tool-call approval table (hook blocked in handle_approval()
        # waiting for a human tap). Extracted to ApprovalService (F040
        # phase 1) — see approval_service.py for the state + lock it owns.
        self._approval_service = ApprovalService(
            transport=self.transport,
            async_loop=self._async,
        )
        # Bus spec-change-request surfacing (watcher lifecycle + pending-
        # decision registry). Extracted to BusSurfaceService (F040 phase 2)
        # — own lock, independent of ApprovalService's: nothing requires
        # the two pending tables to be read/written atomically together
        # (the two dispatch call sites that touch both —
        # _dispatch_inbound_reply, _surface_details — do so as two
        # independent lookups).
        self._bus_service = BusSurfaceService(
            transport=self.transport,
            async_loop=self._async,
            account=self.account,
        )
        # Idle-auto-AFK monitor lifecycle + notifications. Extracted to
        # AfkService (F040 phase 3).
        self._afk_service = AfkService(
            transport=self.transport,
            async_loop=self._async,
            account=self.account,
        )
        self._server: ThreadingHTTPServer | None = None
        self._serve_thread: threading.Thread | None = None
        self._shutdown_requested = threading.Event()
        self._listener_future = None  # concurrent.futures.Future, set on start()

    # ----- test/back-compat accessors --------------------------------------
    # BusSurfaceService owns this state; several tests reach into these
    # attribute names directly (they predate the phase-2 extraction), so
    # they're kept as thin forwarding properties rather than churning the
    # test suite. Treat these names as a frozen seam, same as the daemon
    # attributes EE's routes_dcr.py depends on.

    @property
    def _pending_bus(self) -> dict[str, _PendingBusDecision]:
        return self._bus_service._pending_bus

    @property
    def _bus_watcher(self) -> BusWatcher | None:
        return self._bus_service.bus_watcher

    @property
    def _bus_watcher_future(self):
        return self._bus_service._bus_watcher_future

    # AuthStack owns this state (F040 phase 4). 47 test call sites across
    # 11 files reassign these post-construction, and EE's routes_dcr.py
    # reaches into several of them directly — read/write forwarding
    # properties so neither needed to change.

    @property
    def oidc_validator(self):
        return self._auth_stack.oidc_validator

    @oidc_validator.setter
    def oidc_validator(self, value) -> None:
        self._auth_stack.oidc_validator = value

    @property
    def idp_config(self):
        return self._auth_stack.idp_config

    @idp_config.setter
    def idp_config(self, value) -> None:
        self._auth_stack.idp_config = value

    @property
    def _idp_metadata_cache(self):
        return self._auth_stack._idp_metadata_cache

    @_idp_metadata_cache.setter
    def _idp_metadata_cache(self, value) -> None:
        self._auth_stack._idp_metadata_cache = value

    @property
    def _dcr_mgmt_client_cached(self):
        return self._auth_stack._dcr_mgmt_client_cached

    @_dcr_mgmt_client_cached.setter
    def _dcr_mgmt_client_cached(self, value) -> None:
        self._auth_stack._dcr_mgmt_client_cached = value

    @property
    def web_login_flow(self):
        return self._auth_stack.web_login_flow

    @web_login_flow.setter
    def web_login_flow(self, value) -> None:
        self._auth_stack.web_login_flow = value

    @property
    def session_store(self):
        return self._auth_stack.session_store

    @session_store.setter
    def session_store(self, value) -> None:
        self._auth_stack.session_store = value

    @property
    def session_cookie(self):
        return self._auth_stack.session_cookie

    @session_cookie.setter
    def session_cookie(self, value) -> None:
        self._auth_stack.session_cookie = value

    @property
    def login_completer(self):
        return self._auth_stack.login_completer

    @login_completer.setter
    def login_completer(self, value) -> None:
        self._auth_stack.login_completer = value

    @property
    def auth_provider(self):
        return self._auth_stack.auth_provider

    @auth_provider.setter
    def auth_provider(self, value) -> None:
        self._auth_stack.auth_provider = value

    @property
    def _ee_dcr_try_handle(self):
        return self._auth_stack._ee_dcr_try_handle

    @_ee_dcr_try_handle.setter
    def _ee_dcr_try_handle(self, value) -> None:
        self._auth_stack._ee_dcr_try_handle = value

    def get_or_build_dcr_mgmt_client(self):
        return self._auth_stack.get_or_build_dcr_mgmt_client()

    # McpDispatchService owns this state (F040 phase 5). A couple of
    # tests reach into these directly (daemon._runner_client = stub,
    # daemon.mcp_server.register(...), daemon.inbox.write_message(...)),
    # and the HTTP handler dispatches via daemon.mcp_server.handle_request.

    @property
    def mcp_server(self):
        return self._mcp_service.mcp_server

    @mcp_server.setter
    def mcp_server(self, value) -> None:
        self._mcp_service.mcp_server = value

    @property
    def _runner_client(self):
        return self._mcp_service._runner_client

    @_runner_client.setter
    def _runner_client(self, value) -> None:
        self._mcp_service._runner_client = value

    @property
    def inbox(self):
        return self._mcp_service.inbox

    @inbox.setter
    def inbox(self, value) -> None:
        self._mcp_service.inbox = value

    # ----- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Start the asyncio loop thread + HTTP server. Non-blocking.

        If an endpoint file already exists, ping its recorded port.
        Connection refused = the prior process is gone → stale → overwrite.
        A live response = another daemon really is running → refuse.
        """
        existing = read_endpoint_file(self.endpoint_file)
        if existing:
            if _endpoint_is_live(existing):
                raise RuntimeError(
                    f"endpoint file already exists and a daemon IS running on "
                    f"port {existing.get('port')}: {self.endpoint_file} "
                    f"(run `otaman bridge stop --account {self.account}` first)"
                )
            _log.info(
                "found stale endpoint file (port %s unreachable); replacing it",
                existing.get("port"),
            )
            try:
                self.endpoint_file.unlink()
            except OSError:
                pass

        self._async.start()

        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self.host, self.requested_port), handler)
        # Reuse address so quick restarts don't hit TIME_WAIT.
        self._server.allow_reuse_address = True
        # Socket family is always INET (127.0.0.1)
        assigned_port = self._server.server_address[1]

        write_endpoint_file(
            self.endpoint_file,
            port=assigned_port,
            token=self.token,
            pid=self.pid,
            account=self.account,
            transport=self.transport.name,
        )

        self._serve_thread = threading.Thread(
            target=self._server.serve_forever,
            name="bridge-http",
            daemon=True,
        )
        self._serve_thread.start()

        # Start the transport listener loop in the async thread. It iterates
        # Transport.listen() forever and dispatches InboundReply → ApprovalResponse.
        # Running NullTransport's listen() is harmless (queue is empty).
        self._listener_future = self._async.submit(self._listener_loop())

        # T2d: optionally start the bus watcher in the same async loop.
        # Info-only surfacing in this phase (T2d-2); interactive bus
        # approvals land in T2d-3.
        self._bus_service.start(
            bus_watcher_root=self.bus_watcher_root,
            bus_watcher_project=self.bus_watcher_project,
        )

        # Idle-auto-AFK monitor: enabled when idle_auto_afk_minutes > 0
        # AND an otaman workspace is configured (shares bus_watcher_root since
        # last-user-activity lives in the same .otaman/ directory).
        self._afk_service.start(
            project_root=self.bus_watcher_root,
            idle_minutes=self.idle_auto_afk_minutes,
            project=self.bus_watcher_project,
        )

        # DCR shim cleanup sweep (D6). Background task that periodically
        # prunes shim-managed apps older than ``cleanup_ttl_seconds``. Off
        # when shim disabled or sweep_interval=0 (manual cleanup via the
        # `otaman bridge dcr-cleanup` CLI command still works).
        self._dcr_sweep_future = None
        if (
            self.idp_config is not None
            and self.idp_config.dcr_shim
            and self.idp_config.cleanup_sweep_interval_seconds > 0
        ):
            self._dcr_sweep_future = self._async.submit(self._auth_stack.dcr_cleanup_sweep_loop())

        _log.info(
            "bridge daemon listening on %s:%d (account=%s, transport=%s)",
            self.host,
            assigned_port,
            self.account,
            self.transport.name,
        )

        # ce-ee-release-channels 3.1: one-time honest edition notice at
        # startup when the (EE) auto-session-spawn subsystem is absent.
        emit_ce_notice_once(_log)

    def stop(self) -> None:
        """Graceful shutdown — remove endpoint file, cancel pending approvals.

        Ordering matters: we delete the endpoint file *before* the slow
        async teardown so ``otaman bridge stop`` — which polls the
        endpoint file as its primary "daemon gone" signal — doesn't
        block for the full transport.close()/async-drain budget. Once
        the HTTP server is shut down the daemon can't serve any more
        requests, so "endpoint gone + server stopped" is the correct
        user-visible finish line; the background teardown that follows
        only releases in-process resources.
        """
        if self._shutdown_requested.is_set():
            return
        self._shutdown_requested.set()

        # Cancel pending approvals so hooks get an immediate "daemon-shutdown"
        # response instead of waiting for their timeouts.
        self._approval_service.cancel_all(
            lambda request_id: ApprovalResponse(
                decision="ask",  # fail-safe: let Claude's native prompt show
                request_id=request_id,
                responder="daemon:shutdown",
                message="bridge daemon shutting down",
            )
        )
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

        # Drop the endpoint file NOW so `otaman bridge stop` returns
        # promptly. The remaining async-loop cancellation below happens
        # in the background; a concurrent `otaman bridge run` is free
        # to start a new daemon at this point.
        try:
            self.endpoint_file.unlink(missing_ok=True)
        except OSError:
            pass

        # Cancel the listener future so the async loop exits cleanly.
        if self._listener_future is not None:
            self._listener_future.cancel()
            try:
                # Brief wait so the cancel propagates before loop.stop().
                self._listener_future.result(timeout=1.0)
            except Exception:  # noqa: BLE001 — cancellation is expected
                pass
            self._listener_future = None

        # Stop the bus watcher, if one was started, before the async loop
        # goes away, and drop the pending-bus registry (recovered on next
        # start() via BusSurfaceService._recover_undecided_pendings).
        self._bus_service.stop()

        # Stop the idle-AFK monitor, if running.
        self._afk_service.stop()

        # Give the async thread a chance to let a Transport.close() coroutine
        # run (e.g., TelegramTransport stops its Application's polling).
        # Budget: 4s here + 1s (listener) + 2s (bus_watcher) + 2s (idle_monitor)
        # = 9s total, safely within Docker's 10s SIGKILL window.
        close = getattr(self.transport, "close", None)
        if close is not None and asyncio.iscoroutinefunction(close):
            try:
                fut = self._async.submit(close())
                fut.result(timeout=4.0)
            except Exception:  # noqa: BLE001
                _log.debug("transport.close() failed during shutdown", exc_info=True)

        self._async.stop()
        # Endpoint file was unlinked up-front (right after HTTP server
        # shutdown) so users don't wait on the async teardown above.

    @property
    def port(self) -> int:
        if self._server is None:
            return 0
        return self._server.server_address[1]

    # ----- route handlers (called by the HTTP handler) --------------------

    def handle_approval(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._approval_service.handle_approval(body)

    # ce-refresh-token 1.2: runner-free CE web-auth surface. Thin delegators to
    # the pure response mappers in ce_web_auth; self.ce_web_auth is None (404)
    # when the otaman-core[web-auth] extra is absent or local_auth is unconfigured.
    def handle_ce_login(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        from otaman_bridge.ce_web_auth import login_response

        return login_response(self.ce_web_auth, body)

    def handle_ce_refresh(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        from otaman_bridge.ce_web_auth import refresh_response

        return refresh_response(self.ce_web_auth, body)

    def handle_ce_attach_token(
        self, auth_header: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        from otaman_bridge.ce_web_auth import attach_response

        return attach_response(self.ce_web_auth, auth_header, body)

    def handle_notify(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            msg = InfoMessage.from_dict(body)
        except (TypeError, ValueError) as e:
            return 400, {"error": f"invalid InfoMessage: {e}"}
        # Fire-and-forget: schedule but don't wait.
        self._async.submit(self.transport.send_info(msg))
        return 202, {"queued": True}

    def handle_reply(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Deliver a decision to a pending approval (internal route).

        Used by test harnesses and future transport listener loops that
        translate inbound user actions into approval decisions.
        """
        request_id = body.get("request_id")
        if not request_id:
            return 400, {"error": "request_id required"}

        try:
            response = ApprovalResponse.from_dict(body)
        except (TypeError, ValueError) as e:
            return 400, {"error": f"invalid ApprovalResponse: {e}"}

        if not self._approval_service.resolve(request_id, response):
            return 404, {"error": "no pending approval with that request_id"}
        return 200, {"resolved": True}

    async def _listener_loop(self) -> None:
        """Consume Transport.listen() and dispatch each reply.

        Runs in the daemon's async loop thread. Cancelled on stop().
        Errors are logged but do not break the loop — a flaky transport
        shouldn't crash the daemon.
        """
        try:
            async for reply in self.transport.listen():
                try:
                    self._dispatch_inbound_reply(reply)
                except Exception:  # noqa: BLE001
                    _log.exception("listener: dispatch failed for %r", reply)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _log.exception("listener loop crashed; stopping")

    def _dispatch_inbound_reply(self, reply: InboundReply) -> None:
        """Translate an InboundReply from the transport into an action.

        Decision actions (approve/reject) resolve the pending approval.
        View actions (details) send a follow-up info message with the
        full payload but leave the approval pending — the user still
        taps approve/reject afterwards. Snooze / comment are logged
        but not yet implemented (follow-up work).

        Bus spec-change-requests share the inbound action vocabulary
        but resolve via a different path (write ack + broadcast file).
        We check the bus registry first so a matching request_id
        shortcuts into ``BusSurfaceService.dispatch``.
        """
        if reply.action == "details":
            self._surface_details(reply)
            return

        if reply.action == "snooze":
            self._handle_snooze(reply)
            return

        # Bus spec-change-request? Route to the bus decision handler.
        if self._bus_service.dispatch(reply):
            return

        decision = _ACTION_TO_DECISION.get(reply.action)
        if decision is None:
            # comment / unknown — not a decision, not a view action.
            _log.info(
                "inbound: non-decision action %r for request_id=%s (ignored)",
                reply.action,
                reply.request_id,
            )
            return

        resolved = self._approval_service.resolve(
            reply.request_id,
            ApprovalResponse(
                decision=decision,  # type: ignore[arg-type]
                request_id=reply.request_id,
                responder=reply.responder,
                message=reply.comment,
            ),
        )
        if not resolved:
            _log.info(
                "inbound: no pending approval for request_id=%s (already resolved?)",
                reply.request_id,
            )

    def _surface_details(self, reply: InboundReply) -> None:
        """Post a follow-up info message with the full tool payload.

        Triggered by a "Details" tap on the approval card. Shows the
        full ``tool_input`` (pretty-printed) in the same forum topic,
        without resolving the pending approval — the user still taps
        approve/reject after reading. If the approval is already gone
        (e.g. tapped Approve then Details immediately after), log a
        brief notice and skip.
        """
        pending = self._approval_service.get(reply.request_id)
        bus_pending = self._bus_service.get(reply.request_id)
        if pending is None and bus_pending is None:
            _log.info(
                "details: no pending approval for %s (already resolved?)",
                reply.request_id,
            )
            return

        # Prefer the tool-call pending (has richer repo/agent framing);
        # fall back to the bus one (same shape, just sourced differently).
        req = pending.request if pending is not None else bus_pending.request  # type: ignore[union-attr]
        body_lines = [
            f"Tool: {req.tool_name}",
            f"Agent: {req.agent}",
            f"Repo: {req.repo}",
        ]
        if req.reason:
            body_lines.append(f"Reason: {req.reason}")
        # Pretty-print tool_input with no truncation. Fenced so Telegram
        # monospace-formats it.
        try:
            payload = json.dumps(req.tool_input, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = repr(req.tool_input)
        body_lines.append("")
        body_lines.append("Input:")
        body_lines.append("```")
        # Guard against Telegram's 4096-char message limit.
        if len(payload) > 3500:
            payload = payload[:3500] + "\n… (truncated to fit Telegram limit)"
        body_lines.append(payload)
        body_lines.append("```")

        from otaman_bridge.core import InfoMessage

        info = InfoMessage(
            account=req.account,
            project=req.project,
            severity="info",
            title=f"Details · {req.tool_name} · {req.repo}",
            body="\n".join(body_lines),
            source_agent=req.agent,
            bus_message_id=req.request_id,
        )
        try:
            self._async.submit(self.transport.send_info(info))
        except Exception:  # noqa: BLE001
            _log.exception("details: failed to schedule send_info")

    def _handle_snooze(self, reply: InboundReply) -> None:
        """Defer an approval by ``SNOOZE_SECONDS`` and re-post a fresh card.

        Reads the module-level ``SNOOZE_SECONDS`` at call time (not
        import time) so tests can monkeypatch it down for speed.
        """
        self._approval_service.handle_snooze(
            reply.request_id,
            snooze_seconds=SNOOZE_SECONDS,
        )

    def handle_status(self) -> tuple[int, dict[str, Any]]:
        payload: dict[str, Any] = {
            "account": self.account,
            "transport": self.transport.name,
            "pid": self.pid,
            "port": self.port,
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "pending_approvals": self._approval_service.count(),
        }
        # ce-ee-release-channels 3.1: edition identity + probe-gated
        # capability, plus the honesty diagnostic on file/probe mismatch.
        payload.update(edition_status())
        return 200, payload

    def handle_shutdown(self) -> tuple[int, dict[str, Any]]:
        # Schedule shutdown slightly later so this response can flush first.
        threading.Thread(target=self.stop, name="bridge-stop", daemon=True).start()
        return 200, {"stopping": True}

    def handle_healthz(self) -> tuple[int, dict[str, Any]]:
        """Docker/compose healthcheck endpoint — no auth required.

        200 → bridge is running and accepting requests.
        503 → shutdown in progress (container orchestrator should stop routing).
        """
        if self._shutdown_requested.is_set():
            return 503, {"ok": False, "reason": "shutdown in progress"}
        if self._server is None:
            return 503, {"ok": False, "reason": "http server not started"}
        payload: dict[str, Any] = {
            "ok": True,
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "transport": self.transport.name,
        }
        # ADR-012 gate 2: monitoring must see experimental_multi_tenant mode
        # without parsing logs. Empty dict in normal single mode.
        payload.update(healthz_extras(self.bus_watcher_root))
        return 200, payload


# ---------------------------------------------------------------------------
# Client helpers — tests + `otaman bridge` CLI call these.


def daemon_url(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}"


def _endpoint_is_live(endpoint_data: dict[str, Any], timeout: float = 1.0) -> bool:
    """Check whether the daemon described by ``endpoint_data`` is reachable.

    Connection refused / no route / timeout → daemon is gone (stale file).
    Any HTTP response (even 4xx) → daemon is up.
    Used by BridgeDaemon.start() to auto-clean stale endpoint files.
    """
    port = endpoint_data.get("port")
    if not port:
        return False
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/status",
            timeout=timeout,
        )
        return True
    except urllib.error.HTTPError:
        # A real HTTP error means something IS listening — treat as live.
        return True
    except (urllib.error.URLError, OSError):
        return False


def _check_port_free(host: str, port: int) -> bool:
    """Debug helper — true if we can bind to this port right now."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
            return True
    except OSError:
        return False
