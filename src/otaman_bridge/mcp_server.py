"""Minimal MCP-over-JSON-RPC dispatcher for the bridge.

Implements just enough of the MCP protocol (initialize, tools/list,
tools/call) to expose otaman tools to Claude Code instances. v0 scope
per ``otaman-meta/strategy/team-mode-v0-cross-user-visibility.md``.

Why not the official ``mcp`` SDK: the SDK is async-first and owns its
own transport. Integrating it into our existing sync BaseHTTPServer
route would mean bridging async<->sync. For v0 with a handful of tools
and request-response interactions only, ~120 lines of JSON-RPC here is
simpler than the SDK glue. When we need streaming (notifications,
resources, prompts beyond the simple tool surface), we revisit.

Protocol references:
- MCP spec: https://spec.modelcontextprotocol.io/
- JSON-RPC 2.0: https://www.jsonrpc.org/specification

This module is transport-agnostic: the daemon's POST /mcp route
unpacks the request body into a dict, calls ``handle_request``, and
serializes the result back to JSON. No HTTP-level details leak in here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

_log = logging.getLogger("otaman.bridge.mcp")

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP protocol version this server speaks. Bumped when we adopt new
# protocol features; clients negotiate via the ``initialize`` request.
MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class CallContext:
    """Context passed to tool handlers. Currently only the calling user id.

    Future fields: trace id, session reference, request timestamp.
    """

    user_id: str
    user_email: str | None = None
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tool:
    """A tool the MCP server exposes."""

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, CallContext], dict]


@dataclass
class MCPServer:
    """Tool registry + JSON-RPC dispatch."""

    server_name: str = "otaman-bridge"
    server_version: str = "0.1.0"
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self.tools[tool.name] = tool

    def handle_request(self, body: dict, *, context: CallContext) -> dict:
        """Dispatch a JSON-RPC request envelope and return the response envelope.

        Returns a dict that the caller serializes to JSON. Never raises
        on malformed input -- always returns a well-formed JSON-RPC
        response (success or error).
        """
        # Notifications (no id) get no response. Spec says we MAY ignore.
        rpc_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}

        if not isinstance(method, str) or not method:
            return _error_response(rpc_id, INVALID_REQUEST, "missing method")

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "tools/list":
                result = self._handle_tools_list(params)
            elif method == "tools/call":
                result = self._handle_tools_call(params, context)
            elif method == "ping":
                # MCP keepalive; returns empty object.
                result = {}
            else:
                return _error_response(
                    rpc_id, METHOD_NOT_FOUND,
                    f"method {method!r} not supported by this MCP server",
                )
        except _MCPDispatchError as exc:
            return _error_response(rpc_id, exc.code, exc.message, data=exc.data)
        except Exception as exc:  # noqa: BLE001 -- envelope safety net
            _log.exception("mcp dispatcher: unhandled error in %s", method)
            return _error_response(rpc_id, INTERNAL_ERROR, str(exc))

        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    # ---- method handlers ----------------------------------------------

    def _handle_initialize(self, params: dict) -> dict:
        """Client tells us its protocol version; we tell ours + capabilities."""
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": self.server_name,
                "version": self.server_version,
            },
        }

    def _handle_tools_list(self, params: dict) -> dict:
        """Return the registered tools' schemas. params currently ignored."""
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                }
                for t in self.tools.values()
            ],
        }

    def _handle_tools_call(self, params: dict, context: CallContext) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _MCPDispatchError(INVALID_PARAMS, "tools/call: missing 'name'")
        tool = self.tools.get(name)
        if tool is None:
            raise _MCPDispatchError(METHOD_NOT_FOUND, f"tool {name!r} not found")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _MCPDispatchError(INVALID_PARAMS, "tools/call: 'arguments' must be object")
        try:
            tool_result = tool.handler(args, context)
        except _MCPDispatchError:
            raise
        except Exception as exc:  # noqa: BLE001 -- tool errors are returned as MCP results
            _log.exception("tool %s raised; converting to MCP error result", name)
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"tool error: {exc}"}],
            }
        # Tool handlers return MCP "result" payload directly (the dict the
        # client will see). Wrapping in MCP CallToolResult shape is the
        # handler's job for tools that need it; minimal scaffold here.
        return tool_result


# ---- internal --------------------------------------------------------


class _MCPDispatchError(Exception):
    """Raised inside the dispatcher to signal a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data=None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _error_response(rpc_id, code: int, message: str, *, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "CallContext",
    "Tool",
    "MCPServer",
]
