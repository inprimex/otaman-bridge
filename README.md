# otaman-bridge

> **Otaman platform:** [otaman-core](https://github.com/inprimex/otaman-core) · [otaman-cli](https://github.com/inprimex/otaman-cli) · [otaman-plugin](https://github.com/inprimex/otaman-plugin) · **otaman-bridge (you are here)** · [otaman-runner](https://github.com/inprimex/otaman-runner) · [otaman-adapters](https://github.com/inprimex/otaman-adapters)

Otaman's server daemon — HTTP API, HTTP MCP server, transport coordination, approval flow, and audit log writer. Runs on a shared dev server or self-hosted VPS; agents and the CLI talk to it over loopback or LAN.

## Status

| Component | Shipped | Roadmap |
|---|---|---|
| HTTP server (OpenAPI surface) | shipped | — |
| HTTP MCP server | shipped | — |
| Adapter coordination (Telegram adapter) | shipped | — |
| Approval flow logic (AFK remote-approve) | shipped | — |
| Audit log writer (JSONL on disk) | shipped | — |
| Web login landing page | shipped | otaman-web dashboard serving (Step 3) |
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
- **Web UI** — serves a minimal login landing page today (identity / login / logout); serving the `otaman-web` React/Vite dashboard bundle is roadmap (Step 3).
- **Approval flow** — receives `permissionDecision` requests from the bridge-approval hook, forwards to the human's phone, returns allow/deny.
- **Magic-link auth** — single-click session creation for web UI (Step 3).
- **OIDC middleware** — validates Zitadel tokens for multi-tenant deployments (Step 4).
- **Enforcement coordinator** — license-check enforcement integration with `otaman-license` (Step 5).

## Dependencies

- Python 3.10+
- `uv` (workspace package manager)
- `otaman-core` — the only required runtime dependency (storage protocols, auth
  validators / shared `AuthService`, CloudEvents helpers, OTel setup, program
  lifecycle read point)
- `python-telegram-bot` — optional `telegram` extra; the Telegram transport is
  native to this repo (`transports/telegram.py`)
- `otaman-adapters` — optional; only the Easy8 PM-sync integration imports it
  (lazily, soft-fail when absent)
- NATS server — not used today; the event-source and session-registry seams are
  stubbed for a future Mode-2+ swap
- SQLite (bundled) — `SqliteSessionRegistry` backend

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

## Bus watching — one program bus per instance

A bridge instance watches **exactly one** program bus (`--watch-bus
/path/to/program-meta-dir`, the directory holding that program's
`platform.yaml` and `.agents/bus/`). This is the permanent architecture per
the `single-bus-per-program` spec (post P1 split-brain incident):

- The per-program bus is the *only* bus. An org-level `orgs/<org>/.agents/`
  is never a valid watch target; bare `--watch-bus` auto-detection refuses
  roots that lack `platform.yaml` + `.agents/bus/` rather than silently
  polling an empty directory.
- **Multi-program orgs run one bridge instance per program** (parameterized
  systemd template, `otaman-bridge@<program>` style — owned by
  otaman-deploy). Single-process multi-bus watching is explicitly deferred.
- Envelopes whose `from_org`/`to_org` projections name two different orgs
  are not surfaced: cross-org routing is not yet implemented (ADR-012
  Phase 5+); the watcher logs a warning and leaves the file untouched.

## See also

- [ADR-006 (NATS system bus)](https://github.com/inprimex/otaman-meta/blob/main/adrs/ADR-006-nats-system-bus.md) — Step 4 event substrate
- [ADR-010 (user binding + seat licensing)](https://github.com/inprimex/otaman-meta/blob/main/adrs/ADR-010-user-binding-and-seat-licensing.md) — auth model
- [polyrepo-structure.md](https://github.com/inprimex/otaman-meta/blob/main/polyrepo-structure.md) — ownership map
- [phased-roadmap.md](https://github.com/inprimex/otaman-meta/blob/main/phased-roadmap.md) — Step 1–7 sequencing
- [otaman.ai](https://otaman.ai) — platform docs

## License

AGPL-3.0 (community edition). Commercial license available for teams that cannot ship source — see [otaman.ai](https://otaman.ai).
