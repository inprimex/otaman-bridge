# Bridge Routing Client — Research (tasks 2.1, 2.4)

**Author**: bridge-agent  
**Date**: 2026-06-03  
**Change**: otaman-router-v1-design  
**Output location**: `otaman-bridge/research/bridge-routing-client.md`

---

## Part 1 — Routing Client Design (task 2.1)

### Summary

The bridge-side routing client is the component that calls `POST /route` at session-start
time, interprets the `RoutingDecision` response, and propagates the result to the adapter
selection logic. This document covers the call-site, error handling, fallback behaviour,
and the proposed implementation structure.

---

### Call-Site

The routing call happens at **session-start time** — before a Claude Code session is
launched. In the current bridge architecture this maps to:

**Mode 2+ (EE, auto-session-spawn)**: the `SpawnDecision` component (planned in
`auto-session-spawn-on-bus-events`) reads a bus event and decides whether to spawn a
session. The routing call is the first thing `SpawnDecision` does after deciding to spawn:
classify the task, build a `RoutingRequest`, call the router, then pass the
`RoutingDecision` to `RunnerClient.spawn()` so the runner launches the correct harness.

**Mode 1 (CE, manual launch)**: the routing client is called just before the bridge hands
off to the runner. If the router sidecar is not configured (`OTAMAN_ROUTER_URL` unset),
the client returns the default routing decision without making a network call (in-process
fallback, see §Fallback below).

Pseudocode for the call-site in the spawn path:

```python
async def spawn_session(spawn_req: SpawnRequest, ctx: CallContext) -> None:
    routing_decision = await routing_client.route(
        session_id=spawn_req.session_id,
        org_id=ctx.org_id or "default",
        user_id=ctx.user_id or None,
        roles=ctx.roles,
        task_type=spawn_req.task_type or "general",
        org_posture=load_org_posture(ctx.org_id),
    )
    runner_client.spawn(
        spawn_req.with_harness(routing_decision.harness, routing_decision.backend),
    )
```

---

### Request Construction

The routing client constructs a `RoutingRequest` from the bridge's session context:

```python
from datetime import datetime, timezone
from otaman_core.routing import RoutingRequest, DataClassification


def build_routing_request(
    *,
    session_id: str,
    org_id: str,
    user_id: str | None,
    roles: tuple[str, ...],
    task_type: str,
    org_posture: DataClassification,
    cost_budget_remaining_usd: float | None = None,
    preferred_harness: str | None = None,
) -> RoutingRequest:
    classification = classify_task(
        org_posture=org_posture,
        user_roles=roles,
        task_type=task_type,
    )
    return RoutingRequest(
        session_id=session_id,
        org_id=org_id,
        user_id=user_id,
        roles=roles,
        task_classification=classification,
        task_type=task_type,
        cost_budget_remaining_usd=cost_budget_remaining_usd,
        preferred_harness=preferred_harness,
        timestamp=datetime.now(tz=timezone.utc),
    )
```

---

### HTTP Call

The routing client calls the router sidecar using a lightweight synchronous HTTP call
(same pattern as `RunnerClient` — `urllib.request`, no third-party HTTP library):

```python
import json
import urllib.request
import urllib.error

ROUTER_ENDPOINT_ENV = "OTAMAN_ROUTER_URL"  # e.g. "http://router:8080"
DEFAULT_ROUTER_TIMEOUT = 2.0  # seconds; sub-ms expected latency


class RoutingClient:
    def __init__(
        self,
        router_url: str | None = None,
        timeout: float = DEFAULT_ROUTER_TIMEOUT,
    ) -> None:
        self._url = (router_url or os.environ.get(ROUTER_ENDPOINT_ENV, "")).rstrip("/")
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def route(self, req: RoutingRequest) -> RoutingDecision:
        if not self.enabled:
            return _default_decision(req)

        payload = json.dumps(_request_to_dict(req)).encode()
        http_req = urllib.request.Request(
            f"{self._url}/v1/route",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_req, timeout=self._timeout) as resp:
                body = json.loads(resp.read())
            return _decision_from_dict(body)
        except urllib.error.HTTPError as exc:
            return _handle_http_error(exc, req)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return _handle_network_error(exc, req)
```

---

### Error Handling

#### `409 Conflict` — no eligible backend

The router returns 409 when all backends are blocked by compliance + budget constraints.
The response body includes which rule blocked and which constraint was violated:

```json
{
  "error": "no_eligible_backend",
  "blocked_by": "compliance",
  "rule": 1,
  "detail": "org-acme requires PHI-cleared backend; no PHI-cleared backend is available"
}
```

**Bridge action**: fail the session with a structured error message to the user/bus.
Do NOT fall back to the default backend — the compliance block is intentional. Log the
409 at `WARNING` level with the routing_id for audit.

```python
if exc.code == 409:
    body = json.loads(exc.read())
    raise RoutingBlockedError(
        f"No eligible backend: {body.get('detail', exc.reason)}",
        rule=body.get("rule"),
        blocked_by=body.get("blocked_by"),
    )
```

#### `503 Service Unavailable` — router starting up

The router returns 503 during startup (secrets not yet resolved, config loading). This
is transient.

**Bridge action**: retry up to 3 times with 500ms backoff. If all retries fail, fall back
to the default decision (see §Fallback). Log at `WARNING`.

