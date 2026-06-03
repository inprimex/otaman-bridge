"""Bridge -> runner HTTP client.

Reads the runner's endpoint file (host, port, bearer token) and exposes
read methods for the bridge's MCP tools to consume. Currently used by
``list_team_sessions``; future tools call additional methods here.

Errors:
- RunnerUnreachableError: endpoint file missing OR network/HTTP failure.
  Distinct from "runner returned an empty list" -- the MCP layer maps
  this to a degraded/error response so the LLM can say "list unavailable"
  vs "no team sessions".
- RunnerAuthError: 401 from runner. Means the loopback bearer in the
  endpoint file is stale (runner restarted). Caller can retry by
  re-reading the endpoint file.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_RUNNER_ENDPOINT = Path.home() / ".otaman" / "runner.endpoint"


class RunnerUnreachableError(RuntimeError):
    """Runner can't be reached: endpoint file missing, connection refused,
    timeout, network failure. NOT raised on 4xx/5xx HTTP responses."""


class RunnerAuthError(RuntimeError):
    """Runner returned 401 -- bearer token in endpoint file is stale."""


class SessionNotFoundError(RuntimeError):
    """Runner returned 404 from /kill -- session_id not in the active registry.

    Distinguished from RunnerUnreachableError because the caller can tell
    the user "no such session" instead of a generic "runner failed".
    """


class SpawnError(RuntimeError):
    """Runner returned an error on POST /spawn.

    Distinct from RunnerUnreachableError (network-level failure) -- this means
    the runner was reached but rejected the spawn request (e.g. 4xx/5xx).
    """


class RunnerClient:
    """Read-only client for the runner's loopback HTTP API."""

    def __init__(
        self,
        *,
        endpoint_file: Path | None = None,
        timeout: float = 5.0,
        opener=None,
    ) -> None:
        self.endpoint_file = endpoint_file or DEFAULT_RUNNER_ENDPOINT
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()

    def _read_endpoint(self):
        if not self.endpoint_file.is_file():
            raise RunnerUnreachableError(
                f"runner endpoint file not found: {self.endpoint_file}"
            )
        try:
            text = self.endpoint_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunnerUnreachableError(
                f"failed reading runner endpoint file: {exc}"
            ) from exc
        host, port, token = "127.0.0.1", None, None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k == "host":
                host = v
            elif k == "port":
                port = int(v)
            elif k == "token":
                token = v
        if not port or not token:
            raise RunnerUnreachableError(
                f"runner endpoint file missing required fields: {self.endpoint_file}"
            )
        return host, port, token

    def list_sessions(self) -> list[dict]:
        """Call runner GET /sessions and return the session dicts.

        Each entry has at minimum: session_id, user, agent, repo,
        session_name, started_at. ``user`` may be empty string for
        sessions spawned outside the team-mode flow.
        """
        host, port, token = self._read_endpoint()
        url = f"http://{host}:{port}/sessions"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RunnerAuthError(
                    f"runner rejected loopback bearer (HTTP 401) -- token may be stale"
                ) from exc
            raise RunnerUnreachableError(
                f"runner returned HTTP {exc.code} on {url}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RunnerUnreachableError(
                f"runner unreachable at {url}: {exc}"
            ) from exc
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RunnerUnreachableError(
                f"runner response was not valid JSON: {exc}"
            ) from exc
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            raise RunnerUnreachableError(
                f"runner /sessions response missing 'sessions' list: {data}"
            )
        return sessions


    def spawn(self, agent: str, human: str, mode: str, context: dict) -> str:
        """Call runner POST /spawn; return session_id on success.

        Provisional API — coordinate with runner-agent on /spawn schema (task 2.3).
        Body: {agent, human, mode, context}. Response: {session_id, status}.

        Raises:
        - RunnerUnreachableError on network/connection failures.
        - RunnerAuthError on 401 (stale bearer).
        - SpawnError on 4xx/5xx indicating the runner rejected the request.
        """
        host, port, token = self._read_endpoint()
        url = f"http://{host}:{port}/spawn"
        body = json.dumps({"agent": agent, "human": human, "mode": mode, "context": context}).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RunnerAuthError(
                    "runner rejected loopback bearer (HTTP 401) -- token may be stale"
                ) from exc
            detail = ""
            try:
                detail = exc.read().decode()
            except Exception:
                pass
            raise SpawnError(
                f"runner returned HTTP {exc.code} on POST /spawn: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RunnerUnreachableError(
                f"runner unreachable at {url}: {exc}"
            ) from exc
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise SpawnError(f"runner /spawn response missing session_id: {data}")
        return session_id

    def kill_session(self, session_id: str) -> None:
        """Call runner POST /kill with the given session_id.

        Returns silently on 204 (session stopped). Raises:
        - SessionNotFoundError on 404 (no such session in the registry)
        - RunnerAuthError on 401 (stale bearer)
        - RunnerUnreachableError on network/HTTP failures

        Note: the runner authenticates the bridge's loopback bearer here
        and trusts that the bridge has already authorized the caller.
        The bridge's MCP tool (build_kill_session_for_user_tool) enforces
        the otaman:admin role check before calling this method.
        """
        if not session_id or not isinstance(session_id, str):
            raise ValueError("session_id must be a non-empty string")
        host, port, token = self._read_endpoint()
        url = f"http://{host}:{port}/kill"
        body = json.dumps({"session_id": session_id}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                _ = resp.read()  # drain
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RunnerAuthError(
                    "runner rejected loopback bearer (HTTP 401) -- token may be stale"
                ) from exc
            if exc.code == 404:
                raise SessionNotFoundError(
                    f"session not found: {session_id}"
                ) from exc
            raise RunnerUnreachableError(
                f"runner returned HTTP {exc.code} on {url}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RunnerUnreachableError(
                f"runner unreachable at {url}: {exc}"
            ) from exc


__all__ = [
    "DEFAULT_RUNNER_ENDPOINT",
    "RunnerUnreachableError",
    "RunnerAuthError",
    "SessionNotFoundError",
    "SpawnError",
    "RunnerClient",
]
