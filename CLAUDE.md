# otaman-bridge — developer guide

Otaman's server daemon: HTTP API, HTTP MCP server, transport coordination
(Telegram today), AFK approval flow, and audit-log writer. See
[`README.md`](./README.md) for the component overview and roadmap.

## Development setup

Requires Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).

```bash
# Install with test extras
uv sync --package otaman-bridge --extra test

# Run the daemon in the foreground (loopback, ephemeral port)
uv run --package otaman-bridge python -m otaman_bridge.cli run -v

# Check it's alive
curl http://127.0.0.1:<port>/status
```

`otaman-bridge` depends on `otaman-core` as a workspace/path dependency; CI
wires the two together via a generated workspace `pyproject.toml` (see
`.github/workflows/test.yml`).

## Tests

```bash
uv run --package otaman-bridge pytest
```

The suite is isolated from any live workspace: `tests/conftest.py` adopts the
shared `otaman_core.testing` primitive, which strips workspace-resolution
environment variables and pins root resolution to a per-test sandbox, so tests
never read or write a real bus.

## Lint & format

Ruff is the single lint + formatter. CI runs both as required gates, pinned to
an exact ruff version (see the `lint` job in `.github/workflows/test.yml`):

```bash
uv tool run ruff@<pinned-version> check .
uv tool run ruff@<pinned-version> format --check .
```

Baseline config lives in `pyproject.toml` under `[tool.ruff]`: line length 100,
rule set `E, F, W, I, UP, B`. You may add categories; do not remove baseline
ones. Suppress per-line (`# noqa: <rule>`) or per-file, never by dropping a
category.

## Conventions

- Work in feature branches and open pull requests against `main`. Keep PRs
  focused, include tests for behavioural changes, and run the suite locally
  before pushing.
- Legacy `maestro` identifiers are being migrated to `otaman`. A CI gate
  (`audit-maestro-refs.sh`) requires every remaining `.maestro` / `maestro`
  reference under `src/` to carry an inline `# legacy: <reason>` or
  `# migration: <reason>` annotation on the same line.
- This repo ships two packages: `otaman_bridge` (Community Edition, AGPL-3.0)
  and `otaman_bridge_ee`. See [`LICENSE`](./LICENSE) and
  [`CONTRIBUTING.md`](./CONTRIBUTING.md) for licensing and the CLA.

## Security

Report vulnerabilities per [`SECURITY.md`](./SECURITY.md) — please do not open
a public issue for a security report.
