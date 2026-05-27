# Auth Context at Routing Time -- Research (task 2.3)

**Author**: bridge-agent
**Date**: 2026-05-27
**Change**: otaman-router-v1-design

---

## Summary

This document validates Q5 (Zitadel JWT tie-in): confirm that the bridge already has
user_id, org_id, and roles from the JWT at the point of routing.  If not, this
document identifies what is missing and what bridge-side work would provide it.

---

## Current State: What the Bridge Has at Routing Time

### CallContext (src/otaman_bridge/mcp_server.py)

```python
@dataclass(frozen=True)
class CallContext:
    user_id: str
    user_email: str | None = None
    roles: tuple[str, ...] = ()
```

CallContext is populated by AuthProvider.identify() on every incoming HTTP request.
It is the bridge's primary identity carrier at the point where tool handlers and
approval callbacks are invoked.

### What is Present

| Field | Available? | Source in CE | Source in EE |
|---|---|---|---|
| user_id | YES | X-Otaman-User header or OTAMAN_USER env | Zitadel JWT sub claim |
| user_email | YES (optional) | Not populated in CE | Zitadel JWT email claim |
| roles | YES (empty in CE) | Empty tuple | Zitadel JWT project roles |

### What is Missing

| Field | Available? | Needed for RoutingRequest? | Gap |
|---|---|---|---|
| org_id | **NO** | YES (required field) | Not in CallContext; not extracted from JWT |

**org_id is missing from CallContext.**  RoutingRequest.org_id is a required field
(per the RoutingRequest spec, task 1.2).  The bridge cannot build a valid
RoutingRequest today without adding org_id to CallContext.

---

## Why org_id is Missing

The bridge auth layer was designed before multi-tenant routing was planned.  CE
uses a single-org model (one account = one machine = one implicit org) and never
needed an explicit org_id.  EE auth (OIDCAuthProvider) validates the JWT but the
CallContext dataclass was not updated to carry the org claim.

In the current bridge code:

- LoopbackAuthProvider: no org concept (loopback has no identity at all)
- SimpleAuthProvider: reads X-Otaman-User; no org concept
- OIDCAuthProvider (EE, in otaman_bridge_ee.auth_oidc): extracts sub, email, roles
  from the JWT but does NOT extract org_id

---

## Gap: cost_budget_remaining_usd

RoutingRequest.cost_budget_remaining_usd is an optional float that the bridge passes
to the router for cost-ceiling enforcement (rule 3, hard mode).  This requires a
session-accounting layer that tracks per-org and per-user budget consumption.

**Status**: no session-accounting layer exists in the bridge.  This field will be
None in v1 for all sessions.  Rule 3 still routes to the cheapest backend but does
not enforce a hard ceiling.  Acceptable for v1; deferred to bridge task 3.x.

---

## Required Bridge-Side Work to Close the org_id Gap

### Option A: Add org_id to CallContext (Recommended for v1)

Extend CallContext with an optional org_id field:

```python
@dataclass(frozen=True)
class CallContext:
    user_id: str
    user_email: str | None = None
    roles: tuple[str, ...] = ()
    org_id: str | None = None   # NEW: Zitadel org claim (EE) or synthesised slug (CE)
```

Provider changes required:

| Provider | Change |
|---|---|
| LoopbackAuthProvider | org_id=None (loopback has no org context) |
| SimpleAuthProvider | org_id=None (CE; caller provides no org) |
| OIDCAuthProvider (EE) | Extract X-Zitadel-Org-Id or JWT org claim; set org_id |

**CE fallback for org_id in build_routing_request():**

```python
def _resolve_org_id(context: CallContext, project_root: Path) -> str:
    if context.org_id:
        return context.org_id
    # CE fallback: synthesise from workspace org slug
    try:
        roots = find_otaman_root(project_root)
        if roots and roots.org and roots.org.slug:
            return roots.org.slug
    except Exception:
        pass
    # Last resort: use the account name
    return "default"
```

This approach is backward-compatible: existing code that doesn't use org_id
is unaffected; opt-in callers use context.org_id.

### Option B: Resolve org_id at the RouterClient Call Site (not recommended)

Resolve org_id inside build_routing_request() by reading the workspace config,
without changing CallContext.  Simpler for v1 but means org isolation is not
validated at the auth layer.  Rejected: violates the principle that AuthProvider
is the org-context authority (per Q6 / task 2.4 invariant).

---

## Validation of Q5 Claim

**Q5 claim**: "The bridge already has user_id, org_id, roles from the JWT at
routing time."

**Validation result**: PARTIALLY TRUE

| Claim | Status |
|---|---|
| user_id available | TRUE -- SimpleAuthProvider or OIDCAuthProvider populates this |
| roles available | TRUE for EE (JWT roles); EMPTY for CE (expected, acceptable) |
| org_id available | **FALSE** -- missing from CallContext; requires Option A above |

**Conclusion**: user_id and roles are available.  org_id requires a targeted bridge
change: add org_id field to CallContext and update OIDCAuthProvider to extract it.
This is a small, well-scoped change that does not affect CE functionality.

---

## Impact on RoutingRequest Construction

After the Option A change, build_routing_request() becomes straightforward:

```python
def build_routing_request(
    approval: ApprovalRequest,
    msg: BusMessage,
    context: CallContext,
    project_root: Path,
    router_config: RouterConfig,
) -> RoutingRequest:
    from datetime import datetime, timezone
    return RoutingRequest(
        session_id=_generate_session_id(),
        org_id=_resolve_org_id(context, project_root),  # Option A
        user_id=context.user_id or None,
        roles=context.roles,
        task_classification=classify_task(approval, msg, router_config),  # task 2.2
        task_type=_resolve_task_type(approval, msg),
        cost_budget_remaining_usd=None,  # v1: no accounting layer yet
        preferred_harness=None,          # v1: no caller preference
        timestamp=datetime.now(timezone.utc),
    )
```

---

## Open Questions

1. **Zitadel org claim field name**: the JWT claim carrying org_id in Zitadel is
   typically the X-Zitadel-Org-Id header (for API calls) or a custom claim in the
   JWT body.  The exact field name must be confirmed with the EE deployment config.
   Recommendation: use X-Zitadel-Org-Id header (available at the HTTP layer) as
   primary; JWT claim as fallback.

2. **org_id slug grammar validation**: RoutingRequest.org_id must match
   ^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$ or be the sentinel "_platform".
   The bridge should validate this before calling the router to surface misconfig
   early.  CE fallback slugs should be normalised (lowercase, hyphens for spaces).

3. **OIDCAuthProvider change scope**: the change is in otaman_bridge_ee (EE module),
   not in the CE bridge codebase.  The CE codebase change (CallContext.org_id field)
   is minimal and backward-compatible.  EE module change is a separate PR.

4. **user_id empty string vs None**: LoopbackAuthProvider sets user_id="" (empty
   string), not None.  RoutingRequest.user_id is Optional[str].  The bridge should
   convert empty string to None to keep the RoutingRequest semantics clean.
