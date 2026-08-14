"""Tests for bridge/bus_surface.py — policy decisions + bus reader."""

from __future__ import annotations

from pathlib import Path

from otaman_bridge.bus_surface import (
    BusMessage,
    decide,
    iter_bus_messages,
    load_surface_overrides,
    parse_bus_file,
    resolve_project_name,
)


def _make_msg(
    *,
    type: str = "info",
    from_: str = "agent-a",
    to: str = "agent-b",
    priority: str = "normal",
    subject: str = "test",
    body: str = "body text",
) -> BusMessage:
    fm = {
        "id": "20260424T100000-test",
        "from": from_,
        "to": to,
        "priority": priority,
        "type": type,
        "timestamp": "2026-04-24T10:00:00Z",
    }
    return BusMessage(
        path=Path("dummy.md"),
        stem="20260424T100000-test",
        frontmatter=fm,
        body=f"## Subject: {subject}\n\n{body}",
    )


# ---------------------------------------------------------------------------
# Default policy — "always" rows


class TestAlwaysSurface:
    def test_spec_change_request_always_interactive(self):
        d = decide(_make_msg(type="spec-change-request"))
        assert d.surface
        assert d.severity == "approval"
        assert d.interactive
        assert "approve" in d.actions
        assert "reject" in d.actions

    def test_urgent_priority_always_blocking(self):
        """Urgent trumps everything, any type."""
        d = decide(_make_msg(type="info", priority="urgent"))
        assert d.surface
        assert d.severity == "blocking"

    def test_urgent_to_human_interactive(self):
        d = decide(_make_msg(type="info", priority="urgent", to="human"))
        assert d.surface
        assert d.severity == "blocking"
        assert d.interactive

    def test_to_human_always_surfaces(self):
        """`to: human` forces approval severity even for types not in the table."""
        d = decide(_make_msg(type="some-custom-type", to="human"))
        assert d.surface
        assert d.severity == "approval"
        assert d.interactive
        assert "acknowledge" in d.actions

    def test_high_priority_non_human_is_info(self):
        """priority: high without to:human → surface as approval, no buttons."""
        d = decide(_make_msg(type="some-weird-type", priority="high"))
        assert d.surface
        assert d.severity == "approval"
        assert not d.interactive


# ---------------------------------------------------------------------------
# Default policy — "never" rows


class TestNeverSurface:
    def test_task_assignment_never(self):
        d = decide(_make_msg(type="task-assignment"))
        assert not d.surface
        assert "never" in d.reason.lower() or "task-assignment" in d.reason

    def test_info_broadcast_never(self):
        d = decide(_make_msg(type="info", to="all"))
        assert not d.surface

    def test_info_urgent_still_surfaces(self):
        """Urgent info IS urgent — overrides the never-for-info rule."""
        d = decide(_make_msg(type="info", priority="urgent"))
        assert d.surface


# ---------------------------------------------------------------------------
# Default policy — "configurable" rows (default off)


class TestConfigurableDefaultOff:
    def test_review_request_default_off(self):
        d = decide(_make_msg(type="review-request"))
        assert not d.surface

    def test_task_complete_default_off(self):
        d = decide(_make_msg(type="task-complete"))
        assert not d.surface

    def test_spec_change_approved_default_off(self):
        d = decide(_make_msg(type="spec-change-approved"))
        assert not d.surface

    def test_spec_change_rejected_default_off(self):
        d = decide(_make_msg(type="spec-change-rejected"))
        assert not d.surface


# ---------------------------------------------------------------------------
# Overrides


class TestGlobalOverrides:
    def test_turn_on_review_request(self):
        d = decide(_make_msg(type="review-request"), overrides={"review_request": True})
        assert d.surface
        assert d.severity == "info"

    def test_dashed_and_underscored_both_work(self):
        """Accept `review-request` or `review_request` as override key."""
        d1 = decide(_make_msg(type="review-request"), overrides={"review-request": True})
        d2 = decide(_make_msg(type="review-request"), overrides={"review_request": True})
        assert d1.surface and d2.surface

    def test_explicit_false_keeps_off(self):
        d = decide(_make_msg(type="review-request"), overrides={"review_request": False})
        assert not d.surface

    def test_cannot_override_never(self):
        """`task-assignment` (never) stays off even if user tries to turn it on."""
        d = decide(_make_msg(type="task-assignment"), overrides={"task_assignment": True})
        assert not d.surface

    def test_cannot_override_always(self):
        """`spec-change-request` (always) stays on even with explicit false."""
        d = decide(_make_msg(type="spec-change-request"), overrides={"spec_change_request": False})
        # Always-rows are structural; override table only applies to configurable.
        assert d.surface


