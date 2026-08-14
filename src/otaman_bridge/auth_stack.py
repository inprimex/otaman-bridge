"""Owns the CE/EE auth wiring: OIDC validator, DCR shim, web-login stack,
and the composite auth_provider chain.

Extracted out of ``BridgeDaemon`` (F040, phase 4 of the god-object
decomposition — see the bridge-agent/spec-agent bus thread on
2026-07-03; PRs #33/#34/#35 for phases 1-3). This is the riskiest phase
per the independent Fable-model review of the refactor plan: 47 test
call sites across 11 files reassign ``daemon.oidc_validator`` /
``session_store`` / ``session_cookie`` / ``login_completer`` /
``idp_config`` post-construction, and EE's ``routes_dcr.py`` reaches
directly into ``daemon.idp_config`` / ``daemon._idp_metadata_cache`` /
``daemon.oidc_validator`` / ``daemon.get_or_build_dcr_mgmt_client``.

**These attribute names are the de-facto seam contract** — moving the
construction logic here is safe only because ``BridgeDaemon`` keeps
every one of them as a frozen forwarding property (see daemon.py). No
test or EE call site needed to change for this phase.

Note: the four stateless env-parsing helpers this class depends on
(``_build_oidc_validator_from_env``, ``_build_web_login_flow_from_env``,
``_resolve_public_resource_url``, ``_build_protected_resource_metadata``)
deliberately STAY in daemon.py rather than moving here — EE's
``routes_dcr.py`` and three test files (``test_bridge_oidc.py``,
``test_web_auth_builder.py``, ``test_well_known_routes.py``) import them
directly from ``otaman_bridge.daemon``. Moving them would break those
imports for zero decomposition benefit (they're pure functions, not
state — the god-object problem is about instance state, not free
functions). The import below is deferred to avoid a circular import at
module-load time (``daemon.py`` imports ``AuthStack`` from this module
at the top level; this module imports back from ``daemon.py`` only
inside ``__init__``, by which point ``daemon.py`` has finished loading).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

_log = logging.getLogger("maestro.bridge.auth_stack")  # legacy: logger renamed at otaman-core 1.0


class AuthStack:
    """CE/EE auth wiring + the DCR shim's background cleanup sweep.

    ``BridgeDaemon`` holds one instance, constructed once in
    ``__init__`` (this class has no separate ``start()``/``stop()`` —
    the only lifecycle piece, the cleanup sweep coroutine, is submitted
    to the daemon's async loop the same way the DCR sweep future
    always was).
    """

    def __init__(self, *, token: str, project_root: Path | None = None) -> None:
        from otaman_bridge.daemon import (
            _build_oidc_validator_from_env,
            _build_web_login_flow_from_env,
            _resolve_public_resource_url,
        )

        # Optional OIDC validator built from env. When unset, daemon
        # serves loopback-bearer only (Mode 1 / local-trust pattern).
        self.oidc_validator = _build_oidc_validator_from_env()

        # Optional DCR shim (mcp-oauth wave chunk D3+). When enabled the
        # daemon serves AS metadata overlay routes pointing at itself,
        # so MCP clients (Claude Code) can do RFC 7591 against Zitadel
        # which lacks native DCR. None = inert.
        #
        # DCR shim is EE-only (Phase 2b). CE-only builds (EE absent) skip
        # idp_config entirely — no DCR routes, no metadata overlay.
        self.idp_config = None
        self._idp_metadata_cache = None
        self._dcr_mgmt_client_cached = None
        try:
            from otaman_bridge_ee.dcr_shim import IdpConfig, MetadataCache

            self.idp_config = IdpConfig.from_env(project_root=project_root)
            if self.idp_config is not None:
                self._idp_metadata_cache = MetadataCache(
                    ttl_seconds=self.idp_config.metadata_cache_seconds
                )
        except ImportError:
            _log.debug("EE package absent; DCR shim disabled")
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
            # web_login_flow is only ever non-None when EE imports succeeded
            # in _build_web_login_flow_from_env, so these imports always
            # resolve here.
            from otaman_bridge_ee.web_auth import LoginCompleter, TokenExchanger
            from otaman_bridge_ee.web_session import SessionCookie, SessionStore

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

        # Auth provider seam (CE/EE split per
        # otaman-meta/strategy/bridge-ce-ee-split.md §5).
        # Phase 1: introduced the Protocol + CE providers in otaman_bridge.auth.
        # Phase 2a: OIDCAuthProvider moved to otaman_bridge_ee.auth_oidc.
        #
        # The EE provider is imported conditionally so the CE-only build
        # (no EE package installed) falls back to a loopback-only chain.
        # When EE IS installed, OIDCAuthProvider is in the chain — its
        # activity is gated on validator_getter() returning non-None at
        # call time, so tests can reassign daemon.oidc_validator /
        # session_store / session_cookie at runtime (via the forwarding
        # properties on BridgeDaemon, which write straight through to
        # this object) and have the auth chain track changes without
        # rebuilding the composite.
        from otaman_bridge.auth import CompositeAuthProvider, LoopbackAuthProvider

        providers: list = []
        try:
            from otaman_bridge_ee.auth_oidc import OIDCAuthProvider
        except ImportError:
            _log.info("EE package not installed; CE-only auth chain (loopback only)")
            OIDCAuthProvider = None  # type: ignore[assignment]
        if OIDCAuthProvider is not None:
            providers.append(
                OIDCAuthProvider(
                    validator_getter=lambda d=self: d.oidc_validator,
                    session_store_getter=lambda d=self: d.session_store,
                    session_cookie_getter=lambda d=self: d.session_cookie,
                    resource_url_fn=_resolve_public_resource_url,
                )
            )
        providers.append(LoopbackAuthProvider(token=token))
        self.auth_provider = CompositeAuthProvider(providers=tuple(providers))

        # EE DCR routes (/oauth/register + /.well-known/* overlay). When
        # EE is absent, the handler is None and the daemon's do_POST /
        # do_GET fall through to the catch-all 404.
        try:
            from otaman_bridge_ee.routes_dcr import try_handle as _ee_dcr_try_handle

            self._ee_dcr_try_handle = _ee_dcr_try_handle
        except ImportError:
            self._ee_dcr_try_handle = None

    def get_or_build_dcr_mgmt_client(self):
        """Lazy-construct the Zitadel mgmt API client for the DCR shim.

        Returns None when shim is enabled but credentials aren't fully
        populated (route then returns 503 server_error). Tests can
        set ``daemon._dcr_mgmt_client_cached`` directly to inject a stub.
        """
        if self._dcr_mgmt_client_cached is not None:
            return self._dcr_mgmt_client_cached
        if self.idp_config is None or not self.idp_config.dcr_shim:
            return None
        cfg = self.idp_config
        # Need at least one auth mode (PAT preferred) + org_id.
        has_pat = bool(cfg.mgmt_pat)
        has_client_creds = bool(cfg.machine_user_client_id and cfg.machine_user_client_secret)
        if not cfg.org_id or not (has_pat or has_client_creds):
            return None
        from otaman_bridge_ee.dcr_shim import ZitadelMgmtClient

        # token endpoint is the standard OIDC location on the mgmt host.
        token_url = f"{cfg.management_base_url}/oauth/v2/token"
        self._dcr_mgmt_client_cached = ZitadelMgmtClient(
            base_url=cfg.management_base_url,
            token_url=token_url,
            client_id=cfg.machine_user_client_id,
            client_secret=cfg.machine_user_client_secret,
            pat=cfg.mgmt_pat,
            org_id=cfg.org_id,
            expected_host=cfg.expected_host,
        )
        return self._dcr_mgmt_client_cached

    async def dcr_cleanup_sweep_loop(self) -> None:
        """Background task that periodically prunes stale shim-managed apps.

        Run when both ``idp_config.dcr_shim`` and ``cleanup_sweep_interval_seconds > 0``.
        Each iteration sleeps for the interval first, then sweeps; this lets
        the daemon finish startup before the first sweep request to Zitadel.
        Failures are logged but never abort the loop.
        """
        from otaman_bridge_ee.dcr_shim import sweep_orphans

        cfg = self.idp_config
        interval = cfg.cleanup_sweep_interval_seconds
        _log.info(
            "DCR shim cleanup loop started (interval=%ds ttl=%ds prefix=%s project=%s)",
            interval,
            cfg.cleanup_ttl_seconds,
            cfg.managed_name_prefix,
            cfg.project_id,
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
                        report.found,
                        report.eligible,
                        report.deleted,
                        report.failed,
                    )
                else:
                    _log.debug("DCR sweep: nothing to delete (found=%d)", report.found)
            except Exception as exc:  # noqa: BLE001
                _log.warning("DCR sweep iteration failed: %s", exc)
