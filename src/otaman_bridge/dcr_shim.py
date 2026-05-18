"""DCR shim — make Zitadel speak RFC 7591 for MCP clients.

Chunk D3 (this file) implements the read side:
- IdpConfig: loaded from env vars; gates everything via ``dcr_shim`` flag.
- AS metadata overlay: fetch Zitadel's /.well-known/openid-configuration
  server-side (cached, TTL), inject a ``registration_endpoint`` pointing
  back at the bridge, return the merged doc.

Chunk D4 will add the write side (/oauth/register handler + Zitadel
management API client).

See ``strategy/dcr-shim-for-zitadel-idp.md`` in otaman-meta for the full
design and rationale (incl. the per-IdP gating, fingerprint-reuse
cleanup, decision-gate workflow).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger("otaman.bridge.dcr_shim")


# ---------------------------------------------------------------------------
# Config


@dataclass(frozen=True)
class IdpConfig:
    """Per-IdP DCR-shim configuration.

    Loaded from env at daemon start (matches the existing OIDC/auth env
    pattern in cli.py/daemon.py rather than requiring platform.yaml in
    --no-config mode). The fields map 1:1 to the ``idp:`` block in the
    design doc; future platform.yaml-driven config will populate the
    same dataclass.
    """

    # "zitadel" today; future "keycloak"/"hydra" select different
    # mgmt-API adapters. Today the shim only knows zitadel.
    type: str

    # Master switch. False = shim is inert (routes 404, /.well-known/
    # oauth-protected-resource keeps chunk B's behavior of advertising
    # the OIDC issuer directly).
    dcr_shim: bool

    # Where Zitadel's mgmt API lives. Often same host as OIDC_ISSUER.
    management_base_url: str

    # Project under which DCR-registered apps are created.
    project_id: str

    # Mgmt-API auth (machine user — client_credentials grant). Both
    # populated together; secret resolved via the _secrets chain when
    # daemon-level config loading lands. For D3-only the values may be
    # empty (route works without making mgmt calls).
    machine_user_client_id: str = ""
    machine_user_client_secret: str = ""

    # "open" or "protected". See design §4.1.
    registration_trust: str = "open"

    # Cache TTL for the upstream AS metadata fetch (seconds).
    metadata_cache_seconds: int = 300

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> IdpConfig | None:
        """Build from environment, or return None when shim is disabled.

        Recognized env vars:
            OTAMAN_DCR_SHIM             — "1"/"true" enables (required)
            OTAMAN_DCR_SHIM_TYPE        — defaults to "zitadel"
            OIDC_ISSUER                 — reused as mgmt_base_url default
            OTAMAN_DCR_SHIM_MGMT_BASE   — override mgmt_base_url
            OIDC_PROJECT_ID             — reused as project_id
            OTAMAN_DCR_SHIM_CLIENT_ID   — mgmt machine-user client_id
            OTAMAN_DCR_SHIM_SECRET      — mgmt machine-user secret
            OTAMAN_DCR_SHIM_TRUST       — "open" (default) or "protected"
            OTAMAN_DCR_SHIM_CACHE_SECS  — AS metadata cache TTL
        """
        e = env if env is not None else os.environ
        if e.get("OTAMAN_DCR_SHIM", "").strip().lower() not in ("1", "true", "yes"):
            return None
        issuer = e.get("OIDC_ISSUER", "").strip()
        mgmt = e.get("OTAMAN_DCR_SHIM_MGMT_BASE", "").strip() or issuer
        if not mgmt:
            _log.warning(
                "OTAMAN_DCR_SHIM=true but OIDC_ISSUER + "
                "OTAMAN_DCR_SHIM_MGMT_BASE are both empty; shim disabled"
            )
            return None
        trust = e.get("OTAMAN_DCR_SHIM_TRUST", "open").strip().lower()
        if trust not in ("open", "protected"):
            _log.warning("invalid OTAMAN_DCR_SHIM_TRUST=%r, using 'open'", trust)
            trust = "open"
        try:
            cache_secs = int(e.get("OTAMAN_DCR_SHIM_CACHE_SECS", "300"))
        except ValueError:
            cache_secs = 300
        return cls(
            type=e.get("OTAMAN_DCR_SHIM_TYPE", "zitadel").strip().lower() or "zitadel",
            dcr_shim=True,
            management_base_url=mgmt.rstrip("/"),
            project_id=e.get("OIDC_PROJECT_ID", "").strip(),
            machine_user_client_id=e.get("OTAMAN_DCR_SHIM_CLIENT_ID", "").strip(),
            machine_user_client_secret=e.get("OTAMAN_DCR_SHIM_SECRET", "").strip(),
            registration_trust=trust,
            metadata_cache_seconds=max(1, cache_secs),
        )


# ---------------------------------------------------------------------------
# AS metadata fetch + overlay


class MetadataCache:
    """Thread-safe TTL cache for one upstream AS metadata document.

    The bridge's HTTP server is threaded, so a Lock is required. The
    cache holds at most one entry (keyed implicitly by issuer URL,
    which is set at construction time) — the daemon only ever talks
    to one IdP.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._doc: dict[str, Any] | None = None
        self._expires_at: float = 0.0

    def get(self, now: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            if self._doc is None:
                return None
            if (now if now is not None else time.monotonic()) >= self._expires_at:
                return None
            return self._doc

    def put(self, doc: dict[str, Any], now: float | None = None) -> None:
        with self._lock:
            self._doc = doc
            self._expires_at = (now if now is not None else time.monotonic()) + self._ttl

    def invalidate(self) -> None:
        with self._lock:
            self._doc = None
            self._expires_at = 0.0


class MetadataFetchError(Exception):
    """Upstream IdP returned non-2xx / malformed JSON / network failure."""


def fetch_upstream_metadata(
    base_url: str,
    *,
    timeout_seconds: float = 5.0,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict[str, Any]:
    """Fetch /.well-known/openid-configuration from the upstream IdP.

    Zitadel only serves the OIDC path; RFC 8414's oauth-authorization-server
    path returns 404 (verified 2026-05-18 against v2.64.1). We fetch the
    OIDC doc — it has the same shape RFC 8414 specifies plus OIDC
    additions, and is what AS-metadata-aware clients accept.

    Raises ``MetadataFetchError`` on any network or parse failure.
    """
    url = f"{base_url.rstrip('/')}/.well-known/openid-configuration"
    req = urllib.request.Request(url, method="GET")
    o = opener or urllib.request.build_opener()
    try:
        with o.open(req, timeout=timeout_seconds) as resp:
            if resp.status != 200:
                raise MetadataFetchError(
                    f"upstream {url} returned HTTP {resp.status}"
                )
            raw = resp.read()
    except urllib.error.URLError as exc:
        raise MetadataFetchError(f"upstream {url} unreachable: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise MetadataFetchError(f"upstream {url} failed: {exc}") from exc
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise MetadataFetchError(
            f"upstream {url} returned malformed JSON: {exc}"
        ) from exc
    if not isinstance(doc, dict):
        raise MetadataFetchError(f"upstream {url} returned non-object JSON")
    return doc


def overlay_metadata(
    upstream_doc: dict[str, Any],
    *,
    registration_endpoint: str,
) -> dict[str, Any]:
    """Return a copy of ``upstream_doc`` with shim-injected fields.

    Fields injected:
        registration_endpoint
            URL the MCP client POSTs to for RFC 7591 DCR.
        registration_endpoint_auth_methods_supported
            "none" — anonymous DCR (Zitadel doesn't have RFC 7592, so
            registered clients can't be managed via the same auth path
            anyway; the shim's cleanup policy is server-side).

    The original document is not mutated — callers can hold long-lived
    cache references safely.
    """
    out = dict(upstream_doc)
    out["registration_endpoint"] = registration_endpoint
    out["registration_endpoint_auth_methods_supported"] = ["none"]
    return out


# ---------------------------------------------------------------------------
# Bridge URL helpers


def derive_registration_endpoint(*, bridge_public_url: str) -> str:
    """Build the RFC 7591 registration_endpoint URL on the bridge.

    bridge_public_url is the same value used by chunk B's resource
    identifier (see daemon._resolve_public_resource_url). The path is
    fixed at /oauth/register (matches the route the daemon serves).
    """
    return f"{bridge_public_url.rstrip('/')}/oauth/register"


__all__ = [
    "IdpConfig",
    "MetadataCache",
    "MetadataFetchError",
    "derive_registration_endpoint",
    "fetch_upstream_metadata",
    "overlay_metadata",
]
