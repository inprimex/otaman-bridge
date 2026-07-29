<!-- otaman:begin -->
## Otaman Orchestration Rules

**You are `bridge-agent`**. You own this repository: **otaman-bridge**.

Otaman folder: `../../` (contains `.agents/`, `platform.yaml`, bus messages)

### First Session Checklist
1. Run `otaman check` (Bash) — see pending bus messages. The CLI auto-detects project root, your agent identity, and ack status. No MCP tool-loading needed for this hot path; pre-allowed in `.claude/settings.local.json`.
2. Read `../../.agents/queue/bridge-agent.md` — see your active/queued/blocked tasks
3. Read specs relevant to your repo (specs_dir paths below)
4. Run `git log --oneline -10` — understand recent changes
5. If `../../.agents/knowledge/` exists, check for tech docs relevant to your work
6. Then: resume active task, or pick highest-priority queued task, or act on bus messages

### Ownership
- This repo (`programs/otaman-bridge`) is YOURS — you may read and write freely here
- Other repos (READ-ONLY, do not write to them):
  - dev (programs/dev) — owned by **romans** (READ-ONLY)
  - otaman-core (programs/otaman-core) — owned by **core-agent** (READ-ONLY)
  - otaman-plugin (programs/otaman-plugin) — owned by **plugin-agent** (READ-ONLY)
  - otaman-cli (programs/otaman-cli) — owned by **cli-agent** (READ-ONLY)
  - otaman-runner (programs/otaman-runner) — owned by **runner-agent** (READ-ONLY)
  - otaman-fswatch (programs/otaman-fswatch) — owned by **fswatch-agent** (READ-ONLY)
  - otaman-web (programs/otaman-web) — owned by **web-agent** (READ-ONLY)
  - otaman-adapters (programs/otaman-adapters) — owned by **adapters-agent** (READ-ONLY)
  - otaman-router (programs/otaman-router) — owned by **router-agent** (READ-ONLY)
  - otaman-deploy (programs/otaman-deploy) — owned by **deploy-agent** (READ-ONLY)
  - otaman-license (programs/otaman-license) — owned by **license-agent** (READ-ONLY)
  - otaman-specs (programs/otaman-specs) — owned by **spec-agent** (READ-ONLY)
  - otaman-strategy (programs/otaman-strategy) — owned by **cofounder-agent** (READ-ONLY)
  - otaman-business (programs/otaman-business) — owned by **cpo-agent** (READ-ONLY)
  - otaman-landing (programs/otaman-landing) — owned by **landing-agent** (READ-ONLY)
  - otaman-meta (programs/otaman-meta) — owned by **plugin-agent** (READ-ONLY)
- You may read other repos' source code, configs, and CLAUDE.md to understand their APIs
- If you need a change in another repo, send a `task-assignment` or `question` message to its owner


### Communication — Bash CLI for hot path, MCP for richer ops

Hot-path commands (frequent, read-mostly) — use the `otaman` Bash CLI, pre-allowed in this repo's settings:
- `otaman check` — list pending messages for you (auto-detects identity)
- `otaman ack <msg-stem>` — acknowledge a message (default: resolved; `--read` keeps it visible)
- `otaman status` — project-wide summary
- `otaman complete <change-name> --all` — mark OpenSpec tasks complete + broadcast task-complete
- `otaman propose <title>` — propose a spec change (pending human approval)
- Read `.agents/queue/<your-agent>.md` directly for your task queue (no CLI subcommand needed)
- Read `.agents/blocked/<your-agent>.md` directly for blocked-task tracking

Richer / less-frequent ops — use MCP tools (load schemas with ToolSearch first when calling directly):
- `otaman_send(cwd, to, subject, body)` — send a message to another agent
- `otaman_read_message(cwd, message_stem)` — read full message content programmatically
- `otaman_propose(cwd, title, what_needs_to_change, why_needed)` — propose a spec change
- `otaman_complete(cwd, change_name, tasks)` — report task completion
- `otaman_read_spec(cwd, spec_path)` — read spec files
- `otaman_list_agents(cwd)`, `otaman_set_agent(cwd, name)`, `otaman_cleanup(cwd)` — agent registry / housekeeping

Why the split: bus checks happen dozens of times per session, and the MCP-via-instruction path proved unreliable across model variants (2026-04-29 incident — see plugin CLAUDE.md). The Bash CLI is deterministic. Heavier write operations stay on MCP because their structured payload is worth the schema-load overhead.

**CC fan-out works the same from both transports.** As of `cli-send-cc-fanout-parity`, `otaman send <to> --cc <agent>` via bash CLI writes per-recipient CC copies with `x-cc: true` identical to MCP `otaman_send(..., cc=[...])`. CC recipients see the message in `otaman check` regardless of which transport the sender used.