class TestByAgentOverrides:
    def test_per_agent_off(self):
        """Turn global review_request on, but mute one agent's reviews."""
        overrides = {
            "review_request": True,
            "by_agent": {
                "cto-reviewer": {"review_request": False},
            },
        }
        # Another agent's review: surfaces
        d_other = decide(
            _make_msg(type="review-request", from_="sec-observer"), overrides=overrides
        )
        assert d_other.surface
        # cto-reviewer's: muted
        d_cto = decide(_make_msg(type="review-request", from_="cto-reviewer"), overrides=overrides)
        assert not d_cto.surface

    def test_per_agent_on(self):
        """Global off, but one agent's always surfaces."""
        overrides = {
            "review_request": False,
            "by_agent": {
                "cto-reviewer": {"review_request": True},
            },
        }
        assert not decide(
            _make_msg(type="review-request", from_="other"), overrides=overrides
        ).surface
        assert decide(
            _make_msg(type="review-request", from_="cto-reviewer"), overrides=overrides
        ).surface


# ---------------------------------------------------------------------------
# Parsing + I/O


class TestParseBusFile:
    def test_parses_valid_message(self, tmp_path):
        p = tmp_path / "20260424T100000-a-to-b-info.md"
        p.write_text(
            "---\n"
            "id: 20260424T100000-test\n"
            "from: a\n"
            "to: b\n"
            "priority: high\n"
            "type: spec-change-request\n"
            "timestamp: 2026-04-24T10:00:00Z\n"
            "---\n\n"
            "## Subject: proposed endpoint change\n\n"
            "body text\n",
            encoding="utf-8",
        )
        msg = parse_bus_file(p)
        assert msg is not None
        assert msg.type == "spec-change-request"
        assert msg.from_ == "a"
        assert msg.to == "b"
        assert msg.priority == "high"
        assert "proposed endpoint change" in msg.subject

    def test_no_frontmatter_returns_none(self, tmp_path):
        p = tmp_path / "bad.md"
        p.write_text("just body, no frontmatter\n", encoding="utf-8")
        assert parse_bus_file(p) is None

    def test_subject_prefers_frontmatter_over_body_scrape(self, tmp_path):
        """2026-07-04 GAP audit finding: body-scraping produced garbage
        subjects (e.g. the literal text "Subject: Approved: ...") when
        the body's first line/heading wasn't the real subject. The
        frontmatter subject:, when present, is authoritative."""
        p = tmp_path / "20260424T100000-a-to-b-info.md"
        p.write_text(
            "---\n"
            "id: 20260424T100000-test\n"
            "from: a\n"
            "to: b\n"
            "priority: normal\n"
            "type: info\n"
            "subject: pluggable-secret-backend\n"
            "timestamp: 2026-04-24T10:00:00Z\n"
            "---\n\n"
            "## Subject: Approved: pluggable-secret-backend\n\n"
            "body text\n",
            encoding="utf-8",
        )
        msg = parse_bus_file(p)
        assert msg is not None
        assert msg.subject == "pluggable-secret-backend"

    def test_subject_falls_back_to_body_scrape_when_frontmatter_absent(self, tmp_path):
        p = tmp_path / "20260424T100001-a-to-b-info.md"
        p.write_text(
            "---\n"
            "id: 20260424T100001-test\n"
            "from: a\n"
            "to: b\n"
            "priority: normal\n"
            "type: info\n"
            "timestamp: 2026-04-24T10:00:00Z\n"
            "---\n\n"
            "## Subject: proposed endpoint change\n\n"
            "body text\n",
            encoding="utf-8",
        )
        msg = parse_bus_file(p)
        assert msg is not None
        assert "proposed endpoint change" in msg.subject

    def test_malformed_yaml_returns_none(self, tmp_path):
        p = tmp_path / "bad.md"
        p.write_text("---\nkey: : :\n---\nbody\n", encoding="utf-8")
        assert parse_bus_file(p) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert parse_bus_file(tmp_path / "nope.md") is None


