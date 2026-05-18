"""Bridge daemon — HTTP on 127.0.0.1:<ephemeral> with bearer-token auth.

Design rationale (§5.3 / §9):
- Loopback HTTP everywhere (not unix sockets / named pipes) so one
  implementation works on Linux, macOS, WSL, and Windows, and a future
  local UI reuses the same endpoint.
- Bearer token stored in ``~/.maestro/bridge-<account>.endpoint`` (mode
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
which returns sanitized info without auth (intentional: lets `maestro bridge
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from otaman_bridge.bus_decision import (
    record_decision,
    write_acknowledge,
    write_reply_message,
)
from otaman_bridge.bus_surface import BusMessage
from otaman_bridge.bus_watcher import BusWatcher
from otaman_bridge.idle_afk import IdleAFKMonitor
from otaman_bridge.core import (
    ApprovalRequest,
    ApprovalResponse,
    InboundReply,
    InfoMessage,
    Transport,
    TransportHandle,
)

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

_log = logging.getLogger("maestro.bridge.daemon")


# ---------------------------------------------------------------------------
# Endpoint file


def endpoint_path(account: str, *, home: Path | None = None) -> Path:
    """Standard location for the endpoint file."""
    base = (home or Path.home()) / ".maestro"
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


# ---------------------------------------------------------------------------
# Pending approvals registry


class _PendingApproval:
    """Thread-safe slot for a waiting hook.

    The deadline is a monotonic timestamp rather than the raw duration
    passed into ``wait()`` so that Snooze can push it out while the
    hook is still blocked in ``wait()``. The original ``timeout`` arg
    is retained for backwards compatibility but ignored in favor of
    ``_deadline``.
    """

    __slots__ = ("event", "response", "request", "handle", "_deadline")

    def __init__(self, request: ApprovalRequest):
        self.event = threading.Event()
        self.response: ApprovalResponse | None = None
        self.request = request
        # TransportHandle returned by Transport.send_approval — stored so
        # inbound replies can edit the original message after the decision.
        self.handle: TransportHandle | None = None
        self._deadline = time.monotonic() + request.timeout_seconds

    def resolve(self, response: ApprovalResponse) -> None:
        self.response = response
        self.event.set()

    def extend_by(self, seconds: float) -> None:
        """Push the deadline to at least ``now + seconds`` (never shortens it)."""
        new_deadline = time.monotonic() + seconds
        if new_deadline > self._deadline:
            self._deadline = new_deadline

    def wait(self, timeout: float) -> ApprovalResponse:  # noqa: ARG002
        """Block until resolved or the deadline passes.

        Polls in small chunks (≤5s) so deadline extensions made by
        Snooze after ``wait()`` starts are picked up.
        """
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return ApprovalResponse(
                    decision="timeout",
                    request_id=self.request.request_id,
                )
            if self.event.wait(timeout=min(remaining, 5.0)):
                assert self.response is not None
                return self.response


class _PendingBusDecision:
    """Holds a bus spec-change-request between ``send_approval`` and the
    button tap that resolves it.

    Unlike ``_PendingApproval`` there's no thread blocked on a reply —
    the originating agent's proposal already sits on disk. We just
    remember enough context (the BusMessage + card handle) to write the
    ack + broadcast when the decision arrives, and to edit the card.
    """

    __slots__ = ("request", "msg", "handle", "project_root", "created_at")

    def __init__(
        self,
        request: ApprovalRequest,
        msg: BusMessage,
        project_root: Path,
    ):
        self.request = request
        self.msg = msg
        self.project_root = project_root
        self.handle: TransportHandle | None = None
        self.created_at = time.monotonic()


# ---------------------------------------------------------------------------
# Async loop helper — runs in a dedicated thread so sync HTTP handlers
# can submit coroutines via run_coroutine_threadsafe.


class _AsyncLoopThread:
    """Background asyncio event loop, shut down cleanly on ``stop()``."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="bridge-asyncio", daemon=True,
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
            bool(issuer), bool(client_id), bool(redirect_uri),
        )
        return None
    from otaman_bridge.web_auth import LoginFlow, PendingLoginStore, WebAuthConfig
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
        # messages via the transport. bus_watcher_root is the maestro
        # folder to watch; bus_watcher_project is the project name for
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

        # Optional OIDC validator built from env. When unset, daemon
        # serves loopback-bearer only (Mode 1 / local-trust pattern).
        self.oidc_validator = _build_oidc_validator_from_env()
        # Optional DCR shim (mcp-oauth wave chunk D3+). When enabled the
        # daemon serves AS metadata overlay routes pointing at itself,
        # so MCP clients (Claude Code) can do RFC 7591 against Zitadel
        # which lacks native DCR. None = inert.
        from otaman_bridge.dcr_shim import IdpConfig, MetadataCache
        self.idp_config = IdpConfig.from_env()
        self._idp_metadata_cache = (
            MetadataCache(ttl_seconds=self.idp_config.metadata_cache_seconds)
            if self.idp_config is not None
            else None
        )
        if self.idp_config is not None:
            _log.info(
                "DCR shim enabled (type=%s mgmt=%s trust=%s cache=%ds)",
                self.idp_config.type,
                self.idp_config.management_base_url,
                self.idp_config.registration_trust,
                self.idp_config.metadata_cache_seconds,
            )
        # Optional web-login flow (Authorization Code + PKCE). Built from
        # OIDC_AUDIENCE_BRIDGE + OIDC_BRIDGE_REDIRECT_URI. None disables
        # /auth/login (returns 503).
        _web_login = _build_web_login_flow_from_env()
        self.web_login_flow = _web_login[0] if _web_login is not None else None
        # Web-login support stack -- only built when web_login_flow is.
        # Tests inject stubs onto these attributes after construction.
        self.session_store = None
        self.session_cookie = None
        self.login_completer = None
        if self.web_login_flow is not None:
            from otaman_bridge.web_auth import LoginCompleter, TokenExchanger
            from otaman_bridge.web_session import SessionCookie, SessionStore
            self.session_store = SessionStore()
            # Cookie Secure flag derived from the registered redirect_uri
            # scheme (https -> Secure, http -> not). Production always
            # uses https; local dev / e2e harness can use http.
            redirect_https = self.web_login_flow.config.redirect_uri.startswith("https://")
            self.session_cookie = SessionCookie(secure=redirect_https)
            self.login_completer = LoginCompleter(
                token_exchanger=TokenExchanger(self.web_login_flow.config),
                validator=self.oidc_validator,
                session_store=self.session_store,
                pending_store=self.web_login_flow.store,
            )

        # MCP server: tool registry for the team-mode v0 cross-user
        # visibility flow. Always built (even when web_login_flow is
        # None) -- some tools may not need web auth. Privacy mode is
        # configurable via env (default emails for trusted teams).
        from otaman_bridge.mcp_server import MCPServer
        from otaman_bridge.mcp_tools import (
            PRIVACY_EMAILS,
            PRIVACY_OPAQUE,
            build_list_team_sessions_tool,
        )
        from otaman_bridge.runner_client import RunnerClient
        self.mcp_server = MCPServer()
        self._runner_client = RunnerClient()
        privacy = os.environ.get("OTAMAN_BRIDGE_PRIVACY_MODE", PRIVACY_EMAILS).strip()
        if privacy not in (PRIVACY_EMAILS, PRIVACY_OPAQUE):
            _log.warning(
                "invalid OTAMAN_BRIDGE_PRIVACY_MODE=%r, using emails", privacy,
            )
            privacy = PRIVACY_EMAILS
        # Only register list_team_sessions when session_store exists --
        # the tool's email lookup depends on it. If web auth is
        # disabled, the tool falls back to opaque (no email source).
        if self.session_store is not None:
            self.mcp_server.register(build_list_team_sessions_tool(
                runner_client=self._runner_client,
                session_store=self.session_store,
                privacy_mode=privacy,
            ))
            _log.info("MCP: list_team_sessions registered (privacy=%s)", privacy)
        # Messaging tools (v0+): send_message_to_user / check_messages /
        # mark_message_read. Inbox storage under ~/.otaman/inboxes/ by
        # default; override via OTAMAN_BRIDGE_INBOX_ROOT env var. These
        # tools work without web auth (they read ctx.user_id from any
        # of the three auth paths; loopback bearer is rejected at handler).

        from otaman_bridge.inbox import Inbox
        from otaman_bridge.mcp_tools import (
            build_check_messages_tool,
            build_mark_message_read_tool,
            build_request_review_tool,
            build_send_message_to_user_tool,
        )
        inbox_root = os.environ.get("OTAMAN_BRIDGE_INBOX_ROOT", "").strip()
        self.inbox = Inbox(root=Path(inbox_root)) if inbox_root else Inbox()
        self.mcp_server.register(build_send_message_to_user_tool(
            inbox=self.inbox, session_store=self.session_store,
        ))
        self.mcp_server.register(build_check_messages_tool(inbox=self.inbox))
        self.mcp_server.register(build_mark_message_read_tool(inbox=self.inbox))
        self.mcp_server.register(build_request_review_tool(
            inbox=self.inbox, session_store=self.session_store,
        ))
        _log.info("MCP: messaging tools registered (inbox=%s)", self.inbox.root)

        self._pending: dict[str, _PendingApproval] = {}
        # Parallel registry for bus spec-change-requests waiting on an
        # Approve/Reject tap. Keyed by request_id (= bus message stem).
        # Unlike _pending, these don't block a caller — the daemon just
        # holds the BusMessage + handle until the decision comes in,
        # then writes the ack + broadcast.
        self._pending_bus: dict[str, _PendingBusDecision] = {}
        self._pending_lock = threading.Lock()
        self._async = _AsyncLoopThread()
        self._server: ThreadingHTTPServer | None = None
        self._serve_thread: threading.Thread | None = None
        self._shutdown_requested = threading.Event()
        self._listener_future = None  # concurrent.futures.Future, set on start()
        self._bus_watcher: BusWatcher | None = None
        self._bus_watcher_future = None  # concurrent.futures.Future
        self._idle_monitor: IdleAFKMonitor | None = None
        self._idle_monitor_future = None  # concurrent.futures.Future

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
                    f"(run `maestro bridge stop --account {self.account}` first)"
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
        if self.bus_watcher_root is not None:
            # Project name defaults to platform.yaml's `project:` field so
            # bus-watcher-surfaced messages land in the same Telegram topic
            # as PreToolUse approvals. Falls back to the folder name only
            # when no platform.yaml / no project key is present.
            from otaman_bridge.bus_surface import resolve_project_name  # noqa: PLC0415
            project_name = (
                self.bus_watcher_project
                or resolve_project_name(self.bus_watcher_root)
            )
            self._bus_watcher = BusWatcher(
                project_root=self.bus_watcher_root,
                account=self.account,
                project=project_name,
                on_info=self._bus_on_info,
                on_approval=self._bus_on_approval,
            )
            self._bus_watcher_future = self._async.submit(self._bus_watcher.run())
            _log.info(
                "bus watcher started for %s (project=%s)",
                self.bus_watcher_root, project_name,
            )

        # Idle-auto-AFK monitor: enabled when idle_auto_afk_minutes > 0
        # AND a maestro root is configured (shares bus_watcher_root since
        # last-user-activity lives in the same .maestro/ directory).
        if (self.idle_auto_afk_minutes > 0
                and self.bus_watcher_root is not None):
            from otaman_bridge.bus_surface import resolve_project_name  # noqa: PLC0415
            idle_project = (
                self.bus_watcher_project
                or resolve_project_name(self.bus_watcher_root)
            )
            self._idle_monitor = IdleAFKMonitor(
                project_root=self.bus_watcher_root,
                idle_minutes=self.idle_auto_afk_minutes,
                on_enabled=self._make_idle_notifier(
                    account=self.account, project=idle_project,
                    title="🌙 AFK auto-enabled",
                    body_template=(
                        "Idle auto-AFK triggered: {reason}.\n\n"
                        "Approvals will route here until you return. "
                        "Send a prompt in Claude to clear it."
                    ),
                ),
                on_cleared=self._make_idle_notifier(
                    account=self.account, project=idle_project,
                    title="☀️ AFK cleared",
                    body_template="User activity resumed — back to local prompts.",
                    include_reason=False,
                ),
            )
            self._idle_monitor_future = self._async.submit(self._idle_monitor.run())
            _log.info(
                "idle-afk monitor started (threshold=%d min)",
                self.idle_auto_afk_minutes,
            )

        # DCR shim cleanup sweep (D6). Background task that periodically
        # prunes shim-managed apps older than ``cleanup_ttl_seconds``. Off
        # when shim disabled or sweep_interval=0 (manual cleanup via the
        # `maestro bridge dcr-cleanup` CLI command still works).
        self._dcr_sweep_future = None
        if (
            self.idp_config is not None
            and self.idp_config.dcr_shim
            and self.idp_config.cleanup_sweep_interval_seconds > 0
        ):
            self._dcr_sweep_future = self._async.submit(self._dcr_cleanup_sweep_loop())

        _log.info(
            "bridge daemon listening on %s:%d (account=%s, transport=%s)",
            self.host, assigned_port, self.account, self.transport.name,
        )

    # ----- idle-afk notifications ----------------------------------------

    def _make_idle_notifier(
        self, *, account: str, project: str,
        title: str, body_template: str, include_reason: bool = True,
    ):
        """Build an async callback that sends a Telegram InfoMessage when
        the IdleAFKMonitor flips AFK on or clears it.

        Without these notifications the user would see approvals route to
        their phone without warning ("why is my laptop silent?"); one
        message per transition keeps expectations calibrated.
        """
        transport = self.transport

        async def notify(reason: str = "") -> None:
            body = body_template.format(reason=reason) if include_reason \
                else body_template
            info = InfoMessage(
                account=account,
                project=project,
                severity="info",
                title=title,
                body=body,
                source_agent="bridge-daemon",
                bus_message_id="",
            )
            try:
                await transport.send_info(info)
            except Exception:  # noqa: BLE001
                _log.exception("idle-afk: failed to send notification")

        return notify

    # ----- bus watcher callbacks (T2d-2: info-only) -----------------------

    async def _bus_on_info(self, info: InfoMessage) -> None:
        """Forward a non-interactive bus message to the transport."""
        await self.transport.send_info(info)

    async def _bus_on_approval(
        self, req: ApprovalRequest, msg: BusMessage,
    ) -> None:
        """Surface an interactive bus spec-change-request to Telegram.

        Registers a ``_PendingBusDecision`` keyed by the bus message
        stem (= ``req.request_id``) so that when the user taps
        Approve / Reject, the listener dispatch can find the original
        BusMessage and write the ack + broadcast. The approval card
        gets Approve/Reject/Details buttons via the standard
        ``transport.send_approval`` path.
        """
        if self.bus_watcher_root is None:
            # Shouldn't happen — watcher is only started when root is set.
            _log.warning(
                "bus approval for %s but no bus_watcher_root configured",
                req.request_id,
            )
            return

        pending = _PendingBusDecision(req, msg, self.bus_watcher_root)
        with self._pending_lock:
            self._pending_bus[req.request_id] = pending

        try:
            handle = await self.transport.send_approval(req)
            pending.handle = handle
        except Exception:  # noqa: BLE001
            _log.exception(
                "bus approval: send_approval failed for %s", req.request_id,
            )
            # Drop from registry so the watcher's retry-on-fail path
            # can re-surface on the next scan (state file wasn't
            # written because this callback raises back up to the
            # watcher's dispatch guard).
            with self._pending_lock:
                self._pending_bus.pop(req.request_id, None)
            raise

    def stop(self) -> None:
        """Graceful shutdown — remove endpoint file, cancel pending approvals.

        Ordering matters: we delete the endpoint file *before* the slow
        async teardown so ``maestro bridge stop`` — which polls the
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
        with self._pending_lock:
            for pending in self._pending.values():
                pending.resolve(ApprovalResponse(
                    decision="ask",  # fail-safe: let Claude's native prompt show
                    request_id=pending.request.request_id,
                    responder="daemon:shutdown",
                    message="bridge daemon shutting down",
                ))
            self._pending.clear()
            # Bus pendings don't block anything — just drop them. They'll
            # re-surface on next daemon start because the state file
            # dedup is in-memory only within a single watcher instance
            # (fresh daemon reads state from disk but bus messages that
            # were surfaced but un-decided stay idempotent: ack absent,
            # watcher's state says "already surfaced" — user taps land
            # empty until `/maestro:approve` locally resolves them).
            self._pending_bus.clear()

        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

        # Drop the endpoint file NOW so `maestro bridge stop` returns
        # promptly. The remaining async-loop cancellation below happens
        # in the background; a concurrent `maestro bridge run` is free
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
        # goes away. stop() flips an asyncio.Event; the future then exits
        # via its normal path and we cancel as a backstop.
        if self._bus_watcher is not None:
            self._bus_watcher.stop()
        if self._bus_watcher_future is not None:
            self._bus_watcher_future.cancel()
            try:
                self._bus_watcher_future.result(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            self._bus_watcher_future = None
            self._bus_watcher = None

        # Stop the idle-AFK monitor, if running. Same pattern as the bus
        # watcher — event-driven graceful stop, future.cancel() as backstop.
        if self._idle_monitor is not None:
            self._idle_monitor.stop()
        if self._idle_monitor_future is not None:
            self._idle_monitor_future.cancel()
            try:
                self._idle_monitor_future.result(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            self._idle_monitor_future = None
            self._idle_monitor = None

        # Give the async thread a chance to let a Transport.close() coroutine
        # run (e.g., TelegramTransport stops its Application's polling).
        close = getattr(self.transport, "close", None)
        if close is not None and asyncio.iscoroutinefunction(close):
            try:
                fut = self._async.submit(close())
                fut.result(timeout=5.0)
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
        try:
            req = ApprovalRequest.from_dict(body)
        except (TypeError, ValueError) as e:
            return 400, {"error": f"invalid ApprovalRequest: {e}"}

        pending = _PendingApproval(req)
        with self._pending_lock:
            self._pending[req.request_id] = pending

        # Schedule the transport's send_approval on the async loop and
        # capture the TransportHandle for later update() calls.
        try:
            fut = self._async.submit(self.transport.send_approval(req))
            handle = fut.result(timeout=10.0)
            if isinstance(handle, TransportHandle):
                pending.handle = handle
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(req.request_id, None)
            _log.exception("transport.send_approval failed")
            # Fail-safe: return "ask" so the native terminal prompt takes over.
            return 200, ApprovalResponse(
                decision="ask",
                request_id=req.request_id,
                responder="daemon:send-failed",
                message=str(e),
            ).to_dict()

        try:
            response = pending.wait(timeout=req.timeout_seconds)
        finally:
            with self._pending_lock:
                self._pending.pop(req.request_id, None)

        # Let the transport update the original message (strip buttons,
        # append final status). Best-effort — failures are non-fatal.
        if pending.handle is not None and response.decision in ("allow", "deny", "timeout"):
            status_text = {
                "allow": f"✓ approved by {response.responder or 'user'}",
                "deny": f"✗ rejected by {response.responder or 'user'}",
                "timeout": "⏱️ expired",
            }.get(response.decision, response.decision)
            try:
                self._async.submit(self.transport.update(pending.handle, status_text))
            except Exception:
                _log.debug("transport.update scheduling failed", exc_info=True)

        return 200, response.to_dict()

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

        with self._pending_lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return 404, {"error": "no pending approval with that request_id"}

        pending.resolve(response)
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
        shortcuts into ``_dispatch_bus_decision``.
        """
        if reply.action == "details":
            self._surface_details(reply)
            return

        if reply.action == "snooze":
            self._handle_snooze(reply)
            return

        # Bus spec-change-request? Route to the bus decision handler.
        with self._pending_lock:
            bus_pending = self._pending_bus.get(reply.request_id)
        if bus_pending is not None:
            self._dispatch_bus_decision(reply, bus_pending)
            return

        decision = _ACTION_TO_DECISION.get(reply.action)
        if decision is None:
            # comment / unknown — not a decision, not a view action.
            _log.info(
                "inbound: non-decision action %r for request_id=%s (ignored)",
                reply.action, reply.request_id,
            )
            return

        with self._pending_lock:
            pending = self._pending.get(reply.request_id)
        if pending is None:
            _log.info(
                "inbound: no pending approval for request_id=%s (already resolved?)",
                reply.request_id,
            )
            return

        pending.resolve(ApprovalResponse(
            decision=decision,  # type: ignore[arg-type]
            request_id=reply.request_id,
            responder=reply.responder,
            message=reply.comment,
        ))

    def _dispatch_bus_decision(
        self, reply: InboundReply, pending: _PendingBusDecision,
    ) -> None:
        """Resolve a bus spec-change-request tap.

        Approve → ``approved`` ack + ``spec-change-approved`` broadcast.
        Reject  → ``rejected`` ack + ``spec-change-rejected`` to proposer.
        Details → dump full message body (payload is already in hand,
            no follow-up surfacing needed).
        Other actions are ignored — the card stays as-is, user can try
        again.
        """
        action = reply.action
        if action == "details":
            # Bus messages already carry the full body in tool_input; the
            # generic _surface_details path handles that.
            self._surface_details(reply)
            return

        if action == "comment":
            # Free-text reply to a bus card. Writes an info message
            # from human to the original proposer. Decision stays
            # pending — user may follow up with Approve/Reject.
            self._record_bus_comment(reply, pending)
            return

        if action == "acknowledge":
            # "to: human" messages (design §5.6) get Acknowledge
            # instead of Approve/Reject. We write the ack file +
            # optional reply, then clear the registry.
            self._record_bus_acknowledge(reply, pending)
            return

        decision_map = {"approve": "approved", "reject": "rejected"}
        decision = decision_map.get(action)
        if decision is None:
            _log.info(
                "bus decision: non-decision action %r for %s (ignored)",
                action, reply.request_id,
            )
            return

        # For non-SCR bus cards (e.g., `to: human` messages), Approve
        # means "acknowledged" — not a spec-change-approval broadcast.
        # Only spec-change-request types route through record_decision.
        if pending.msg.type != "spec-change-request":
            self._record_bus_acknowledge(reply, pending)
            return

        try:
            ack_path, broadcast_path = record_decision(
                pending.project_root,
                pending.msg,
                decision=decision,
                responder=reply.responder,
                comment=reply.comment or "",
            )
            _log.info(
                "bus decision: %s for %s → %s + %s",
                decision, pending.msg.stem,
                ack_path.name, broadcast_path.name,
            )
        except Exception:  # noqa: BLE001
            _log.exception(
                "bus decision: record_decision failed for %s",
                pending.msg.stem,
            )
            # Leave in registry so the user can retry tapping.
            return

        # Clear the pending slot so a second tap is a no-op.
        with self._pending_lock:
            self._pending_bus.pop(reply.request_id, None)

        # Edit the card to show the result so the user can't tap again.
        if pending.handle is not None:
            status_text = {
                "approved": f"✓ approved by {reply.responder or 'user'}",
                "rejected": f"✗ rejected by {reply.responder or 'user'}",
            }.get(decision, decision)
            try:
                self._async.submit(
                    self.transport.update(pending.handle, status_text)
                )
            except Exception:  # noqa: BLE001
                _log.debug("bus decision: transport.update failed", exc_info=True)

    def _record_bus_comment(
        self, reply: InboundReply, pending: _PendingBusDecision,
    ) -> None:
        """Write a free-text reply bus message for a card that stays pending.

        For spec-change-requests, a comment is supplementary — the
        Approve/Reject decision is still open. We DON'T clear the
        registry here; the user may tap a decision button after.
        """
        text = (reply.comment or "").strip()
        if not text:
            _log.info(
                "bus comment: empty reply for %s (ignored)",
                pending.msg.stem,
            )
            return
        try:
            reply_path = write_reply_message(
                pending.project_root, pending.msg,
                text=text, responder=reply.responder,
            )
            _log.info(
                "bus comment: wrote %s (in_reply_to=%s)",
                reply_path.name, pending.msg.stem,
            )
        except Exception:  # noqa: BLE001
            _log.exception(
                "bus comment: write_reply_message failed for %s",
                pending.msg.stem,
            )

    def _record_bus_acknowledge(
        self, reply: InboundReply, pending: _PendingBusDecision,
    ) -> None:
        """Record an Acknowledge tap on a ``to: human`` card.

        Writes the ack file + optional reply, clears the pending
        slot, and edits the card to confirm.
        """
        try:
            ack_path, reply_path = write_acknowledge(
                pending.project_root, pending.msg,
                responder=reply.responder, comment=reply.comment or "",
            )
            _log.info(
                "bus ack: wrote %s for %s%s",
                ack_path.name, pending.msg.stem,
                f" + reply {reply_path.name}" if reply_path else "",
            )
        except Exception:  # noqa: BLE001
            _log.exception(
                "bus ack: write_acknowledge failed for %s", pending.msg.stem,
            )
            return

        with self._pending_lock:
            self._pending_bus.pop(reply.request_id, None)

        if pending.handle is not None:
            try:
                self._async.submit(
                    self.transport.update(
                        pending.handle,
                        f"👍 acknowledged by {reply.responder or 'user'}",
                    )
                )
            except Exception:  # noqa: BLE001
                _log.debug("bus ack: transport.update failed", exc_info=True)

    def _surface_details(self, reply: InboundReply) -> None:
        """Post a follow-up info message with the full tool payload.

        Triggered by a "Details" tap on the approval card. Shows the
        full ``tool_input`` (pretty-printed) in the same forum topic,
        without resolving the pending approval — the user still taps
        approve/reject after reading. If the approval is already gone
        (e.g. tapped Approve then Details immediately after), log a
        brief notice and skip.
        """
        with self._pending_lock:
            pending = self._pending.get(reply.request_id)
            bus_pending = self._pending_bus.get(reply.request_id)
        if pending is None and bus_pending is None:
            _log.info(
                "details: no pending approval for %s (already resolved?)",
                reply.request_id,
            )
            return

        # Prefer the tool-call pending (has richer repo/agent framing);
        # fall back to the bus one (same shape, just sourced differently).
        req = (pending.request if pending is not None else bus_pending.request)  # type: ignore[union-attr]
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
        """Defer an approval by SNOOZE_SECONDS and re-post a fresh card.

        1. Extend the pending approval's deadline so the hook doesn't
           time out during the snooze window (adds a 30s buffer over
           SNOOZE_SECONDS).
        2. Edit the original card to strip buttons + show "snoozed
           until HH:MM" so the stale card can't be tapped again.
        3. Schedule a coroutine that sleeps SNOOZE_SECONDS then calls
           ``transport.send_approval`` again (unless the pending
           approval has been resolved in the meantime). The new handle
           replaces the stored one so any subsequent ``update()`` /
           ``details`` goes to the re-posted card.
        """
        with self._pending_lock:
            pending = self._pending.get(reply.request_id)
        if pending is None:
            _log.info(
                "snooze: no pending approval for %s (already resolved?)",
                reply.request_id,
            )
            return

        snooze_seconds = SNOOZE_SECONDS
        pending.extend_by(snooze_seconds + 30)

        # Edit the original card — strip buttons, show the snooze wall-clock.
        if pending.handle is not None:
            snooze_clock = (datetime.now() + timedelta(seconds=snooze_seconds)).strftime("%H:%M")
            try:
                self._async.submit(
                    self.transport.update(
                        pending.handle,
                        f"⏱️ snoozed — re-posting at ~{snooze_clock}",
                    )
                )
            except Exception:  # noqa: BLE001
                _log.debug("snooze: transport.update failed", exc_info=True)

        # Schedule the re-post in the async loop.
        try:
            self._async.submit(self._snooze_repost(reply.request_id, snooze_seconds))
        except Exception:  # noqa: BLE001
            _log.exception("snooze: failed to schedule re-post")

    async def _snooze_repost(self, request_id: str, after_seconds: float) -> None:
        """Sleep, then re-send the approval card if it's still pending."""
        try:
            await asyncio.sleep(after_seconds)
        except asyncio.CancelledError:
            return  # daemon shutting down

        with self._pending_lock:
            pending = self._pending.get(request_id)
        if pending is None:
            _log.info("snooze: %s resolved during snooze; skipping re-post", request_id)
            return

        try:
            new_handle = await self.transport.send_approval(pending.request)
        except Exception:  # noqa: BLE001
            _log.exception("snooze: send_approval re-post failed for %s", request_id)
            return

        with self._pending_lock:
            still_pending = self._pending.get(request_id)
            if still_pending is pending:
                still_pending.handle = new_handle

    async def _dcr_cleanup_sweep_loop(self):
        """Background task that periodically prunes stale shim-managed apps.

        Run when both ``idp_config.dcr_shim`` and ``cleanup_sweep_interval_seconds > 0``.
        Each iteration sleeps for the interval first, then sweeps; this lets
        the daemon finish startup before the first sweep request to Zitadel.
        Failures are logged but never abort the loop.
        """
        from otaman_bridge.dcr_shim import sweep_orphans
        cfg = self.idp_config
        interval = cfg.cleanup_sweep_interval_seconds
        _log.info(
            "DCR shim cleanup loop started "
            "(interval=%ds ttl=%ds prefix=%s project=%s)",
            interval, cfg.cleanup_ttl_seconds, cfg.managed_name_prefix, cfg.project_id,
        )
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                _log.debug("DCR cleanup loop cancelled — daemon shutting down")
                return
            mgmt_client = self.get_or_build_dcr_mgmt_client()
            if mgmt_client is None:
                _log.debug("DCR cleanup skipped — mgmt client unavailable")
                continue
            try:
                report = await asyncio.to_thread(
                    sweep_orphans,
                    mgmt_client=mgmt_client,
                    project_id=cfg.project_id,
                    name_prefix=cfg.managed_name_prefix,
                    ttl_seconds=cfg.cleanup_ttl_seconds,
                )
                if report.deleted or report.failed:
                    _log.info(
                        "DCR sweep: found=%d eligible=%d deleted=%d failed=%d",
                        report.found, report.eligible, report.deleted, report.failed,
                    )
                else:
                    _log.debug("DCR sweep: nothing to delete (found=%d)", report.found)
            except Exception as exc:  # noqa: BLE001
                _log.warning("DCR sweep iteration failed: %s", exc)

    def get_or_build_dcr_mgmt_client(self):
        """Lazy-construct the Zitadel mgmt API client for the DCR shim.

        Returns None when shim is enabled but credentials aren't fully
        populated (route then returns 503 server_error). Tests can
        monkey-patch self._dcr_mgmt_client to inject a stub.
        """
        if getattr(self, "_dcr_mgmt_client_cached", None) is not None:
            return self._dcr_mgmt_client_cached
        if self.idp_config is None or not self.idp_config.dcr_shim:
            return None
        cfg = self.idp_config
        if not (cfg.machine_user_client_id and cfg.machine_user_client_secret and cfg.org_id):
            return None
        from otaman_bridge.dcr_shim import ZitadelMgmtClient
        # token endpoint is the standard OIDC location on the mgmt host.
        token_url = f"{cfg.management_base_url}/oauth/v2/token"
        self._dcr_mgmt_client_cached = ZitadelMgmtClient(
            base_url=cfg.management_base_url,
            token_url=token_url,
            client_id=cfg.machine_user_client_id,
            client_secret=cfg.machine_user_client_secret,
            org_id=cfg.org_id,
            expected_host=cfg.expected_host,
        )
        return self._dcr_mgmt_client_cached

    def handle_status(self) -> tuple[int, dict[str, Any]]:
        with self._pending_lock:
            pending_count = len(self._pending)
        return 200, {
            "account": self.account,
            "transport": self.transport.name,
            "pid": self.pid,
            "port": self.port,
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "pending_approvals": pending_count,
        }

    def handle_shutdown(self) -> tuple[int, dict[str, Any]]:
        # Schedule shutdown slightly later so this response can flush first.
        threading.Thread(target=self.stop, name="bridge-stop", daemon=True).start()
        return 200, {"stopping": True}