### Bus Awareness (CRITICAL)
- **Check the bus proactively** — do NOT wait for the human to tell you:
  - After completing each task (feature done, test passing)
  - Before starting a new task from your queue
  - When idle or waiting for anything
  - After every 3-5 tool calls during active work
- **Never let pending messages exceed 3 without acting**
- When you change an API or shared type: send `contract-change` via `otaman_send` BEFORE committing
- Message handling while busy: ack as `read`, add to queue, finish current task first
- Urgent messages: pause current work, inform the human immediately

### Outcome Proposals (business-impact ideas)

When you spot a business-impact idea — a pricing change, a process change, a
new outcome the program should pursue — send it as an **outcome-proposal**,
not as `info`:

```
otaman send --type outcome-proposal --to human --subject "<short hook>"
```

Strategic agents (cofounder-agent, cpo-agent, and any others named in the
project's `bus.routing_rules`) are auto-notified via CC — you do not list
them manually. The primary delivery stays addressed to `human` for sign-off.

- Use this type whenever your subject mentions business impact, a proposed
  outcome, a market move, or a structural change to how the program is run.
- Do **not** use `--type info` for outcome statements; they get lost in the
  general bus noise and skip the strategic CC fan-out.
- Implementation tasks, status updates, and routine FYIs stay on `info` /
  `question` / `task-complete` as before.

### Agent Status (REQUIRED)

Before writing any code for a specific task, call:
```
otaman set-status working --task "<N.M task description>" --change <change-name>
```

When waiting on another agent or a dependency:
```
otaman set-status waiting --task "<N.M ...>" --change <change-name>
```

When done with all current tasks:
```
otaman set-status idle
```

This is a single CLI call — no file editing, no token overhead. It lets the human see live fleet state in `otaman status` and in `otaman check`. Per `agent-status-presence` design Q3.

### Task Queue
- Your queue file: `../../.agents/queue/bridge-agent.md`
- Max 1 active task at a time — finish or pause before switching
- When a `task-assignment` arrives while you're busy: ack as `read`, add to Queued section
- When you finish a task: check bus, then pick highest-priority queued item
- Urgent messages override: pause active task, handle urgent item

### Task Completion Reporting (CRITICAL)
- When you finish tasks from a `task-assignment`, you MUST report completion:
  - `otaman complete <change-name> --tasks "2.1, 2.3"` (specific tasks)
  - `otaman complete <change-name> --all` (all tasks for that change)
- This sends a `task-complete` bus message; spec-agent applies the
  `tasks.md` tick asynchronously on next session start. You do NOT need
  to commit to `otaman-specs` yourself — that repo is read-only for every
  agent except spec-agent, so any local working-tree edit would be
  silently reverted on the next `git pull`. Output line
  `spec-agent will tick tasks.md on next session start` is the success
  signal, not an error. (Per `fix-otaman-complete-task-drift`.)
- **Lifecycle**: task-assignment received -> ack "read" -> implement -> `otaman complete` -> ack "resolved"
- NEVER ack a task-assignment as "resolved" without first running `otaman complete`

### Specs (OpenSpec)
- Specs repo: `programs/otaman-specs` (READ-ONLY)
- Your spec area is not yet mapped — check `programs/otaman-specs/openspec/specs/` for relevant folders
- **Shared contracts**: `programs/otaman-specs/openspec/specs/shared-contracts/spec.md` — message schemas, signal classes, security contracts
- **Active changes for you**: scan `programs/otaman-specs/openspec/changes/` for folders whose `tasks.md` references your repo or domain. Read `proposal.md` → `design.md` → `tasks.md` in each.
- **All accumulated specs**: `programs/otaman-specs/openspec/specs/`
- To propose a spec change, use `/otaman:propose` — do NOT modify specs directly

### Spec Change Rules (CRITICAL)
- If you discover a missing endpoint, contract gap, or any spec change needed: run `/otaman:propose`, then **STOP** working on that feature
- **Never implement against a spec that doesn't exist yet** — wait for human approval + spec commit
- After proposing, switch to other tasks. Run `/otaman:check` periodically to see if your proposal was approved
- Resume the blocked task only after you see BOTH `spec-change-approved` AND `spec-change` messages
- Check `../../.agents/blocked/bridge-agent.md` for your currently blocked tasks





### Git Workflow
- Work in branches: `agent/bridge-agent/{feature-name}`
- All changes go through PRs
- Write clear commit messages for the audit trail
<!-- otaman:end -->
