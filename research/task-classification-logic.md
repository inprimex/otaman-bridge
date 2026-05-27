# Task Classification Logic -- Research (task 2.2)

**Author**: bridge-agent
**Date**: 2026-05-27
**Change**: otaman-router-v1-design
**Output location**: src/otaman_bridge/routing_client.py (build_routing_request function)

---

## Summary

This document defines how the bridge classifies a session context into a
DataClassification value, which is the primary input to the router rule chain.
The classification is fixed at session-start routing time (no mid-session
reclassification in v1).

---

## DataClassification Levels (from otaman-core task 1.1)

| Level | Meaning |
|---|---|
| INTERNAL | Non-public org data, no regulatory label |
| SENSITIVE | Commercially sensitive / NDA-restricted |
| PII | Personally Identifiable Information (GDPR/CCPA) |
| PHI | Protected Health Information (HIPAA) |
| REGULATED | PCI-DSS, ITAR/EAR, sovereign/export-control data |

**Ordering**: INTERNAL < SENSITIVE < PII ~= PHI < REGULATED

The bridge always selects the **most restrictive applicable** classification.

---

## Four Signal Sources

The bridge evaluates four signal sources in order.  Each can escalate the
classification; none can de-escalate it.

### Signal 1: Org Compliance Posture (from routing.yaml)

The per-org routing.yaml overlay (or platform default) declares a minimum
classification floor for all sessions in that org:

```yaml
# orgs/org-healthcare/routing.yaml
compliance:
  minimum_classification: phi   # every session is at least PHI
```

If the org posture declares minimum_classification, the bridge uses that as the
floor and no lower classification can be returned.

In Mode 1 (CE, no per-org overlay), the platform routing.yaml floor applies
(typically INTERNAL -- no escalation from posture).

### Signal 2: Task Type

The task_type string (free-form, e.g. "code_review", "summarise") provides
a coarse classification hint.  In v1 the bridge uses a static mapping table
(extensible without code changes via routing.yaml specialisation block):

| Task type | Implied minimum classification | Notes |
|---|---|---|
| code_review (internal repo) | INTERNAL | No customer data expected |
| code_review (auth/session code) | SENSITIVE | Credentials may be in context |
| code_review (payment service) | REGULATED | PCI-DSS scope |
| code_generation | INTERNAL | Unless tool calls escalate |
| security_audit | SENSITIVE | Code may expose vulnerabilities |
| summarise (internal docs) | INTERNAL | -- |
| summarise (HR reviews) | SENSITIVE | Personal but not PII in most jurisdictions |
| summarise (support tickets with emails) | PII | Email addresses = PII |
| summarise (EHR documents) | PHI | HIPAA scope |
| agentic (Bash tool, production DB) | SENSITIVE | Elevated risk |

The bridge resolves task type from the session context (approval request body or
MCP tool call metadata).  Unknown task types default to INTERNAL.

### Signal 3: Tool Calls Requested

Certain tools imply a higher classification independent of task type:

| Tool call pattern | Classification escalation |
|---|---|
| Bash with production DB env variables | SENSITIVE minimum |
| Bash with plaintext credentials in args | SENSITIVE minimum |
| File read/write on /etc/, /secrets/, ~/.ssh/ | SENSITIVE minimum |
| Any tool call referencing PHI column names (configured per-org) | PHI minimum |
| Network call to external endpoint outside allowlist | SENSITIVE minimum |

Tool-call-based escalation is applied AFTER task-type classification.  The bridge
scans the tool_input fields in the approval request to detect these patterns.

In v1, the pattern list is configured in .otaman/routing.yaml:

```yaml
classification_hints:
  tool_patterns:
    - pattern: ".*(/etc/|~/.ssh/|/secrets/).*"
      escalate_to: sensitive
    - pattern: ".*(credit_card|card_number|ccnum).*"
      escalate_to: regulated
    - pattern: ".*(patient_id|diagnosis|dob|mrn).*"
      escalate_to: phi
```

### Signal 4: User Role

JWT roles (from OIDCAuthProvider in EE mode, empty in CE Mode 1) can restrict or
expand the classification.  In v1 roles are informational only (audit trail); the
compliance rule (rule 1) in the router uses the org routing.yaml overlay to map
roles to backend restrictions, not the bridge classification logic.

Future v2: role-based classification escalation (e.g., users with role
"phi-handler" always trigger PHI minimum regardless of task type).

---

## Classification Decision Tree

```
START
  |
  v
[1] Load org posture floor from routing.yaml overlay
    -> classification = posture_floor (default: INTERNAL)
  |
  v
[2] Evaluate task_type -> implied minimum
    -> classification = max(classification, task_type_implied)
  |
  v
[3] Scan tool_input patterns for each tool call in the request
    -> for each matched pattern: classification = max(classification, pattern_escalation)
  |
  v
[4] (Future v2) Apply role-based escalation from JWT roles
  |
  v
RETURN classification
```

**max()** here means the more restrictive of the two values, using the ordering:
INTERNAL < SENSITIVE < PII ~= PHI < REGULATED
(For PII vs PHI ties, PHI is preferred as it implies stricter HIPAA requirements.)

---

## Proposed Implementation

In the approval handler, before calling RouterClient.route():

```python
from otaman_core.routing import DataClassification

def classify_task(
    approval: ApprovalRequest,
    msg: BusMessage,
    org_posture: OrgRoutingConfig,
    pattern_config: ClassificationPatternConfig,
) -> DataClassification:
    classification = org_posture.minimum_classification or DataClassification.INTERNAL

    # Signal 2: task type
    task_type = _resolve_task_type(approval, msg)
    type_implied = _TASK_TYPE_TABLE.get(task_type, DataClassification.INTERNAL)
    classification = _max_classification(classification, type_implied)

    # Signal 3: tool patterns
    for tool_call in _extract_tool_calls(approval, msg):
        for pattern, escalation in pattern_config.patterns:
            if pattern.search(str(tool_call.tool_input)):
                classification = _max_classification(classification, escalation)
                break

    return classification

def _max_classification(
    a: DataClassification,
    b: DataClassification,
) -> DataClassification:
    "Most restrictive of two classifications."
    if a == DataClassification.REGULATED or b == DataClassification.REGULATED:
        return DataClassification.REGULATED
    if a == DataClassification.PHI or b == DataClassification.PHI:
        return DataClassification.PHI
    if a == DataClassification.PII or b == DataClassification.PII:
        return DataClassification.PII
    if a == DataClassification.SENSITIVE or b == DataClassification.SENSITIVE:
        return DataClassification.SENSITIVE
    return DataClassification.INTERNAL
```

---

## Open Questions

1. **task_type resolution**: in the bus-triggered session flow, how does the bridge
   determine task_type?  Proposed: parse the BusMessage frontmatter for a type: field
   (e.g. type: code-review -> code_review).  If absent, default to code_generation.

2. **Pattern config hot-reload**: the tool-pattern table is in .otaman/routing.yaml.
   Should it be re-read per routing request, or cached at daemon start?  Recommendation:
   cache with a 5-minute TTL (same pattern as bus message scanning).

3. **PII vs PHI ordering**: confirmed PHI > PII in _max_classification because HIPAA
   requirements (BAA) are strictly more complex than GDPR DPA requirements.  This is a
   design decision; not derived from any spec -- recommend adding to shared-contracts.

4. **Role-based escalation**: deferred to v2.  In v1, roles are passed through to the
   RoutingRequest for the router to use in overlay evaluation; the bridge does not
   classify based on roles.