# ---------------------------------------------------------------------------
# HTTP handler


def _make_handler(daemon: BridgeDaemon) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to this daemon instance."""

    class Handler(BaseHTTPRequestHandler):
        # Silence default stderr logging; route through module logger instead.
        def log_message(self, fmt: str, *args: Any) -> None:
            _log.debug("%s - - [%s] %s",
                       self.client_address[0],
                       self.log_date_time_string(),
                       fmt % args)

        def _auth_identify(self):
            """Identify the caller across all three auth paths.

            Returns a CallContext for OIDC bearer / session cookie /
            loopback bearer, or None if all paths reject. Mirrors
            _auth_ok's three-way logic but surfaces the user identity
            so MCP tool handlers can attribute the request.
            """
            from otaman_bridge.mcp_server import CallContext
            header = self.headers.get("Authorization", "")
            if daemon.oidc_validator is not None and header.startswith("Bearer "):
                result = daemon.oidc_validator.validate(header)
                if result.ok:
                    return CallContext(
                        user_id=result.user_id or "",
                        user_email=result.email,
                        roles=tuple(result.roles),
                    )
            if daemon.session_store is not None and daemon.session_cookie is not None:
                cookie_header = self.headers.get("Cookie", "")
                sid = daemon.session_cookie.parse(cookie_header)
                if sid is not None:
                    sess = daemon.session_store.get(sid)
                    if sess is not None:
                        return CallContext(
                            user_id=sess.user_id,
                            user_email=sess.email,
                            roles=tuple(sess.roles),
                        )
            if header.startswith("Bearer "):
                supplied = header[len("Bearer "):].strip()
                if _secrets.compare_digest(supplied, daemon.token):
                    # Loopback bearer = same-host CLI; no user identity
                    return CallContext(user_id="", user_email=None, roles=())
            return None

        def _auth_ok(self) -> bool:
            # When OIDC is configured, try it first. Fall back to the
            # loopback bearer for local same-host clients (CLI
            # introspection, `maestro bridge status`, etc.).
            header = self.headers.get("Authorization", "")
            if daemon.oidc_validator is not None and header.startswith("Bearer "):
                result = daemon.oidc_validator.validate(header)
                if result.ok:
                    _log.debug("OIDC auth ok: user_id=%s roles=%s", result.user_id, result.roles)
                    return True
                # OIDC failed; fall through to loopback bearer (don't 401
                # yet — same-host CLI may be using the loopback token).
            # session cookie auth path: try the browser session cookie
            # before falling back to loopback bearer
            if daemon.session_store is not None and daemon.session_cookie is not None:
                cookie_header = self.headers.get("Cookie", "")
                sid = daemon.session_cookie.parse(cookie_header)
                if sid is not None:
                    sess = daemon.session_store.get(sid)
                    if sess is not None:
                        _log.debug("session cookie auth ok: user_id=%s", sess.user_id)
                        return True
                    # Unknown / expired cookie; fall through to loopback
            if not header.startswith("Bearer "):
                return False
            supplied = header[len("Bearer "):].strip()
            return _secrets.compare_digest(supplied, daemon.token)

        def _drain_body(self) -> None:
            """Consume the request body so Windows doesn't RST on close.

            Idempotent: safe to call from error-reply helpers after the
            body has already been read by ``_read_body``. Without the
            flag, a second ``rfile.read(length)`` on a fully-consumed
            stream blocks until the connection timeout.
            """
            if getattr(self, "_body_consumed", False):
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 0:
                self.rfile.read(length)
            self._body_consumed = True

        def _read_body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length == 0:
                self._body_consumed = True
                return {}
            raw = self.rfile.read(length)
            self._body_consumed = True
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        def _reply_json(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def _reply_error(self, status: int, message: str) -> None:
            # Drain any pending body so the client can read our response
            # cleanly instead of hitting a TCP RST on Windows.
            try:
                self._drain_body()
            except OSError:
                pass
            self._reply_json(status, {"error": message})

        def _reply_unauthenticated(
            self,
            *,
            error: str = "invalid_token",
            description: str = "unauthorized",
        ) -> None:
            """Send 401 with a WWW-Authenticate challenge per RFC 6750 + 9728.

            The header lets MCP clients (Claude Code) discover this
            bridge's OIDC issuer and run the auth_code+PKCE flow without
            any preconfigured token. Falls back to a plain 401 without
            the challenge when OIDC isn't configured on this daemon —
            there's no authorization server to point at anyway.
            """
            try:
                self._drain_body()
            except OSError:
                pass
            payload = json.dumps({"error": description}).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            if daemon.oidc_validator is not None:
                rm_url = (
                    f"{_resolve_public_resource_url(self.headers.get('Host', ''))}"
                    f"/.well-known/oauth-protected-resource"
                )
                challenge = f'Bearer resource_metadata="{rm_url}", error="{error}"'
                self.send_header("WWW-Authenticate", challenge)
            self.end_headers()
            self.wfile.write(payload)

        # --- routes -------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802 — stdlib name
            import urllib.parse as _u_parse_post
            route = _u_parse_post.urlparse(self.path).path.rstrip("/")
            if route == "/mcp":
                # MCP JSON-RPC endpoint. Auth via the standard three-path
                # _auth_identify; tools see the caller via CallContext.
                ctx = self._auth_identify()
                if ctx is None:
                    # No auth at all: 401 + WWW-Authenticate so MCP clients
                    # (Claude Code) initiate the OAuth flow against the
                    # issuer named in our protected-resource metadata.
                    self._reply_unauthenticated(
                        error="invalid_token",
                        description="unauthorized",
                    )
                    return
                body = self._read_body()
                if body is None:
                    from otaman_bridge.mcp_server import PARSE_ERROR
                    self._reply_json(200, {
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": PARSE_ERROR, "message": "invalid JSON"},
                    })
                    return
                # Identity-requiring tools (send_message_to_user etc.)
                # called by an identity-less caller (loopback bearer = no
                # user identity) get a 401 + WWW-Authenticate so the
                # client upgrades to an OIDC bearer instead of seeing a
                # tool-level isError that no client can recover from.
                if (
                    isinstance(body, dict)
                    and body.get("method") == "tools/call"
                    and not ctx.user_id
                ):
                    from otaman_bridge.mcp_tools import IDENTITY_REQUIRED_TOOLS
                    tool_name = (body.get("params") or {}).get("name", "")
                    if tool_name in IDENTITY_REQUIRED_TOOLS:
                        self._reply_unauthenticated(
                            error="insufficient_scope",
                            description=(
                                f"tool {tool_name!r} requires authenticated user"
                            ),
                        )
                        return
                response = daemon.mcp_server.handle_request(body, context=ctx)
                # JSON-RPC responses are always HTTP 200 -- errors are in
                # the envelope, not the transport status.
                self._reply_json(200, response)
                return
            if route == "/auth/logout":
                # Unauth: idempotent. Always 204; clears the cookie regardless.
                if daemon.session_store is None or daemon.session_cookie is None:
                    self._reply_error(503, "web login flow not configured")
                    return
                self._drain_body()
                cookie_header = self.headers.get("Cookie", "")
                sid = daemon.session_cookie.parse(cookie_header)
                if sid is not None:
                    daemon.session_store.delete(sid)
                # 302 -> / so the browser auto-navigates and re-renders the
                # landing page (which now shows "Not logged in" because the
                # cookie was cleared by the Set-Cookie header below).
                # 204 would leave the user on the same page visually.
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", daemon.session_cookie.clear_header())
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if route in ("/approval", "/notify", "/reply", "/shutdown"):
                if not self._auth_ok():
                    self._reply_error(401, "invalid or missing bearer token")
                    return
                body = self._read_body()
                if body is None:
                    self._reply_error(400, "invalid JSON body")
                    return
                if route == "/approval":
                    status, resp = daemon.handle_approval(body)
                elif route == "/notify":
                    status, resp = daemon.handle_notify(body)
                elif route == "/reply":
                    status, resp = daemon.handle_reply(body)
                elif route == "/shutdown":
                    status, resp = daemon.handle_shutdown()
                else:
                    status, resp = 404, {"error": "unknown route"}
                self._reply_json(status, resp)
                return
            if route == "/oauth/register":
                # DCR endpoint (RFC 7591). Validates the request, looks
                # up an existing app by deterministic fingerprint name
                # (reuse path), and creates a new Zitadel OIDC app when
                # not found. Returns the resulting client_id in RFC 7591
                # client_information_response shape.
                if daemon.idp_config is None or not daemon.idp_config.dcr_shim:
                    self._reply_error(404, "DCR shim not enabled")
                    return
                # Trust gate (design §4.1):
                #   open      — accept any caller
                #   protected — require an authenticated user (real OIDC
                #               bearer; loopback bearer's empty user_id is
                #               not enough)
                if daemon.idp_config.registration_trust == "protected":
                    ctx = self._auth_identify()
                    if ctx is None or not getattr(ctx, "user_id", ""):
                        self._reply_unauthenticated(
                            error="invalid_token",
                            description="DCR shim requires authenticated user when trust=protected",
                        )
                        return
                body = self._read_body()
                if body is None:
                    self._reply_json(400, {
                        "error": "invalid_client_metadata",
                        "error_description": "request body is not valid JSON",
                    })
                    return
                from otaman_bridge.dcr_shim import (
                    DCRError,
                    ZitadelMgmtError,
                    find_or_create_client,
                    parse_register_request,
                    to_rfc7591_response,
                )
                try:
                    request = parse_register_request(body)
                except DCRError as exc:
                    self._reply_json(exc.http_status, {
                        "error": exc.error,
                        "error_description": exc.description,
                    })
                    return
                # Lazy-build the mgmt client (idempotent — once per daemon).
                mgmt_client = daemon.get_or_build_dcr_mgmt_client()
                if mgmt_client is None:
                    self._reply_json(503, {
                        "error": "server_error",
                        "error_description": (
                            "DCR shim enabled but management API credentials "
                            "(client_id/client_secret/org_id) are not configured."
                        ),
                    })
                    return
                try:
                    client_id = find_or_create_client(
                        mgmt_client=mgmt_client,
                        project_id=daemon.idp_config.project_id,
                        request=request,
                        name_prefix=daemon.idp_config.managed_name_prefix,
                    )
                except ZitadelMgmtError as exc:
                    _log.warning("DCR mgmt API failure: %s", exc)
                    self._reply_json(502, {
                        "error": "server_error",
                        "error_description": f"upstream IdP rejected: {exc}",
                    })
                    return
                import time as _t
                self._reply_json(201, to_rfc7591_response(
                    request=request,
                    client_id=client_id,
                    now_unix=int(_t.time()),
                ))
                return
            self._reply_error(404, f"unknown route: {self.path}")

        def _render_root_html(self, daemon) -> str:
            """Build the minimal landing-page HTML.

            Three cases: web auth not configured (show diagnostic), no
            cookie or unknown cookie (show login link), valid cookie
            (show user identity + logout button).
            """
            import html as _h
            if daemon.session_store is None or daemon.session_cookie is None:
                body_inner = (
                    "<p>Web login flow is not configured (loopback bearer only).</p>"
                    "<p>Set OTAMAN_AUTH_MODE, OIDC_ISSUER, OIDC_BRIDGE_WEB_CLIENT_ID, "
                    "OIDC_BRIDGE_REDIRECT_URI and restart the daemon.</p>"
                )
            else:
                cookie_header = self.headers.get("Cookie", "")
                sid = daemon.session_cookie.parse(cookie_header)
                sess = daemon.session_store.get(sid) if sid else None
                if sess is None:
                    body_inner = (
                        "<p>Not logged in.</p>"
                        "<p><a href=\"/auth/login\">Log in with Zitadel</a></p>"
                    )
                else:
                    user = _h.escape(sess.user_id)
                    email = _h.escape(sess.email or "(no email)")
                    roles = _h.escape(", ".join(sess.roles) if sess.roles else "(none)")
                    body_inner = (
                        f"<p>Logged in as <strong>{email}</strong></p>"
                        f"<dl><dt>user_id</dt><dd>{user}</dd>"
                        f"<dt>roles</dt><dd>{roles}</dd></dl>"
                        "<form method=\"post\" action=\"/auth/logout\">"
                        "<button type=\"submit\">Log out</button></form>"
                    )
            return (
                "<!DOCTYPE html><html><head>"
                "<meta charset=\"utf-8\"><title>otaman bridge</title>"
                "</head><body>"
                "<h1>otaman bridge</h1>"
                + body_inner +
                "</body></html>"
            )

        def do_GET(self) -> None:  # noqa: N802
            # Parse the path so query strings (e.g. /auth/callback?code=...)
            # are stripped before route matching. Without this, dispatch
            # falls through to the 404 handler for any URL with a query.
            import urllib.parse as _u_parse
            route = _u_parse.urlparse(self.path).path.rstrip("/")
            if route == "/status":
                # /status does NOT require auth — intentional (§5.3 design).
                status, resp = daemon.handle_status()
                self._reply_json(status, resp)
                return
            if route == "/.well-known/oauth-protected-resource":
                # RFC 9728 Protected Resource Metadata. Unauthenticated by
                # design — MCP clients fetch this to discover the OIDC
                # issuer before they have a token. If OIDC isn't enabled
                # on this daemon, there's no protected resource to describe.
                if daemon.oidc_validator is None:
                    self._reply_error(404, "OIDC not configured on this bridge")
                    return
                resource = _resolve_public_resource_url(self.headers.get("Host", ""))
                # With the DCR shim enabled (D3+), advertise the bridge
                # itself as the authorization server so MCP clients fetch
                # the AS metadata overlay (with injected registration_endpoint)
                # from us. Without the shim, point clients at the real
                # OIDC issuer URL directly (chunk B behavior).
                if daemon.idp_config is not None and daemon.idp_config.dcr_shim:
                    authorization_server = resource
                else:
                    authorization_server = daemon.oidc_validator.config.issuer
                metadata = _build_protected_resource_metadata(
                    issuer=authorization_server,
                    resource=resource,
                )
                self._reply_json(200, metadata)
                return
            if route in (
                "/.well-known/oauth-authorization-server",
                "/.well-known/openid-configuration",
            ):
                # AS metadata overlay (mcp-oauth chunk D3). Only served
                # when the DCR shim is enabled — without it, MCP clients
                # are routed directly to the IdP's own metadata URL by
                # /.well-known/oauth-protected-resource above. Same payload
                # for both paths (different MCP clients prefer different
                # ones; serve both).
                if daemon.idp_config is None or not daemon.idp_config.dcr_shim:
                    self._reply_error(404, "DCR shim not enabled on this bridge")
                    return
                from otaman_bridge.dcr_shim import (
                    MetadataFetchError,
                    derive_registration_endpoint,
                    fetch_upstream_metadata,
                    overlay_metadata,
                )
                cached = daemon._idp_metadata_cache.get()
                if cached is None:
                    try:
                        cached = fetch_upstream_metadata(
                            daemon.idp_config.management_base_url,
                        )
                    except MetadataFetchError as exc:
                        _log.warning("AS metadata upstream fetch failed: %s", exc)
                        self._reply_error(502, f"upstream metadata unavailable: {exc}")
                        return
                    daemon._idp_metadata_cache.put(cached)
                bridge_url = _resolve_public_resource_url(self.headers.get("Host", ""))
                overlaid = overlay_metadata(
                    cached,
                    registration_endpoint=derive_registration_endpoint(
                        bridge_public_url=bridge_url,
                    ),
                )
                self._reply_json(200, overlaid)
                return
            if route == "" or route == "/":
                # Minimal landing page so a browser landing here after
                # /auth/callback s 302 to / sees actual content.
                html = self._render_root_html(daemon)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return
            if route == "/auth/login":
                # Unauth: this IS the start of the auth flow. Returns a
                # 302 to Zitadel's /oauth/v2/authorize, or 503 if the web
                # login flow is not configured (env vars incomplete).
                if daemon.web_login_flow is None:
                    self._reply_error(503, "web login flow not configured")
                    return
                started = daemon.web_login_flow.start()
                self.send_response(302)
                self.send_header("Location", started.authorize_url)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if route == "/auth/callback":
                # Unauth: completes the auth flow started at /auth/login.
                # Reads ?code=&state=, calls LoginCompleter, sets session
                # cookie + 302 to "/". Maps errors:
                #   - LoginCompleteError -> 400 (state / id_token problems)
                #   - TokenExchangeError -> 502 (token endpoint failure)
                if daemon.login_completer is None:
                    self._reply_error(503, "web login flow not configured")
                    return
                import urllib.parse as _u
                from otaman_bridge.web_auth import LoginCompleteError, TokenExchangeError
                qs = _u.urlparse(self.path).query
                params = dict(_u.parse_qsl(qs))
                if "error" in params:
                    self._reply_error(400, f"oauth error: {params['error']}")
                    return
                code = params.get("code", "")
                state = params.get("state", "")
                if not code or not state:
                    self._reply_error(400, "missing code or state")
                    return
                try:
                    session = daemon.login_completer.complete(code=code, state=state)
                except LoginCompleteError as exc:
                    self._reply_error(400, f"login failed: {exc}")
                    return
                except TokenExchangeError as exc:
                    self._reply_error(502, f"token endpoint failed: {exc}")
                    return
                except Exception as exc:
                    # Catches OIDCError from JWKS fetch + any other infra-level
                    # failure during id_token validation. Without this, the state
                    # gets consumed but the exception bubbles to a 500 traceback;
                    # on retry the user sees "unknown or expired state" (the
                    # state-consumed-on-first-call side effect). Map all such
                    # cases to 502 -- they are IdP-side / network failures, not
                    # user input problems.
                    _log.exception("auth callback failed unexpectedly")
                    self._reply_error(502, f"auth callback failed: {exc}")
                    return
                cookie = daemon.session_cookie.set_header(
                    session.id,
                    max_age=daemon.session_store.ttl,
                )
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", cookie)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            self._reply_error(404, f"unknown route: {self.path}")

    return Handler


# ---------------------------------------------------------------------------
# Client helpers — tests + `maestro bridge` CLI call these.


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
            f"http://127.0.0.1:{port}/status", timeout=timeout,
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
