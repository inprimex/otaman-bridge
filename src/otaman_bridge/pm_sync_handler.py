"""PM sync handler — bridges bus events ↔ Easy8 REST API.

Loaded lazily by the bridge daemon when pm-sync: is present in platform.yaml.
Outbound path: spec-change-approved → create_issue; task-complete → update_issue + comment.
Inbound path: POST /pm-sync/<provider> → handle_inbound_event → emit bus message.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PmSyncHandler:
    """Bridges bus events ↔ Easy8 REST API.

    Instantiated lazily by the daemon on first ``/pm-sync/<provider>`` POST or
    on bus-watcher startup when pm-sync: is present in platform.yaml.

    If ``otaman_core`` is not installed, or the pm-sync block is absent from
    platform.yaml, ``self.enabled`` is ``False`` and all public methods become
    no-ops / error-returning stubs.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.enabled: bool = False
        self.adapter: Any = None
        self.config: Any = None

        # Locate platform.yaml (check both project_root and otaman-meta folder)
        platform_yaml = project_root / "platform.yaml"
        if not platform_yaml.is_file():
            meta_yaml = project_root / "otaman-meta" / "platform.yaml"
            if meta_yaml.is_file():
                platform_yaml = meta_yaml

        if not platform_yaml.is_file():
            logger.debug("pm_sync_handler: no platform.yaml found under %s", project_root)
            return

        # Load PmSyncConfig from core
        try:
            from otaman_core.pm_sync import load_pm_sync_config
            self.config = load_pm_sync_config(platform_yaml)
        except ImportError:
            logger.debug("pm_sync_handler: otaman_core not available")
            return

        if self.config is None:
            logger.debug("pm_sync_handler: no pm-sync block in platform.yaml")
            return

        # Load adapter
        self.adapter = self._load_adapter(self.config)
        self.enabled = self.adapter is not None

    # ------------------------------------------------------------------
    # Outbound: bus event -> PM
    # ------------------------------------------------------------------

    def handle_bus_event(
        self,
        msg_type: str,
        msg_from: str,
        msg_to: str,
        subject: str,
        spec_path: str | None,
        change_name: str | None,
    ) -> None:
        """Called by bus watcher when a relevant bus event fires."""
        if not self.enabled or self.adapter is None:
            return

        try:
            if msg_type == "spec-change-approved" and change_name:
                try:
                    from otaman_core.pm_sync import SpecChange
                    spec_change = SpecChange(
                        change_name=change_name,
                        title=subject,
                        agent_name=msg_from,
                        spec_path=spec_path or "",
                        jtbd_id=None,
                    )
                except ImportError:
                    logger.warning("pm_sync_handler: cannot import SpecChange from otaman_core")
                    return
                issue = self.adapter.create_issue(spec_change)
                logger.info(
                    "pm_sync_handler: created issue #%s for spec-change-approved '%s'",
                    issue.id,
                    change_name,
                )

            elif msg_type == "task-complete":
                # Resolve issue id from change_name or subject
                issue_id = self._resolve_issue_id(change_name, subject)
                if issue_id is None:
                    logger.debug(
                        "pm_sync_handler: task-complete — no issue id resolved for change=%r",
                        change_name,
                    )
                    return
                try:
                    from otaman_core.pm_sync import SpecState
                    state = SpecState(status="done")
                except ImportError:
                    state = "done"  # type: ignore[assignment]

                self.adapter.update_issue(issue_id, state)
                comment = (
                    f"Task complete reported by {msg_from}.\n"
                    f"change: {change_name or '-'}\n"
                    f"subject: {subject}"
                )
                self.adapter.add_comment(issue_id, comment)
                logger.info(
                    "pm_sync_handler: updated issue #%s -> Done + added comment", issue_id
                )

            elif msg_type == "task-assignment":
                if self.adapter.capabilities.issue_comments:
                    issue_id = self._resolve_issue_id(change_name, subject)
                    if issue_id is not None:
                        comment = (
                            f"Task assigned to {msg_to} by {msg_from}.\n"
                            f"subject: {subject}"
                        )
                        self.adapter.add_comment(issue_id, comment)
                        logger.info(
                            "pm_sync_handler: added assignment comment on issue #%s",
                            issue_id,
                        )

        except Exception:
            logger.exception(
                "pm_sync_handler: error handling bus event type=%r change=%r",
                msg_type,
                change_name,
            )

    # ------------------------------------------------------------------
    # Inbound: PM webhook -> bus event
    # ------------------------------------------------------------------

    def handle_inbound_webhook(self, payload: dict) -> dict:
        """Called from the HTTP route /pm-sync/<provider>. Returns response dict."""
        if not self.enabled or self.adapter is None:
            return {"ok": False, "error": "pm-sync not configured"}
        try:
            event = self.adapter.handle_inbound_event(payload)
            self._emit_bus_event(event)
            return {"ok": True, "event_type": event.event_type}
        except Exception as e:
            logger.exception("pm_sync_handler: error handling inbound webhook")
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_bus_event(self, event: Any) -> None:
        """Write pm-issue-* or spec-update-requested bus message to active bus."""
        active = self.project_root / ".agents" / "bus" / "active"
        active.mkdir(parents=True, exist_ok=True)

        ts_file = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

        # Routing rule: Issue->Done with spec-path -> spec-update-requested to spec-agent
        new_status = getattr(event, "new_status", None)
        spec_path = getattr(event, "spec_path", None)
        project_id = getattr(event, "project_id", None) or 0
        issue_id = getattr(event, "issue_id", None) or 0

        if event.event_type == "issue_updated" and new_status == "Done" and spec_path:
            msg_type = "spec-update-requested"
            to = "spec-agent"
            subject = f"PM issue marked Done — update spec: {spec_path}"
            body = (
                f"Issue #{issue_id} in project {project_id} transitioned to Done.\n"
                f"spec-path: {spec_path}"
            )
        else:
            type_map = {
                "issue_created": "pm-issue-created",
                "issue_updated": "pm-issue-updated",
                "issue_deleted": "pm-issue-deleted",
            }
            msg_type = type_map.get(event.event_type, "pm-issue-updated")
            to = "human"
            subject = f"PM {event.event_type}: issue #{issue_id} in project {project_id}"
            body = (
                f"event_type: {event.event_type}\n"
                f"project_id: {project_id}\n"
                f"issue_id: {issue_id}"
            )

        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower())[:30].strip("-")
        filename = f"{ts_file}-bridge-to-{to}-{slug}.md"
        content = f"""---
id: {ts_file}-pm-sync-{slug}
from: bridge
to: {to}
type: {msg_type}
priority: normal
timestamp: {datetime.now(timezone.utc).isoformat()}
status: pending
---

## {subject}

{body}
"""
        (active / filename).write_text(content, encoding="utf-8")
        logger.debug("pm_sync_handler: emitted bus message %s", filename)

    def _load_adapter(self, config: Any) -> Any:
        """Load and instantiate the PM adapter for *config.provider*.

        Resolution order:
        1. ``otaman_core.pm_sync.get_pm_adapter`` registry
        2. Direct import from ``otaman_adapters.easy8``

        Returns ``None`` on any failure.
        """
        provider = config.provider
        cls = None

        try:
            from otaman_core.pm_sync import get_pm_adapter
            cls = get_pm_adapter(provider)
        except (ImportError, KeyError):
            # Registry not available or provider not registered — try direct import
            pass

        if cls is None and provider == "easy8":
            try:
                from otaman_adapters.easy8 import Easy8Adapter
                cls = Easy8Adapter
            except ImportError:
                logger.warning(
                    "pm_sync_handler: otaman_adapters not installed; "
                    "cannot load Easy8Adapter"
                )
                return None

        if cls is None:
            logger.warning(
                "pm_sync_handler: no adapter found for provider=%r", provider
            )
            return None

        api_key = os.environ.get(f"OTAMAN_PM_{provider.upper()}_API_KEY", "")
        try:
            _status_map = getattr(config, "status_map", {}) or {}
            _tracker = getattr(config, "tracker", "Task") or "Task"
            adapter = cls(
                base_url=config.base_url,
                api_key=api_key,
                status_map=_status_map,
                tracker=_tracker,
            )
        except Exception:
            logger.exception(
                "pm_sync_handler: failed to instantiate adapter for provider=%r", provider
            )
            return None

        if hasattr(adapter, "set_project_map") and config.project_map:
            adapter.set_project_map(config.project_map)

        return adapter

    def _resolve_issue_id(
        self, change_name: str | None, subject: str
    ) -> int | None:
        """Attempt to resolve a PM issue id from context.

        Looks up the adapter's issue list for a matching subject keyword.
        Returns ``None`` when unresolvable so callers can decide whether to skip or log.
        """
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
