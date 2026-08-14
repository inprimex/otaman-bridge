"""Tests for otaman_bridge.inbox -- file-backed per-user inbox store."""

from __future__ import annotations

import time

import pytest

from otaman_bridge.inbox import (
    MAX_SUBJECT_LEN,
    Inbox,
    _derive_subject,
    _slugify,
)


@pytest.fixture
def inbox(tmp_path):
    return Inbox(root=tmp_path / "inboxes")


# ---- helpers ----------------------------------------------------------


class TestHelpers:
    def test_derive_subject_from_first_line(self):
        assert _derive_subject("Hello world\nrest") == "Hello world"

    def test_derive_subject_skips_blank_lines(self):
        assert _derive_subject("\n\nfirst real line\nsecond") == "first real line"

    def test_derive_subject_strips_markdown(self):
        assert _derive_subject("# Big title\nbody") == "Big title"
        assert _derive_subject("> quoted line") == "quoted line"
        assert _derive_subject("- bullet") == "bullet"

    def test_derive_subject_truncates(self):
        long = "x" * 200
        assert len(_derive_subject(long)) == MAX_SUBJECT_LEN

    def test_derive_subject_empty(self):
        assert _derive_subject("") == ""
        assert _derive_subject("\n\n\n") == ""

    def test_slugify_lowercase_dashed(self):
        assert _slugify("Hello World") == "hello-world"

    def test_slugify_strips_special_chars(self):
        assert _slugify("Help! @ #1?") == "help-1"

    def test_slugify_collapses_dashes(self):
        assert _slugify("a -- b --- c") == "a-b-c"

    def test_slugify_empty_fallback(self):
        assert _slugify("") == "msg"
        assert _slugify("!!!") == "msg"


# ---- write_message ----------------------------------------------------


class TestWriteMessage:
    def test_creates_file_with_frontmatter_and_body(self, inbox):
        msg = inbox.write_message(
            from_user="user-A",
            from_email="a@x",
            to_user="user-B",
            subject="Hi",
            body="Hello there\nsecond line",
        )
        assert msg.path.is_file()
        content = msg.path.read_text()
        assert content.startswith("---\n")
        assert "from_user: user-A" in content
        assert "to_user: user-B" in content
        assert "subject: Hi" in content
        assert "Hello there" in content
        # body separator
        assert content.count("---\n") == 2

    def test_lazy_creates_recipient_inbox_dir(self, inbox, tmp_path):
        target = inbox.root / "user-NEW" / "active"
        assert not target.is_dir()
        inbox.write_message(
            from_user="A",
            from_email=None,
            to_user="user-NEW",
            body="hi",
        )
        assert target.is_dir()

    def test_subject_auto_derived_when_omitted(self, inbox):
        msg = inbox.write_message(
            from_user="A",
            from_email=None,
            to_user="B",
            body="# Important header\nmore content",
        )
        assert msg.subject == "Important header"

    def test_empty_body_raises(self, inbox):
        with pytest.raises(ValueError, match="body"):
            inbox.write_message(from_user="A", from_email=None, to_user="B", body="")
        with pytest.raises(ValueError, match="body"):
            inbox.write_message(from_user="A", from_email=None, to_user="B", body="   \n  ")

    def test_invalid_priority_raises(self, inbox):
        with pytest.raises(ValueError, match="priority"):
            inbox.write_message(
                from_user="A", from_email=None, to_user="B", body="hi", priority="urgent"
            )

    def test_invalid_user_id_raises(self, inbox):
        with pytest.raises(ValueError, match="invalid user_id"):
            inbox.write_message(from_user="A", from_email=None, to_user="../escape", body="hi")
        with pytest.raises(ValueError, match="invalid user_id"):
            inbox.write_message(from_user="A", from_email=None, to_user="with/slash", body="hi")

    def test_concurrent_collision_appends_suffix(self, inbox, monkeypatch):
        """Two messages with same timestamp+subject get distinct filenames."""
        # Pin the clock so timestamps collide
        from datetime import datetime, timezone

        from otaman_bridge import inbox as inbox_mod

        fixed = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)

        class _FixedDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(inbox_mod, "datetime", _FixedDT)
        m1 = inbox.write_message(
            from_user="A", from_email=None, to_user="B", body="hello", subject="Greeting"
        )
        m2 = inbox.write_message(
            from_user="A", from_email=None, to_user="B", body="hello again", subject="Greeting"
        )
        assert m1.id != m2.id
        assert m1.path != m2.path
        assert m2.id.endswith("-1")


# ---- list_messages ----------------------------------------------------


