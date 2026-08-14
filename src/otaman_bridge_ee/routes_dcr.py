"""EE-only HTTP routes for the DCR shim + RFC 9728 / RFC 7591 metadata.

Three routes live here:

- ``POST /oauth/register`` — RFC 7591 Dynamic Client Registration shim
  endpoint. Validates the request, deterministically reuses an existing
  Zitadel app via fingerprint, or creates a new one. Returns the
  RFC 7591 client_information_response.
- ``GET /.well-known/oauth-protected-resource`` — RFC 9728 Protected
  Resource Metadata. Tells MCP clients where the authorization server
  lives (either the IdP issuer directly, or this bridge when the DCR
  shim is overlaying AS metadata).
- ``GET /.well-known/oauth-authorization-server`` and
  ``GET /.well-known/openid-configuration`` — AS metadata overlay
  (DCR shim chunk D3). Fetched-and-cached upstream IdP metadata with
  ``registration_endpoint`` injected to point at this bridge's
  ``/oauth/register``.

Phase 2c of the CE/EE split: this module is imported conditionally by
``otaman_bridge.daemon``. CE-only builds (EE absent) see ``ImportError``
and skip the dispatcher — routes return 404 from CE's catch-all.

Handler access: each ``_handle_*`` function takes the daemon's
``BaseHTTPRequestHandler`` subclass instance (``handler``) so it can use
the private reply helpers (``_reply_json``, ``_reply_error``,
``_reply_unauthenticated``, ``_auth_identify``, ``_read_body``). This is
an intentional cross-module access pattern for the transitional phase;
Phase 2.5 (going-public split) promotes these helpers to public methods
or moves the handlers into a subclass.
"""

from __future__ import annotations

import logging
import time as _time
from typing import Any

_log = logging.getLogger("otaman.bridge.ee.routes_dcr")


def try_handle(handler: Any, daemon: Any, method: str, route: str) -> bool:
    """Dispatch an HTTP request to an EE-DCR route handler.

    Returns ``True`` if the route was matched and handled (response already
    written). Returns ``False`` if the route isn't an EE-DCR route — caller
    falls through to its own route table.

    ``method`` is the uppercased HTTP method ("GET" / "POST"); ``route``
    is the URL path with trailing slashes stripped (matches the daemon's
    own normalization).
    """
    if method == "POST" and route == "/oauth/register":
        _handle_oauth_register(handler, daemon)
        return True
    if method == "GET" and route == "/.well-known/oauth-protected-resource":
        _handle_protected_resource(handler, daemon)
        return True
    if method == "GET" and route in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/openid-configuration",
    ):
        _handle_authorization_server(handler, daemon)
        return True
    return False


def _handle_oauth_register(handler: Any, daemon: Any) -> None:
    """POST /oauth/register — DCR shim endpoint.

    Validates the request, looks up an existing app by deterministic
    fingerprint name (reuse path), and creates a new Zitadel OIDC app
    when not found. Returns the resulting client_id in RFC 7591
    client_information_response shape.
    """
    if daemon.idp_config is None or not daemon.idp_config.dcr_shim:
        handler._reply_error(404, "DCR shim not enabled")
        return
    # Trust gate (design §4.1):
    #   open      — accept any caller
    #   protected — require an authenticated user (real OIDC bearer;
    #               loopback bearer's empty user_id is not enough)
    if daemon.idp_config.registration_trust == "protected":
        ctx = handler._auth_identify()
        if ctx is None or not getattr(ctx, "user_id", ""):
            handler._reply_unauthenticated(
                error="invalid_token",
                description="DCR shim requires authenticated user when trust=protected",
            )
            return
    body = handler._read_body()
    if body is None:
        handler._reply_json(
            400,
            {
                "error": "invalid_client_metadata",
                "error_description": "request body is not valid JSON",
            },
        )
        return
    from otaman_bridge_ee.dcr_shim import (
        DCRError,
        ZitadelMgmtError,
        find_or_create_client,
        parse_register_request,
        to_rfc7591_response,
    )

    try:
        request = parse_register_request(body)
    except DCRError as exc:
        handler._reply_json(
            exc.http_status,
            {
                "error": exc.error,
                "error_description": exc.description,
            },
        )
        return
    # Lazy-build the mgmt client (idempotent — once per daemon).
    mgmt_client = daemon.get_or_build_dcr_mgmt_client()
    if mgmt_client is None:
        handler._reply_json(
            503,
            {
                "error": "server_error",
                "error_description": (
                    "DCR shim enabled but management API credentials "
                    "(client_id/client_secret/org_id) are not configured."
                ),
            },
        )
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
        handler._reply_json(
            502,
            {
                "error": "server_error",
                "error_description": f"upstream IdP rejected: {exc}",
            },
        )
        return
    handler._reply_json(
        201,
        to_rfc7591_response(
            request=request,
            client_id=client_id,
            now_unix=int(_time.time()),
        ),
    )


def _handle_protected_resource(handler: Any, daemon: Any) -> None:
    """GET /.well-known/oauth-protected-resource — RFC 9728 metadata.

    Unauthenticated by design — MCP clients fetch this to discover the
    OIDC issuer before they have a token. If OIDC isn't enabled on this
    daemon, there's no protected resource to describe.
    """
    if daemon.oidc_validator is None:
        handler._reply_error(404, "OIDC not configured on this bridge")
        return
    # Imported here (not at module top) to keep this module's import-time
    # surface small — the helper lives in CE so it stays available to
    # CE-only builds via the daemon module directly.
    from otaman_bridge.daemon import (
        _build_protected_resource_metadata,
        _resolve_public_resource_url,
    )

    resource = _resolve_public_resource_url(handler.headers.get("Host", ""))
    # With the DCR shim enabled (D3+), advertise the bridge itself as the
    # authorization server so MCP clients fetch the AS metadata overlay
    # (with injected registration_endpoint) from us. Without the shim,
    # point clients at the real OIDC issuer URL directly.
    if daemon.idp_config is not None and daemon.idp_config.dcr_shim:
        authorization_server = resource
    else:
        authorization_server = daemon.oidc_validator.config.issuer
    metadata = _build_protected_resource_metadata(
        issuer=authorization_server,
        resource=resource,
    )
    handler._reply_json(200, metadata)


def _handle_authorization_server(handler: Any, daemon: Any) -> None:
    """GET /.well-known/oauth-authorization-server (+ openid-configuration).

    AS metadata overlay. Only served when the DCR shim is enabled —
    without it, MCP clients are routed directly to the IdP's own
    metadata URL by /.well-known/oauth-protected-resource. Same payload
    for both paths (different MCP clients prefer different ones).
    """
    if daemon.idp_config is None or not daemon.idp_config.dcr_shim:
        handler._reply_error(404, "DCR shim not enabled on this bridge")
        return
    from otaman_bridge.daemon import _resolve_public_resource_url
    from otaman_bridge_ee.dcr_shim import (
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
            handler._reply_error(502, f"upstream metadata unavailable: {exc}")
            return
        daemon._idp_metadata_cache.put(cached)
    bridge_url = _resolve_public_resource_url(handler.headers.get("Host", ""))
    overlaid = overlay_metadata(
        cached,
        registration_endpoint=derive_registration_endpoint(
            bridge_public_url=bridge_url,
        ),
    )
    handler._reply_json(200, overlaid)


__all__ = ["try_handle"]
