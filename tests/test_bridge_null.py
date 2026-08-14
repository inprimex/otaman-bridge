"""Tests for bridge/transports/null.py — in-memory transport for tests."""

from __future__ import annotations

import asyncio

import pytest

from otaman_bridge.core import (
    ApprovalRequest,
    InboundReply,
    InfoMessage,
    get_transport,
)
from otaman_bridge.transports.null import NullTransport


def _make_request(**overrides) -> ApprovalRequest:
    defaults = dict(
        account="personal",
        project="demo",
        repo="auth",
        agent="backend",
        tool_name="Bash",
        tool_input={"command": "ls"},
    )
    defaults.update(overrides)
    return ApprovalRequest(**defaults)


class TestSendRecording:
    def test_send_approval_records(self):
        async def run():
            t = NullTransport()
            req = _make_request()
            handle = await t.send_approval(req)
            assert t.sent_approvals == [req]
            assert handle.transport == "null"
            assert handle.data["request_id"] == req.request_id

        asyncio.run(run())

    def test_send_info_records(self):
        async def run():
            t = NullTransport()
            msg = InfoMessage(
                account="p",
                project="x",
                severity="info",
                title="hi",
            )
            await t.send_info(msg)
            assert t.sent_infos == [msg]

        asyncio.run(run())

    def test_update_records(self):
        async def run():
            t = NullTransport()
            req = _make_request()
            handle = await t.send_approval(req)
            await t.update(handle, "approved")
            assert len(t.updates) == 1
            assert t.updates[0][1] == "approved"

        asyncio.run(run())


class TestInboundQueue:
    def test_listen_yields_pushed_replies(self):
        async def run():
            t = NullTransport()
            req = _make_request()
            reply = InboundReply(
                request_id=req.request_id,
                action="approve",
                responder="test:user",
            )
            t.push_reply(reply)
            it = t.listen()
            got = await asyncio.wait_for(it.__anext__(), timeout=1.0)
            assert got == reply

        asyncio.run(run())

    def test_listen_blocks_when_empty(self):
        async def run():
            t = NullTransport()
            it = t.listen()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(it.__anext__(), timeout=0.1)

        asyncio.run(run())


class TestAllowlist:
    def test_default_rejects_all(self):
        async def run():
            t = NullTransport()
            assert not await t.allowlist_check("anyone")

        asyncio.run(run())

    def test_wildcard_allows_all(self):
        async def run():
            t = NullTransport(allowlist={"*"})
            assert await t.allowlist_check("anyone")

        asyncio.run(run())

    def test_specific_user_allowed(self):
        async def run():
            t = NullTransport(allowlist={"telegram:123"})
            assert await t.allowlist_check("telegram:123")
            assert not await t.allowlist_check("telegram:456")

        asyncio.run(run())


class TestRegistration:
    def test_null_is_registered(self):
        """Importing bridge.transports.null registers the transport."""
        # Force a re-import by accessing the module
        import importlib

        import otaman_bridge.transports.null as null_mod

        importlib.reload(null_mod)
        assert get_transport("null") is null_mod.NullTransport


class TestReset:
    def test_reset_clears_state(self):
        async def run():
            t = NullTransport()
            await t.send_approval(_make_request())
            await t.send_info(
                InfoMessage(
                    account="p",
                    project="x",
                    severity="info",
                    title="h",
                )
            )
            t.push_reply(
                InboundReply(
                    request_id="x",
                    action="approve",
                    responder="u",
                )
            )
            assert t.sent_approvals and t.sent_infos
            assert not t.inbound_queue.empty()

            t.reset()
            assert t.sent_approvals == []
            assert t.sent_infos == []
            assert t.inbound_queue.empty()

        asyncio.run(run())
