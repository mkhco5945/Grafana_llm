from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ALLOWED_TOOL_NAMES = frozenset(
    {
        "list_datasources",
        "get_datasource",
        "check_datasources_health",
        "list_prometheus_metric_names",
        "list_prometheus_label_names",
        "list_prometheus_label_values",
        "query_prometheus",
        "query_prometheus_histogram",
        "search_dashboards",
        "get_dashboard_by_uid",
        "get_dashboard_summary",
        "get_dashboard_panel_queries",
        "update_dashboard",
    }
)

CORE_DISCOVERY_TOOL_NAMES = frozenset(
    {
        "list_datasources",
        "get_datasource",
        "check_datasources_health",
        "list_prometheus_metric_names",
        "query_prometheus",
        "query_prometheus_histogram",
        "search_dashboards",
        "get_dashboard_by_uid",
        "get_dashboard_summary",
        "get_dashboard_panel_queries",
    }
)

LABEL_DISCOVERY_TOOL_NAMES = frozenset(
    {"list_prometheus_label_names", "list_prometheus_label_values"}
)

# Kept as a compatibility name for callers that want the complete read-only
# discovery phase. Ordinary requests use CORE_DISCOVERY_TOOL_NAMES and opt in
# to label tools only when label discovery is relevant.
DISCOVERY_TOOL_NAMES = CORE_DISCOVERY_TOOL_NAMES | LABEL_DISCOVERY_TOOL_NAMES


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict(by_alias=True, exclude_none=True)
    return value


def mcp_tool_to_ollama_tool(tool: Any) -> dict[str, Any]:
    """Convert the live MCP Tool metadata into Ollama's function schema."""
    dumped = _dump_model(tool)
    if isinstance(dumped, dict):
        name = dumped.get("name")
        description = dumped.get("description") or ""
        schema = dumped.get("inputSchema") or dumped.get("input_schema") or {}
    else:
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", None) or ""
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
        schema = _dump_model(schema) if schema is not None else {}
    if not name or not isinstance(schema, dict):
        raise ValueError(f"Invalid MCP tool metadata: {tool!r}")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def select_allowed_tools(tools: list[Any], allowlist: set[str] | frozenset[str] = ALLOWED_TOOL_NAMES) -> list[Any]:
    selected = [tool for tool in tools if getattr(tool, "name", None) in allowlist]
    return sorted(selected, key=lambda tool: tool.name)


def serialize_mcp_result(result: Any, max_chars: int = 60000) -> str:
    structured = getattr(result, "structured_content", None)
    content: list[Any] = []
    for item in getattr(result, "content", []) or []:
        item_dump = _dump_model(item)
        if isinstance(item_dump, dict) and item_dump.get("type") == "text":
            text = item_dump.get("text", "")
            # mcp-grafana commonly returns a JSON array/object encoded in a
            # text content block. Preserve plain text and errors verbatim, but
            # expose successful structured values as JSON so small models do
            # not have to parse a second encoding layer.
            if isinstance(text, str):
                try:
                    content.append(json.loads(text))
                except json.JSONDecodeError:
                    content.append(text)
            else:
                content.append(text)
        else:
            content.append(item_dump)
    payload = {
        "is_error": bool(getattr(result, "is_error", False)),
        "structured_content": _dump_model(structured),
        "content": content,
    }
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    if len(encoded) > max_chars:
        encoded = encoded[:max_chars] + "\n[tool result truncated]"
    return encoded


class GrafanaMCPClient:
    def __init__(self, url: str, bearer_token: str, timeout_seconds: float) -> None:
        self.url = url
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "GrafanaMCPClient":
        timeout = httpx2.Timeout(
            self.timeout_seconds,
            connect=30.0,
            read=self.timeout_seconds,
            write=self.timeout_seconds,
            pool=self.timeout_seconds,
        )
        http_client = await self._stack.enter_async_context(
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {self.bearer_token}"},
                timeout=timeout,
                trust_env=False,
            )
        )
        read_stream, write_stream = await self._stack.enter_async_context(
            streamable_http_client(self.url, http_client=http_client)
        )
        self.session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self.session.initialize()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._stack.aclose()

    async def list_tools(self) -> list[Any]:
        if self.session is None:
            raise RuntimeError("MCP client is not connected")
        result = await self.session.list_tools()
        return list(result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in ALLOWED_TOOL_NAMES:
            raise PermissionError(f"MCP tool is not allowlisted: {name}")
        if self.session is None:
            raise RuntimeError("MCP client is not connected")
        return await self.session.call_tool(name, arguments)
