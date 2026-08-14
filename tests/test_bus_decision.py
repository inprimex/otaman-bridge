"""Tests for bridge/bus_decision.py — ack + broadcast file writers."""

from __future__ import annotations

from pathlib import Path

import pytest

from otaman_bridge.bus_decision import (
    broadcast_decision,
    record_decision,
    write_acknowledge,
    write_approval_ack,
    write_reply_message,
)
from otaman_bridge.bus_surface import BusMessage


def _make_msg(
    *,
    stem: str = "20260424T100000-backend-to-human-scr",
    from_: str = "backend-agent",
    to: str = "human",
    type: str = "spec-change-request",
    subject: str = "Add pagination to GET /users",
) -> BusMessage:
    fm = {
        "id": stem,
        "from": from_,
        "to": to,
        "type": type,
        "priority": "normal",
        "timestamp": "2026-04-24T10:00:00Z",
    }
    return BusMessage(
        path=Path(f"{stem}.md"),
        stem=stem,
        frontmatter=fm,
        body=f"## Subject: {subject}\n\nWe need pagination.\n",
    )


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "maestro"
    (root / ".agents" / "bus" / "active" / "acks").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# write_approval_ack


class TestWriteApprovalAck:
    def test_approved_writes_ack_file(self, project_root):
        path = write_approval_ack(
            project_root,
            "20260424T100000-backend-to-human-scr",
            decision="approved",
        )
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == "approved\n"
        assert path.name == "20260424T100000-backend-to-human-scr.human.ack"
        assert path.parent.name == "acks"

    def test_rejected_writes_ack_file(self, project_root):
        path = write_approval_ack(
            project_root,
            "some-stem",
            decision="rejected",
        )
        assert path.read_text(encoding="utf-8") == "rejected\n"

    def test_invalid_decision_raises(self, project_root):
        with pytest.raises(ValueError):
            write_approval_ack(project_root, "stem", decision="maybe")

    def test_creates_acks_dir_if_missing(self, tmp_path):
        root = tmp_path / "fresh"
        # .agents/bus/active/acks does NOT exist yet.
        path = write_approval_ack(root, "stem", decision="approved")
        assert path.is_file()
        assert path.parent.is_dir()


# ---------------------------------------------------------------------------
# broadcast_decision


class TestBroadcastApproved:
    def test_broadcast_has_correct_frontmatter(self, project_root):
        msg = _make_msg()
        path = broadcast_decision(
            project_root,
            msg,
            decision="approved",
            responder="telegram:12345",
            comment="Looks good",
        )
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert "type: spec-change-approved" in content
        assert "from: human" in content
        assert "to: all" in content
        assert msg.stem in content  # reference to original proposal
        assert "Looks good" in content  # comment surfaced
        assert "telegram:12345" in content  # responder noted
        # Filename slug should include the msg type
        assert path.name.endswith("-human-to-all-spec-change-approved.md")


class TestBroadcastRejected:
    def test_rejected_goes_to_proposer_not_all(self, project_root):
        msg = _make_msg(from_="frontend-agent")
        path = broadcast_decision(
            project_root,
            msg,
            decision="rejected",
            responder="telegram:12345",
            comment="Not now, focus on MVP",
        )
        content = path.read_text(encoding="utf-8")
        assert "type: spec-change-rejected" in content
        assert "to: frontend-agent" in content
        assert "Not now, focus on MVP" in content
        assert path.name.endswith("-human-to-frontend-agent-spec-change-rejected.md")

    def test_rejected_without_comment_has_default_reason(self, project_root):
        path = broadcast_decision(
            project_root,
            _make_msg(),
            decision="rejected",
        )
        content = path.read_text(encoding="utf-8")
        assert "No reason provided" in content


class TestBroadcastInvalid:
    def test_invalid_decision_raises(self, project_root):
        with pytest.raises(ValueError):
            broadcast_decision(project_root, _make_msg(), decision="maybe")


# ---------------------------------------------------------------------------
# record_decision


class TestRecordDecision:
    def test_writes_both_ack_and_broadcast(self, project_root):
        msg = _make_msg()
        ack, broadcast = record_decision(
            project_root,
            msg,
            decision="approved",
            responder="telegram:roman",
            comment="ship it",
        )
        assert ack.is_file()
        assert broadcast.is_file()
        assert ack.read_text(encoding="utf-8").strip() == "approved"
        bc_text = broadcast.read_text(encoding="utf-8")
        assert "ship it" in bc_text
        assert "spec-change-approved" in bc_text

    def test_reject_record(self, project_root):
        msg = _make_msg(from_="observer-agent")
        ack, broadcast = record_decision(
            project_root,
            msg,
            decision="rejected",
            comment="scope creep",
        )
        assert ack.read_text(encoding="utf-8").strip() == "rejected"
        bc_text = broadcast.read_text(encoding="utf-8")
        assert "scope creep" in bc_text
        assert "to: observer-agent" in bc_text


# ---------------------------------------------------------------------------
# write_reply_message (T2d-4)


class TestWriteReplyMessage:
    def test_reply_goes_to_original_proposer(self, project_root):
        msg = _make_msg(from_="backend-agent")
        path = write_reply_message(
            project_root,
            msg,
            text="Use cursor pagination, not offset.",
            responder="telegram:roman",
        )
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert "from: human" in content
        assert "to: backend-agent" in content
        assert "type: info" in content
        assert "Use cursor pagination" in content
        assert f"in_reply_to: {msg.stem}" in content
        assert "telegram:roman" in content
        assert path.name.endswith("-human-to-backend-agent-reply.md")

    def test_reply_without_responder_still_works(self, project_root):
        msg = _make_msg()
        path = write_reply_message(
            project_root,
            msg,
            text="sounds good",
        )
        assert path.is_file()
        # Responder line is optional — absent when empty.
        content = path.read_text(encoding="utf-8")
        assert "**Via**:" not in content


# ---------------------------------------------------------------------------
# write_acknowledge (T2d-4)


class TestWriteAcknowledge:
    def test_ack_only_no_comment(self, project_root):
        msg = _make_msg(to="human")
        ack_path, reply_path = write_acknowledge(
            project_root,
            msg,
            responder="telegram:roman",
        )
        assert ack_path.is_file()
        assert ack_path.read_text(encoding="utf-8").strip() == "acknowledged"
        assert reply_path is None

    def test_ack_with_comment_writes_both(self, project_root):
        msg = _make_msg(to="human", from_="ops-agent")
        ack_path, reply_path = write_acknowledge(
            project_root,
            msg,
            responder="telegram:roman",
            comment="on it — ETA 30 min",
        )
        assert ack_path.is_file()
        assert reply_path is not None
        assert reply_path.is_file()
        reply_content = reply_path.read_text(encoding="utf-8")
        assert "on it — ETA 30 min" in reply_content
        assert "to: ops-agent" in reply_content
