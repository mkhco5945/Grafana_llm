from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from .config import Settings
from .demo_client import (
    LOCAL_DEMO_TOOL_NAMES,
    DemoScenarioClient,
    local_demo_tools,
)
from .mcp_client import (
    ALLOWED_TOOL_NAMES,
    DISCOVERY_TOOL_NAMES,
    GrafanaMCPClient,
    mcp_tool_to_ollama_tool,
    serialize_mcp_result,
)


SYSTEM_PROMPT = """You are a local Grafana operations agent. You have authenticated MCP tools for a real local Grafana instance.

Rules:
- Use MCP tools for facts; never invent datasource UIDs, metric names, dashboard UIDs, or API fields.
- Discover the Prometheus datasource and actual metrics before designing queries.
- Validate important PromQL queries with query_prometheus before creating or updating a dashboard.
- Create or update dashboards only with update_dashboard. Prefer a useful, simple dashboard with clear units and legends.
- For an existing dashboard, use update_dashboard patch mode with its uid and operations; do not send a full replacement dashboard object that can carry a stale version.
- For rates derived from counters, use rate(); for histogram percentiles, use the _bucket series with rate() and aggregate by le. Reuse the exact validated expressions in panel targets.
- After a dashboard write, retrieve it and confirm the saved expressions, units, and legends match what you validated; then validate its important panel queries return data.
- If a tool errors, inspect the error and retry with corrected arguments.
- For simple PromQL validation, prefer an instant query with endTime "now". Never repeat an unchanged failed tool call.
- Never claim success without verification. Keep tool calls focused and avoid unnecessary tools.
- Prometheus data is observed time-series data; do not try to edit its database. If the user asks to change the simulated workload, use only the dedicated local demo scenario tools (never shell, curl, arbitrary HTTP, or files).
- After changing a demo scenario, allow Prometheus at least one scrape interval to observe new samples, then query Prometheus and verify the requested change before describing it as fact.
- Demo scenario tools control the exporter source workload; dashboard discovery, validation, creation, and updates remain Grafana MCP operations.

For update_dashboard, use the live schema. It supports a full dashboard JSON for creation or uid plus operations for targeted updates. Use the discovered Prometheus datasource UID in panel targets.
"""


class OllamaError(RuntimeError):
    pass


class OllamaUnavailable(OllamaError):
    pass


