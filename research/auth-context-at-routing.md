# Auth Context at Routing Time — Research (tasks 2.3, Q5 validation)

**Author**: bridge-agent  
**Date**: 2026-06-03  
**Change**: otaman-router-v1-design  
**Output location**: `otaman-bridge/research/auth-context-at-routing.md`

---

## Summary

`RoutingRequest` requires three identity fields from the bridge: `user_id`, `org_id`, and
`roles`. This document validates which fields the bridge already has at session-start time
and what work is required to supply the missing ones.

**Finding**: `user_id` and `roles` are available today (Mode 2+). `org_id` is NOT per-request
in the current bridge — it requires a bridge-side change to extract from the JWT or read from
deployment config.

---

## Current Auth Context at Session-Start

The bridge's auth identity at session-start time is represented by `CallContext`
(`otaman_bridge/mcp_server.py`):

```python
@dataclass(frozen=True)
class CallContext:
    user_id: str
    user_email: str | None = None
    roles: tuple[str, ...] = ()
```

This struct is populated by the `AuthProvider` chain at request time.

### Source of each field by mode

| Field | Mode 1 (CE loopback) | Mode 2+ (OIDC/EE) | Notes |
|---|---|---|---|
| `user_id` | `""` (empty string) | JWT `sub` claim | `OIDCAuthProvider` → `OIDCAuthResult.user_id` |
| `roles` | `()` (empty tuple) | Zitadel project-role claims | `_extract_roles()` in `otaman_core/auth_oidc.py` |
| `user_email` | `None` | JWT `email` claim | Present but not needed for routing |

### Zitadel role extraction

`OIDCValidator` extracts roles from Zitadel's project-scoped claim
(`urn:zitadel:iam:org:project:<PROJECT_ID>:roles`) and the legacy
`urn:zitadel:iam:org:projects:roles` claim. The value is a dict keyed by role name;
`_extract_roles()` returns the union of role keys across all project claims.

Example JWT fragment:
```json
{
  "sub": "user-xyz",
  "email": "alice@acme.com",
  "urn:zitadel:iam:org:project:proj-abc:roles": {
    "otaman:developer": {"org-acme": "acme.example.com"}
  }
}
```

→ `CallContext(user_id="user-xyz", user_email="alice@acme.com", roles=("otaman:developer",))`

### ✅ `user_id` — available today

`CallContext.user_id` maps directly to `RoutingRequest.user_id`. In Mode 1 it is `""`;
the bridge converts `""` → `None` when building the `RoutingRequest` (empty string is not
a valid user identity for routing purposes).

### ✅ `roles` — available today

`CallContext.roles` maps directly to `RoutingRequest.roles`. Empty tuple in Mode 1; no change
needed.

### ⚠️ `org_id` — NOT available per-request today

`CallContext` has no `org_id` field. The bridge has `org_id` in one place:
`idp_config.org_id` — the operator-configured Zitadel organisation ID used by the DCR shim
(`daemon.py:1465`). This is set at daemon startup from `launch-settings.yaml`, not derived
per-request from the JWT.

**Zitadel JWT does carry org information**, but `OIDCValidator` does not currently extract it.
Zitadel embeds the org in the project-role claim value:

```json
"urn:zitadel:iam:org:project:proj-abc:roles": {
  "otaman:developer": {
    "<org-id>": "<org-domain>"
  }
}
```

The dict *value* for each role is a map of `{org_id: org_domain}` — the org the role belongs
to. `_extract_roles()` currently discards this value and only extracts the role key.

---

## Gap Analysis

| Field | Available? | Source | Work needed |
|---|---|---|---|
| `user_id` | ✅ | `CallContext.user_id` | Convert `""` → `None` when building `RoutingRequest` |
| `roles` | ✅ | `CallContext.roles` | None |
| `org_id` | ⚠️ | Not in `CallContext` | See options below |

### Options for `org_id`

**Option A — Extract from JWT (recommended for Mode 2+)**

Extend `_extract_roles()` (or add a sibling `_extract_org_id()`) in
`otaman_core/auth_oidc.py` to extract the org_id from the project-role claim value.
Add `org_id: str | None` to `OIDCAuthResult` and propagate it into `CallContext`.

Pros: correct per-user org context; supports future multi-org tokens.  
Cons: requires change to `otaman-core` and `otaman-bridge`; Zitadel must populate the claim
(it does by default for project-scoped roles).

**Option B — Deployment-level `org_id` from config (fallback for Mode 1)**

The bridge daemon already knows its deployment org from `idp_config.org_id`. In Mode 1
(no OIDC), use this as the `RoutingRequest.org_id`. In Mode 2+, prefer the JWT-extracted
value (Option A) and fall back to config if absent.

Pros: works today for Mode 1 without any new code.  
Cons: wrong for multi-org single-bridge deployments (the deployment org ≠ the user's org
if Zitadel is configured to serve multiple orgs through one bridge).

**Recommended approach**:
- Mode 1: synthesise `org_id` from the deployment slug in `.otaman/config.yaml`
  (or the `routing.yaml` default `org:` field). Default value: `"default"`.
- Mode 2+: extract `org_id` from JWT (Option A). This requires `otaman-core` task
  (not bridge-agent scope but a prerequisite dependency for the bridge's routing client).

---

## Bridge-Side Work Required (for `otaman-router-v1-impl`)

| Item | Owner | Scope |
|---|---|---|
| Add `org_id` extraction to `OIDCValidator` / `OIDCAuthResult` | otaman-core | ~20 lines in `auth_oidc.py` |
| Add `org_id: str \| None` to `CallContext` | otaman-bridge | `mcp_server.py` |
| Propagate `org_id` through `OIDCAuthProvider.identify()` | otaman-bridge | `auth_oidc.py` (EE) |
| Synthesise `org_id` for Mode 1 (from config slug or "default") | otaman-bridge | routing client module |

The bridge's routing client (task 2.1) should treat `org_id` resolution as a first-class
concern: if `org_id` is unavailable at routing time, the client must use a safe default
(`"default"`) rather than failing the session.

---

## Q5 Design Decision Confirmation

The design.md Q5 direction is **confirmed correct**:

> Bridge passes authenticated user claims in the `RoutingRequest`; router does NOT call
> Zitadel directly.

The bridge is the right authority. The gap is that `CallContext` doesn't yet carry `org_id`.
This is a small, well-scoped implementation gap — not an architectural issue. The routing
client (task 2.1) can be designed with the assumption that `org_id` will be available in
`CallContext` by the time `otaman-router-v1-impl` ships.

**Mode 1 special case**: `user_id=None`, `roles=()`, `org_id="default"` (synthesised from
config). The router's compliance rule uses the platform-level `routing.yaml` only (no per-org
overlay). This is the correct CE behaviour per design.md Q5.
