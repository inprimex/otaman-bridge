# Experimental-mode banner contract

**Changes**: `multi-tenant-org-runtime` task 2.2, `containerized-agent-execution` task 4.3  
**Owner**: bridge-agent  
**Status**: implemented (see `src/otaman_bridge/experimental_mode.py`)

---

## Purpose

ADR-012 gate 2 requires that any deployment running in
`experimental_multi_tenant` mode makes this unambiguous to operators.
The gate prevents accidental production use of an unaudited isolation model.

---

## Detection

Runtime mode is read from `platform.yaml` at:

1. `<project_root>/_platform/platform.yaml` — multi-tenant layout (canonical)
2. `<project_root>/platform.yaml` — flat layout fallback

Field path:

```yaml
runtime:
  multi_tenant:
    mode: experimental_multi_tenant   # triggers the banner
```

Any other value (e.g., `mode: single`) or an absent field → no banner.

---

## Canonical banner strings

All banner text lives in `src/otaman_bridge/experimental_mode.py` as module
constants.  Other modules MUST import from there rather than duplicating text.

### `BANNER_ONELINE` — one-line prefix

Used in:
- Telegram approval prompts (prepended to every approval body)
- `/healthz` response `"experimental_warning"` field
- Log lines where a block would be too verbose

```
⚠️ EXPERIMENTAL MULTI-TENANT MODE — not validated for production; data isolation not audited
```

### `BANNER_BLOCK` — multi-line block

Used in:
- Startup log (emitted once via `emit_startup_banner()`)
- CLI `otaman bridge status` output (deferred to cli-agent task)

```
╔══════════════════════════════════════════════════════════════════╗
║  ⚠️  EXPERIMENTAL MULTI-TENANT MODE                              ║
║                                                                  ║
║  This bridge is running in experimental_multi_tenant mode.       ║
║  Data isolation between Organisations is NOT audited.            ║
║  Upgrade from this mode is manual (no automated migration).      ║
║  Use only on non-production workspaces.                          ║
╚══════════════════════════════════════════════════════════════════╝
```

### `BANNER_LABEL` — short label for structured fields

Used in:
- Web UI footer (deferred to web-agent)
- Structured JSON fields where the full block would break parsers

```
experimental_multi_tenant
```

---

## Emission points

| Point | When | Text used | Module |
|-------|------|-----------|--------|
| Daemon startup | After `daemon.start()`, once per process | `BANNER_BLOCK` (log.warning) | `cli.py` via `emit_startup_banner()` |
| Telegram approval prompts | Every `send_approval` call | `BANNER_ONELINE` (body prefix) | `experimental_mode.prefix_approval_body()` — **not yet wired** (deferred to transport layer, Phase 2) |
| `/healthz` response | Every request when experimental | `BANNER_ONELINE` in `"experimental_warning"` field | `daemon.handle_healthz()` |
| `otaman bridge status` CLI | On demand | `BANNER_BLOCK` | deferred to cli-agent |
| Web UI footer | Page load | `BANNER_LABEL` | deferred to web-agent |

---

## What is NOT yet wired (Phase 2 / open tasks)

1. **Telegram approval prompt prefix** — `prefix_approval_body()` is
   implemented but not yet called from the transport layer.  Wire in
   Phase 2 when transport gets the workspace-root reference.

2. **Web UI footer** — web-agent scope; pass `BANNER_LABEL` via the
   status API or a dedicated endpoint.

3. **`otaman bridge status` CLI** — cli-agent scope; read `runtime_mode`
   from the `/status` or `/healthz` response and render `BANNER_BLOCK`
   when the field is present.

---

## Contract stability

The constants `BANNER_ONELINE`, `BANNER_BLOCK`, `BANNER_LABEL` are stable
once this document is merged.  Any change to the text requires:
1. Updating `experimental_mode.py`
2. Updating this contract document
3. A `contract-change` bus message to all agents that render the banner
   (currently: bridge-agent only; eventually web-agent + cli-agent)

---

## Cross-reference

- `multi-tenant-org-runtime` design.md — ADR-012 gate 2 requirement
- `containerized-agent-execution` tasks.md — task 4.3
- `src/otaman_bridge/experimental_mode.py` — implementation
- `research/healthz-contract.md` — `/healthz` endpoint that surfaces the warning
