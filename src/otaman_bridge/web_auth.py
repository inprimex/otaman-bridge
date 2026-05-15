"""Authorization Code + PKCE login flow helper for the bridge web UI.

Per Zitadel integration spec sec. 4.3, the bridge's web login uses OAuth 2.0
Authorization Code Grant with PKCE (RFC 7636) against Zitadel. This module
owns the state-machine pieces of that flow that don't touch HTTP:

- ``WebAuthConfig`` -- where Zitadel lives + this bridge's client id +
  the callback URL we registered with Zitadel.
- ``LoginFlow`` -- builds the authorization request URL with PKCE
  challenge + opaque state token, returns both the URL and the data
  the callback will need to verify the response (state, code verifier).
- ``PendingLoginStore`` -- thread-safe map of state -> (code_verifier,
  expires_at) so the callback handler can recover the verifier when the
  user comes back from Zitadel.

PKCE follows RFC 7636 sec. 4.1 (code_verifier = 43-128 unreserved chars,
recommended 32 random bytes base64url-encoded) and sec. 4.2 (code_challenge
= base64url(sha256(code_verifier)), method = S256).

State follows RFC 6749 sec. 4.1.1 (opaque, unguessable; bridge stores it
server-side keyed in PendingLoginStore so the callback can verify it).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass

DEFAULT_PENDING_TTL = 600.0
DEFAULT_SCOPES: tuple[str, ...] = (
    "openid",
    "profile",
    "email",
    "urn:zitadel:iam:org:projects:roles",
)


@dataclass(frozen=True)
class WebAuthConfig:
    """Where Zitadel lives + this bridge's OIDC client identity."""

    issuer: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    project_id: str | None = None

    def authorize_endpoint(self) -> str:
        return f"{self.issuer.rstrip('/')}/oauth/v2/authorize"

    def token_endpoint(self) -> str:
        return f"{self.issuer.rstrip('/')}/oauth/v2/token"

    def effective_scopes(self) -> tuple[str, ...]:
        if self.project_id:
            aud = f"urn:zitadel:iam:org:project:id:{self.project_id}:aud"
            if aud not in self.scopes:
                return tuple(self.scopes) + (aud,)
        return tuple(self.scopes)


@dataclass(frozen=True)
class StartedLogin:
    """What ``LoginFlow.start()`` returns -- everything the daemon needs
    to redirect the user and remember the flow until the callback fires."""

    authorize_url: str
    state: str
    code_verifier: str


class PendingLoginStore:
    """Thread-safe map of state -> (code_verifier, expires_at).

    Lazy expiration on take(). Callers must invoke take() (which both
    returns AND removes) so the verifier can never be replayed.
    """

    def __init__(self, *, ttl: float = DEFAULT_PENDING_TTL, clock=None) -> None:
        self.ttl = ttl
        self._clock = clock or time.time
        self._pending: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def put(self, state: str, code_verifier: str) -> None:
        with self._lock:
            self._pending[state] = (code_verifier, self._clock() + self.ttl)

    def take(self, state):
        """Pop and return the verifier for ``state`` if present + not expired.

        Returns ``None`` for unknown / expired state. Always removes the
        entry on success, so a returned state can never be replayed.
        """
        if not state:
            return None
        with self._lock:
            entry = self._pending.pop(state, None)
            if entry is None:
                return None
            verifier, expires_at = entry
            if self._clock() >= expires_at:
                return None
            return verifier

    def purge_expired(self) -> int:
        now = self._clock()
        with self._lock:
            stale = [s for s, (_, exp) in self._pending.items() if now >= exp]
            for s in stale:
                del self._pending[s]
        return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


