"""Bus message surface policy — decide what to route to the phone.

The bridge daemon's second responsibility (design §5.6) is draining
``.agents/bus/active/`` for messages that need human attention and
surfacing them to the configured transport. Not every message
qualifies — agent-to-agent coordination stays on the file bus —
but decisions that need a human tap come through.

**Default policy** (verbatim from the design doc):

    spec-change-request  →  always   · 🟡 approval · [Approve] [Reject] [View diff] [Comment]
    priority: urgent     →  always   · 🔴 blocking · context-dependent
    priority: high       →  always   · 🟡 approval · buttons if to:human else info
    to: human            →  always   · 🟡          · [Acknowledge] + free-text reply
    question → human     →  always   · 🟡          · free-text reply
    review-request       →  off      · 🟢 info     · link to review file
    task-complete        →  off      · 🟢 info     · silent
    spec-change-approved →  off      · 🟢 info     · none
    spec-change-rejected →  off      · 🟢 info     · none
    task-assignment      →  never    · —           · —
    info broadcast       →  never    · —           · —

The ``off`` rows default hidden but can be turned on per project/agent
via ``platform.yaml surface:`` overrides. Never rows are agent-to-agent
and stay quiet regardless.

**Override schema** (platform.yaml):

    surface:
      review_request: true       # turn a default-off rule on globally
      task_complete: false       # explicit (same as default)
      by_agent:
        cto-reviewer:
          review_request: false  # this agent's reviews stay quiet
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Types


Severity = Literal["info", "approval", "blocking"]
SurfaceMode = Literal["always", "configurable", "never"]


@dataclass
class BusMessage:
    """Parsed .agents/bus/active/*.md file."""

    path: Path
    stem: str   # filename without extension — used for ack-file lookup
    frontmatter: dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return str(self.frontmatter.get("id") or self.stem)

    @property
    def type(self) -> str:
        return str(self.frontmatter.get("type", ""))

    @property
    def from_(self) -> str:
        return str(self.frontmatter.get("from", ""))

    @property
    def to(self) -> str:
        return str(self.frontmatter.get("to", ""))

    @property
    def priority(self) -> str:
        return str(self.frontmatter.get("priority", "normal")).lower()

    @property
    def subject(self) -> str:
        """Best-effort subject — first ## heading or first non-empty line."""
        for line in self.body.splitlines():
            stripped = line.strip()
            if stripped.startswith("##"):
                return stripped.lstrip("# ").strip()
            if stripped and not stripped.startswith("#"):
                return stripped[:120]
        return ""


@dataclass
class SurfaceDecision:
    """Outcome of asking 'should we surface this message?'"""

    surface: bool
    severity: Severity = "info"
    interactive: bool = False
    actions: list[str] = field(default_factory=list)   # "approve", "reject", etc.
    reason: str = ""   # why we made this decision (for debugging)

    def __bool__(self) -> bool:
        return self.surface


# ---------------------------------------------------------------------------
# Default policy — the hard-coded table from design §5.6


_DEFAULT_POLICY: dict[str, tuple[SurfaceMode, Severity, list[str]]] = {
    # type → (mode, default severity, default interactive actions)
    "spec-change-request":   ("always", "approval", ["approve", "reject", "details", "comment"]),
    "review-request":        ("configurable", "info",   []),
    "task-complete":         ("configurable", "info",   []),
    "spec-change-approved":  ("configurable", "info",   []),
    "spec-change-rejected":  ("configurable", "info",   []),
    "task-assignment":       ("never",        "info",   []),
    "info":                  ("never",        "info",   []),
    "spec-change":           ("configurable", "info",   []),  # post-commit notice
    "contract-change":       ("configurable", "info",   []),
    "proposal":              ("configurable", "info",   []),
}


def _is_to_human(to: str) -> bool:
    t = to.strip().lower()
    return t in ("human", "humans", "user")


def _override_for(overrides: dict[str, Any], key: str) -> bool | None:
    """Translate ``surface`` override keys (review_request, task_complete…)
    to message types (review-request, task-complete…)."""
    if not isinstance(overrides, dict):
        return None
    # Accept both dashed ('review-request') and underscored ('review_request').
    for variant in (key.replace("-", "_"), key.replace("_", "-")):
        if variant in overrides:
            val = overrides[variant]
            if isinstance(val, bool):
                return val
    return None


def decide(
    msg: BusMessage,
    *,
    overrides: dict[str, Any] | None = None,
) -> SurfaceDecision:
    """Apply the policy table + any overrides to a single message.

    Priority ordering (first match wins):

      1. ``priority: urgent`` — always surface blocking, regardless of type.
      2. Type-specific rule (looked up in ``_DEFAULT_POLICY``).
      3. ``to: human`` (any type) — always surface approval with reply.
      4. ``priority: high`` — always surface approval (buttons iff to:human).
      5. Fall through → don't surface.

    Overrides (from ``platform.yaml surface:``) turn configurable defaults
    on/off and can add per-agent rules. They cannot override the ``never``
    or ``always`` classifications in the default table — those are
    structural (info broadcasts stay quiet; spec-change-request always
    buzzes).
    """
    overrides = overrides or {}

    # 1. Urgent — always blocking, regardless of type
    if msg.priority == "urgent":
        return SurfaceDecision(
            surface=True, severity="blocking",
            interactive=bool(msg.to and _is_to_human(msg.to)),
            actions=["acknowledge"] if _is_to_human(msg.to) else [],
            reason="priority: urgent",
        )

    # 2. Type-specific rule
    rule = _DEFAULT_POLICY.get(msg.type)
    if rule is not None:
        mode, severity, actions = rule
        if mode == "never":
            # Agent-to-agent noise — stays on file bus.
            return SurfaceDecision(
                surface=False,
                reason=f"type={msg.type} is never surfaced",
            )
        if mode == "always":
            return SurfaceDecision(
                surface=True, severity=severity,
                interactive=bool(actions), actions=list(actions),
                reason=f"type={msg.type} is always surfaced",
            )
        # mode == "configurable": check per-agent override first, then global
        by_agent = overrides.get("by_agent") or {}
        if isinstance(by_agent, dict):
            agent_overrides = by_agent.get(msg.from_) or {}
            if isinstance(agent_overrides, dict):
                agent_decision = _override_for(agent_overrides, msg.type)
                if agent_decision is False:
                    return SurfaceDecision(
                        surface=False,
                        reason=f"by_agent.{msg.from_}.{msg.type}=false",
                    )
                if agent_decision is True:
                    return SurfaceDecision(
                        surface=True, severity=severity,
                        interactive=bool(actions), actions=list(actions),
                        reason=f"by_agent.{msg.from_}.{msg.type}=true",
                    )
        global_decision = _override_for(overrides, msg.type)
        if global_decision is True:
            return SurfaceDecision(
                surface=True, severity=severity,
                interactive=bool(actions), actions=list(actions),
                reason=f"global override: {msg.type}=true",
            )
        # Default off
        return SurfaceDecision(
            surface=False,
            reason=f"type={msg.type} defaults off (configurable)",
        )

    # 3. to: human — approval with reply, regardless of type
    if _is_to_human(msg.to):
        return SurfaceDecision(
            surface=True, severity="approval",
            interactive=True, actions=["acknowledge", "comment"],
            reason="to: human",
        )

    # 4. priority: high — approval (buttons iff to:human, but that branched above)
    if msg.priority == "high":
        return SurfaceDecision(
            surface=True, severity="approval",
            interactive=False, actions=[],
            reason="priority: high",
        )

    return SurfaceDecision(
        surface=False,
        reason=f"no rule matched (type={msg.type}, to={msg.to}, priority={msg.priority})",
    )


# ---------------------------------------------------------------------------
# Bus message reader


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.+?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_bus_file(path: Path) -> BusMessage | None:
    """Load + parse a single bus message file."""
    if yaml is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    return BusMessage(
        path=path, stem=path.stem,
        frontmatter=fm,
        body=m.group(2),
    )


def iter_bus_messages(project_root: Path) -> list[BusMessage]:
    """Return all .md files under ``.agents/bus/active/`` parsed into BusMessage.

    Sorted by filename (timestamp-prefixed, so chronological). Skips the
    ``acks/`` subdirectory and anything that doesn't parse.
    """
    active = project_root / ".agents" / "bus" / "active"
    if not active.is_dir():
        return []
    msgs: list[BusMessage] = []
    for p in sorted(active.glob("*.md")):
        parsed = parse_bus_file(p)
        if parsed is not None:
            msgs.append(parsed)
    return msgs


def load_surface_overrides(project_root: Path) -> dict[str, Any]:
    """Read ``surface:`` block from platform.yaml, if present."""
    if yaml is None:
        return {}
    platform_yaml = project_root / "platform.yaml"
    if not platform_yaml.is_file():
        return {}
    try:
        data = yaml.safe_load(platform_yaml.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    surface = data.get("surface")
    return surface if isinstance(surface, dict) else {}


def resolve_project_name(project_root: Path) -> str:
    """Single source of truth for the project name used in Telegram topics.

    Hook path (``scripts/bridge_approval.py``) and daemon path
    (``BusWatcher`` via ``bridge/cli.py``) both derive a project name
    when talking to the transport. If they disagree, Telegram's
    auto-create ends up spawning two topics for what the user thinks
    is one project — so this helper is the only place that decision
    gets made.

    Order:

      1. ``platform.yaml``'s ``project:`` field — authoritative.
      2. ``project_root.name`` — fallback if platform.yaml is missing
         or malformed.

    Done with a lightweight string scan rather than a full YAML parse
    so hooks (which run on every PreToolUse) don't pay a PyYAML import
    cost. The YAML-parse path below is kept as a safety net for quoted
    or indented values.
    """
    platform_yaml = project_root / "platform.yaml"
    if not platform_yaml.is_file():
        return project_root.name
    try:
        text = platform_yaml.read_text(encoding="utf-8")
    except OSError:
        return project_root.name

    # Cheap scan for top-level `project: <value>`.
    for line in text.splitlines():
        if line.startswith("project:"):
            _, _, value = line.partition(":")
            value = value.strip().strip("'").strip('"')
            # Reject obviously-malformed values (colons, braces, brackets,
            # whitespace) — they mean the line was structural YAML, not a
            # simple scalar. Fall through to the real YAML parser below;
            # if that also fails we land on project_root.name.
            if value and not any(c in value for c in ":{}[]\n\t "):
                return value
            break

    # Fallback: full YAML parse (handles quoted / indented-by-whitespace).
    if yaml is not None:
        try:
            data = yaml.safe_load(text) or {}
            if isinstance(data, dict):
                value = data.get("project")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except yaml.YAMLError:
            pass
    return project_root.name
