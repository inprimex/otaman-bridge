# otaman-bridge

Otaman's server daemon — HTTP API, HTTP MCP server, transport coordination, approval flow, and audit log writer. Runs on a shared dev server or self-hosted VPS; agents and the CLI talk to it over loopback or LAN.

## Status

| Component | Shipped | Roadmap |
|---|---|---|
| HTTP server (OpenAPI surface) | shipped | — |
| HTTP MCP server | shipped | — |
| Adapter coordination (Telegram adapter) | shipped | — |
| Approval flow logic (AFK remote-approve) | shipped | — |
| Audit log writer (JSONL on disk) | shipped | — |
| Web UI static serving | shipped | Step 3 web UI |
| Magic-link auth | — | Step 3 |
| SQLite-backed multi-user sessions | — | Step 3 |
| NATS pub/sub gateway | — | Step 4 (ADR-006) |
| OIDC middleware (Zitadel) | stubbed | Step 4 |
| Slack / Discord transport adapters | — | post-Step 4 |
| Enforcement coordinator (license layer) | — | Step 5 |

## What this repo owns

- **HTTP server** — OpenAPI-documented REST surface consumed by the CLI, plugin hooks, and runner.
- **HTTP MCP server** — MCP-over-HTTP endpoint for Claude Code sessions that can reach the bridge over the network.
- **NATS pub/sub** — event gateway and request-reply bus (Step 4; stubbed today).
- **Audit log writer** — appends CloudEvents-shaped JSONL; shared read path exposed via `GET /audit`.
- **Adapter coordination** — owns the lifecycle of registered transport adapters; Telegram is the first-class adapter today.
- **Transport surfacing** — Telegram today; Slack / Discord arrive after Step 4.
- **Web UI** — serves the static web dashboard built by `otaman-web`; React/Vite bundle drop-in.
- **Approval flow** — receives `permissionDecision` requests from the bridge-approval hook, forwards to the human's phone, returns allow/deny.
- **Magic-link auth** — single-click session creation for web UI (Step 3).
- **OIDC middleware** — validates Zitadel tokens for multi-tenant deployments (Step 4).
- **Enforcement coordinator** — license-check enforcement integration with `otaman-license` (Step 5).

## Dependencies

- Python 3.11+
- `uv` (workspace package manager)
- `otaman-core` (storage protocols, auth validators, CloudEvents helpers, OTel setup)
- `otaman-adapters` (Telegram adapter and adapter registry)
- `otaman-router` (message routing logic)
- NATS server (optional today, required Step 4)
- SQLite (bundled) — Postgres optional at import time

## Quick start (development)

```bash
# Install with dev + test extras
uv sync --package otaman-bridge --extra test

# Run the test suite
uv run --package otaman-bridge pytest

# Start the daemon in foreground (loopback, ephemeral port)
uv run --package otaman-bridge python -m otaman_bridge.cli run -v

# Check it's alive
curl http://127.0.0.1:<port>/status
```

The daemon writes an endpoint file to `~/.otaman/bridge.endpoint` (mode 0600) so the CLI and plugin scripts can locate it without configuration.

## See also

- [ADR-006 (NATS system bus)](https://github.com/inprimex/otaman-meta/blob/main/adrs/ADR-006-nats-system-bus.md) — Step 4 event substrate
- [ADR-010 (user binding + seat licensing)](https://github.com/inprimex/otaman-meta/blob/main/adrs/ADR-010-user-binding-and-seat-licensing.md) — auth model
- [polyrepo-structure.md](https://github.com/inprimex/otaman-meta/blob/main/polyrepo-structure.md) — ownership map
- [phased-roadmap.md](https://github.com/inprimex/otaman-meta/blob/main/phased-roadmap.md) — Step 1–7 sequencing
- [otaman.ai](https://otaman.ai) — platform docs

## License

AGPL-3.0 (community edition). Commercial license available for teams that cannot ship source — see [otaman.ai](https://otaman.ai).