#### Network errors (timeout, connection refused)

The router sidecar may be temporarily unreachable (container restart, cold start).

**Bridge action**: fall back to the default decision immediately. Log at `WARNING` with
the error. Do NOT block the session on router unavailability — routing is best-effort
infrastructure; sessions must proceed when the router is down.

---

### Fallback Behaviour

When the router is unavailable (no URL configured, network error, 503 after retries),
the routing client returns a **default decision**:

```python
def _default_decision(req: RoutingRequest) -> RoutingDecision:
    return RoutingDecision(
        harness="claude-code",  # platform default
        backend="anthropic",  # platform default
        model=None,  # let the harness pick
        rule_matched="default",
        cost_estimate_usd=None,
        compliance_cleared=True,  # operator responsibility in fallback mode
        routing_id=f"fallback-{req.session_id}",
    )
```

The fallback decision uses the platform's configured default harness/backend. The
`routing_id` prefix `"fallback-"` makes fallback decisions identifiable in audit logs.

**Design note**: the fallback is intentionally permissive (`compliance_cleared=True`).
In a production regulated environment, the operator should configure the router with
high availability (e.g., two sidecar replicas) so the fallback is never reached for
compliance-gated orgs. The bridge cannot know which backends are compliance-cleared
without the router's overlay evaluation — so it assumes the default is acceptable.
Operators requiring hard compliance enforcement should configure the bridge to reject
sessions when the router is unreachable (a future `routing.fail_open: false` flag).

---

### Adapter Selection Propagation

Once the routing decision is received, the bridge propagates `harness` and `backend`
to the runner's spawn call. Current bridge architecture:

- `RunnerClient.spawn()` today uses the `AccountConfig.config_dir` to select which
  Claude Code binary to use — there is no harness/backend concept yet.
- In `otaman-router-v1-impl`, the runner spawn API will be extended to accept
  `harness` and `backend` parameters. The bridge passes them from the `RoutingDecision`.
- For Mode 1 CE (single harness, single backend), the routing decision's `harness` and
  `backend` always match the one configured harness — the router's default rule (rule 4)
  guarantees this.

---

### Module Location

```
otaman-bridge/src/otaman_bridge/routing_client.py
```

Key exports:
- `RoutingClient` — the HTTP client class
- `RoutingBlockedError` — raised on 409 (no eligible backend)
- `build_routing_request()` — constructs `RoutingRequest` from session context
- `classify_task()` — see `task-classification-logic.md`

---

## Part 2 — Org Isolation Enforcement (task 2.4, Q6 validation)

### Summary

`RoutingRequest` must never carry cross-org context. This section validates that the bridge's
architecture makes cross-org requests structurally impossible.

### Enforcement Point

The bridge daemon is configured with a **single routing profile** at startup:

```
otaman bridge start --account <name>
```

Each routing profile (`launch-settings.yaml routing.<name>`) maps to exactly one `config_dir`
(one Claude Code identity) and, in Mode 2+, one Zitadel tenant/org. The daemon serves
requests for that one org for its entire lifetime.

There is no code path in the bridge that accepts a request from one org and routes it
to another org's backend. The org context flows as:

```
daemon startup
  └── AccountConfig.name = "<routing-profile>"
        └── idp_config.org_id = "<zitadel-org-id>"  (EE only; Mode 1: absent)

per-request
  └── CallContext.org_id = "<derived from JWT or deployment config>"
        └── RoutingRequest.org_id = CallContext.org_id
              └── Router loads orgs/<slug>/routing.yaml  (isolated per-request)
```

There is no shared state between requests from different orgs. The router process itself
is stateless — it reads the org overlay on each `/route` call and discards it.

### Cross-Org Requests Are Structurally Impossible

In the current bridge architecture:
1. **One bridge daemon = one routing profile = one org** (from `AccountConfig`).
2. **`CallContext.org_id`** (once added, per task 2.3) is derived from the JWT or
   deployment config — both are single-org in the current design.
3. The bridge has no multi-org session store, no org-switching API, and no endpoint that
   accepts `org_id` as a request parameter.

For a multi-tenant Otaman deployment (ADR-012), the correct architecture is **one bridge
daemon per org** (or one per routing profile that is scoped to one org). The `multi-tenant-org-runtime`
proposal governs how orgs are isolated at the execution level; the bridge is already
consistent with that design.

### Single-Org Boundary Documentation

**Enforcement point in code**: `daemon.py` `__init__` — the account/routing profile is
set once at construction and never mutated:

```python
class BridgeDaemon:
    def __init__(self, *, account: str, ...):
        self.account = account   # immutable; single routing profile for lifetime
        # idp_config derived from AccountConfig for this account
```

**Future note**: if a future feature introduces multi-org bridge instances (e.g., a
single bridge serving multiple orgs behind a tenant-dispatch layer), the org isolation
enforcement point must move to request-time validation: assert `CallContext.org_id ==
request.claimed_org_id` before constructing the `RoutingRequest`. This is explicitly
NOT needed in v1 — one bridge, one org.

### Q6 Design Decision Confirmation

The design.md Q6 direction is **confirmed correct**:

> One router process per deployment; per-org routing decisions enforced via per-org
> `routing.yaml` overlays. Bridge never receives cross-org requests in a single call.

The bridge's single-account architecture guarantees org isolation. No additional
enforcement is required beyond what already exists.
