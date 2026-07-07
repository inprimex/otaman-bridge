"""Raw HTTP routing for the bridge daemon.

Extracted out of ``BridgeDaemon`` (F040, phase 6 — the thin-host finish
of the god-object decomposition; see the bridge-agent/spec-agent bus
thread on 2026-07-03; PRs #33-#37 for phases 1-5). This is the
~400-line nested ``BaseHTTPRequestHandler`` subclass that used to live
inside ``daemon.py``'s ``_make_handler`` function — moved verbatim,
with no behavior change: it's a pure function of the ``daemon``
instance passed in, dispatching to the same ``daemon.handle_*``
methods and the same forwarding-property attributes (``auth_provider``,
``session_store``, ``session_cookie``, ``login_completer``,
``web_login_flow``, ``mcp_server``, ``_ee_dcr_try_handle``,
``bus_watcher_root``) that phases 1-5 already made safe to relocate.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from otaman_bridge.daemon import BridgeDaemon

# CE auth for the /pm-sync/<provider> webhook: a single static shared
# secret compared via a Bearer header, constant-time. F052 (2026-07-02
# GAP audit, Security lens): this route previously accepted and acted
# on completely unauthenticated payloads, writing real bus messages
# addressed to other agents from attacker-controlled input. EE may
# later add a stronger HMAC-over-raw-body signature scheme as an
# alternative/upgrade; this env var is the CE baseline.
_PM_SYNC_WEBHOOK_SECRET_ENV = "OTAMAN_BRIDGE_PM_SYNC_WEBHOOK_SECRET"

_log = logging.getLogger("maestro.bridge.http_handler")


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
            """Identify the caller via the daemon's configured AuthProvider.

            Delegates to ``daemon.auth_provider.identify(self.headers)``.
            The provider chain composes OIDC bearer + session cookie +
            loopback bearer when OIDC is configured; loopback alone
            otherwise. See ``otaman_bridge.auth`` for the seam design.
            """
            return daemon.auth_provider.identify(self.headers)

        def _auth_ok(self) -> bool:
            """Authorize a request without caring about user identity.

            Delegates to ``daemon.auth_provider.identify`` — any
            non-None CallContext means the request is authorized for
            non-identity-requiring routes. Identity-requiring routes
            use ``_auth_identify`` directly to surface the user.
            """
            return daemon.auth_provider.identify(self.headers) is not None

        def _pm_sync_webhook_auth_ok(self) -> tuple[bool, int, str]:
            """Check the pm-sync webhook's shared-secret Bearer header.

            Independent of ``daemon.auth_provider`` -- this route is hit
            by an external PM tool, not an otaman client, so it doesn't
            participate in the OIDC/loopback identity chain. Returns
            ``(ok, status_if_not_ok, message_if_not_ok)``.

            Fails closed: if the secret isn't configured, the route is
            disabled (503) rather than left open (the pre-fix behavior).
            """
            secret = os.environ.get(_PM_SYNC_WEBHOOK_SECRET_ENV, "").strip()
            if not secret:
                return False, 503, "pm-sync webhook auth not configured"
            auth_header = self.headers.get("Authorization", "")
            provided = (
                auth_header[len("Bearer "):].strip()
                if auth_header.startswith("Bearer ")
                else ""
            )
            if not provided or not hmac.compare_digest(provided, secret):
                return False, 401, "invalid or missing pm-sync webhook secret"
            return True, 200, ""

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

            Delegates challenge construction to the configured
            ``AuthProvider``. When the provider chain includes an
            ``OIDCAuthProvider``, the 401 carries a Bearer challenge
            pointing at the bridge's protected-resource-metadata
            endpoint so MCP clients (Claude Code) can discover the
            issuer and run auth_code+PKCE. CE-style providers (loopback
            / simple) return None and the 401 ships without a challenge
            header — there's no authorization server to point at.

            ``error`` lets callers send ``insufficient_scope`` instead
            of the default ``invalid_token`` when the caller IS
            authenticated (loopback bearer) but lacks user identity for
            an identity-required MCP tool. This signals to MCP clients
            that they should upgrade to an OIDC bearer rather than
            retry the same auth.
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
            host = self.headers.get("Host", "")
            # Prefer OIDC's challenge_with_error so the error code can
            # be overridden (insufficient_scope vs invalid_token). Fall
            # back to the generic challenge() for providers that don't
            # have a per-error variant. OIDCAuthProvider lives in the
            # EE package (Phase 2a); CE-only builds skip this path.
            try:
                from otaman_bridge_ee.auth_oidc import OIDCAuthProvider
                oidc = daemon.auth_provider.first_of_type(OIDCAuthProvider)
            except ImportError:
                oidc = None
            if oidc is not None:
                challenge = oidc.challenge_with_error(host, error)
            else:
                challenge = daemon.auth_provider.challenge(host)
            if challenge is not None:
                self.send_header("WWW-Authenticate", challenge)
            self.end_headers()
            self.wfile.write(payload)

        # --- routes -------------------------------------------------------

        def do_POST(self) -> None:  # noqa: N802 — stdlib name
            import urllib.parse as _u_parse_post
            route = _u_parse_post.urlparse(self.path).path.rstrip("/")
            # EE-DCR routes first (Phase 2c). When EE is absent, handler
            # is None — falls through to CE's own route table.
            if daemon._ee_dcr_try_handle is not None:
                if daemon._ee_dcr_try_handle(self, daemon, "POST", route):
                    return
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
            if route.startswith("/pm-sync/"):
                # F052: shared-secret Bearer auth. This route accepts
                # webhooks from an external PM tool and writes real bus
                # messages from the payload -- it must not be reachable
                # by anyone who can send it an HTTP request.
                ok, status, message = self._pm_sync_webhook_auth_ok()
                if not ok:
                    self._reply_error(status, message)
                    return
                body = self._read_body()
                if body is None:
                    self._reply_error(400, "invalid JSON body")
                    return
                # Lazy-load pm_sync_handler
                if not hasattr(daemon, "_pm_sync_handler"):
                    from pathlib import Path as _Path
                    from otaman_bridge.pm_sync_handler import PmSyncHandler
                    _root = _Path(daemon.bus_watcher_root) if daemon.bus_watcher_root else _Path.cwd()
                    daemon._pm_sync_handler = PmSyncHandler(_root)
                result = daemon._pm_sync_handler.handle_inbound_webhook(body)
                self._reply_json(200, result)
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
            # EE-DCR routes (/.well-known/* metadata overlays). When EE
            # is absent, handler is None and we fall through to CE's
            # own route table.
            if daemon._ee_dcr_try_handle is not None:
                if daemon._ee_dcr_try_handle(self, daemon, "GET", route):
                    return
            if route == "/status":
                # /status does NOT require auth — intentional (§5.3 design).
                status, resp = daemon.handle_status()
                self._reply_json(status, resp)
                return
            if route == "/healthz":
                # No auth — container orchestrator probes this unauthenticated.
                status, resp = daemon.handle_healthz()
                self._reply_json(status, resp)
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
                from otaman_bridge_ee.web_auth import LoginCompleteError, TokenExchangeError
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
