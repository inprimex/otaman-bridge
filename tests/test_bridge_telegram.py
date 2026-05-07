"""Tests for bridge/transports/telegram.py — mocked python-telegram-bot.

All Telegram interactions are mocked. Real bot integration is a manual
smoke test run with `maestro bridge run --transport telegram` (T2c).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# Skip the whole module cleanly when python-telegram-bot isn't available
# (CI matrix, users without the optional dep). Every test here depends on
# the real library's types.
pytest.importorskip("telegram")
pytest.importorskip("telegram.ext")

from otaman_bridge.core import ApprovalRequest, InboundReply, InfoMessage  # noqa: E402
from otaman_bridge.transports import telegram as tg_module  # noqa: E402
from otaman_bridge.transports.telegram import (  # noqa: E402
    TelegramTransport,
    decode_callback,
    encode_callback,
)


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def base_config(tmp_path):
    """Base config with an isolated topic cache file per test."""
    return {
        "bot_token": "fake-token-123",
        "group_id": -1001111111111,
        "allowed_user_ids": [12345, 67890],
        "topic_cache_file": str(tmp_path / "topics.json"),
        "auto_create_topics": False,  # default off in tests unless overridden
    }


@pytest.fixture
def mock_bot(monkeypatch):
    """Replace telegram.Bot with a MagicMock that records calls."""
    bot_instance = MagicMock()
    bot_instance.send_message = AsyncMock()
    bot_instance.edit_message_text = AsyncMock()
    bot_instance.edit_message_reply_markup = AsyncMock()

    # When send_message is awaited, return a Mock with a message_id attr.
    sent_msg = MagicMock()
    sent_msg.message_id = 42
    bot_instance.send_message.return_value = sent_msg

    bot_cls = MagicMock(return_value=bot_instance)
    monkeypatch.setattr(tg_module, "Bot", bot_cls)
    return bot_instance


def _make_request(project: str = "demo", request_id: str = "20260423T193000-abcd") -> ApprovalRequest:
    return ApprovalRequest(
        account="personal",
        project=project,
        repo="auth-service",
        agent="backend-agent",
        tool_name="Bash",
        tool_input={"command": "npm install jsonwebtoken@9.0.2"},
        reason="installing dependency for JWT signing",
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Callback encoding


class TestCallbackEncoding:
    def test_roundtrip(self):
        for action in ("approve", "reject", "details", "snooze"):
            encoded = encode_callback(action, "req-123")
            decoded_action, decoded_req = decode_callback(encoded)
            assert decoded_action == action
            assert decoded_req == "req-123"

    def test_encoded_fits_in_callback_data_limit(self):
        """Telegram caps callback_data at 64 bytes. Our request_ids are
        24 chars; action codes 1 char; separator 1 char → max 26 bytes."""
        rid = "20260423T193000-abcdefgh"  # max realistic request_id shape
        for action in ("approve", "reject", "details", "snooze"):
            encoded = encode_callback(action, rid)
            assert len(encoded.encode("utf-8")) <= 64

    def test_unknown_action_passes_through(self):
        """Decoder gracefully handles unknown codes (defense)."""
        action, rid = decode_callback("ZZZ|req-1")
        assert action == "ZZZ"
        assert rid == "req-1"

    def test_malformed_returns_empty_request_id(self):
        action, rid = decode_callback("approve-with-no-separator")
        # No "|" → whole string becomes the code, rid is empty
        assert rid == ""


# ---------------------------------------------------------------------------
# Construction / validation


class TestConstruction:
    def test_requires_bot_token(self, base_config):
        bad = dict(base_config)
        bad["bot_token"] = ""
        with pytest.raises(ValueError, match="bot_token"):
            TelegramTransport(bad)

    def test_requires_group_id(self, base_config):
        bad = dict(base_config)
        del bad["group_id"]
        with pytest.raises(ValueError, match="group_id"):
            TelegramTransport(bad)

    def test_normalizes_allowed_user_ids(self, mock_bot, base_config):
        """Accepts str or int user IDs; normalizes to ints internally."""
        base_config["allowed_user_ids"] = [12345, "67890"]
        t = TelegramTransport(base_config)
        assert t.allowed_user_ids == {12345, 67890}

    def test_topic_map_coerces_values(self, mock_bot, base_config):
        base_config["topic_map"] = {"demo": 7, "other": "8"}
        t = TelegramTransport(base_config)
        assert t.topic_map == {"demo": 7, "other": 8}

    def test_default_topic_id_optional(self, mock_bot, base_config):
        t = TelegramTransport(base_config)
        assert t.default_topic_id is None
        t2 = TelegramTransport({**base_config, "default_topic_id": 5})
        assert t2.default_topic_id == 5


# ---------------------------------------------------------------------------
# Outbound: send_approval


class TestSendApproval:
    def test_posts_to_group_with_keyboard(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            req = _make_request()
            handle = await transport.send_approval(req)

            mock_bot.send_message.assert_awaited_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == base_config["group_id"]
            assert "reply_markup" in call_kwargs
            # The text should at least include project, repo, agent
            assert req.project in call_kwargs["text"]
            assert req.repo in call_kwargs["text"]
            assert req.agent in call_kwargs["text"]

            # Handle carries enough to edit the message later
            assert handle.transport == "telegram"
            assert handle.data["chat_id"] == base_config["group_id"]
            assert handle.data["message_id"] == 42
            assert handle.data["request_id"] == req.request_id

        asyncio.run(run())

    def test_uses_topic_from_topic_map(self, mock_bot, base_config):
        base_config["topic_map"] = {"demo": 7}
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request(project="demo"))
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["message_thread_id"] == 7

        asyncio.run(run())

    def test_falls_back_to_default_topic(self, mock_bot, base_config):
        base_config["default_topic_id"] = 99
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request(project="unmapped"))
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["message_thread_id"] == 99

        asyncio.run(run())

    def test_no_topic_when_none_configured(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request())
            call_kwargs = mock_bot.send_message.call_args.kwargs
            # message_thread_id omitted — lets Telegram post to general.
            assert "message_thread_id" not in call_kwargs

        asyncio.run(run())

    def test_includes_bash_command_in_preview(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request())
            text = mock_bot.send_message.call_args.kwargs["text"]
            assert "npm install" in text

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Outbound: send_info


class TestSendInfo:
    def test_no_keyboard_on_info(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            msg = InfoMessage(
                account="personal", project="demo",
                severity="info", title="task complete",
                body="auth finished task 3.1",
            )
            handle = await transport.send_info(msg)
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert "reply_markup" not in call_kwargs
            assert "task complete" in call_kwargs["text"]
            assert handle.transport == "telegram"

        asyncio.run(run())

    def test_severity_emoji_in_header(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            for severity, expected_emoji in [
                ("info", "🟢"),
                ("approval", "🟡"),
                ("blocking", "🔴"),
            ]:
                mock_bot.send_message.reset_mock()
                await transport.send_info(InfoMessage(
                    account="p", project="x", severity=severity, title="t",
                ))
                text = mock_bot.send_message.call_args.kwargs["text"]
                assert expected_emoji in text

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Outbound: update


class TestUpdate:
    def test_strips_keyboard_and_appends_status(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            from otaman_bridge.core import TransportHandle
            handle = TransportHandle(
                transport="telegram",
                data={"chat_id": -1001111, "message_id": 42},
            )
            await transport.update(handle, "✓ approved by Roman")

            mock_bot.edit_message_reply_markup.assert_awaited_once()
            rm_kwargs = mock_bot.edit_message_reply_markup.call_args.kwargs
            assert rm_kwargs["chat_id"] == -1001111
            assert rm_kwargs["message_id"] == 42
            assert rm_kwargs["reply_markup"] is None

            mock_bot.edit_message_text.assert_awaited_once()
            et_kwargs = mock_bot.edit_message_text.call_args.kwargs
            assert "approved by Roman" in et_kwargs["text"]

        asyncio.run(run())

    def test_malformed_handle_silently_skipped(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            from otaman_bridge.core import TransportHandle
            bad = TransportHandle(transport="telegram", data={})
            # No exception
            await transport.update(bad, "status")
            mock_bot.edit_message_reply_markup.assert_not_called()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Allowlist


class TestAllowlist:
    def test_allowed_uid_passes(self, mock_bot, base_config):
        async def run():
            t = TelegramTransport(base_config)
            assert await t.allowlist_check("telegram:12345")

        asyncio.run(run())

    def test_unlisted_uid_rejected(self, mock_bot, base_config):
        async def run():
            t = TelegramTransport(base_config)
            assert not await t.allowlist_check("telegram:99999")

        asyncio.run(run())

    def test_non_telegram_prefix_rejected(self, mock_bot, base_config):
        async def run():
            t = TelegramTransport(base_config)
            assert not await t.allowlist_check("slack:12345")
            assert not await t.allowlist_check("12345")

        asyncio.run(run())

    def test_malformed_uid_rejected(self, mock_bot, base_config):
        async def run():
            t = TelegramTransport(base_config)
            assert not await t.allowlist_check("telegram:not-a-number")

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Inbound: callback query handler


class TestCallbackHandler:
    def _make_query(self, user_id: int, data: str):
        """Build a fake Update with a callback_query."""
        query = MagicMock()
        query.answer = AsyncMock()
        query.data = data
        query.from_user = MagicMock()
        query.from_user.id = user_id
        update = MagicMock()
        update.callback_query = query
        return update, query

    def test_allowed_tap_pushes_inbound_reply(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            update, query = self._make_query(
                user_id=12345,
                data=encode_callback("approve", "req-abc"),
            )
            await transport._on_callback_query(update, None)
            query.answer.assert_awaited()  # silent ack

            reply = await asyncio.wait_for(
                transport._inbound.get(), timeout=1.0,
            )
            assert reply.request_id == "req-abc"
            assert reply.action == "approve"
            assert reply.responder == "telegram:12345"

        asyncio.run(run())

    def test_unauthorized_tap_rejected_no_inbound(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            update, query = self._make_query(
                user_id=99999,  # not in allowlist
                data=encode_callback("approve", "req-abc"),
            )
            await transport._on_callback_query(update, None)
            # Alert shown to user — show_alert=True
            query.answer.assert_awaited_once()
            assert query.answer.call_args.kwargs.get("show_alert") is True

            # Nothing reaches inbound queue
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    transport._inbound.get(), timeout=0.1,
                )

        asyncio.run(run())

    def test_malformed_callback_data_rejected(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            update, query = self._make_query(
                user_id=12345,
                data="",  # empty callback data
            )
            await transport._on_callback_query(update, None)
            # Replied with an alert, no inbound push
            query.answer.assert_awaited_once()
            kwargs = query.answer.call_args.kwargs
            assert kwargs.get("show_alert") is True

        asyncio.run(run())

    def test_multiple_actions_yield_correct_replies(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            for action in ("approve", "reject", "details", "snooze"):
                update, _ = self._make_query(
                    user_id=12345,
                    data=encode_callback(action, f"req-{action}"),
                )
                await transport._on_callback_query(update, None)

            # Drain and verify
            replies = []
            for _ in range(4):
                replies.append(await asyncio.wait_for(
                    transport._inbound.get(), timeout=1.0,
                ))
            actions = [r.action for r in replies]
            assert actions == ["approve", "reject", "details", "snooze"]

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Reply handler (T2d-4)


class TestReplyHandler:
    """MessageHandler path: free-text replies to approval cards become
    comment InboundReply objects. Unrelated or unauthorized replies are
    dropped."""

    def _make_reply_update(
        self, *, user_id: int, text: str, reply_to_message_id: int,
    ):
        """Build a fake Update for a text message that replies to an
        earlier message_id."""
        replied = MagicMock()
        replied.message_id = reply_to_message_id

        msg = MagicMock()
        msg.text = text
        msg.reply_to_message = replied
        msg.from_user = MagicMock()
        msg.from_user.id = user_id

        update = MagicMock()
        update.message = msg
        return update

    def test_reply_to_known_card_produces_comment(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            # Simulate that message_id 42 was a prior approval card for
            # request "req-abc" (normally populated by send_approval).
            transport._reply_index[42] = "req-abc"

            update = self._make_reply_update(
                user_id=12345, text="use cursor pagination",
                reply_to_message_id=42,
            )
            await transport._on_reply_message(update, None)

            reply = await asyncio.wait_for(
                transport._inbound.get(), timeout=1.0,
            )
            assert reply.action == "comment"
            assert reply.request_id == "req-abc"
            assert reply.comment == "use cursor pagination"
            assert reply.responder == "telegram:12345"

        asyncio.run(run())

    def test_reply_to_unknown_message_id_is_dropped(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            # No entry for message_id=99 in _reply_index.
            update = self._make_reply_update(
                user_id=12345, text="some reply", reply_to_message_id=99,
            )
            await transport._on_reply_message(update, None)

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    transport._inbound.get(), timeout=0.1,
                )

        asyncio.run(run())

    def test_reply_from_unauthorized_user_is_dropped(self, mock_bot, base_config):
        async def run():
            transport = TelegramTransport(base_config)
            transport._reply_index[42] = "req-abc"

            update = self._make_reply_update(
                user_id=99999,  # not in allowlist
                text="try to sneak in", reply_to_message_id=42,
            )
            await transport._on_reply_message(update, None)

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    transport._inbound.get(), timeout=0.1,
                )

        asyncio.run(run())

    def test_send_approval_registers_message_id(self, mock_bot, base_config):
        """send_approval populates _reply_index for future replies."""
        async def run():
            transport = TelegramTransport(base_config)
            # mock_bot fixture already sets message_id = 42 on send return.
            await transport.send_approval(_make_request(request_id="new-req"))
            assert transport._reply_index.get(42) == "new-req"

        asyncio.run(run())

    def test_reply_index_is_trimmed_when_full(self, mock_bot, base_config):
        """The message_id → request_id map has an upper bound."""
        async def run():
            transport = TelegramTransport(base_config)
            transport._reply_index_max = 3
            # Simulate sending 5 cards with different message_ids.
            for i in range(5):
                mock_bot.send_message.return_value.message_id = 100 + i
                await transport.send_approval(_make_request(request_id=f"r-{i}"))
            assert len(transport._reply_index) <= 3
            # Most recent ones should survive.
            assert 104 in transport._reply_index
            assert transport._reply_index[104] == "r-4"

        asyncio.run(run())

    def test_reply_without_reply_to_is_dropped(self, mock_bot, base_config):
        """Plain messages (not replies) are ignored — filters.REPLY in
        _ensure_running handles this at the dispatcher level, but the
        handler itself must still be defensive."""
        async def run():
            transport = TelegramTransport(base_config)
            msg = MagicMock()
            msg.reply_to_message = None
            update = MagicMock()
            update.message = msg
            await transport._on_reply_message(update, None)

            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    transport._inbound.get(), timeout=0.1,
                )

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Registration


class TestRegistration:
    def test_telegram_is_registered(self):
        """Importing bridge.transports.telegram registers it by name."""
        from otaman_bridge.core import get_transport
        # register_transport runs at module import time
        cls = get_transport("telegram")
        assert cls is TelegramTransport


# ---------------------------------------------------------------------------
# Keyboard structure


class TestForumTopicAutoCreate:
    """Auto-create forum topics for unmapped projects, cache per-account."""

    def _make_topic_result(self, thread_id: int):
        topic = MagicMock()
        topic.message_thread_id = thread_id
        return topic

    def test_auto_create_when_unmapped(self, mock_bot, base_config, tmp_path):
        base_config["auto_create_topics"] = True
        mock_bot.create_forum_topic = AsyncMock(
            return_value=self._make_topic_result(77),
        )
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request(project="newproj"))
            mock_bot.create_forum_topic.assert_awaited_once()
            call_kwargs = mock_bot.create_forum_topic.call_args.kwargs
            assert call_kwargs["chat_id"] == base_config["group_id"]
            assert call_kwargs["name"] == "newproj"
            # send_message should have used the newly-created topic id
            send_kwargs = mock_bot.send_message.call_args.kwargs
            assert send_kwargs["message_thread_id"] == 77

        asyncio.run(run())

    def test_caches_across_calls(self, mock_bot, base_config):
        base_config["auto_create_topics"] = True
        mock_bot.create_forum_topic = AsyncMock(
            return_value=self._make_topic_result(77),
        )
        async def run():
            transport = TelegramTransport(base_config)
            # First call creates
            await transport.send_approval(_make_request(project="p1"))
            # Second call for same project — should hit cache, no new create
            await transport.send_approval(_make_request(project="p1"))
            assert mock_bot.create_forum_topic.await_count == 1

        asyncio.run(run())

    def test_cache_persists_to_disk(self, mock_bot, base_config):
        import json
        base_config["auto_create_topics"] = True
        mock_bot.create_forum_topic = AsyncMock(
            return_value=self._make_topic_result(77),
        )
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request(project="p1"))

            cache_file = Path(base_config["topic_cache_file"])
            assert cache_file.is_file()
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            assert data == {"p1": 77}

        asyncio.run(run())

    def test_cache_loaded_on_new_transport(self, mock_bot, base_config, tmp_path):
        import json
        base_config["auto_create_topics"] = True
        # Pre-populate the cache file
        Path(base_config["topic_cache_file"]).write_text(
            json.dumps({"existing": 55}), encoding="utf-8",
        )
        mock_bot.create_forum_topic = AsyncMock(
            return_value=self._make_topic_result(77),
        )
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request(project="existing"))
            # Should NOT call create — cache hit
            mock_bot.create_forum_topic.assert_not_awaited()
            send_kwargs = mock_bot.send_message.call_args.kwargs
            assert send_kwargs["message_thread_id"] == 55

        asyncio.run(run())

    def test_topic_map_beats_cache(self, mock_bot, base_config):
        """Explicit topic_map in config wins over any cache entry."""
        import json
        Path(base_config["topic_cache_file"]).write_text(
            json.dumps({"p1": 55}), encoding="utf-8",
        )
        base_config["topic_map"] = {"p1": 99}
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request(project="p1"))
            send_kwargs = mock_bot.send_message.call_args.kwargs
            assert send_kwargs["message_thread_id"] == 99

        asyncio.run(run())

    def test_create_failure_retries_on_next_message(self, mock_bot, base_config):
        """Failures are NOT cached — user fixing permissions self-heals
        on the next message (no manual cache clear needed)."""
        base_config["auto_create_topics"] = True
        # First call fails, second call succeeds (simulates user promoting
        # the bot to admin with Manage topics between messages).
        topic_result = MagicMock()
        topic_result.message_thread_id = 123
        mock_bot.create_forum_topic = AsyncMock(side_effect=[
            RuntimeError("Not enough rights to create a topic"),
            topic_result,
        ])
        async def run():
            transport = TelegramTransport(base_config)
            # First call — fails, falls back to None topic (no default set)
            await transport.send_approval(_make_request(project="bad"))
            send_kwargs = mock_bot.send_message.call_args.kwargs
            assert "message_thread_id" not in send_kwargs

            # Second call — SHOULD retry create_forum_topic, succeeds,
            # caches the positive thread_id for subsequent calls.
            await transport.send_approval(_make_request(project="bad"))
            assert mock_bot.create_forum_topic.await_count == 2
            send_kwargs = mock_bot.send_message.call_args.kwargs
            assert send_kwargs["message_thread_id"] == 123

            # Third call — positive cache hit, no more create_forum_topic calls.
            mock_bot.create_forum_topic.reset_mock()
            await transport.send_approval(_make_request(project="bad"))
            mock_bot.create_forum_topic.assert_not_awaited()

        asyncio.run(run())

    def test_create_failure_falls_back_to_default_topic(self, mock_bot, base_config):
        """If create fails but default_topic_id is set, use that."""
        base_config["auto_create_topics"] = True
        base_config["default_topic_id"] = 88
        mock_bot.create_forum_topic = AsyncMock(side_effect=RuntimeError("no rights"))
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request(project="bad"))
            send_kwargs = mock_bot.send_message.call_args.kwargs
            assert send_kwargs["message_thread_id"] == 88

        asyncio.run(run())

    def test_auto_create_disabled_falls_back(self, mock_bot, base_config):
        """With auto_create_topics=False, never call create_forum_topic."""
        base_config["auto_create_topics"] = False
        base_config["default_topic_id"] = 33
        mock_bot.create_forum_topic = AsyncMock()
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request(project="nope"))
            mock_bot.create_forum_topic.assert_not_awaited()
            send_kwargs = mock_bot.send_message.call_args.kwargs
            assert send_kwargs["message_thread_id"] == 33

        asyncio.run(run())

    def test_corrupt_cache_treated_as_empty(self, mock_bot, base_config):
        """Garbage cache file → fresh state, log warning, no crash."""
        base_config["auto_create_topics"] = True
        Path(base_config["topic_cache_file"]).write_text(
            "not json", encoding="utf-8",
        )
        mock_bot.create_forum_topic = AsyncMock(
            return_value=self._make_topic_result(77),
        )
        async def run():
            transport = TelegramTransport(base_config)
            await transport.send_approval(_make_request(project="p1"))
            mock_bot.create_forum_topic.assert_awaited_once()

        asyncio.run(run())


class TestKeyboardStructure:
    def test_keyboard_has_four_buttons(self, mock_bot, base_config):
        transport = TelegramTransport(base_config)
        markup = transport._build_keyboard("req-1")
        # InlineKeyboardMarkup is serializable; inspect its .to_dict()
        data = markup.to_dict()
        # Flatten rows
        buttons = [btn for row in data["inline_keyboard"] for btn in row]
        assert len(buttons) == 4
        texts = [b["text"] for b in buttons]
        assert any("Approve" in t for t in texts)
        assert any("Reject" in t for t in texts)
        assert any("Details" in t for t in texts)
        assert any("Snooze" in t for t in texts)

    def test_callback_data_round_trips(self, mock_bot, base_config):
        transport = TelegramTransport(base_config)
        markup = transport._build_keyboard("req-xyz")
        data = markup.to_dict()
        for row in data["inline_keyboard"]:
            for btn in row:
                # Every button's callback_data should decode to (action, req-xyz)
                action, rid = decode_callback(btn["callback_data"])
                assert rid == "req-xyz"
                assert action in {"approve", "reject", "details", "snooze"}
