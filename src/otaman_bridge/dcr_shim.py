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

    # Zitadel org_id — required as x-zitadel-orgid header on every
    # /management/v1/* call. Without it Zitadel returns 404.
    org_id: str = ""

    # Hostname Zitadel expects in the Host header (matches its
    # ExternalDomain config). Derived from management_base_url when
    # left unset. Production deployments behind a reverse proxy
    # often override this.
    expected_host: str = ""

    # Prefix that marks an OIDC app as shim-managed (for fingerprint
    # lookup + cleanup sweep — see design §6).
    managed_name_prefix: str = "dcr-shim:"

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
        mgmt = mgmt.rstrip("/")
        # Default Host header = host portion of management_base_url
        # (matches the bootstrap script's pattern). Override via env
        # when the bridge talks to Zitadel via a different name than
        # the one Zitadel checks against ExternalDomain.
        host_default = ""
        if mgmt.startswith(("http://", "https://")):
            host_default = mgmt.split("://", 1)[1].split("/", 1)[0]
        return cls(
            type=e.get("OTAMAN_DCR_SHIM_TYPE", "zitadel").strip().lower() or "zitadel",
            dcr_shim=True,
            management_base_url=mgmt,
            project_id=e.get("OIDC_PROJECT_ID", "").strip(),
            machine_user_client_id=e.get("OTAMAN_DCR_SHIM_CLIENT_ID", "").strip(),
            machine_user_client_secret=e.get("OTAMAN_DCR_SHIM_SECRET", "").strip(),
            org_id=(
                e.get("OTAMAN_DCR_SHIM_ORG_ID", "").strip()
                or e.get("OIDC_ORG_ID", "").strip()
            ),
            expected_host=(
                e.get("OTAMAN_DCR_SHIM_EXPECTED_HOST", "").strip() or host_default
            ),
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
    """Return a copy of ``upstream_doc`` with shim-injected + constrained fields.

    Fields injected:
        registration_endpoint
            URL the MCP client POSTs to for RFC 7591 DCR.
        registration_endpoint_auth_methods_supported
            ["none"] — anonymous DCR (Zitadel doesn't have RFC 7592, so
            registered clients can't be managed via the same auth path
            anyway; the shim's cleanup policy is server-side).

    Fields constrained (overwritten):
        token_endpoint_auth_methods_supported
            Locked to ["none"] regardless of what the upstream IdP
            advertises. The shim only ever creates PUBLIC clients
            (OIDC_AUTH_METHOD_TYPE_NONE + PKCE), so advertising
            other auth methods misleads the MCP client. Claude Code
            v2.1.143 was observed (2026-05-18) picking client_secret_basic
            from Zitadel's advertised list and then erroring at token
            exchange because no client_secret existed. Constraining to
            ["none"] forces the right method.
        code_challenge_methods_supported
            Locked to ["S256"] — public clients without secrets MUST use
            PKCE, and S256 is the only RFC-7636-compliant method.
            (Zitadel already advertises this; we constrain anyway as
            belt-and-braces against future IdPs that might add "plain".)

    The original document is not mutated — callers can hold long-lived
    cache references safely.
    """
    out = dict(upstream_doc)
    out["registration_endpoint"] = registration_endpoint
    out["registration_endpoint_auth_methods_supported"] = ["none"]
    out["token_endpoint_auth_methods_supported"] = ["none"]
    out["code_challenge_methods_supported"] = ["S256"]
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


# ---------------------------------------------------------------------------
# RFC 7591 registration — request/response shapes + validation


# RFC 7591 error codes we emit.
ERR_INVALID_REDIRECT_URI = "invalid_redirect_uri"
ERR_INVALID_CLIENT_METADATA = "invalid_client_metadata"
ERR_SERVER_ERROR = "server_error"

# Grant types we accept. Anything outside this set is invalid_client_metadata.
ALLOWED_GRANT_TYPES = frozenset({"authorization_code", "refresh_token"})
# Response types we accept.
ALLOWED_RESPONSE_TYPES = frozenset({"code"})


class DCRError(Exception):
    """RFC 7591 error response with code + description.

    Raised by parse/validate functions. The route handler catches and
    serializes into the 400 / 502 body shape RFC 7591 specifies.
    """

    def __init__(self, error: str, description: str, *, http_status: int = 400):
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description
        self.http_status = http_status


@dataclass(frozen=True)
class RegisterRequest:
    """Subset of RFC 7591 client metadata that we support."""

    redirect_uris: tuple[str, ...]
    client_name: str | None = None
    grant_types: tuple[str, ...] = ("authorization_code",)
    response_types: tuple[str, ...] = ("code",)
    token_endpoint_auth_method: str = "none"
    scope: str | None = None
    software_id: str | None = None
    software_version: str | None = None


def parse_register_request(body: Any) -> RegisterRequest:
    """Parse + validate an RFC 7591 registration request body.

    Per design §5.2 validation rules:
    - redirect_uris must be loopback http URIs only
    - token_endpoint_auth_method must be "none" (public + PKCE)
    - grant_types must be a subset of {authorization_code, refresh_token}
    - response_types must be a subset of {code}

    Raises DCRError on any failure.
    """
    if not isinstance(body, dict):
        raise DCRError(ERR_INVALID_CLIENT_METADATA, "request body must be a JSON object")

    redirects = body.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects:
        raise DCRError(ERR_INVALID_REDIRECT_URI, "redirect_uris is required and must be non-empty")
    for u in redirects:
        if not isinstance(u, str):
            raise DCRError(ERR_INVALID_REDIRECT_URI, "redirect_uris must contain strings")
        if not _is_loopback_http_uri(u):
            raise DCRError(
                ERR_INVALID_REDIRECT_URI,
                f"redirect_uri {u!r} must be http://localhost:<port>/... or http://127.0.0.1:<port>/...",
            )

    auth_method = body.get("token_endpoint_auth_method", "none")
    if auth_method != "none":
        raise DCRError(
            ERR_INVALID_CLIENT_METADATA,
            "token_endpoint_auth_method must be 'none' (the shim only emits public clients with PKCE)",
        )

    grant_types = body.get("grant_types", ["authorization_code"])
    if not isinstance(grant_types, list) or not grant_types:
        raise DCRError(ERR_INVALID_CLIENT_METADATA, "grant_types must be a non-empty array")
    bad = [g for g in grant_types if g not in ALLOWED_GRANT_TYPES]
    if bad:
        raise DCRError(
            ERR_INVALID_CLIENT_METADATA,
            f"grant_types contains unsupported value(s): {bad}; allowed: {sorted(ALLOWED_GRANT_TYPES)}",
        )

    response_types = body.get("response_types", ["code"])
    if not isinstance(response_types, list) or not response_types:
        raise DCRError(ERR_INVALID_CLIENT_METADATA, "response_types must be a non-empty array")
    bad_r = [r for r in response_types if r not in ALLOWED_RESPONSE_TYPES]
    if bad_r:
        raise DCRError(
            ERR_INVALID_CLIENT_METADATA,
            f"response_types contains unsupported value(s): {bad_r}; allowed: {sorted(ALLOWED_RESPONSE_TYPES)}",
        )

    def _str_or_none(key: str) -> str | None:
        v = body.get(key)
        if v is None:
            return None
        if not isinstance(v, str):
            raise DCRError(ERR_INVALID_CLIENT_METADATA, f"{key} must be a string")
        return v

    return RegisterRequest(
        redirect_uris=tuple(redirects),
        client_name=_str_or_none("client_name"),
        grant_types=tuple(grant_types),
        response_types=tuple(response_types),
        token_endpoint_auth_method=auth_method,
        scope=_str_or_none("scope"),
        software_id=_str_or_none("software_id"),
        software_version=_str_or_none("software_version"),
    )


def _is_loopback_http_uri(u: str) -> bool:
    """RFC 8252 §7.3 loopback-redirect check (http://localhost or 127.0.0.1)."""
    if not u.startswith("http://"):
        return False
    rest = u[len("http://"):]
    # Strip optional port + path.
    host = rest.split("/", 1)[0].split(":", 1)[0]
    return host in ("localhost", "127.0.0.1")


def compute_fingerprint(
    *,
    software_id: str | None,
    redirect_uris: tuple[str, ...] | list[str],
) -> str:
    """Deterministic 16-hex-char fingerprint over software_id + sorted redirects.

    Same inputs → same fingerprint → same app name → fingerprint-reuse
    in the daemon's find_or_create path. Different inputs → different
    fingerprint → new app created (TTL sweep prunes orphans later).
    """
    import hashlib
    parts = [software_id or "", *sorted(redirect_uris)]
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Zitadel management API client


class ZitadelMgmtError(Exception):
    """Zitadel mgmt API returned non-2xx or malformed response."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class ZitadelMgmtClient:
    """Minimal Zitadel mgmt API client used by the DCR shim.

    Authenticates via client_credentials grant against ``<issuer>/oauth/v2/token``.
    Caches the access token until ~30s before expiry. All mgmt calls
    include the ``x-zitadel-orgid`` header (required by Zitadel for
    org-scoped operations) and the ``Host`` header expected by Zitadel's
    ExternalDomain origin check.
    """

    # Refresh the access token this many seconds before its claimed expiry,
    # to avoid racing the boundary while a request is in flight.
    _TOKEN_REFRESH_LEEWAY = 30

    def __init__(
        self,
        *,
        base_url: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        org_id: str,
        expected_host: str = "",
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.org_id = org_id
        self.expected_host = expected_host
        self._opener = opener
        # Token cache.
        self._token_lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    # ---- auth ----------------------------------------------------------

    def _get_access_token(self, *, now: float | None = None) -> str:
        """Return a fresh access token, minting via client_credentials if needed."""
        n = now if now is not None else time.monotonic()
        with self._token_lock:
            if self._access_token and n < self._token_expires_at - self._TOKEN_REFRESH_LEEWAY:
                return self._access_token
            # Mint
            tok = self._fetch_client_credentials_token()
            self._access_token = tok["access_token"]
            expires_in = int(tok.get("expires_in", 3600))
            self._token_expires_at = n + max(60, expires_in)
            return self._access_token

    def _fetch_client_credentials_token(self) -> dict:
        """POST to /oauth/v2/token with client_credentials + Zitadel-IAM scope.

        The scope ``urn:zitadel:iam:org:project:id:zitadel:aud`` is what
        Zitadel uses to authorize the bearer for /management/v1 calls.
        """
        import base64
        body = (
            "grant_type=client_credentials"
            "&scope=" + urllib.parse.quote(
                "openid urn:zitadel:iam:org:project:id:zitadel:aud"
            )
        ).encode("utf-8")
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
        }
        if self.expected_host:
            headers["Host"] = self.expected_host
        req = urllib.request.Request(
            self.token_url, data=body, headers=headers, method="POST",
        )
        o = self._opener or urllib.request.build_opener()
        try:
            with o.open(req, timeout=10) as resp:
                if resp.status != 200:
                    raise ZitadelMgmtError(
                        f"client_credentials token request returned HTTP {resp.status}",
                        status=resp.status,
                    )
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            raise ZitadelMgmtError(
                f"client_credentials request HTTP {e.code}: {text}",
                status=e.code, body=text,
            ) from e
        except urllib.error.URLError as exc:
            raise ZitadelMgmtError(f"token endpoint unreachable: {exc}") from exc
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise ZitadelMgmtError("token response missing access_token")
        return payload

    # ---- mgmt API calls ------------------------------------------------

    def _mgmt_request(self, method: str, path: str, *, body: dict | None = None) -> dict:
        access_token = self._get_access_token()
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "x-zitadel-orgid": self.org_id,
        }
        if data:
            headers["Content-Type"] = "application/json"
        if self.expected_host:
            headers["Host"] = self.expected_host
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        o = self._opener or urllib.request.build_opener()
        try:
            with o.open(req, timeout=15) as resp:
                payload = resp.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            raise ZitadelMgmtError(
                f"Zitadel API {method} {path} HTTP {e.code}: {text}",
                status=e.code, body=text,
            ) from e
        except urllib.error.URLError as exc:
            raise ZitadelMgmtError(
                f"Zitadel API {method} {path} unreachable: {exc}",
            ) from exc

    def find_app_by_name(self, *, project_id: str, name: str) -> dict | None:
        """Search project apps by exact-match name. Returns the first match or None."""
        r = self._mgmt_request(
            "POST",
            f"/management/v1/projects/{project_id}/apps/_search",
            body={
                "queries": [
                    {"nameQuery": {"name": name, "method": "TEXT_QUERY_METHOD_EQUALS"}}
                ]
            },
        )
        results = r.get("result") or []
        return results[0] if results else None

    def create_oidc_app(self, *, project_id: str, payload: dict) -> dict:
        """POST /management/v1/projects/{id}/apps/oidc. Returns the create response."""
        return self._mgmt_request(
            "POST",
            f"/management/v1/projects/{project_id}/apps/oidc",
            body=payload,
        )


# ---------------------------------------------------------------------------
# DCR orchestration (find-or-create) + response shaping


def build_zitadel_oidc_payload(
    *,
    name: str,
    redirect_uris: tuple[str, ...] | list[str],
    grant_types: tuple[str, ...] | list[str],
) -> dict:
    """Build the Zitadel mgmt-API request body for creating a public PKCE app.

    Always uses OIDC_APP_TYPE_NATIVE (Claude Code = CLI client opening
    a localhost callback) + OIDC_AUTH_METHOD_TYPE_NONE (public client,
    PKCE-required by Zitadel for this auth method).
    """
    zitadel_grants = []
    if "authorization_code" in grant_types:
        zitadel_grants.append("OIDC_GRANT_TYPE_AUTHORIZATION_CODE")
    if "refresh_token" in grant_types:
        zitadel_grants.append("OIDC_GRANT_TYPE_REFRESH_TOKEN")
    return {
        "name": name,
        "redirectUris": list(redirect_uris),
        "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
        "grantTypes": zitadel_grants,
        "appType": "OIDC_APP_TYPE_NATIVE",
        "authMethodType": "OIDC_AUTH_METHOD_TYPE_NONE",
        "version": "OIDC_VERSION_1_0",
        "devMode": False,
        "accessTokenType": "OIDC_TOKEN_TYPE_BEARER",
        "accessTokenRoleAssertion": False,
        "idTokenRoleAssertion": True,
        "idTokenUserinfoAssertion": False,
        "clockSkew": "0s",
        "additionalOrigins": [],
        "postLogoutRedirectUris": [],
    }


def _extract_client_id_from_create(create_resp: dict) -> str:
    """Zitadel create response shape: {appId, clientId, ...}."""
    cid = create_resp.get("clientId") or ""
    if not cid:
        raise ZitadelMgmtError(f"create response missing clientId: {create_resp!r}")
    return cid


def _extract_client_id_from_search(app: dict) -> str:
    """Search result shape: {id, oidcConfig: {clientId, ...}, ...}."""
    oidc_cfg = app.get("oidcConfig") or {}
    cid = oidc_cfg.get("clientId") or ""
    if not cid:
        raise ZitadelMgmtError(f"search result missing oidcConfig.clientId: {app!r}")
    return cid


def find_or_create_client(
    *,
    mgmt_client: ZitadelMgmtClient,
    project_id: str,
    request: RegisterRequest,
    name_prefix: str = "dcr-shim:",
) -> str:
    """Idempotent: same fingerprint → same Zitadel app → same client_id.

    Used by the /oauth/register route handler. Returns the Zitadel
    clientId string. Raises ZitadelMgmtError on any upstream failure.
    """
    fp = compute_fingerprint(
        software_id=request.software_id, redirect_uris=request.redirect_uris,
    )
    name = f"{name_prefix}{fp}"
    existing = mgmt_client.find_app_by_name(project_id=project_id, name=name)
    if existing is not None:
        return _extract_client_id_from_search(existing)
    payload = build_zitadel_oidc_payload(
        name=name,
        redirect_uris=request.redirect_uris,
        grant_types=request.grant_types,
    )
    try:
        created = mgmt_client.create_oidc_app(project_id=project_id, payload=payload)
    except ZitadelMgmtError as exc:
        # Race: another laptop registered the same fingerprint between
        # our search and our create. Retry the lookup once.
        if exc.status == 409 or "already exists" in (exc.body or "").lower():
            re_lookup = mgmt_client.find_app_by_name(project_id=project_id, name=name)
            if re_lookup is not None:
                return _extract_client_id_from_search(re_lookup)
        raise
    return _extract_client_id_from_create(created)


def to_rfc7591_response(
    *, request: RegisterRequest, client_id: str, now_unix: int,
) -> dict:
    """Shape a successful create as an RFC 7591 client_information_response.

    Public clients don't get a secret; we emit ``client_secret: ""`` for
    spec-strict clients that prefer the field present over absent.
    """
    out: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": now_unix,
        "client_secret": "",
        "redirect_uris": list(request.redirect_uris),
        "grant_types": list(request.grant_types),
        "response_types": list(request.response_types),
        "token_endpoint_auth_method": request.token_endpoint_auth_method,
    }
    if request.client_name:
        out["client_name"] = request.client_name
    if request.scope:
        out["scope"] = request.scope
    if request.software_id:
        out["software_id"] = request.software_id
    if request.software_version:
        out["software_version"] = request.software_version
    return out


__all__ = [
    "ALLOWED_GRANT_TYPES",
    "ALLOWED_RESPONSE_TYPES",
    "DCRError",
    "ERR_INVALID_CLIENT_METADATA",
    "ERR_INVALID_REDIRECT_URI",
    "ERR_SERVER_ERROR",
    "IdpConfig",
    "MetadataCache",
    "MetadataFetchError",
    "RegisterRequest",
    "ZitadelMgmtClient",
    "ZitadelMgmtError",
    "build_zitadel_oidc_payload",
    "compute_fingerprint",
    "derive_registration_endpoint",
    "fetch_upstream_metadata",
    "find_or_create_client",
    "overlay_metadata",
    "parse_register_request",
    "to_rfc7591_response",
]