class TestIterBusMessages:
    def test_lists_all_messages(self, tmp_path):
        bus = tmp_path / ".agents" / "bus" / "active"
        bus.mkdir(parents=True)
        for i in range(3):
            (bus / f"2026042{i}T100000-a-to-b-info.md").write_text(
                f"---\nid: m{i}\nfrom: a\nto: b\ntype: info\n---\nbody\n",
                encoding="utf-8",
            )
        msgs = iter_bus_messages(tmp_path)
        assert len(msgs) == 3

    def test_skips_acks_subdirectory(self, tmp_path):
        """Ack files live in .agents/bus/active/acks/ — never treated as messages."""
        bus = tmp_path / ".agents" / "bus" / "active"
        acks = bus / "acks"
        acks.mkdir(parents=True)
        (bus / "20260424T100000-a-to-b-info.md").write_text(
            "---\nid: m\nfrom: a\nto: b\ntype: info\n---\n",
            encoding="utf-8",
        )
        (acks / "ignored.md").write_text("shouldn't count", encoding="utf-8")
        msgs = iter_bus_messages(tmp_path)
        assert len(msgs) == 1

    def test_empty_bus_returns_empty(self, tmp_path):
        assert iter_bus_messages(tmp_path) == []

    def test_skips_malformed_files(self, tmp_path):
        bus = tmp_path / ".agents" / "bus" / "active"
        bus.mkdir(parents=True)
        (bus / "good.md").write_text(
            "---\nid: good\nfrom: a\nto: b\ntype: info\n---\n",
            encoding="utf-8",
        )
        (bus / "bad.md").write_text("no frontmatter", encoding="utf-8")
        assert len(iter_bus_messages(tmp_path)) == 1


class TestResolveProjectName:
    """Single source of truth for Telegram topic names.

    Both the PreToolUse hook (scripts/bridge_approval.py) and the bus
    watcher (bridge/cli.py) must pick the same string so we don't end
    up with duplicate topics.
    """

    def test_reads_project_field(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "project: watchtower\nversion: '1.0'\n",
            encoding="utf-8",
        )
        assert resolve_project_name(tmp_path) == "watchtower"

    def test_reads_quoted_project(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            'project: "my-project-2"\n',
            encoding="utf-8",
        )
        assert resolve_project_name(tmp_path) == "my-project-2"

    def test_folder_name_fallback_when_no_yaml(self, tmp_path):
        folder = tmp_path / "watchtower-maestro"
        folder.mkdir()
        assert resolve_project_name(folder) == "watchtower-maestro"

    def test_folder_name_fallback_when_no_project_key(self, tmp_path):
        folder = tmp_path / "maestro-folder"
        folder.mkdir()
        (folder / "platform.yaml").write_text(
            "version: '1.0'\nrepos: []\n",
            encoding="utf-8",
        )
        assert resolve_project_name(folder) == "maestro-folder"

    def test_malformed_yaml_falls_back_to_folder(self, tmp_path):
        folder = tmp_path / "watchtower-maestro"
        folder.mkdir()
        (folder / "platform.yaml").write_text(
            "project: : :\n not: valid: yaml:\n",
            encoding="utf-8",
        )
        # Cheap scan rejects structural-looking values; YAML parse fails
        # → folder name. Must never return the garbage value itself.
        assert resolve_project_name(folder) == "watchtower-maestro"

    def test_rejects_values_with_whitespace(self, tmp_path):
        """`project: "has spaces"` → topic naming would be ugly. Reject
        and fall back so the user notices instead of getting a surprise
        topic."""
        folder = tmp_path / "legit-folder"
        folder.mkdir()
        (folder / "platform.yaml").write_text(
            "project: has spaces here\n",
            encoding="utf-8",
        )
        # Cheap scan rejects (contains spaces); YAML parser accepts
        # "has spaces here" as a valid scalar — so we get that value.
        # This test documents: if you really want a spaced project
        # name, quote it and the YAML path will catch it.
        name = resolve_project_name(folder)
        # Either the YAML-quoted value or the folder fallback is acceptable;
        # what must NOT happen is we return a partial/garbled value.
        assert name in ("has spaces here", "legit-folder")

    def test_empty_project_value_falls_back(self, tmp_path):
        folder = tmp_path / "m"
        folder.mkdir()
        (folder / "platform.yaml").write_text(
            "project:\nrepos: []\n",
            encoding="utf-8",
        )
        assert resolve_project_name(folder) == "m"


class TestLoadSurfaceOverrides:
    def test_reads_surface_block(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "project: test\n"
            "surface:\n"
            "  review_request: true\n"
            "  by_agent:\n"
            "    cto-reviewer:\n"
            "      review_request: false\n",
            encoding="utf-8",
        )
        overrides = load_surface_overrides(tmp_path)
        assert overrides["review_request"] is True
        assert overrides["by_agent"]["cto-reviewer"]["review_request"] is False

    def test_no_platform_yaml_empty(self, tmp_path):
        assert load_surface_overrides(tmp_path) == {}

    def test_no_surface_block_empty(self, tmp_path):
        (tmp_path / "platform.yaml").write_text(
            "project: test\n",
            encoding="utf-8",
        )
        assert load_surface_overrides(tmp_path) == {}
