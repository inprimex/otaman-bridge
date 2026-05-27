# `/healthz` endpoint contract

**Change**: `containerized-agent-execution` task 4.1  
**Owner**: bridge-agent  
**Status**: implemented (see `src/otaman_bridge/daemon.py:handle_healthz`)

---

## Endpoint

```
GET /healthz
```

No authentication required. Docker compose healthchecks run without
credentials; requiring auth would make the container permanently unhealthy
when the credential is rotated.

---

## Responses

### 200 OK — healthy

```json
{
  "status": "ok",
  "account": "my-account",
  "uptime_seconds": 42
}
```

When a workspace is configured (``--watch-bus``), the response also includes
runtime-mode fields from ``experimental_mode.healthz_extras()``:

```json
{
  "status": "ok",
  "account": "my-account",
  "uptime_seconds": 42,
  "runtime_mode": "experimental_multi_tenant",
  "experimental_warning": "⚠️ EXPERIMENTAL MULTI-TENANT MODE — not validated for production; data isolation not audited"
}
```

### 503 Service Unavailable — degraded

```json
{
  "status": "degraded",
  "reason": "shutdown in progress"
}
```

Currently the only degraded condition is a shutdown in progress.  v2 may
add transport-liveness and bus-watcher-lag checks.

---

## Docker Compose integration

```yaml
services:
  bridge:
    image: otaman/org-ce:latest
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:${BRIDGE_PORT:-8080}/healthz"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 5s
```

The ``start_period`` absorbs the startup time before the daemon begins
responding.  ``retries: 3`` means the container is marked unhealthy only
after three consecutive failures — tolerates brief stalls during bus-watcher
initialisation.

---

## Relationship to `/status`

| | `/status` | `/healthz` |
|---|---|---|
| Auth required | No | No |
| Purpose | Operator introspection (counts, transport name, PID) | Orchestrator liveness probe |
| Returns | Full status dict | Minimal ok/degraded + uptime |
| In compose healthcheck | Not recommended (verbose, unstable schema) | Yes |
| Transport/bus fields | Yes | No (v1) |

`/status` is stable for human-facing tools.  `/healthz` is stable for
automated probes; its schema is deliberately narrow so adding fields is
non-breaking.

---

## v2 extended health checks (deferred)

- **Transport liveness**: ping the Telegram API or OIDC issuer and include
  ``"transport": "ok" | "degraded"`` in the response.
- **Bus-watcher lag**: report time since the last successful bus scan; flag
  if lag > 2× poll interval.
- **State-file age**: report `.otaman/bus-surfaced.state` mtime; old mtime
  with active messages suggests the watcher is stuck.