class TestListMessages:
    def test_empty_inbox_returns_empty(self, inbox):
        assert inbox.list_messages("user-X") == []

    def test_returns_all_when_unread_only_false(self, inbox):
        for i in range(3):
            inbox.write_message(from_user="A", from_email=None, to_user="B", body=f"msg {i}")
        msgs = inbox.list_messages("B", unread_only=False)
        assert len(msgs) == 3

    def test_unread_only_default(self, inbox):
        m1 = inbox.write_message(from_user="A", from_email=None, to_user="B", body="m1")
        m2 = inbox.write_message(from_user="A", from_email=None, to_user="B", body="m2")
        inbox.mark_read("B", m1.id)
        msgs = inbox.list_messages("B")  # unread_only=True default
        assert {m.id for m in msgs} == {m2.id}

    def test_from_user_filter(self, inbox):
        inbox.write_message(from_user="A", from_email=None, to_user="B", body="from A")
        inbox.write_message(from_user="C", from_email=None, to_user="B", body="from C")
        msgs = inbox.list_messages("B", from_user="A", unread_only=False)
        assert len(msgs) == 1
        assert msgs[0].from_user == "A"

    def test_since_filter(self, inbox):
        m1 = inbox.write_message(from_user="A", from_email=None, to_user="B", body="m1")
        time.sleep(1.1)  # ensure different sent_at second
        m2 = inbox.write_message(from_user="A", from_email=None, to_user="B", body="m2")
        msgs = inbox.list_messages("B", since=m1.sent_at, unread_only=False)
        # only m2 is strictly after m1.sent_at
        assert len(msgs) == 1
        assert msgs[0].id == m2.id

    def test_limit_applies(self, inbox):
        for i in range(5):
            inbox.write_message(from_user="A", from_email=None, to_user="B", body=f"m{i}")
        msgs = inbox.list_messages("B", limit=3, unread_only=False)
        assert len(msgs) == 3

    def test_limit_bounds(self, inbox):
        with pytest.raises(ValueError, match="limit"):
            inbox.list_messages("B", limit=0)
        with pytest.raises(ValueError, match="limit"):
            inbox.list_messages("B", limit=999)

    def test_sorted_newest_first(self, inbox):
        m1 = inbox.write_message(from_user="A", from_email=None, to_user="B", body="first")
        time.sleep(1.1)
        m2 = inbox.write_message(from_user="A", from_email=None, to_user="B", body="second")
        msgs = inbox.list_messages("B", unread_only=False)
        assert [m.id for m in msgs] == [m2.id, m1.id]


# ---- mark_read --------------------------------------------------------


class TestMarkRead:
    def test_marks_single_message(self, inbox):
        m = inbox.write_message(from_user="A", from_email=None, to_user="B", body="hi")
        assert m.read_at is None
        count = inbox.mark_read("B", m.id)
        assert count == 1
        msgs = inbox.list_messages("B", unread_only=False)
        assert msgs[0].read_at is not None

    def test_unknown_message_returns_0(self, inbox):
        assert inbox.mark_read("B", "no-such-id") == 0

    def test_already_read_returns_0_doesnt_overwrite(self, inbox):
        m = inbox.write_message(from_user="A", from_email=None, to_user="B", body="hi")
        inbox.mark_read("B", m.id)
        first_read_at = inbox.list_messages("B", unread_only=False)[0].read_at
        time.sleep(0.05)
        assert inbox.mark_read("B", m.id) == 0
        # Read-at preserved
        assert inbox.list_messages("B", unread_only=False)[0].read_at == first_read_at

    def test_mark_all_before(self, inbox):
        inbox.write_message(from_user="A", from_email=None, to_user="B", body="m1")
        time.sleep(1.1)
        m2 = inbox.write_message(from_user="A", from_email=None, to_user="B", body="m2")
        time.sleep(1.1)
        m3 = inbox.write_message(from_user="A", from_email=None, to_user="B", body="m3")
        # mark_all_before on m2 -> m1 + m2 marked, m3 still unread
        count = inbox.mark_read("B", m2.id, mark_all_before=True)
        assert count == 2
        unread = inbox.list_messages("B")
        assert {m.id for m in unread} == {m3.id}


# ---- roundtrip --------------------------------------------------------


class TestRoundtrip:
    def test_message_survives_write_read(self, inbox):
        sent = inbox.write_message(
            from_user="user-A",
            from_email="a@example",
            to_user="user-B",
            subject="Test",
            body="Hello world",
            in_reply_to="prev-msg-id",
            priority="high",
            msg_type="review-request",
        )
        loaded = inbox.list_messages("user-B", unread_only=False)[0]
        assert loaded.id == sent.id
        assert loaded.from_user == "user-A"
        assert loaded.from_email == "a@example"
        assert loaded.to_user == "user-B"
        assert loaded.subject == "Test"
        assert loaded.in_reply_to == "prev-msg-id"
        assert loaded.priority == "high"
        assert loaded.type == "review-request"
        assert "Hello world" in loaded.body
        assert loaded.read_at is None

    def test_special_chars_in_subject_quoted(self, inbox):
        # YAML-special chars in subject should round-trip
        inbox.write_message(
            from_user="A",
            from_email=None,
            to_user="B",
            subject="What about: this? & this!",
            body="hi",
        )
        loaded = inbox.list_messages("B", unread_only=False)[0]
        assert loaded.subject == "What about: this? & this!"

    def test_email_none_writes_and_reads_back(self, inbox):
        inbox.write_message(from_user="A", from_email=None, to_user="B", body="hi")
        loaded = inbox.list_messages("B", unread_only=False)[0]
        assert loaded.from_email is None


# ---- file permissions -------------------------------------------------


class TestFilePermissions:
    def test_file_mode_is_0600(self, inbox, tmp_path):
        m = inbox.write_message(from_user="A", from_email=None, to_user="B", body="hi")
        import stat

        mode = stat.S_IMODE(m.path.stat().st_mode)
        # On POSIX: 0o600. On non-POSIX: skip the check.
        if hasattr(stat, "S_IRWXG"):  # POSIX
            assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
