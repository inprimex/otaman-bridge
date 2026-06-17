# /healthz Contract

**Change:** containerized-agent-execution task 4.1

## Endpoint

```
GET /healthz
```

No `Authorization` header required — container orchestrators probe this unauthenticated.

## Responses

| Code | Body | Meaning |
|------|------|---------|
| 200 | `{"ok": true, "uptime_seconds": N, "transport": "<name>"}` | Bridge is healthy and accepting requests |
| 503 | `{"ok": false, "reason": "shutdown in progress"}` | SIGTERM received; container should stop routing |
| 503 | `{"ok": false, "reason": "http server not started"}` | Internal race at startup (should not appear in steady state) |

## Implementation

`BridgeDaemon.handle_healthz()` in `daemon.py`. Registered as an unauthenticated GET route alongside `/status`.

Checks:
1. `_shutdown_requested` event — if set, returns 503.
2. `_server is None` — if true, returns 503.
3. Otherwise 200.

## Docker compose wiring

```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -sf http://localhost:${BRIDGE_PORT:-7860}/healthz || exit 1"]
  interval: 30s
  timeout: 5s
  retries: 3
  start_period: 10s
```

`start_period: 10s` gives the bridge time to bind the port before the first probe counts as a failure.

## SIGTERM graceful shutdown budget

SIGTERM triggers `daemon.stop()` via the `_install_signal_handlers()` hook in `cli.py`.

Shutdown phases and their worst-case wall-clock budget:

| Phase | Timeout |
|-------|---------|
| HTTP server shutdown + pending approval cancellation | ~0s (immediate) |
| Listener future cancel + drain | 1.0s |
| Bus watcher stop + future cancel | 2.0s |
| Idle-AFK monitor stop + future cancel | 2.0s |
| Transport close (e.g. Telegram polling stop) | 4.0s |
| **Total** | **9.0s** |

Docker's default SIGKILL grace period is 10s. The 9s budget leaves 1s headroom. If a deployment's transport requires a longer close window, increase `stop_grace_period` in the compose service definition rather than extending the in-process timeout.
