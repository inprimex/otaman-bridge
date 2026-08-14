"""Tests for the bridge's minimal MCP JSON-RPC dispatcher.

Pure protocol tests -- no HTTP, no real tools. Real tool integration
(`list_team_sessions`) lands in chunk 4 with its own tests.
"""

from __future__ import annotations

import pytest

from otaman_bridge.mcp_server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    MCP_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    CallContext,
    MCPServer,
    Tool,
)


@pytest.fixture
def ctx():
    return CallContext(user_id="user-42", user_email="u@e", roles=("otaman:developer",))


@pytest.fixture
def server():
    return MCPServer()


def _echo_tool() -> Tool:
    """Tool that echoes its input as content. Used as test fixture."""
    return Tool(
        name="echo",
        description="Echo arguments back as text content.",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        handler=lambda args, ctx: {
            "content": [{"type": "text", "text": args.get("msg", "")}],
        },
    )


# ---- register ---------------------------------------------------------


class TestRegister:
    def test_registers_tool(self, server):
        server.register(_echo_tool())
        assert "echo" in server.tools

    def test_duplicate_register_raises(self, server):
        server.register(_echo_tool())
        with pytest.raises(ValueError, match="already registered"):
            server.register(_echo_tool())


# ---- initialize -------------------------------------------------------


class TestInitialize:
    def test_returns_protocol_version_and_capabilities(self, server, ctx):
        resp = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            context=ctx,
        )
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        result = resp["result"]
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "otaman-bridge"


# ---- tools/list -------------------------------------------------------


class TestToolsList:
    def test_empty_when_no_tools(self, server, ctx):
        resp = server.handle_request(
            {"jsonrpc": "2.0", "id": "x", "method": "tools/list"},
            context=ctx,
        )
        assert resp["result"]["tools"] == []

    def test_returns_registered_tool_schemas(self, server, ctx):
        server.register(_echo_tool())
        resp = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            context=ctx,
        )
        tools = resp["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "echo"
        assert tools[0]["description"] == "Echo arguments back as text content."
        assert tools[0]["inputSchema"]["type"] == "object"


# ---- tools/call -------------------------------------------------------


class TestToolsCall:
    def test_calls_handler_with_args(self, server, ctx):
        server.register(_echo_tool())
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"msg": "hello"}},
            },
            context=ctx,
        )
        result = resp["result"]
        assert result["content"] == [{"type": "text", "text": "hello"}]

    def test_unknown_tool_returns_method_not_found(self, server, ctx):
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            },
            context=ctx,
        )
        assert "error" in resp
        assert resp["error"]["code"] == METHOD_NOT_FOUND

    def test_missing_name_returns_invalid_params(self, server, ctx):
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"arguments": {}},
            },
            context=ctx,
        )
        assert resp["error"]["code"] == INVALID_PARAMS

    def test_non_dict_arguments_returns_invalid_params(self, server, ctx):
        server.register(_echo_tool())
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": "not-a-dict"},
            },
            context=ctx,
        )
        assert resp["error"]["code"] == INVALID_PARAMS

    def test_handler_exception_returns_mcp_error_result(self, server, ctx):
        """Tool errors become CallToolResult with isError=true, NOT JSON-RPC errors."""

        def boom(args, ctx):
            raise RuntimeError("tool blew up")

        server.register(
            Tool(
                name="boom",
                description="",
                input_schema={},
                handler=boom,
            )
        )
        resp = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "boom", "arguments": {}},
            },
            context=ctx,
        )
        # JSON-RPC envelope is success (not "error" field), result has isError=true
        assert "result" in resp
        assert resp["result"]["isError"] is True
        assert "tool blew up" in resp["result"]["content"][0]["text"]

    def test_handler_receives_context(self, server, ctx):
        captured = []

        def capture(args, c):
            captured.append((c.user_id, c.user_email, c.roles))
            return {"content": []}

        server.register(
            Tool(
                name="capture",
                description="",
                input_schema={},
                handler=capture,
            )
        )
        server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "capture", "arguments": {}},
            },
            context=ctx,
        )
        assert captured == [("user-42", "u@e", ("otaman:developer",))]


# ---- error paths ------------------------------------------------------


class TestErrorPaths:
    def test_missing_method_returns_invalid_request(self, server, ctx):
        resp = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "params": {}},
            context=ctx,
        )
        assert resp["error"]["code"] == INVALID_REQUEST

    def test_unknown_method_returns_method_not_found(self, server, ctx):
        resp = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "not/a/thing"},
            context=ctx,
        )
        assert resp["error"]["code"] == METHOD_NOT_FOUND

    def test_ping_returns_empty_result(self, server, ctx):
        resp = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            context=ctx,
        )
        assert resp["result"] == {}

    def test_response_carries_request_id(self, server, ctx):
        resp = server.handle_request(
            {"jsonrpc": "2.0", "id": "abc-xyz", "method": "ping"},
            context=ctx,
        )
        assert resp["id"] == "abc-xyz"