class LoginFlow:
    """Builds Authorization Code + PKCE requests against Zitadel."""

    def __init__(
        self,
        config: WebAuthConfig,
        store: PendingLoginStore,
        *,
        rng=None,
    ) -> None:
        self.config = config
        self.store = store
        self._rng = rng or secrets.token_urlsafe

    def start(self) -> StartedLogin:
        """Generate state + PKCE verifier, register state in the store,
        and build the authorize URL the user should be redirected to."""
        state = self._rng(32)
        code_verifier = self._rng(64)
        challenge = self._pkce_challenge(code_verifier)
        self.store.put(state, code_verifier)

        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.config.effective_scopes()),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = f"{self.config.authorize_endpoint()}?{urllib.parse.urlencode(params)}"
        return StartedLogin(authorize_url=url, state=state, code_verifier=code_verifier)

    @staticmethod
    def _pkce_challenge(code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


__all__ = [
    "DEFAULT_PENDING_TTL",
    "DEFAULT_SCOPES",
    "WebAuthConfig",
    "StartedLogin",
    "PendingLoginStore",
    "LoginFlow",
]


import json as _json
import urllib.error as _urlerror
import urllib.parse as _urlparse
import urllib.request as _urlrequest


@dataclass(frozen=True)
class TokenResponse:
    """The /oauth/v2/token response we care about (RFC 6749 sec. 5.1)."""

    access_token: str
    id_token: str | None
    refresh_token: str | None
    expires_in: int
    token_type: str

    @classmethod
    def from_dict(cls, d) -> "TokenResponse":
        if "access_token" not in d:
            raise ValueError(f"token response missing access_token: keys={sorted(d)}")
        return cls(
            access_token=str(d["access_token"]),
            id_token=d.get("id_token"),
            refresh_token=d.get("refresh_token"),
            expires_in=int(d.get("expires_in", 0)),
            token_type=str(d.get("token_type", "Bearer")),
        )


class TokenExchangeError(RuntimeError):
    """Raised when /oauth/v2/token returns an error or is unreachable.

    Distinct from a token-validation error -- this fires before we even
    have a token to validate. Callers map this to a 502 / 504 response,
    not a 401.
    """


class TokenExchanger:
    """Exchanges an authorization code for a token set against Zitadel.

    Implements the RFC 6749 sec. 4.1.3 token request with PKCE
    code_verifier (RFC 7636 sec. 4.5). Public client style (no client
    secret); confidential-client mode is added later if Zitadel
    requires it for our app type.
    """

    def __init__(
        self,
        config: WebAuthConfig,
        *,
        fetcher=None,
        timeout: float = 10.0,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self._fetcher = fetcher or self._default_fetcher

    def exchange_code(self, code: str, code_verifier: str) -> TokenResponse:
        """Exchange an authorization code for a token set.

        Raises ``TokenExchangeError`` on network failure, non-2xx HTTP
        response, or malformed JSON. Returns ``TokenResponse`` on
        success. Does NOT validate the id_token -- that's the caller's
        job (via OIDCValidator).
        """
        body = _urlparse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
        }).encode("ascii")
        try:
            payload = self._fetcher(self.config.token_endpoint(), body, self.timeout)
        except TokenExchangeError:
            raise
        except Exception as exc:  # noqa: BLE001 -- adapter boundary
            raise TokenExchangeError(f"token endpoint call failed: {exc}") from exc
        try:
            data = _json.loads(payload)
        except _json.JSONDecodeError as exc:
            raise TokenExchangeError(f"token response not JSON: {exc}") from exc
        if "error" in data:
            desc = data.get("error_description", "")
            raise TokenExchangeError(f"token endpoint error: {data['error']}: {desc}")
        try:
            return TokenResponse.from_dict(data)
        except ValueError as exc:
            raise TokenExchangeError(str(exc)) from exc

    @staticmethod
    def _default_fetcher(url: str, body: bytes, timeout: float) -> bytes:
        req = _urlrequest.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with _urlrequest.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except _urlerror.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            try:
                data = _json.loads(text)
                err = data.get("error", "")
                desc = data.get("error_description", "")
                raise TokenExchangeError(f"HTTP {e.code} from token endpoint: {err}: {desc}")
            except _json.JSONDecodeError:
                raise TokenExchangeError(f"HTTP {e.code} from token endpoint: {text[:200]}")
        except _urlerror.URLError as e:
            raise TokenExchangeError(f"network error contacting token endpoint: {e}") from e


__all__.extend([
    "TokenResponse",
    "TokenExchangeError",
    "TokenExchanger",
])
