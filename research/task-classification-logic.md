# Task Classification Logic — Research (task 2.2)

**Author**: bridge-agent  
**Date**: 2026-06-03  
**Change**: otaman-router-v1-design  
**Output location**: `otaman-bridge/research/task-classification-logic.md`

---

## Summary

The bridge must classify each session's task context into a `DataClassification` value
before calling the router. This document defines the classification decision tree for v1:
which signals are used, how they combine, and what the fallback is.

---

## Classification Signals Available to the Bridge

At session-start time, the bridge has access to:

| Signal | Source | Available in Mode 1? |
|---|---|---|
| Org's declared compliance posture | `orgs/<slug>/routing.yaml` or `.otaman/routing.yaml` | Yes (platform default) |
| User's roles | `CallContext.roles` | No (empty tuple) |
| Session `task_type` | Caller-provided in the session-start request | Yes |
| Tool calls requested in the spawn request | `SpawnRequest.tool_calls` (future, via auto-session-spawn) | Not yet |
| Repo/project name | `ApprovalRequest.repo` / `SpawnRequest.repo` | Yes |

In v1, the three actionable signals are: **org posture**, **user role**, and **task type**.
Tool-call-based escalation is deferred to v1.1 (requires the spawn request to carry tool
declarations, which auto-session-spawn will provide).

---

## Decision Tree

```
classify(org_slug, user_roles, task_type) → DataClassification

1. Load org posture
   ├── if orgs/<slug>/routing.yaml has `compliance.default_classification`
   │     → use that as the baseline
   └── else use platform routing.yaml `compliance.default_classification`
         (fallback: INTERNAL)

2. Apply role escalation
   ├── if "otaman:phi-handler" ∈ user_roles  → escalate to max(baseline, PHI)
   ├── if "otaman:pci-handler" ∈ user_roles  → escalate to max(baseline, REGULATED)
   └── (other roles: no escalation; roles narrow backend selection, not classification)

3. Apply task_type escalation (keyword matching, v1 static table)
   ├── task_type matches "security_audit" | "credentials_review"  → max(current, SENSITIVE)
   ├── task_type matches "patient_*" | "*_health*" | "ehr_*"       → max(current, PHI)
   ├── task_type matches "payment_*" | "*_pci_*" | "card_*"        → max(current, REGULATED)
   ├── task_type matches "*_pii_*" | "gdpr_*" | "user_data_*"      → max(current, PII)
   └── unknown task_type                                            → no escalation

4. Return final classification
```

### `max()` semantics

Classification levels form a partial order for escalation purposes:

```
INTERNAL < SENSITIVE < PII ≈ PHI < REGULATED
```

Where `PII ≈ PHI` means either escalates to `REGULATED` but neither escalates to the other.
The `max()` function resolves as follows:

```python
_ESCALATION_ORDER = {
    DataClassification.INTERNAL:   0,
    DataClassification.SENSITIVE:  1,
    DataClassification.PII:        2,
    DataClassification.PHI:        2,   # same tier as PII
    DataClassification.REGULATED:  3,
}

def classification_max(a, b):
    if _ESCALATION_ORDER[a] >= _ESCALATION_ORDER[b]:
        return a
    return b
```

When `a` and `b` are at the same tier (e.g., `PII` and `PHI`), `a` is retained — the first
signal that escalated to that tier wins. This is deterministic and avoids conflating two
distinct regulatory frameworks.

---

## Org Posture

The org's `routing.yaml` overlay (or the platform default) declares the **minimum floor**
for all sessions in that org:

```yaml
# orgs/org-acme/routing.yaml
compliance:
  default_classification: sensitive   # floor; never routes to INTERNAL-only backends
```

An org with `default_classification: phi` forces all sessions — regardless of task_type —
to route to PHI-cleared backends. This supports healthcare orgs where even "general
development" work may touch PHI-adjacent data.

**Platform default** (when no org overlay exists): `internal`. This is the CE/Mode 1
default — the operator hasn't declared a compliance posture.

---

## Role-Based Escalation

Roles are Zitadel project roles extracted from the JWT. Two roles escalate classification
in v1:

| Role | Effect |
|---|---|
| `otaman:phi-handler` | Escalates baseline to min `PHI`. Sessions run by users with this role are assumed to work with PHI data. |
| `otaman:pci-handler` | Escalates baseline to min `REGULATED`. Sessions run by users with this role are assumed to be in PCI scope. |

All other Otaman roles (`otaman:developer`, `otaman:viewer`, `otaman:admin`) do not
escalate classification — they control access and capabilities, not data sensitivity.

In Mode 1 (no OIDC), `roles = ()` → no role-based escalation. Classification is determined
by org posture + task_type alone.

---

## Task-Type Keyword Matching

Task types are free-form strings (per core-agent task 1.2 rationale). Classification uses
glob-style prefix/suffix matching in v1 — no regex, no ML. The match table:

| Pattern | Escalation | Rationale |
|---|---|---|
| `security_audit`, `credentials_review` | `SENSITIVE` | Likely touches auth tokens or keys |
| `patient_*`, `*_health*`, `ehr_*` | `PHI` | Healthcare domain indicators |
| `payment_*`, `*_pci_*`, `card_*` | `REGULATED` | Payment card industry scope |
| `*_pii_*`, `gdpr_*`, `user_data_*` | `PII` | Explicitly labelled personal data work |
| All others | no escalation | Unknown = no assumption |

This table is **operator-extensible** via the platform `routing.yaml`:

```yaml
compliance:
  task_type_escalation:
    - pattern: "customer_support_*"
      classification: pii
    - pattern: "legal_*"
      classification: regulated
```

The v1 static table above is the built-in fallback when no operator extensions are defined.

---

## Classification When No Task Type Is Provided

If the spawn request has no `task_type` (e.g., manually launched sessions, Mode 1 CE):

1. Use the org posture floor.
2. Apply role escalation.
3. Result is the org floor (or `INTERNAL` if no posture is declared).

This is safe: an `INTERNAL` classification routes to the default backend — whatever the
operator has configured. For a CE deployment with one backend, this is always correct.

---

## Implementation Location

The classification logic should live in a new module:

```
otaman-bridge/src/otaman_bridge/routing_client.py
```

The key function:

```python
def classify_task(
    *,
    org_posture: DataClassification,
    user_roles: tuple[str, ...],
    task_type: str,
    task_type_escalation_table: list[dict] | None = None,
) -> DataClassification:
    ...
```

This is a pure function — easily unit-tested without any network or file I/O. The routing
client module calls it before constructing the `RoutingRequest`.

---

## Deferred to v1.1

- **Tool-call-based escalation**: when the spawn request carries declared tool names (e.g.,
  `Bash`, `Write`), the presence of certain tools escalates classification. A session with
  `Bash` + a production DB connection string in context is more sensitive than the `task_type`
  alone suggests. This requires the spawn request schema (from auto-session-spawn) to carry
  tool declarations.
- **ML-based classification**: classify based on a lightweight model scoring the session
  prompt. Out of scope for v1 — latency and complexity.
- **Mid-session reclassification**: per design.md, v1 classification is fixed at session-start.
