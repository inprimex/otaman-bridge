# Bridge-Side Routing Client -- Research (tasks 2.1 + 2.4)

**Author**: bridge-agent
**Date**: 2026-05-27
**Change**: otaman-router-v1-design
**Output location**: src/otaman_bridge/routing_client.py (proposed)

---

## Summary

Two tasks are covered here:

- **Task 2.1** -- Design the bridge-side routing client: the component that calls
  POST /v1/route at session start, handles the RoutingDecision response, and propagates
  it to adapter selection.  Covers call-site design, error handling (409/503), and
  fallback behaviour when the router is unavailable.

- **Task 2.4** -- Validate Q6 (single-org boundary): confirm the bridge never passes
  cross-org context in a single RoutingRequest.  Documents the enforcement point.

---

## Task 2.1 -- Bridge Routing Client

### Where Does Routing Fit in Session Start?

In v1, the bridge has one session-spawn path: **bus-triggered sessions** via
BusWatcher.on_approval.  A spec-change-request or equivalent message arrives; the
bridge surfaces an approval request to the human operator.  When approved, a session
adapter is instantiated for the task.

The routing call belongs at the point of **adapter selection** -- just before
instantiating a SessionAdapter.  Current bridge code has no RouterClient class and
no POST /route call; this module is entirely new.

Call stack placement:

```
BusWatcher._scan_once()
  |-- on_approval(ApprovalRequest, BusMessage)  [callback]
       |-- bridge approval handler
            |-- RouterClient.route(RoutingRequest) -> RoutingDecision  <- NEW
            +-- SessionAdapter.instantiate(harness, backend, model)
```

---

### Proposed Module: routing_client.py

Location: src/otaman_bridge/routing_client.py

Two deployment modes configured via .otaman/routing.yaml or OTAMAN_ROUTER_URL env var:

- **Sidecar HTTP** (Mode 3/4 -- container compose): POST http://router:8080/v1/route
- **In-process** (Mode 2 -- no container): call RouterEngine.route() directly

Key class outline:

```python
ROUTER_URL_ENV = "OTAMAN_ROUTER_URL"
DEFAULT_ROUTER_URL = "http://router:8080"

# CE fallback: rule-4 (default) hardcoded outcome.
# Used when router is unreachable AND fallback_policy == "ce_default".
_CE_FALLBACK = RoutingDecision(
    harness="claude-code",
    backend="anthropic",
    model="claude-sonnet-4-6",
    rule_matched="default",
    cost_estimate_usd=None,
    compliance_cleared=True,
    routing_id="route-fallback",
)

class RouterUnavailableError(RuntimeError): ...

@dataclass
class HttpRouterClient:
    base_url: str = ""
    timeout_seconds: float = 5.0
    fallback_policy: str = "ce_default"  # "ce_default" | "error"

    async def route(self, req: RoutingRequest) -> RoutingDecision:
        url = f"{self.base_url}/v1/route"
        for attempt in range(3):
            try:
                status, body = await _http_post(url, _to_json(req), self.timeout_seconds)
            except OSError as exc:
                if attempt == 0: await asyncio.sleep(1.0); continue
                return self._fallback(exc)
            if status == 200: return _parse_decision(body)
            if status == 409: _raise_409(body)  # raises RoutingNoEligibleBackend etc.
            if status == 503:
                await asyncio.sleep(2 ** attempt); continue
            raise RuntimeError(f"Unexpected status {status}")
        return self._fallback(RouterNotReady("503 after 3 attempts"))

    def _fallback(self, exc):
        if self.fallback_policy == "error":
            raise RouterUnavailableError("Router unavailable") from exc
        _log.warning("router unreachable; CE default fallback")
        return _CE_FALLBACK
```

---

### Error Handling

#### 409 -- No Eligible Backend

Returned when no backend survives the router rule chain.

Response body:

```json
{
  "error":        "routing_no_eligible_backend",
  "rule_blocked": "compliance",
  "constraint":   "task_classification=phi; no backend with phi clearance in org-acme"
}
```

**Bridge action**:

1. Catch RoutingNoEligibleBackend / RoutingBudgetExceeded in the approval handler.
2. Surface a blocking InfoMessage to the operator via the configured transport.
3. **Do NOT apply fallback.**  A 409 is a deliberate compliance refusal from the
   router; bypassing it would violate the deployment compliance posture.
4. Write a task-blocked bus message so the session appears as blocked in the queue
   (not silently dropped).  NOTE: task-blocked message type may need a spec addition
   -- see Open Question 5.

#### 503 -- Router Not Ready

Returned during startup when secrets are unresolved or config is not yet loaded.

**Bridge action**: retry with exponential backoff (immediate, +1s, +2s).
After 3 failures apply fallback_policy.

#### Network Failure (OSError, timeout, DNS failure)

**Bridge action**: single retry after 1s, then apply fallback_policy.

#### Fallback Policy

Configured in .otaman/routing.yaml:

```yaml
router:
  url: http://router:8080
  fallback: ce_default   # CE: return rule-4 hardcoded decision
  # fallback: error      # EE: raise RouterUnavailableError -> operator alerted
```

**ce_default** -- return _CE_FALLBACK (claude-code + anthropic + claude-sonnet-4-6).
Safe for CE because CE has no compliance restrictions beyond platform routing.yaml;
rule-4 is always the correct CE outcome.

