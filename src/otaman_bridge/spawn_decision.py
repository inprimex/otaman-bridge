"""Spawn-decision module — auto-session-spawn policy (task 1.4).

Entry point: ``handle_bus_event(message_path, ...)``

Policy (per design.md Q1–Q6):
1. Parse the message; skip if not ``type: task-assignment``.
2. Skip if the message is not addressed to an agent this bridge owns.
3. Extract (agent_id, human_id, change_id, mode_annotation).
4. Compute dedup key: sha256(agent_id + ":" + change_id)[:16].
5. Check SessionRegistry.is_sessioned(agent_id, human_id) — if True, warm session
   absorbs the new assignment; no fresh spawn.
6. [headless]: call runner_client.spawn(); on success call registry.claim_session().
7. [interactive]: emit a request-human-review bus message to the human.

``trigger_source`` distinguishes bus-event fires from scheduled fires in telemetry.
Coordinate with runner-agent before finalising — spawn() API is provisional (task 2.3).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

from .lifecycle_telemetry import emit_spawn_failed, emit_spawn_start, otel_spawn_span
from .runner_client import RunnerClient, RunnerUnreachableError, SpawnError
from .session_registry import SessionRegistry

_log = logging.getLogger(__name__)

# Grammar per design.md §Q2: "- [ ] 1.1 @otaman-bridge [headless] body"
_TASK_LINE_RE = re.compile(
    r"^\s*-\s+\[[ x]\]\s+[\d.]+\s+@otaman-([a-z0-9-]+)"
    r"(?:\s+\[(headless|interactive)\])?"
)

_RESERVED_TOKENS = frozenset(["paused", "gated", "hitl-required", "urgent"])


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnOutcome:
    """Result of a single handle_bus_event call."""

    agent_id: str
    human_id: str
    mode: str  # "headless" | "interactive"
    action: str  # "spawned" | "warm-session" | "interactive-review" | "spawn-failed"
    session_id: str | None
    change_id: str
    dedup_key: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _dedup_key(agent_id: str, change_id: str) -> str:
    return hashlib.sha256(f"{agent_id}:{change_id}".encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _ts_prefix() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")


def _parse_message(path: Path) -> dict | None:
    """Parse frontmatter + body from a bus .md file. Returns None if invalid."""
    if _yaml is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = _yaml.safe_load(parts[1])
    except Exception:
        return None
    if not isinstance(fm, dict):
        return None
    return {"frontmatter": fm, "body": parts[2]}


def _extract_mode(body: str, owned_agents: dict[str, str]) -> tuple[str, str, str] | None:
    """Scan task lines in body; return (agent_id, mode, repo_name) for the first owned repo.

    Mode defaults to "interactive" if the annotation is absent (per design.md Q2 rule 2).
    Raises ValueError on conflict (both [headless] and [interactive] on one line) or
    unknown bracketed token in the mode position.
    """
    for raw_line in body.splitlines():
        m = _TASK_LINE_RE.match(raw_line)
        if not m:
            continue
        repo_name = m.group(1)
        annotation = m.group(2)  # "headless", "interactive", or None

        # Regex captures just the suffix after "@otaman-"; reconstruct full key
        full_repo_name = f"otaman-{repo_name}"
        if full_repo_name not in owned_agents:
            continue

        # Validate: check for unknown reserved tokens appearing after @otaman-<repo>
        after_prefix = raw_line[m.end() :]
        bracket_m = re.match(r"\s*\[([^\]]+)\]", after_prefix)
        if bracket_m and annotation is None:
            token = bracket_m.group(1)
            if token in _RESERVED_TOKENS:
                raise ValueError(
                    f"Reserved annotation [{token}] not yet supported in: {raw_line.strip()!r}"
                )
            if token not in ("headless", "interactive"):
                raise ValueError(f"Unknown mode annotation [{token}] in: {raw_line.strip()!r}")

        mode = annotation if annotation else "interactive"
        return owned_agents[full_repo_name], mode, full_repo_name

    return None


def _emit_request_human_review(
    *,
    bus_dir: Path,
    from_agent: str,
    human_id: str,
    session_id: str,
    agent_id: str,
    change_id: str,
    message_path: Path,
) -> Path:
    """Write a request-human-review bus message; return the written path."""
    ts = _now_iso()
    prefix = _ts_prefix()
    slug = f"{prefix}-{from_agent}-to-{human_id}-review-{session_id[:8]}"
    filename = f"{slug}.md"
    content = (
        f"---\n"
        f"id: {slug}\n"
        f"from: {from_agent}\n"
        f"to: {human_id}\n"
        f"priority: normal\n"
        f"type: request-human-review\n"
        f"timestamp: {ts}\n"
        f"status: pending\n"
        f"session-id: {session_id}\n"
        f"decision-type: approve-reject\n"
        f"change: {change_id}\n"
        f"---\n"
        f"\n"
        f"## Subject: Interactive task requires human review — {agent_id}\n"
        f"\n"
        f"### Context\n"
        f"A `[interactive]` task-assignment was received for agent **{agent_id}** as part\n"
        f"of change **{change_id}**. Interactive tasks require a human-launched session.\n"
        f"\n"
        f"Original message: `{message_path.name}`\n"
        f"\n"
        f"### Question\n"
        f"Should an interactive session be launched for **{agent_id}** now?\n"
        f"\n"
        f"### Options\n"
        f"- **approve**: Launch an interactive session immediately.\n"
        f"- **reject**: Skip — task remains pending on the bus.\n"
        f"- **approve-with-changes**: Launch with modified context (specify in rationale).\n"
        f"\n"
        f"### Auto-default if no decision by deadline\n"
        f"None — task remains pending on the bus indefinitely.\n"
        f"\n"
        f"### Cost / risk if wrong\n"
        f"Approving opens a new terminal session. Rejecting defers the work.\n"
    )
    out = bus_dir / "active" / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    _log.info("Emitted request-human-review: %s", filename)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def handle_bus_event(
    message_path: Path,
    *,
    registry: SessionRegistry,
    runner_client: RunnerClient,
    owned_agents: dict[str, str],  # repo_name -> agent_id, e.g. {"otaman-bridge": "bridge-agent"}
    bus_dir: Path,
    project_root: str,  # required by runner POST /spawn; absolute path to project root
    this_agent: str = "bridge-agent",
    trigger_source: str = "bus-event",
    linger_manager=None,  # optional SessionLingerManager; if provided, start linger after spawn
) -> SpawnOutcome | None:
    """Process one bus file event; return SpawnOutcome or None if the message was skipped.

    Returns None when:
    - The message cannot be parsed.
    - The message type is not ``task-assignment``.
    - The message's ``to`` field is not an agent this bridge owns.
    """
    parsed = _parse_message(message_path)
    if parsed is None:
        _log.debug("Could not parse bus message: %s", message_path)
        return None

    fm: dict = parsed["frontmatter"]
    body: str = parsed["body"]

    if fm.get("type") != "task-assignment":
        return None

    our_agent_ids = set(owned_agents.values())
    to_field = str(fm.get("to", ""))
    if to_field not in our_agent_ids:
        _log.debug(
            "task-assignment not for us (to=%r, ours=%s): %s",
            to_field,
            our_agent_ids,
            message_path.name,
        )
        return None

    human_id = str(fm.get("from", "human"))
    # change field is preferred; fall back to stripping timestamp prefix from id
    raw_change = fm.get("change") or fm.get("id", message_path.stem)
    change_id = str(raw_change)

    try:
        task_info = _extract_mode(body, owned_agents)
    except ValueError as exc:
        _log.error("Mode-annotation parse error in %s: %s", message_path.name, exc)
        return None

    if task_info is None:
        # Message targets this agent but no matching task lines found — use conservative defaults.
        agent_id = to_field
        mode = "interactive"
        repo_name = next((r for r, a in owned_agents.items() if a == to_field), to_field)
    else:
        agent_id, mode, repo_name = task_info

    dedup = _dedup_key(agent_id, change_id)

    # Warm-session check — one session per (agent_id, human_id)
    if registry.is_sessioned(agent_id, human_id):
        _log.info(
            "Warm session exists for (%s, %s) — no spawn (trigger=%s, change=%s)",
            agent_id,
            human_id,
            trigger_source,
            change_id,
        )
        return SpawnOutcome(
            agent_id=agent_id,
            human_id=human_id,
            mode=mode,
            action="warm-session",
            session_id=None,
            change_id=change_id,
            dedup_key=dedup,
        )

    if mode == "headless":
        context = {
            "change_id": change_id,
            "message_path": str(message_path),
            "trigger_source": trigger_source,
        }
        try:
            spawned_id = runner_client.spawn(
                agent=agent_id,
                human=human_id,
                mode="headless",
                context=context,
                repo=repo_name,
                project_root=project_root,
            )
        except (RunnerUnreachableError, SpawnError) as exc:
            _log.error(
                "Spawn failed for (%s, %s) change=%s: %s",
                agent_id,
                human_id,
                change_id,
                exc,
            )
            try:
                emit_spawn_failed(
                    bus_dir=bus_dir,
                    from_agent=this_agent,
                    agent_id=agent_id,
                    human_id=human_id,
                    change_id=change_id,
                    error=str(exc),
                )
                otel_spawn_span(
                    "spawn-failed", agent_id=agent_id, session_id=dedup, change_id=change_id
                )
            except Exception:
                _log.exception("Failed to emit spawn-failed lifecycle event")
            return SpawnOutcome(
                agent_id=agent_id,
                human_id=human_id,
                mode=mode,
                action="spawn-failed",
                session_id=None,
                change_id=change_id,
                dedup_key=dedup,
            )

        claimed = registry.claim_session(agent_id, human_id, spawned_id, mode=mode)
        if not claimed:
            _log.warning(
                "Session %s spawned but claim_session returned False for (%s, %s) — race?",
                spawned_id,
                agent_id,
                human_id,
            )
        _log.info(
            "Spawned headless session %s for (%s, %s) trigger=%s change=%s",
            spawned_id,
            agent_id,
            human_id,
            trigger_source,
            change_id,
        )
        try:
            emit_spawn_start(
                bus_dir=bus_dir,
                from_agent=this_agent,
                agent_id=agent_id,
                human_id=human_id,
                session_id=spawned_id,
                change_id=change_id,
                mode=mode,
                trigger_source=trigger_source,
            )
            otel_spawn_span(
                "spawn-start", agent_id=agent_id, session_id=spawned_id, change_id=change_id
            )
        except Exception:
            _log.exception("Failed to emit spawn-start lifecycle event")
        if linger_manager is not None:
            linger_manager.start(agent_id, human_id, spawned_id, change_id)
        return SpawnOutcome(
            agent_id=agent_id,
            human_id=human_id,
            mode=mode,
            action="spawned",
            session_id=spawned_id,
            change_id=change_id,
            dedup_key=dedup,
        )

    # interactive mode
    _emit_request_human_review(
        bus_dir=bus_dir,
        from_agent=this_agent,
        human_id=human_id,
        session_id=dedup,
        agent_id=agent_id,
        change_id=change_id,
        message_path=message_path,
    )
    _log.info(
        "Interactive task: emitted request-human-review for (%s, %s) change=%s",
        agent_id,
        human_id,
        change_id,
    )
    return SpawnOutcome(
        agent_id=agent_id,
        human_id=human_id,
        mode=mode,
        action="interactive-review",
        session_id=dedup,
        change_id=change_id,
        dedup_key=dedup,
    )


__all__ = [
    "SpawnOutcome",
    "handle_bus_event",
]
