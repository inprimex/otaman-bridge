"""Tests for bridge/core.py — transport-agnostic types + registry."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


from otaman_bridge import core
from otaman_bridge.core import (
    ApprovalRequest,
    ApprovalResponse,
    InboundReply,
    InfoMessage,
    Transport,
    TransportHandle,
    get_transport,
    list_transports,
    register_transport,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the transport registry between tests.

    Built-in registrations (``null``, ``telegram``) are restored after
    teardown so later test files don't see an empty registry.
    ``register_transport`` is idempotent per name.
    """
    core._reset_registry_for_tests()
    yield
    core._reset_registry_for_tests()
    from otaman_bridge.transports.null import NullTransport
    core.register_transport("null", NullTransport)
    try:
        from otaman_bridge.transports.telegram import TelegramTransport
        core.register_transport("telegram", TelegramTransport)
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Dataclass round-trips


class TestApprovalRequest:
    def test_defaults(self):
        req = ApprovalRequest(
            account="personal",
            project="demo",
            repo="auth",
            agent="backend",
            tool_name="Bash",
            tool_input={"command": "npm install"},
        )
        assert req.priority == "normal"
        assert req.timeout_seconds == 540
        assert req.request_id
        # request_id contains a timestamp + random suffix
        assert "-" in req.request_id

    def test_request_id_unique(self):
        r1 = ApprovalRequest(
            account="p", project="x", repo="r", agent="a",
            tool_name="Bash", tool_input={},
        )
        r2 = ApprovalRequest(
            account="p", project="x", repo="r", agent="a",
            tool_name="Bash", tool_input={},
        )
        assert r1.request_id != r2.request_id

    def test_to_from_dict_roundtrip(self):
        req = ApprovalRequest(
            account="p", project="x", repo="r", agent="a",
            tool_name="Bash", tool_input={"command": "ls"},
            reason="list files", priority="high",
        )
        d = req.to_dict()
        req2 = ApprovalRequest.from_dict(d)
        assert req2 == req


class TestApprovalResponse:
    def test_minimal(self):
        resp = ApprovalResponse(decision="allow", request_id="abc")
        d = resp.to_dict()
        # Optional field absent when None
        assert "updated_input" not in d
        assert d["decision"] == "allow"

    def test_with_updated_input(self):
        resp = ApprovalResponse(
            decision="allow", request_id="abc",
            updated_input={"command": "ls -la"},
        )
        d = resp.to_dict()
        assert d["updated_input"] == {"command": "ls -la"}

    def test_roundtrip(self):
        resp = ApprovalResponse(
            decision="deny", request_id="xyz",
            responder="telegram:@roman", message="too risky",
        )
        assert ApprovalResponse.from_dict(resp.to_dict()) == resp

    def test_decision_values(self):
        # Literal type is advisory — runtime accepts any string,
        # but we test the documented vocabulary round-trips cleanly.
        for d in ("allow", "deny", "ask", "timeout"):
            resp = ApprovalResponse(decision=d, request_id="x")
            assert ApprovalResponse.from_dict(resp.to_dict()).decision == d


class TestInfoMessage:
    def test_roundtrip(self):
        msg = InfoMessage(
            account="personal", project="demo", severity="info",
            title="task complete", body="auth-service finished task 3.1",
        )
        assert InfoMessage.from_dict(msg.to_dict()) == msg


class TestInboundReply:
    def test_roundtrip(self):
        r = InboundReply(
            request_id="abc", action="approve",
            responder="telegram:@roman", comment="looks fine",
        )
        assert InboundReply.from_dict(r.to_dict()) == r


class TestTransportHandle:
    def test_opaque_data_survives_roundtrip(self):
        h = TransportHandle(
            transport="telegram",
            data={"chat_id": -1001234567890, "message_id": 42},
        )
        h2 = TransportHandle.from_dict(h.to_dict())
        assert h2 == h
        assert h2.data["message_id"] == 42


# ---------------------------------------------------------------------------
# Registry


class DummyTransport:
    """Minimal stub that satisfies the Protocol at runtime."""

    name = "dummy"

    async def send_approval(self, req):  # pragma: no cover (not invoked)
        raise NotImplementedError

    async def send_info(self, msg):  # pragma: no cover
        raise NotImplementedError

    async def update(self, handle, status):  # pragma: no cover
        raise NotImplementedError

    async def listen(self):  # pragma: no cover
        if False:
            yield

    async def allowlist_check(self, user_id):  # pragma: no cover
        return True


class TestRegistry:
    def test_register_and_lookup(self):
        register_transport("dummy", DummyTransport)
        assert get_transport("dummy") is DummyTransport

    def test_lookup_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown transport"):
            get_transport("ghost")

    def test_list_transports_sorted(self):
        register_transport("zzz", DummyTransport)
        register_transport("aaa", DummyTransport)
        assert list_transports() == ["aaa", "zzz"]

    def test_register_rejects_bad_names(self):
        with pytest.raises(ValueError):
            register_transport("", DummyTransport)
        with pytest.raises(ValueError):
            register_transport("bad name!", DummyTransport)

    def test_register_overwrites(self):
        class Other:
            name = "dummy"
            async def send_approval(self, req): pass  # noqa: E704
            async def send_info(self, msg): pass  # noqa: E704
            async def update(self, h, s): pass  # noqa: E704
            async def listen(self):
                if False: yield  # noqa: E701,E702,E703
            async def allowlist_check(self, u): return True  # noqa: E704

        register_transport("dummy", DummyTransport)
        register_transport("dummy", Other)
        assert get_transport("dummy") is Other


class TestProtocolConformance:
    """Runtime-checkable Protocol: isinstance works against instances."""

    def test_null_satisfies_protocol(self):
        from otaman_bridge.transports.null import NullTransport
        assert isinstance(NullTransport(), Transport)

    def test_plain_object_does_not_satisfy(self):
        class NotATransport:
            name = "x"

        assert not isinstance(NotATransport(), Transport)