**error** -- raise RouterUnavailableError.  The approval handler catches it, emits a
blocking InfoMessage, and writes a task-blocked bus entry.  EE deployments with a
compliance posture must not silently route without the router compliance check.

---

### Propagating RoutingDecision to Adapter Selection

Adapter-selection fields from the decision (used to configure the session):

| Field | Bridge usage |
|---|---|
| decision.harness | Look up SessionAdapter subclass in AdapterRegistry by runtime_id |
| decision.backend | Pass as BackendConfig.backend to the adapter |
| decision.model | Pass as BackendConfig.model to the adapter |

Audit fields written to session JSONL after the decision is received:

| Field | Audit purpose |
|---|---|
| decision.routing_id | Cross-reference with router-side audit log |
| decision.cost_estimate_usd | Future billing aggregation |
| decision.compliance_cleared | Always True; recorded explicitly for unambiguity |
| decision.rule_matched | Post-hoc rule-usage distribution analysis |

Proposed call-site (pseudocode in the approval handler):

```python
async def _handle_approved_session(approval, msg, router_client, context):
    req = build_routing_request(approval, msg, context)    # tasks 2.2 + 2.3
    try:
        decision = await router_client.route(req)
    except (RoutingNoEligibleBackend, RoutingBudgetExceeded) as exc:
        await _surface_routing_blocked(exc, approval)
        await _write_task_blocked_bus_message(approval)
        return
    except RouterUnavailableError as exc:
        await _surface_routing_blocked(exc, approval)  # EE mode only
        return
    adapter = AdapterRegistry.get(decision.harness)
    await adapter.start_session(
        backend=decision.backend,
        model=decision.model,
        session_context=_build_session_context(approval, msg, decision),
    )
```

---

### Circuit Breaker (deferred to v2)

A circuit breaker (stop calling router after N consecutive failures) is deferred to v2.
The 3-attempt retry is adequate for v1.  In EE with fallback=error, a persistent
outage surfaces immediately as a blocking InfoMessage.

---

## Task 2.4 -- Single-Org Boundary Validation (Q6)

### Claim Under Validation

Design Q6: the bridge enforces org isolation before calling the router -- the router
never receives cross-org requests in the same call.

### Enforcement Point: AuthProvider.identify()

Every HTTP request to the bridge daemon passes through AuthProvider.identify(), which
returns exactly one CallContext:

```python
@dataclass(frozen=True)
class CallContext:
    user_id: str
    user_email: str | None = None
    roles: tuple[str, ...] = ()
    # org_id: str | None = None  <- proposed addition; see auth-context-at-routing.md
```

A single identify() call processes a single HTTP request, which carries either:

- **CE (Mode 1)**: X-Otaman-User header or OTAMAN_USER env.  One machine, one user,
  one org (account name or workspace org.slug).
  SimpleAuthProvider: "CE deployments assume single-user-per-machine."
- **EE (Mode 2+)**: Zitadel JWT with exactly one sub (user) and one org_id claim.
  OIDCAuthProvider validates the JWT; CallContext.org_id is set from the single claim.
  A JWT cannot carry two org IDs.

Structurally impossible for one request to yield a CallContext with two org_id values.

### One-to-One Mapping Through the Call Stack

```
1 HTTP request
  -> 1 AuthProvider.identify()
    -> 1 CallContext   (single org_id)
      -> 1 build_routing_request()
        -> 1 RoutingRequest   (single org_id)
          -> 1 HttpRouterClient.route()
            -> 1 RoutingDecision   (single harness/backend/model)
```

No code path merges two CallContext values or passes multiple org IDs in one call.

### CE Synthesised org_id

In Mode 1 (no Zitadel) the bridge synthesises org_id from OtamanRoots.org.slug
(or account name as fallback).  Same value for every request from that machine --
single-org by construction.

### Invariant Statement

> **The bridge never passes cross-org context in a single RoutingRequest.**
>
> The enforcement point is AuthProvider.identify().  It returns exactly one CallContext
> per HTTP request.  CallContext.org_id (once added per task 2.3 findings) is sourced
> from one validated JWT claim (EE) or one synthesised slug (CE).
> build_routing_request() copies org_id one-to-one into RoutingRequest.org_id.
> No cross-org merge occurs at any point in the session-start flow.

### Trust Model Impact

The router can treat RoutingRequest.org_id as authoritative and load the per-org
routing overlay without further validation.  Q6 design intent: the bridge is the
org-context authority.

---

## Open Questions

1. **AdapterRegistry by harness string**: no adapter registry exists today.
   Bridge task 3.x will design and wire this.

2. **cost_budget_remaining_usd in v1**: always None (no session-accounting layer).
   Rule 3 still routes cheapest; no hard ceiling.  Confirmed by auth-context-at-routing.md.

3. **Audit JSONL writer**: bridge must append routing_decision audit event after receiving
   the RoutingDecision.  Not yet implemented; deferred to bridge task 3.x.

4. **routing.yaml parsing**: bridge reads .otaman/routing.yaml to configure
   HttpRouterClient.  Reuses YAML loading from config.py.  No new dependency.

5. **task-blocked bus message type**: may not be in shared-contracts spec.  The bridge
   will check before implementing the 409 handler; raises a spec-change-request first
   if missing (per spec-change rules in CLAUDE.md).
