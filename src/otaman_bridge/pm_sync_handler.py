"""PM sync event handler — bus-to-PM outbound sync and inbound webhook routing.

Tasks 4.1–4.7 + 9.3 of pm-sync-adapter spec change (JTBD-37).

Outbound flow (bus → PM):
  spec-change-approved  → adapter.create_issue(); store issue_id in issue-map
  task-assignment       → adapter.update_issue(in_progress) + add_comment()
  task-complete         → adapter.update_issue(done) + add_comment()
  spec-change-request, question  → add_comment() only (status unchanged)

Inbound flow (PM → bus), routing table per pm-sync-adapter.md Q9:
  Issue update, status→Done, has spec-path   → spec-update-requested to spec-agent
  Issue update, comment contains @spec-agent → spec-update-requested to spec-agent
  Issue create (external)                    → pm-issue-created to responsible agent
  Issue update (other)                       → pm-issue-updated to responsible agent
  Issue destroy                              → pm-issue-deleted to responsible agent

MCP Tier 2 (task 9.3):
  Complex queries (fleet summary, bulk-transition) route via Easy8McpClient when
  available; all CRUD hot-path operations stay on REST.

Issue ID persistence:
  Primary:   openspec/changes/{change}/.pm-sync.yaml  (spec-side canonical)
  Secondary: {project_root}/.otaman/pm-sync/issue-map.json  (runtime cache)
  Fallback:  live GET /issues.json search
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional MCP Tier 2 client (task 9.3) — graceful absent
# ---------------------------------------------------------------------------

try:
    from otaman_adapters.easy8 import Easy8McpClient as _Easy8McpClient  # type: ignore[attr-defined]
    _MCP_CLIENT_CLS = _Easy8McpClient
except (ImportError, AttributeError):
    _MCP_CLIENT_CLS = None  # task 9.2 not yet merged; fall back to REST always


# ---------------------------------------------------------------------------
# Human-roster assignee resolution (human-roster spec)
# ---------------------------------------------------------------------------

def resolve_assignee(msg_to: str, roster: list) -> "int | None":
    """Return pm-user-id for the person responsible for msg_to.

    Algorithm:
    - ``<role>-agent`` → find first roster entry with that role
    - ``human``        → first entry matching priority order cofounder→cto→cpo→developer
    - no match / pm-user-id absent → None (issue created unassigned)
    """
    if msg_to.endswith("-agent"):
        role = msg_to.removesuffix("-agent")
        for person in roster:
            if isinstance(person, dict) and role in person.get("roles", []):
                uid = person.get("pm-user-id")
                return int(uid) if uid is not None else None
    if msg_to == "human":
        for role in ("cofounder", "cto", "cpo", "developer"):
            for person in roster:
                if isinstance(person, dict) and role in person.get("roles", []):
                    uid = person.get("pm-user-id")
                    return int(uid) if uid is not None else None
    return None


class _SpecChangeWithAssignee:
    """Thin wrapper around SpecChange that adds assigned_to_id for adapters that support it."""

    def __init__(self, spec_change: object, assigned_to_id: "int | None") -> None:
        self._sc = spec_change
        self.assigned_to_id = assigned_to_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._sc, name)


# ---------------------------------------------------------------------------
# Comment format per pm-sync-adapter.md § Bus-as-PM-Comments
# ---------------------------------------------------------------------------

_COMMENT_TEMPLATES: dict[str, str] = {
    "task-assignment":    "🤖 {from_} → {to}: {subject}",
    "task-complete":      "✅ {from_}: task complete — {subject}",
    "spec-change-request": "📋 {from_} proposed spec change — {subject} — awaiting human approval",
    "spec-change-approved": "✅ Spec approved — {subject}",
    "question":           "❓ {from_} → {to}: {subject}",
}

# Bus event types that mirror as PM comments (gated by capabilities.issue_comments)
_COMMENT_EVENT_TYPES = frozenset(_COMMENT_TEMPLATES)


class PmSyncHandler:
    """Bridges bus events ↔ PM adapter.

    Instantiated lazily by the daemon on first ``/pm-sync/<provider>`` POST or
    on bus-watcher startup when pm-sync is present in platform.yaml.

    When ``otaman_core`` is absent, or the pm-sync block is missing,
    ``self.enabled`` is ``False`` and all public methods are no-ops.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.enabled: bool = False
        self.adapter: Any = None
        self.config: Any = None

        # platform.yaml — check project_root directly, then otaman-meta subfolder
        platform_yaml = project_root / "platform.yaml"
        if not platform_yaml.is_file():
            candidate = project_root / "otaman-meta" / "platform.yaml"
            if candidate.is_file():
                platform_yaml = candidate

        if not platform_yaml.is_file():
            logger.debug("pm_sync_handler: no platform.yaml found under %s", project_root)
            return

        try:
            from otaman_core.pm_sync import load_pm_sync_config
            self.config = load_pm_sync_config(platform_yaml)
        except ImportError:
            logger.debug("pm_sync_handler: otaman_core not available")
            return

        if self.config is None:
            logger.debug("pm_sync_handler: no pm-sync block in platform.yaml")
            return

        self.adapter = self._load_adapter(self.config)
        self.enabled = self.adapter is not None

        # Invert project-map: project_id → repo_name for webhook agent dispatch (task 4.7)
        self._project_id_to_repo: dict[int, str] = {
            v: k for k, v in (self.config.project_map or {}).items()
        }

        # Human roster for PM assignee resolution (human-roster spec)
        self._human_roster: list = self._load_human_roster(platform_yaml)

    # ------------------------------------------------------------------
    # Outbound: bus event → PM (tasks 4.2–4.6)
    # ------------------------------------------------------------------

    def handle_event(self, msg: object) -> None:
        """Sync callback for BusWatcher.on_event — adapts BusMessage to handle_bus_event."""
        msg_type: str = str(getattr(msg, "type", "") or "")
        msg_from: str = str(getattr(msg, "from_", "") or "")
        msg_to: str = str(getattr(msg, "to", "") or "")
        subject: str = str(getattr(msg, "subject", "") or "")
        frontmatter: dict = getattr(msg, "frontmatter", {}) or {}

        change_name: str | None = frontmatter.get("change") or None
        spec_path: str | None = frontmatter.get("spec-path") or None

        # spec-change-approved messages have no `change:` field — derive from subject
        # format: "Approved: <change-name>: <description>" or "Approved: <change-name>"
        if msg_type == "spec-change-approved" and not change_name:
            after = subject.removeprefix("Approved:").strip()
            change_name = after.split(":")[0].strip() or None

        self.handle_bus_event(msg_type, msg_from, msg_to, subject, spec_path, change_name)

    def handle_bus_event(
        self,
        msg_type: str,
        msg_from: str,
        msg_to: str,
        subject: str,
        spec_path: str | None,
        change_name: str | None,
    ) -> None:
        """Called by the bus-watcher when a mapped bus event fires."""
        if not self.enabled or self.adapter is None:
            return

        try:
            self._dispatch_outbound(msg_type, msg_from, msg_to, subject, spec_path, change_name)
        except Exception:
            logger.exception(
                "pm_sync_handler: error handling bus event type=%r change=%r",
                msg_type, change_name,
            )

    def _dispatch_outbound(
        self,
        msg_type: str,
        msg_from: str,
        msg_to: str,
        subject: str,
        spec_path: str | None,
        change_name: str | None,
    ) -> None:
        assert self.adapter is not None

        if msg_type == "spec-change-approved" and change_name:
            # task 4.2
            try:
                from otaman_core.pm_sync import SpecChange
            except ImportError:
                logger.warning("pm_sync_handler: cannot import SpecChange")
                return
            # Use specs repo name (not msg sender) so project_map lookup resolves
            # to the right Easy8 sub-project (e.g. "otaman-specs" → project_id 32).
            specs_repo = self._specs_repo_name()
            spec_change: object = SpecChange(
                change_name=change_name,
                title=subject,
                agent_name=specs_repo,
                spec_path=spec_path or "",
                jtbd_id=None,
            )
            # human-roster: resolve PM assignee from roster
            assigned_to_id = resolve_assignee(msg_to, self._human_roster)
            if assigned_to_id is not None:
                spec_change = _SpecChangeWithAssignee(spec_change, assigned_to_id)
            issue = self.adapter.create_issue(spec_change)
            self._save_issue_id(change_name, issue.id)
            self._write_pm_sync_yaml(change_name, issue.id)
            logger.info(
                "pm_sync_handler: created issue #%s for spec-change '%s'", issue.id, change_name
            )
            # Also post spec-change-approved comment if comments capability present (task 4.6)
            if self.adapter.capabilities.issue_comments:
                comment = _COMMENT_TEMPLATES["spec-change-approved"].format(
                    from_=msg_from, to=msg_to, subject=subject,
                )
                self.adapter.add_comment(issue.id, comment)

        elif msg_type == "task-assignment":
            # task 4.3: update status → In-Progress AND add comment
            issue_id = self._resolve_issue_id(change_name, subject)
            if issue_id is None:
                logger.debug(
                    "pm_sync_handler: task-assignment — no issue id for change=%r", change_name
                )
                return
            try:
                from otaman_core.pm_sync import SpecState
                state = SpecState(status="in_progress")
            except ImportError:
                state = "in_progress"  # type: ignore[assignment]
            self.adapter.update_issue(issue_id, state)
            if self.adapter.capabilities.issue_comments:
                comment = _COMMENT_TEMPLATES["task-assignment"].format(
                    from_=msg_from, to=msg_to, subject=subject,
                )
                self.adapter.add_comment(issue_id, comment)
                logger.info(
                    "pm_sync_handler: updated issue #%s → In-Progress + comment", issue_id
                )

        elif msg_type == "task-complete":
            # task 4.4: update status → Done AND add comment
            issue_id = self._resolve_issue_id(change_name, subject)
            if issue_id is None:
                logger.debug(
                    "pm_sync_handler: task-complete — no issue id for change=%r", change_name
                )
                return
            try:
                from otaman_core.pm_sync import SpecState
                state = SpecState(status="done")
            except ImportError:
                state = "done"  # type: ignore[assignment]
            self.adapter.update_issue(issue_id, state)
            if self.adapter.capabilities.issue_comments:
                comment = _COMMENT_TEMPLATES["task-complete"].format(
                    from_=msg_from, to=msg_to, subject=subject,
                )
                self.adapter.add_comment(issue_id, comment)
                logger.info(
                    "pm_sync_handler: updated issue #%s → Done + comment", issue_id
                )

        elif msg_type in ("spec-change-request", "question") and self.adapter.capabilities.issue_comments:
            # task 4.6: comment-only event types
            issue_id = self._resolve_issue_id(change_name, subject)
            if issue_id is not None:
                comment = _COMMENT_TEMPLATES[msg_type].format(
                    from_=msg_from, to=msg_to, subject=subject,
                )
                self.adapter.add_comment(issue_id, comment)
                logger.info(
                    "pm_sync_handler: posted %s comment on issue #%s", msg_type, issue_id
                )

    # ------------------------------------------------------------------
    # Inbound: PM webhook → bus event (task 4.5)
    # ------------------------------------------------------------------

    def handle_inbound_webhook(self, payload: dict) -> dict:
        """Called from HTTP route /pm-sync/<provider>. Returns response dict."""
        if not self.enabled or self.adapter is None:
            return {"ok": False, "error": "pm-sync not configured"}
        try:
            event = self.adapter.handle_inbound_event(payload)
            self._route_inbound_event(event, payload)
            return {"ok": True, "event_type": event.event_type}
        except Exception as exc:
            logger.exception("pm_sync_handler: error handling inbound webhook")
            return {"ok": False, "error": str(exc)}

    def _route_inbound_event(self, event: Any, raw_payload: dict) -> None:
        """Apply the Q9 routing table and emit the appropriate bus event."""
        # Normalise event_type — adapter may return Easy8 action strings ("create",
        # "update", "destroy") or already-normalised names ("issue_created", etc.)
        etype = str(getattr(event, "event_type", "") or "")
        _action_map = {"create": "issue_created", "update": "issue_updated", "destroy": "issue_deleted"}
        norm_type = _action_map.get(etype, etype)

        project_id: int = int(getattr(event, "project_id", 0) or 0)
        issue_id: int = int(getattr(event, "issue_id", 0) or 0)

        # Fields from core PmInboundEvent (may be absent in stand-in)
        new_status: str | None = getattr(event, "new_status", None)
        spec_path: str | None = getattr(event, "spec_path", None)
        issue_subject: str | None = getattr(event, "issue_subject", None)

        # Fall back to raw payload for fields missing in stand-in
        if spec_path is None:
            raw_issue = raw_payload.get("issue") or {}
            if isinstance(raw_issue, dict):
                for cf in raw_issue.get("custom_fields", []):
                    if isinstance(cf, dict) and cf.get("name") == "spec-path":
                        spec_path = cf.get("value")
        if new_status is None:
            raw_issue = raw_payload.get("issue") or {}
            if isinstance(raw_issue, dict):
                status_obj = raw_issue.get("status") or {}
                new_status = status_obj.get("name") if isinstance(status_obj, dict) else None

        # Task 4.7: find responsible agent from project-map by project_id
        to_agent = self._project_id_to_repo.get(project_id)
        repo_agent = self._repo_to_agent(to_agent) if to_agent else "human"
        bus_to = repo_agent if repo_agent else "human"

        # Q9 routing rules
        if norm_type == "issue_updated" and new_status == "Done" and spec_path:
            # Rule 1: Done + spec-path → spec-update-requested to spec-agent
            self._write_bus_message(
                msg_type="spec-update-requested",
                to="spec-agent",
                subject=f"PM issue #{issue_id} marked Done — update spec: {spec_path}",
                body=(
                    f"Issue #{issue_id} in project {project_id} transitioned to Done.\n"
                    f"spec-path: {spec_path}\n"
                    f"project_id: {project_id}"
                ),
            )
            return

        if norm_type == "issue_updated":
            # Rule 2: comment body contains @spec-agent
            raw_issue = raw_payload.get("issue") or {}
            raw_journals = raw_payload.get("journals") or []
            for journal in raw_journals:
                notes = journal.get("notes", "") if isinstance(journal, dict) else ""
                if "@spec-agent" in notes:
                    self._write_bus_message(
                        msg_type="spec-update-requested",
                        to="spec-agent",
                        subject=f"PM comment mentions @spec-agent on issue #{issue_id}",
                        body=(
                            f"Issue #{issue_id} comment contains @spec-agent reference.\n"
                            f"project_id: {project_id}\n"
                            f"note: {notes[:200]}"
                        ),
                    )
                    return

        # Rules 3-5: general pm-issue-* events to responsible agent / human
        type_to_bus = {
            "issue_created": "pm-issue-created",
            "issue_updated": "pm-issue-updated",
            "issue_deleted": "pm-issue-deleted",
        }
        bus_type = type_to_bus.get(norm_type, "pm-issue-updated")
        self._write_bus_message(
            msg_type=bus_type,
            to=bus_to,
            subject=f"PM {norm_type}: {issue_subject or 'issue #' + str(issue_id)}",
            body=(
                f"event_type: {norm_type}\n"
                f"project_id: {project_id}\n"
                f"issue_id: {issue_id}\n"
                + (f"status: {new_status}\n" if new_status else "")
                + (f"spec_path: {spec_path}\n" if spec_path else "")
            ),
        )

    # ------------------------------------------------------------------
    # MCP Tier 2 path (task 9.3) — complex queries only
    # ------------------------------------------------------------------

    def call_mcp_complex_query(self, tool_name: str, arguments: dict) -> dict | None:
        """Route a complex query via Easy8 MCP when available; return None on fallback.

        Used for fleet summaries, bulk transitions, and other multi-issue operations
        that benefit from MCP's richer context. All CRUD hot-path operations (create_issue,
        update_issue, add_comment) stay on REST regardless of this method.

        Returns the MCP tool result dict on success, None when MCP is unavailable or
        the call fails — callers fall back to REST in that case.
        """
        if _MCP_CLIENT_CLS is None:
            return None
        if not self.enabled or self.adapter is None:
            return None
        if not getattr(self.adapter.capabilities, "mcp_support", False):
            return None

        api_key = os.environ.get(
            f"OTAMAN_PM_{(self.config.provider or 'easy8').upper()}_API_KEY", ""
        )
        base_url = getattr(self.config, "base_url", "")
        if not api_key or not base_url:
            return None

        try:
            client = _MCP_CLIENT_CLS(base_url=base_url, api_key=api_key)
            return client.call_tool(tool_name, arguments)
        except Exception:
            logger.debug(
                "pm_sync_handler: MCP Tier 2 call failed for tool=%r; falling back to REST",
                tool_name,
            )
            return None

    # ------------------------------------------------------------------
    # Persistence: issue-map (change_name → PM issue_id)
    # ------------------------------------------------------------------

    @property
    def _issue_map_path(self) -> Path:
        return self.project_root / ".otaman" / "pm-sync" / "issue-map.json"

    def _save_issue_id(self, change_name: str, issue_id: int) -> None:
        path = self._issue_map_path
        path.parent.mkdir(parents=True, exist_ok=True)
        mapping: dict[str, int] = {}
        if path.is_file():
            try:
                mapping = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        mapping[change_name] = issue_id
        path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")

    def _load_issue_id(self, change_name: str) -> int | None:
        path = self._issue_map_path
        if not path.is_file():
            return None
        try:
            mapping: dict = json.loads(path.read_text(encoding="utf-8"))
            val = mapping.get(change_name)
            return int(val) if val is not None else None
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    # ------------------------------------------------------------------
    # .pm-sync.yaml — spec-side issue ID (pm-sync-issue-id-on-spec spec)
    # ------------------------------------------------------------------

    def _specs_root(self) -> "Path | None":
        """Resolve the openspec changes directory from platform.yaml specs.path."""
        platform_yaml = self.project_root / "platform.yaml"
        if not platform_yaml.is_file():
            platform_yaml = self.project_root / "otaman-meta" / "platform.yaml"
        try:
            import yaml
            data = yaml.safe_load(platform_yaml.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        specs_path_str = (data.get("specs") or {}).get("path", "")
        if specs_path_str:
            sp = Path(specs_path_str)
            if not sp.is_absolute():
                sp = (platform_yaml.parent / sp).resolve()
            return sp / "openspec" / "changes"
        # fallback: collocated openspec (e.g. in test fixtures)
        candidate = self.project_root / "openspec" / "changes"
        return candidate if candidate.is_dir() else None

    def _pm_sync_file(self, change_name: str) -> "Path | None":
        """Return path to .pm-sync.yaml for a change, or None if unresolvable."""
        root = self._specs_root()
        if root is None:
            return None
        return root / change_name / ".pm-sync.yaml"

    def _write_pm_sync_yaml(self, change_name: str, issue_id: int) -> None:
        """Write/update .pm-sync.yaml with change_issue_id, preserving existing task entries."""
        path = self._pm_sync_file(change_name)
        if path is None:
            logger.warning(
                "pm_sync_handler: cannot resolve spec dir for %r; skipping .pm-sync.yaml write",
                change_name,
            )
            return
        try:
            import yaml
            existing: dict = {}
            if path.is_file():
                try:
                    existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except Exception:
                    existing = {}
            path.parent.mkdir(parents=True, exist_ok=True)
            data: dict = {
                "provider": getattr(self.config, "provider", "") or "",
                "base_url": getattr(self.config, "base_url", "") or "",
                "change_issue_id": issue_id,
            }
            if existing.get("tasks"):
                data["tasks"] = existing["tasks"]
            path.write_text(
                yaml.dump(data, default_flow_style=False, sort_keys=True), encoding="utf-8",
            )
            logger.debug("pm_sync_handler: wrote .pm-sync.yaml for %r → #%s", change_name, issue_id)
        except Exception:
            logger.warning(
                "pm_sync_handler: failed to write .pm-sync.yaml for %r", change_name, exc_info=True,
            )

    def _read_pm_sync_yaml(self, change_name: str) -> "int | None":
        """Read change_issue_id from .pm-sync.yaml; returns None on miss or error."""
        path = self._pm_sync_file(change_name)
        if path is None or not path.is_file():
            return None
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            val = data.get("change_issue_id")
            return int(val) if val is not None else None
        except Exception:
            logger.warning(
                "pm_sync_handler: malformed .pm-sync.yaml for %r; treating as cache miss",
                change_name,
            )
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_issue_id(self, change_name: str | None, subject: str) -> int | None:
        """Return PM issue id for a change.

        Priority chain:
        1. .pm-sync.yaml change_issue_id (spec-side canonical)
        2. .otaman/pm-sync/issue-map.json (runtime cache)
        3. Live GET /issues.json search (last resort)
        """
        if change_name:
            # 1. spec-side .pm-sync.yaml
            from_spec = self._read_pm_sync_yaml(change_name)
            if from_spec is not None:
                return from_spec
            # 2. runtime cache
            stored = self._load_issue_id(change_name)
            if stored is not None:
                return stored

        if self.adapter is None or self.config is None:
            return None
        try:
            from otaman_core.pm_sync import PmIssueFilters
        except ImportError:
            return None
        try:
            issues = self.adapter.list_issues(PmIssueFilters())
        except Exception:
            logger.debug("pm_sync_handler: list_issues failed; cannot resolve issue id")
            return None

        keyword = change_name or subject
        for issue in issues:
            if keyword and keyword.lower() in (issue.subject or "").lower():
                return issue.id
        return None

    def _load_adapter(self, config: Any) -> Any:
        """Load and instantiate the PM adapter for config.provider."""
        provider = config.provider
        cls = None

        try:
            from otaman_core.pm_sync import get_pm_adapter
            cls = get_pm_adapter(provider)
        except (ImportError, KeyError):
            pass

        if cls is None and provider == "easy8":
            try:
                from otaman_adapters.easy8 import Easy8Adapter
                cls = Easy8Adapter
            except ImportError:
                logger.warning(
                    "pm_sync_handler: otaman_adapters not installed; cannot load Easy8Adapter"
                )
                return None

        if cls is None:
            logger.warning("pm_sync_handler: no adapter found for provider=%r", provider)
            return None

        api_key = os.environ.get(f"OTAMAN_PM_{provider.upper()}_API_KEY", "")
        try:
            adapter = cls(
                base_url=config.base_url,
                api_key=api_key,
                status_map=getattr(config, "status_map", {}) or {},
                tracker=getattr(config, "tracker", "Task") or "Task",
            )
        except Exception:
            logger.exception(
                "pm_sync_handler: failed to instantiate adapter for provider=%r", provider
            )
            return None

        if hasattr(adapter, "set_project_map") and config.project_map:
            adapter.set_project_map(config.project_map)
        return adapter

    def _repo_to_agent(self, repo_name: str | None) -> str | None:
        """Look up the owning agent for a repo name from platform.yaml repos[]."""
        if not repo_name or not self.config:
            return None
        # platform.yaml repos[] is not in PmSyncConfig — read platform.yaml directly
        platform_yaml = self.project_root / "platform.yaml"
        if not platform_yaml.is_file():
            platform_yaml = self.project_root / "otaman-meta" / "platform.yaml"
        try:
            import yaml
            data = yaml.safe_load(platform_yaml.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        for repo in data.get("repos", []):
            if isinstance(repo, dict) and repo.get("name") == repo_name:
                return str(repo.get("owner", ""))
        return None

    def _specs_repo_name(self) -> str:
        """Derive the specs repo name from the resolved specs root for project_map lookup.

        e.g. /home/user/otaman/otaman-specs/openspec/changes → "otaman-specs"
        Falls back to "otaman-specs" if unresolvable.
        """
        root = self._specs_root()
        if root is not None:
            # root is openspec/changes — two levels up is the repo dir
            name = root.parent.parent.name
            if name:
                return name
        return "otaman-specs"

    def _load_human_roster(self, platform_yaml: Path) -> list:
        """Read human-roster list from platform.yaml; returns [] on any error."""
        try:
            import yaml
            data = yaml.safe_load(platform_yaml.read_text(encoding="utf-8")) or {}
            roster = data.get("human-roster", [])
            return roster if isinstance(roster, list) else []
        except Exception:
            return []

    def _write_bus_message(
        self,
        *,
        msg_type: str,
        to: str,
        subject: str,
        body: str,
        priority: str = "normal",
    ) -> None:
        active = self.project_root / ".agents" / "bus" / "active"
        active.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc)
        ts_prefix = ts.strftime("%Y%m%dT%H%M%S")
        ts_iso = ts.isoformat()
        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower())[:40].strip("-")
        filename = f"{ts_prefix}-bridge-to-{to}-{slug}.md"
        content = (
            f"---\n"
            f"id: {ts_prefix}-pm-sync-{slug}\n"
            f"from: bridge-agent\n"
            f"to: {to}\n"
            f"type: {msg_type}\n"
            f"priority: {priority}\n"
            f"timestamp: {ts_iso}\n"
            f"status: pending\n"
            f"---\n"
            f"\n"
            f"## {subject}\n"
            f"\n"
            f"{body}\n"
        )
        (active / filename).write_text(content, encoding="utf-8")
        logger.debug("pm_sync_handler: emitted %s to %s → %s", msg_type, to, filename)
