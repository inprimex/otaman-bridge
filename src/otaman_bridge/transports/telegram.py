"""TelegramTransport — Telegram-flavored bridge transport.

Implements the Transport Protocol via ``python-telegram-bot``. All
Telegram-specific imports (`telegram`, `telegram.ext`) live in this file
only — the boundary lint fails the build if they leak anywhere else.

**Outbound** — ``send_approval`` / ``send_info`` / ``update``:
    Uses the ``Bot`` API directly. Messages land in a supergroup (per
    account) and a forum topic (per project). Approvals carry an inline
    keyboard with Approve / Reject / Details / Snooze buttons.

**Inbound** — ``listen`` + callback handler:
    An ``Application`` runs long-polling in a background task. Button
    taps arrive as ``CallbackQuery`` updates, get allowlist-checked,
    and become ``InboundReply`` objects yielded from ``listen()``.

**Config** (``accounts.<name>.transport_config``)::

    group_id: -1001234567890             # supergroup ID (with forum topics)
    bot_token: <resolved from _secrets>  # never in YAML
    allowed_user_ids: [12345, 67890]     # Telegram user IDs; taps from others
                                         # are rejected at the handler layer
    topic_map:                           # project → topic_id (optional)
      my-project: 42
      other-project: 43
    default_topic_id: null               # fallback if project absent from map

T2b-3 adds auto-creation of missing topics via ``Bot.create_forum_topic``.
For T2b-2, a missing topic_id causes messages to post to the group's
general topic (threadless) — good enough for smoke testing.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
from dataclasses import field
from pathlib import Path
from typing import Any, AsyncIterator

try:
    from telegram import (  # type: ignore[import-not-found]
        Bot,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Update,
    )
    from telegram.ext import (  # type: ignore[import-not-found]
        Application,
        CallbackQueryHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    _HAS_TELEGRAM = True
except ImportError:  # pragma: no cover — tests mock the library
    Bot = None  # type: ignore[assignment]
    InlineKeyboardButton = None  # type: ignore[assignment]
    InlineKeyboardMarkup = None  # type: ignore[assignment]
    Update = None  # type: ignore[assignment]
    Application = None  # type: ignore[assignment]
    CallbackQueryHandler = None  # type: ignore[assignment]
    ContextTypes = None  # type: ignore[assignment]
    MessageHandler = None  # type: ignore[assignment]
    filters = None  # type: ignore[assignment]
    _HAS_TELEGRAM = False

from otaman_bridge.core import (
    ApprovalRequest,
    InboundReply,
    InfoMessage,
    TransportHandle,
    register_transport,
)

_log = logging.getLogger("maestro.bridge.telegram")  # legacy: logger renamed at otaman-core 1.0


# ---------------------------------------------------------------------------
# Callback data encoding


_CALLBACK_SEP = "|"

# Short-form action codes kept under Telegram's 64-byte callback_data limit
# (action + "|" + request_id ≤ 64 bytes; our request_ids are ~24 chars).
_ACTION_CODES = {
    "approve": "A",
    "reject": "R",
    "details": "D",
    "snooze": "S",
    "comment": "C",
    "view-diff": "V",
}
_ACTION_DECODE = {v: k for k, v in _ACTION_CODES.items()}


def encode_callback(action: str, request_id: str) -> str:
    code = _ACTION_CODES.get(action, action)
    return f"{code}{_CALLBACK_SEP}{request_id}"


def decode_callback(data: str) -> tuple[str, str]:
    """Return (action, request_id). Unknown codes pass through verbatim."""
    code, _, request_id = data.partition(_CALLBACK_SEP)
    return _ACTION_DECODE.get(code, code), request_id


# ---------------------------------------------------------------------------
# TelegramTransport


_SEVERITY_EMOJI = {
    "info": "🟢",
    "approval": "🟡",
    "blocking": "🔴",
}


def _format_approval(req: ApprovalRequest) -> str:
    """Human-readable Telegram message body for an approval request."""
    lines = [
        f"🟡 [{req.project}] {req.repo} · {req.agent}",
        f"Tool: {req.tool_name}",
    ]
    # Best-effort summary of the tool input — keep under ~200 chars so
    # notifications stay short; the full payload is revealed by Details.
    cmd = req.tool_input.get("command") if isinstance(req.tool_input, dict) else None
    if cmd:
        lines.append(f"Command: {_truncate(cmd, 200)}")
    elif req.tool_input:
        lines.append(f"Input: {_truncate(str(req.tool_input), 200)}")
    if req.reason:
        lines.append(f"Reason: {req.reason}")
    return "\n".join(lines)


def _format_info(msg: InfoMessage) -> str:
    emoji = _SEVERITY_EMOJI.get(msg.severity, "ℹ️")
    header = f"{emoji} [{msg.project}] {msg.title}"
    if msg.body:
        return f"{header}\n{msg.body}"
    return header


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


class TelegramTransport:
    """Implements ``bridge.core.Transport`` via python-telegram-bot."""

    name = "telegram"

    def __init__(self, config: dict[str, Any]) -> None:
        if not _HAS_TELEGRAM:
            raise ImportError(
                "python-telegram-bot not installed. Install with: "
                "pip install python-telegram-bot>=21"
            )

        bot_token = config.get("bot_token")
        if not bot_token:
            raise ValueError(
                "TelegramTransport: bot_token is required (resolve via "
                "env / .otaman/secrets.env / keychain — see bridge/config.py)"
            )
        group_id = config.get("group_id")
        if group_id is None:
            raise ValueError(
                "TelegramTransport: group_id is required (Telegram supergroup ID)"
            )

        self.bot_token: str = str(bot_token)
        self.group_id: int = int(group_id)
        self.allowed_user_ids: set[int] = {
            int(x) for x in config.get("allowed_user_ids", [])
        }
        self.topic_map: dict[str, int] = {
            str(k): int(v) for k, v in (config.get("topic_map") or {}).items()
        }
        self.default_topic_id: int | None = (
            int(config["default_topic_id"])
            if config.get("default_topic_id") is not None else None
        )
        # Auto-create topics for unmapped projects unless explicitly disabled.
        self.auto_create_topics: bool = bool(
            config.get("auto_create_topics", True),
        )
        # Per-account topic cache; defaults to ~/.otaman/bridge-<account>-topics.json.
        cache_file = config.get("topic_cache_file")
        if cache_file:
            self.topic_cache_file: Path = Path(cache_file).expanduser()
        else:
            account = config.get("account_name") or "default"
            from otaman_bridge.daemon import endpoint_path as _ep  # noqa: PLC0415
            self.topic_cache_file = (
                _ep(account).parent / f"bridge-{account}-topics.json"
            )

        self._bot = Bot(token=self.bot_token)
        self._app: Any = None  # Application, lazily started in listen()
        self._app_lock = asyncio.Lock()
        self._inbound: asyncio.Queue[InboundReply] = asyncio.Queue()

        # Lock guards concurrent create_forum_topic calls for the same project.
        self._topic_lock = asyncio.Lock()
        # In-memory mirror of the cache file (loaded lazily).
        self._cached_topics: dict[str, int] | None = None
        # Map Telegram message_id (approval card) → request_id so that
        # MessageHandler can turn replies into InboundReply(action="comment",
        # request_id=…). Populated in send_approval; bounded by keeping
        # only the newest N entries (simple LRU via dict insertion order).
        self._reply_index: dict[int, str] = {}
        self._reply_index_max = 512

    # ----- outbound -------------------------------------------------------

    async def send_approval(self, req: ApprovalRequest) -> TransportHandle:
        text = _format_approval(req)
        markup = self._build_keyboard(req.request_id)
        topic_id = await self._resolve_topic(req.project)

        send_kwargs: dict[str, Any] = {
            "chat_id": self.group_id,
            "text": text,
            "reply_markup": markup,
        }
        if topic_id is not None:
            send_kwargs["message_thread_id"] = topic_id

        msg = await self._bot.send_message(**send_kwargs)
        # Record message_id → request_id so replies to this card are
        # routable. Trim FIFO so the map can't grow unbounded.
        self._reply_index[msg.message_id] = req.request_id
        if len(self._reply_index) > self._reply_index_max:
            # Drop oldest entries.
            extra = len(self._reply_index) - self._reply_index_max
            for k in list(self._reply_index.keys())[:extra]:
                self._reply_index.pop(k, None)
        return TransportHandle(
            transport=self.name,
            data={
                "chat_id": self.group_id,
                "message_id": msg.message_id,
                "topic_id": topic_id,
                "request_id": req.request_id,
            },
        )

    async def send_info(self, msg: InfoMessage) -> TransportHandle:
        text = _format_info(msg)
        topic_id = await self._resolve_topic(msg.project)

        send_kwargs: dict[str, Any] = {
            "chat_id": self.group_id,
            "text": text,
        }
        if topic_id is not None:
            send_kwargs["message_thread_id"] = topic_id

        tg_msg = await self._bot.send_message(**send_kwargs)
        return TransportHandle(
            transport=self.name,
            data={
                "chat_id": self.group_id,
                "message_id": tg_msg.message_id,
                "topic_id": topic_id,
            },
        )

    async def update(self, handle: TransportHandle, status: str) -> None:
        data = handle.data
        if not data.get("chat_id") or not data.get("message_id"):
            _log.warning("telegram.update: malformed handle: %r", handle)
            return
        await self._bot.edit_message_reply_markup(
            chat_id=data["chat_id"],
            message_id=data["message_id"],
            reply_markup=None,  # strip buttons — decision is final
        )
        now = _dt.datetime.now().strftime("%H:%M")
        suffix = f"\n\n{status} · {now}"
        try:
            await self._bot.edit_message_text(
                chat_id=data["chat_id"],
                message_id=data["message_id"],
                text=suffix.strip(),
            )
        except Exception as e:  # noqa: BLE001
            # Telegram refuses "message not modified" errors; any edit error
            # here is non-fatal — the decision is already in the audit log.
            _log.debug("telegram.update edit_text failed (non-fatal): %s", e)

    # ----- inbound --------------------------------------------------------

    async def listen(self) -> AsyncIterator[InboundReply]:
        """Start polling lazily, yield InboundReply objects as they arrive."""
        await self._ensure_running()
        while True:
            reply = await self._inbound.get()
            yield reply

    async def allowlist_check(self, user_id: str) -> bool:
        """Check a ``telegram:<uid>`` user-id string against the allowlist."""
        if not user_id.startswith("telegram:"):
            return False
        try:
            uid = int(user_id[len("telegram:"):])
        except ValueError:
            return False
        return uid in self.allowed_user_ids

    async def close(self) -> None:
        """Stop the polling Application if it was started."""
        if self._app is not None:
            try:
                if self._app.updater.running:  # type: ignore[attr-defined]
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:  # noqa: BLE001
                _log.debug("telegram.close: %s", e)
            self._app = None

    # ----- internals ------------------------------------------------------

    def _build_keyboard(self, request_id: str) -> Any:
        """Inline keyboard: Approve / Reject / Details / Snooze."""
        buttons = [
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=encode_callback("approve", request_id),
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=encode_callback("reject", request_id),
                ),
            ],
            [
                InlineKeyboardButton(
                    "📄 Details",
                    callback_data=encode_callback("details", request_id),
                ),
                InlineKeyboardButton(
                    "⏱ Snooze 15m",
                    callback_data=encode_callback("snooze", request_id),
                ),
            ],
        ]
        return InlineKeyboardMarkup(buttons)

    async def _resolve_topic(self, project: str) -> int | None:
        """Resolve a project name to a forum topic thread id.

        Order:
          1. Explicit ``topic_map[project]`` (from launch-settings.yaml).
          2. Per-account cache (``.otaman/bridge-<account>-topics.json``).
          3. Auto-create via ``Bot.create_forum_topic`` if
             ``auto_create_topics`` is enabled; write result to cache.
          4. ``default_topic_id`` (catch-all fallback).
          5. None → post to the group's general/threadless context.

        Failures to create a topic are **not** cached — they log a
        warning and fall back to the default topic for this message.
        The NEXT message for the same project will retry. This makes
        the system self-heal when the user fixes permissions
        ("add bot as admin with Manage topics") without having to
        manually clear a cache file.
        """
        if project in self.topic_map:
            return self.topic_map[project]

        cache = self._load_topic_cache()
        if project in cache:
            return cache[project]

        if not self.auto_create_topics:
            return self.default_topic_id

        async with self._topic_lock:
            # Re-check after acquiring lock (another task may have raced
            # and already created the topic).
            cache = self._load_topic_cache()
            if project in cache:
                return cache[project]

            try:
                topic = await self._bot.create_forum_topic(
                    chat_id=self.group_id,
                    name=project,
                )
                thread_id = int(getattr(topic, "message_thread_id", 0))
                if thread_id > 0:
                    cache[project] = thread_id
                    self._write_topic_cache(cache)
                    _log.info(
                        "telegram: created forum topic for %r → thread_id=%d",
                        project, thread_id,
                    )
                    return thread_id
                _log.warning(
                    "telegram: create_forum_topic returned no thread id for %r "
                    "— falling back to default topic; will retry next message",
                    project,
                )
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "telegram: create_forum_topic failed for %r: %s — "
                    "falling back to default topic; will retry next message. "
                    "Fix by promoting the bot to admin with 'Manage topics' "
                    "permission in the group.",
                    project, e,
                )

            # No caching of failures — next message retries.
            return self.default_topic_id

    def _load_topic_cache(self) -> dict[str, int]:
        """Load the per-account topic cache, creating an empty one if absent."""
        if self._cached_topics is not None:
            return self._cached_topics
        if self.topic_cache_file.is_file():
            try:
                data = json.loads(
                    self.topic_cache_file.read_text(encoding="utf-8"),
                )
                self._cached_topics = {
                    str(k): int(v) for k, v in (data or {}).items()
                }
                return self._cached_topics
            except (OSError, ValueError):
                _log.warning(
                    "telegram: topic cache at %s is corrupt; starting fresh",
                    self.topic_cache_file,
                )
        self._cached_topics = {}
        return self._cached_topics

    def _write_topic_cache(self, cache: dict[str, int]) -> None:
        """Persist the topic cache with mode 0600 on POSIX."""
        self._cached_topics = dict(cache)
        self.topic_cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.topic_cache_file.write_text(
            json.dumps(cache, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if os.name == "posix":
            try:
                self.topic_cache_file.chmod(0o600)
            except OSError:
                pass

    async def _ensure_running(self) -> None:
        """Start the Application + polling exactly once."""
        async with self._app_lock:
            if self._app is not None:
                return
            app = Application.builder().token(self.bot_token).build()
            app.add_handler(CallbackQueryHandler(self._on_callback_query))
            # Reply-to-card handler: turns free-text replies into comment/
            # acknowledge InboundReply objects. filters.REPLY narrows to
            # messages that carry reply_to_message so we don't buzz on
            # normal chatter.
            app.add_handler(MessageHandler(
                filters.REPLY & filters.TEXT & ~filters.COMMAND,
                self._on_reply_message,
            ))
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            self._app = app

    async def _on_callback_query(
        self, update: Any, context: Any,  # noqa: ARG002
    ) -> None:
        """CallbackQueryHandler: button tap → InboundReply on the queue."""
        query = getattr(update, "callback_query", None)
        if query is None:
            return

        user = query.from_user
        uid = int(user.id) if user else 0
        if uid not in self.allowed_user_ids:
            await query.answer(
                text="Not authorized to approve this request.",
                show_alert=True,
            )
            _log.warning("telegram: rejected tap from uid=%s", uid)
            return

        action, request_id = decode_callback(query.data or "")
        if not action or not request_id:
            await query.answer(text="Malformed callback data.", show_alert=True)
            return

        await query.answer()  # lightweight ack; no popup
        reply = InboundReply(
            request_id=request_id,
            action=action,  # type: ignore[arg-type]
            responder=f"telegram:{uid}",
            comment="",
        )
        await self._inbound.put(reply)

    async def _on_reply_message(
        self, update: Any, context: Any,  # noqa: ARG002
    ) -> None:
        """MessageHandler: reply-to-card → InboundReply(action="comment").

        We look up the replied-to ``message_id`` in ``self._reply_index``;
        if found, we turn the text into a ``comment`` InboundReply so
        the daemon can persist it as a human reply bus message. Replies
        to non-card messages (or expired entries) are ignored.
        """
        msg = getattr(update, "message", None)
        if msg is None:
            return
        replied = getattr(msg, "reply_to_message", None)
        if replied is None:
            return
        request_id = self._reply_index.get(replied.message_id)
        if not request_id:
            # Reply to something that's not an approval card — ignore.
            return

        user = msg.from_user
        uid = int(user.id) if user else 0
        if uid not in self.allowed_user_ids:
            _log.warning(
                "telegram: rejected reply from uid=%s to request %s",
                uid, request_id,
            )
            return

        text = (msg.text or "").strip()
        if not text:
            return

        reply = InboundReply(
            request_id=request_id,
            action="comment",
            responder=f"telegram:{uid}",
            comment=text,
        )
        await self._inbound.put(reply)


# Register on import so ``get_transport("telegram")`` works.
register_transport("telegram", TelegramTransport)
