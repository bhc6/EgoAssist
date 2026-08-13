"""MCP (Model Context Protocol) tool provider for EgoAssist.

EgoAssist acts as an MCP *client*.  External MCP servers expose tools
(calendar, file system, web search, etc.) over stdio or HTTP/SSE.
This module provides an asynchronous client for those servers.

Supported transports
--------------------
* ``StdioTransport``   — launches a subprocess; communicates over stdin/stdout
                         with JSON-RPC 2.0 framing (one JSON object per line).
* ``HttpSSETransport`` — HTTP POST for requests, SSE for streaming; compatible
                         with the Omi /v1/mcp endpoint and the official MCP SDK.

Usage
-----
from egoassist.realtime.mcp_provider import MCPToolProvider, StdioTransport
provider = MCPToolProvider(StdioTransport(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]))
await provider.start()
tools = await provider.list_tools()
result = await provider.call_tool("read_file", {"path": "/tmp/notes.txt"})
await provider.stop()
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class StdioTransport:
    """Launch a local MCP server subprocess; communicate over stdin/stdout."""

    def __init__(self, command: list[str]) -> None:
        self._command = list(command)
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._proc or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("StdioTransport not started")
        line = json.dumps(request, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()
        raw = await self._proc.stdout.readline()
        return json.loads(raw.decode("utf-8"))

    async def close(self) -> None:
        if self._proc:
            try:
                self._proc.stdin.close()
                await self._proc.wait()
            except Exception:
                pass
            self._proc = None


class HttpSSETransport:
    """HTTP-based MCP transport; POST requests, SSE or JSON response.

    Compatible with the Omi /v1/mcp endpoint and the official MCP Python SDK.
    Requires ``aiohttp`` (already in core dependencies).
    """

    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json", **(headers or {})}
        self._session: Any | None = None  # aiohttp.ClientSession

    async def start(self) -> None:
        import aiohttp  # noqa: PLC0415
        self._session = aiohttp.ClientSession(headers=self._headers)

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._session:
            raise RuntimeError("HttpSSETransport not started")
        async with self._session.post(self._base_url, json=request) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None


# ---------------------------------------------------------------------------
# MCPToolProvider
# ---------------------------------------------------------------------------

class MCPToolProvider:
    """Bridge between an MCP server and SkillToolExecutor.

    Implements JSON-RPC 2.0 over the given transport.  The ``initialize``
    handshake is sent on ``start()``.  Tool listings are cached until the
    next ``refresh_tools()`` call.
    """

    _JSONRPC = "2.0"

    def __init__(self, transport: StdioTransport | HttpSSETransport) -> None:
        self._transport = transport
        self._seq = 0
        self._tools: list[dict[str, Any]] = []

    async def start(self) -> None:
        await self._transport.start()
        await self._initialize()
        await self.refresh_tools()

    async def stop(self) -> None:
        await self._transport.close()

    # ---- JSON-RPC helpers ----

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request = {
            "jsonrpc": self._JSONRPC,
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        response = await self._transport.send(request)
        if "error" in response:
            raise RuntimeError(f"MCP error ({method}): {response['error']}")
        return response.get("result")

    async def _initialize(self) -> None:
        await self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "egoassist", "version": "0.1.0"},
            "capabilities": {},
        })

    # ---- Tool management ----

    async def refresh_tools(self) -> list[dict[str, Any]]:
        result = await self._call("tools/list")
        raw_tools = result.get("tools", []) if isinstance(result, dict) else []
        self._tools = [self._normalize_tool(t) for t in raw_tools]
        return list(self._tools)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return cached tool declarations (SkillToolExecutor-compatible)."""
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._call("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict):
            content = result.get("content", [])
            if isinstance(content, list) and content:
                text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
                return {"status": "ok", "output": "\n".join(text_parts), "raw": result}
        return {"status": "ok", "output": str(result), "raw": result}

    @staticmethod
    def _normalize_tool(raw: dict[str, Any]) -> dict[str, Any]:
        """Convert MCP tool schema to SkillToolExecutor declaration format."""
        input_schema = raw.get("inputSchema") or {}
        return {
            "name": f"mcp.{raw['name']}",
            "description": raw.get("description", ""),
            "handler": f"mcp.{raw['name']}",
            "requires_confirmation": False,
            "arguments": input_schema,
            "_mcp_name": raw["name"],
        }


# ---------------------------------------------------------------------------
# Config helper: parse EGOASSIST_MCP_SERVERS env var
# ---------------------------------------------------------------------------

def providers_from_env(env_value: str) -> list[MCPToolProvider]:
    """Parse ``EGOASSIST_MCP_SERVERS`` and return MCPToolProvider instances.

    Format: ``name:command_or_url[,name2:command2]``
    Examples::

        filesystem:npx -y @modelcontextprotocol/server-filesystem /tmp
        search:http://localhost:8000/mcp
    """
    providers = []
    for entry in env_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            LOGGER.warning("MCP server entry missing name prefix, skipping: %s", entry)
            continue
        _, rest = entry.split(":", 1)
        rest = rest.strip()
        if rest.startswith("http://") or rest.startswith("https://"):
            transport: StdioTransport | HttpSSETransport = HttpSSETransport(rest)
        else:
            transport = StdioTransport(rest.split())
        providers.append(MCPToolProvider(transport))
    return providers