class OllamaAgent:
    def __init__(
        self,
        settings: Settings,
        mcp: GrafanaMCPClient,
        ollama_request: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.mcp = mcp
        self._ollama_request = ollama_request or self._request_ollama
        self.progress = progress or (lambda message: print(message, flush=True))
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.tools: list[dict[str, Any]] = []
        self.mcp_tool_names: set[str] = set()
        self.local_tool_names: set[str] = set(LOCAL_DEMO_TOOL_NAMES)
        self.tool_names: set[str] = set()
        self._all_mcp_tools: dict[str, Any] = {}
        self.demo = DemoScenarioClient(
            getattr(settings, "demo_api_url", "http://127.0.0.1:8000"),
            getattr(settings, "demo_api_timeout_seconds", 10.0),
        )
        self._dashboard_tools_enabled = False
        self.inference_turns = 0
        self.mcp_calls = 0

    async def prepare(self) -> None:
        mcp_tools = await self.mcp.list_tools()
        self._all_mcp_tools = {tool.name: tool for tool in mcp_tools if tool.name in ALLOWED_TOOL_NAMES}
        missing = ALLOWED_TOOL_NAMES - set(self._all_mcp_tools)
        if missing:
            raise OllamaError(f"MCP allowlist missing from server: {sorted(missing)}")
        self._set_tool_phase(DISCOVERY_TOOL_NAMES)

    def _set_tool_phase(self, names: set[str] | frozenset[str]) -> None:
        selected = [self._all_mcp_tools[name] for name in names]
        mcp_tools = [
            mcp_tool_to_ollama_tool(tool)
            for tool in sorted(selected, key=lambda item: item.name)
        ]
        self.tools = mcp_tools + local_demo_tools()
        self.mcp_tool_names = {tool["function"]["name"] for tool in mcp_tools}
        self.tool_names = self.mcp_tool_names | self.local_tool_names
        phase = "full dashboard" if self._dashboard_tools_enabled else "discovery"
        self.progress(
            f"[agent] exposing {len(self.tools)} {phase} MCP tools: "
            + ", ".join(sorted(self.mcp_tool_names))
        )

    async def run(self, user_prompt: str) -> str:
        if not self.tools:
            await self.prepare()
        self.messages.append({"role": "user", "content": user_prompt})
        for turn in range(1, self.settings.max_tool_turns + 1):
            response = await self._ollama_request(
                {
                    "model": self.settings.ollama_model,
                    "messages": self.messages,
                    "tools": self.tools,
                    "stream": False,
                    "think": self.settings.ollama_thinking,
                    "keep_alive": self.settings.ollama_keep_alive,
                    "options": {
                        "num_ctx": self.settings.ollama_context_size,
                        "num_predict": self.settings.ollama_num_predict,
                        "temperature": self.settings.ollama_temperature,
                    },
                }
            )
            self.inference_turns += 1
            self._log_inference_stats(response)
            message = response.get("message")
            if not isinstance(message, dict):
                raise OllamaError(f"Ollama response omitted message: {response}")
            self.messages.append(message)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = str(message.get("content") or "").strip()
                if not content:
                    raise OllamaError("Ollama returned neither a final answer nor tool calls")
                return content
            self.progress(f"[qwen] tool turn {turn}/{self.settings.max_tool_turns}: {len(tool_calls)} call(s)")
            for call in tool_calls:
                name, arguments = self._parse_tool_call(call)
                self.progress(f"[qwen] requesting tool: {name}")
                try:
                    if name in self.local_tool_names:
                        result = await self.demo.call_tool(name, arguments)
                        if name in {"set_demo_scenario", "set_demo_scenario_preset", "reset_demo_scenario"}:
                            wait_seconds = max(0.0, float(getattr(self.settings, "demo_scrape_wait_seconds", 6.0)))
                            if wait_seconds:
                                self.progress(f"[demo] waiting {wait_seconds:.0f}s for Prometheus scrape")
                                await asyncio.sleep(wait_seconds)
                        result_text = json.dumps({"is_error": False, "scenario": result}, ensure_ascii=False)
                        self.progress(f"[demo] tool succeeded: {name}")
                    else:
                        self.mcp_calls += 1
                        result = await self.mcp.call_tool(name, arguments)
                        result_text = serialize_mcp_result(result, self.settings.max_tool_result_chars)
                        if getattr(result, "is_error", False):
                            argument_preview = json.dumps(arguments, ensure_ascii=False)[:1000]
                            error_preview = result_text[:1000]
                            self.progress(
                                f"[mcp] tool returned an error: {name} "
                                f"arguments={argument_preview} result={error_preview}"
                            )
                        else:
                            self.progress(f"[mcp] tool succeeded: {name}")
                        if name in {"query_prometheus", "query_prometheus_histogram"} and not self._dashboard_tools_enabled:
                            self._dashboard_tools_enabled = True
                            self._set_tool_phase(ALLOWED_TOOL_NAMES)
                except Exception as exc:
                    result_text = json.dumps({"error": str(exc)})
                    self.progress(f"[{('demo' if name in self.local_tool_names else 'mcp')}] tool failed: {name}: {exc}")
                self.messages.append(
                    {"role": "tool", "tool_name": name, "content": result_text}
                )
        raise OllamaError(
            f"Maximum tool-turn limit reached ({self.settings.max_tool_turns})"
        )

    def _log_inference_stats(self, response: dict[str, Any]) -> None:
        total_ns = response.get("total_duration")
        load_ns = response.get("load_duration")
        prompt_tokens = response.get("prompt_eval_count")
        output_tokens = response.get("eval_count")
        if not isinstance(total_ns, (int, float)):
            return
        details = [f"total={total_ns / 1_000_000_000:.1f}s"]
        if isinstance(load_ns, (int, float)):
            details.append(f"load={load_ns / 1_000_000_000:.1f}s")
        if isinstance(prompt_tokens, int):
            details.append(f"prompt_tokens={prompt_tokens}")
        if isinstance(output_tokens, int):
            details.append(f"output_tokens={output_tokens}")
        self.progress("[ollama] " + " ".join(details))

    def _parse_tool_call(self, call: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(call, dict):
            raise OllamaError(f"Malformed Ollama tool call: {call!r}")
        function = call.get("function")
        if not isinstance(function, dict):
            raise OllamaError(f"Malformed Ollama tool call function: {call!r}")
        name = function.get("name")
        allowed_names = getattr(self, "tool_names", None) or self.mcp_tool_names
        if name not in allowed_names:
            raise PermissionError(f"Ollama requested unknown/non-allowlisted tool: {name}")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise OllamaError(f"Malformed JSON arguments for {name}: {arguments}") from exc
        if not isinstance(arguments, dict):
            raise OllamaError(f"Tool arguments for {name} must be an object")
        return name, arguments

    async def _request_ollama(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._request_ollama_sync, payload)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OllamaUnavailable(f"Ollama request failed: {exc}") from exc

    def _request_ollama_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.settings.ollama_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.ollama_timeout_seconds
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise OllamaError(f"Ollama HTTP {exc.code}: {detail[:1000]}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise OllamaError("Ollama returned invalid JSON") from exc
