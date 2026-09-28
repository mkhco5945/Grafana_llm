from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from jsonschema import Draft202012Validator

from .config import Settings
from .demo_client import (
    LOCAL_DEMO_TOOL_NAMES,
    DemoScenarioClient,
    local_demo_tools,
    validate_local_arguments,
)
from .mcp_client import (
    ALLOWED_TOOL_NAMES,
    CORE_DISCOVERY_TOOL_NAMES,
    LABEL_DISCOVERY_TOOL_NAMES,
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
- Label discovery is optional. Do not block dashboard creation on label enumeration when direct PromQL queries already work; use label tools only when a selector is genuinely needed or the user asks about labels.
- Once a discovery tool returns useful data, do not call it again unchanged; use the returned metric names and move on to targeted PromQL validation and dashboard work.
- If an identical successful call is offered again, treat the prior result as authoritative and move on; do not spend another turn re-running it.
- For label matcher filters, the only legal type values are "=", "!=", "=~", and "!~". Do not add quotes or backslashes to those values.
- After a failed tool call, correct the arguments, choose another tool, or continue without that optional operation. Do not repeat the unchanged call.
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
        # These are deliberately request-scoped. Interactive history remains
        # in self.messages, but one request must not poison a later request.
        self._failed_call_fingerprints: dict[str, str] = {}
        self._tool_failure_counts: dict[str, int] = {}
        self._call_outcomes: dict[str, str] = {}
        self._successful_call_results: dict[str, str] = {}
        self._last_tool_name = ""
        self._same_tool_streak = 0
        self._tool_call_counts: dict[str, int] = {}
        self._prewrite_recovery_blocks = 0
        self._dashboard_write_seen = False
        self._prewrite_guard_enabled = False
        self._current_user_prompt = ""

    async def prepare(self) -> None:
        mcp_tools = await self.mcp.list_tools()
        self._all_mcp_tools = {tool.name: tool for tool in mcp_tools if tool.name in ALLOWED_TOOL_NAMES}
        missing = ALLOWED_TOOL_NAMES - set(self._all_mcp_tools)
        if missing:
            raise OllamaError(f"MCP allowlist missing from server: {sorted(missing)}")
        self._set_tool_phase(CORE_DISCOVERY_TOOL_NAMES)

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

    def _set_request_tool_phase(self, user_prompt: str) -> None:
        """Expose the lean default set, opting into label tools when relevant."""
        names = set(CORE_DISCOVERY_TOOL_NAMES)
        if self._prompt_requests_label_discovery(user_prompt):
            names |= LABEL_DISCOVERY_TOOL_NAMES
        if self._dashboard_tools_enabled:
            names.add("update_dashboard")
        self._set_tool_phase(names)

    @staticmethod
    def _prompt_requests_label_discovery(prompt: str) -> bool:
        lowered = prompt.lower()
        if any(phrase in lowered for phrase in ("do not need label", "don't need label", "without label", "no label enumeration")):
            return False
        return "label" in lowered or "tag" in lowered

    @staticmethod
    def _prompt_expects_dashboard_write(prompt: str) -> bool:
        lowered = prompt.lower()
        return "dashboard" in lowered and any(
            word in lowered for word in ("create", "build", "make", "update", "new")
        )

    async def run(self, user_prompt: str) -> str:
        if not self.tools:
            await self.prepare()
        self._failed_call_fingerprints = {}
        self._tool_failure_counts = {}
        self._call_outcomes = {}
        self._successful_call_results = {}
        self._last_tool_name = ""
        self._same_tool_streak = 0
        self._tool_call_counts = {}
        self._prewrite_recovery_blocks = 0
        self._dashboard_write_seen = False
        self._prewrite_guard_enabled = self._prompt_expects_dashboard_write(user_prompt)
        self._current_user_prompt = user_prompt
        if self._all_mcp_tools:
            self._set_request_tool_phase(user_prompt)
        self.messages.append({"role": "user", "content": user_prompt})
        for turn in range(1, self.settings.max_tool_turns + 1):
            if (
                self._prewrite_guard_enabled
                and not self._dashboard_write_seen
                and turn > int(getattr(self.settings, "max_prewrite_turns", 8))
            ):
                raise OllamaError(
                    "Agent is stuck before dashboard creation; no update_dashboard call was made "
                    f"within the configured pre-write budget ({turn - 1} turns)."
                )
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
                    done_reason = response.get("done_reason")
                    if done_reason in {"length", "max_tokens"}:
                        raise OllamaError(
                            "Ollama generation ended at its length limit without a final answer "
                            f"or tool calls (done_reason={done_reason}; "
                            f"output_tokens={response.get('eval_count', 'unknown')})"
                        )
                    raise OllamaError("Ollama returned neither a final answer nor tool calls")
                return content
            self.progress(f"[qwen] tool turn {turn}/{self.settings.max_tool_turns}: {len(tool_calls)} call(s)")
            for call in tool_calls:
                name, arguments = self._parse_tool_call(call)
                self.progress(f"[qwen] requesting tool: {name} arguments={json.dumps(arguments, ensure_ascii=False)[:600]}")
                fingerprint = self.tool_call_fingerprint(name, arguments)
                result_text = await self._dispatch_tool_call(name, arguments, fingerprint)
                self.messages.append(
                    {"role": "tool", "tool_name": name, "content": result_text}
                )
        raise OllamaError(
            f"Maximum tool-turn limit reached ({self.settings.max_tool_turns})"
        )

    @staticmethod
    def tool_call_fingerprint(name: str, arguments: dict[str, Any]) -> str:
        """Return a stable identity for a structured tool request."""
        return f"{name}:{json.dumps(arguments, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)}"

    async def _dispatch_tool_call(self, name: str, arguments: dict[str, Any], fingerprint: str) -> str:
        self._tool_call_counts[name] = self._tool_call_counts.get(name, 0) + 1
        if name == self._last_tool_name:
            self._same_tool_streak += 1
        else:
            self._last_tool_name = name
            self._same_tool_streak = 1

        cached_success = self._successful_call_results.get(fingerprint)
        if cached_success is not None:
            self.progress(f"[agent] reused prior successful call and blocked repeat: {name}")
            return json.dumps(
                {
                    "status": "already_succeeded",
                    "tool": name,
                    "message": "This exact tool call already succeeded during this request; use its prior result and continue.",
                    "prior_result": cached_success,
                },
                ensure_ascii=False,
            )

        if self._same_tool_streak >= 3 and name in {
            "list_prometheus_metric_names",
            "list_prometheus_label_names",
            "list_prometheus_label_values",
        }:
            self._prewrite_recovery_blocks += 1
            self.progress(f"[agent] blocked repetitive successful tool pattern: {name}")
            if self._prewrite_recovery_blocks > int(getattr(self.settings, "max_prewrite_recovery_blocks", 3)):
                raise OllamaError(
                    "Agent is stuck in repetitive tool calls before dashboard creation; "
                    "stopping early after corrective results were ignored."
                )
            return json.dumps(
                {
                    "error": "REPEATED_TOOL_PATTERN",
                    "tool": name,
                    "message": "This tool has just been called repeatedly. Use the data already returned and choose the next operation.",
                    "instruction": "Do not repeat this discovery call; continue with a targeted query or dashboard operation.",
                },
                ensure_ascii=False,
            )

        discovery_budget = {
            "list_prometheus_metric_names": 2,
            "list_prometheus_label_names": 2,
            "list_prometheus_label_values": 2,
            "query_prometheus": 6,
            "query_prometheus_histogram": 3,
        }.get(name)
        if not self._dashboard_write_seen and discovery_budget is not None and self._tool_call_counts[name] > discovery_budget:
            self._prewrite_recovery_blocks += 1
            self.progress(
                f"[agent] blocked excessive pre-write discovery calls: {name} "
                f"({self._tool_call_counts[name]} > {discovery_budget})"
            )
            if self._prewrite_recovery_blocks > int(getattr(self.settings, "max_prewrite_recovery_blocks", 3)):
                raise OllamaError(
                    "Agent is stuck in repetitive discovery calls before dashboard creation; "
                    "stopping early after corrective tool results were ignored."
                )
            return json.dumps(
                {
                    "error": "DISCOVERY_CALL_BUDGET_EXCEEDED",
                    "tool": name,
                    "message": "Discovery has already been attempted enough times for this request.",
                    "instruction": "Use the data already returned, validate the direct PromQL you have, and proceed to update_dashboard. Do not repeat discovery.",
                },
                ensure_ascii=False,
            )

        previous_error = self._failed_call_fingerprints.get(fingerprint)
        if previous_error is not None:
            message = {
                "error": "REPEATED_FAILED_TOOL_CALL",
                "message": "This exact tool call already failed and was blocked from being executed again.",
                "previous_error": previous_error[:2000],
                "instruction": "Change the arguments, choose another tool, or continue without this optional operation. Do not repeat this exact call.",
            }
            self._record_failure(fingerprint, name, message["message"])
            self.progress(f"[agent] blocked repeated failed call: {name}")
            return json.dumps(message, ensure_ascii=False)

        if self._tool_failure_counts.get(name, 0) >= self._max_same_tool_failures():
            message = {
                "error": "STUCK_TOOL_FAILURE_PATTERN",
                "tool": name,
                "message": f"This tool has failed {self._tool_failure_counts[name]} times in this request, so this failing pattern is blocked.",
                "instruction": "Choose a different approach or continue without this optional operation.",
            }
            if name in {"list_prometheus_label_names", "list_prometheus_label_values"}:
                message["instruction"] += " This operation is optional. Continue using the already discovered metrics and query_prometheus if label discovery is not required."
            self._record_failure(fingerprint, name, message["message"])
            self.progress(f"[agent] blocked stuck tool failure pattern: {name}")
            return json.dumps(message, ensure_ascii=False)

        validation_error = self._validate_tool_arguments(name, arguments)
        if validation_error:
            result_text = json.dumps(
                {
                    "error": "INVALID_TOOL_ARGUMENTS",
                    "tool": name,
                    "details": validation_error,
                    "instruction": "Correct the arguments according to the supplied tool schema.",
                },
                ensure_ascii=False,
            )
            self._record_failure(fingerprint, name, validation_error, outcome="rejected")
            self.progress(f"[agent] rejected invalid tool arguments: {name}: {validation_error}")
            return result_text

        try:
            if name in self.local_tool_names:
                result = await self.demo.call_tool(name, arguments)
                if name in {"set_demo_scenario", "set_demo_scenario_preset", "reset_demo_scenario"}:
                    wait_seconds = max(0.0, float(getattr(self.settings, "demo_scrape_wait_seconds", 6.0)))
                    if wait_seconds:
                        self.progress(f"[demo] waiting {wait_seconds:.0f}s for Prometheus scrape")
                        await asyncio.sleep(wait_seconds)
                result_text = json.dumps({"is_error": False, "scenario": result}, ensure_ascii=False)
                self._record_success(fingerprint, name)
                if name == "update_dashboard":
                    self._dashboard_write_seen = True
                self.progress(f"[demo] tool succeeded: {name}")
                return result_text
            self.mcp_calls += 1
            result = await self.mcp.call_tool(name, arguments)
            result_text = serialize_mcp_result(result, self.settings.max_tool_result_chars)
            if getattr(result, "is_error", False):
                argument_preview = json.dumps(arguments, ensure_ascii=False)[:1000]
                error_preview = result_text[:1000]
                self._record_failure(fingerprint, name, error_preview, outcome="mcp_error")
                result_text = self._augment_mcp_error(name, result_text)
                self.progress(
                    f"[mcp] tool returned an error: {name} "
                    f"arguments={argument_preview} result={error_preview}"
                )
            else:
                self._record_success(fingerprint, name)
                self._successful_call_results[fingerprint] = result_text
                if name == "update_dashboard":
                    self._dashboard_write_seen = True
                self.progress(f"[mcp] tool succeeded: {name}")
                if name in {"query_prometheus", "query_prometheus_histogram"} and not self._dashboard_tools_enabled:
                    self._dashboard_tools_enabled = True
                    if self._all_mcp_tools:
                        self._set_request_tool_phase(self._current_user_prompt)
            return result_text
        except Exception as exc:
            self._record_failure(fingerprint, name, str(exc), outcome="exception")
            self.progress(f"[{('demo' if name in self.local_tool_names else 'mcp')}] tool failed: {name}: {exc}")
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _max_same_tool_failures(self) -> int:
        return max(1, int(getattr(self.settings, "max_same_tool_failures", 2)))

    def _record_success(self, fingerprint: str, name: str) -> None:
        self._call_outcomes[fingerprint] = "succeeded"
        self._tool_failure_counts[name] = 0

    def _record_failure(
        self,
        fingerprint: str,
        name: str,
        error: str,
        *,
        outcome: str = "rejected",
    ) -> None:
        self._call_outcomes[fingerprint] = outcome
        self._failed_call_fingerprints.setdefault(fingerprint, error)
        self._tool_failure_counts[name] = self._tool_failure_counts.get(name, 0) + 1

    def _validate_tool_arguments(self, name: str, arguments: dict[str, Any]) -> str | None:
        """Validate against the live MCP schema (or local tool schema) before execution."""
        schema: dict[str, Any] | None = None
        if name in self.local_tool_names:
            for tool in local_demo_tools():
                if tool["function"]["name"] == name:
                    schema = tool["function"]["parameters"]
                    break
        elif name in self._all_mcp_tools:
            converted = mcp_tool_to_ollama_tool(self._all_mcp_tools[name])
            schema = converted["function"]["parameters"]
        # Unit tests may construct a minimal agent without live metadata. In a
        # real run every MCP tool comes from list_tools and is always checked.
        if schema is not None:
            errors = sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda e: list(e.path))
            if errors:
                error = errors[0]
                path = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.path).lstrip(".")
                return f"{path or '<arguments>'}: {error.message}"
        if name in self.local_tool_names:
            try:
                validate_local_arguments(name, arguments)
            except (PermissionError, ValueError, TypeError) as exc:
                return str(exc)
        if name in {"list_prometheus_label_names", "list_prometheus_label_values"}:
            matches = arguments.get("matches") or []
            for match in matches:
                for matcher in match.get("filters", []) if isinstance(match, dict) else []:
                    matcher_type = matcher.get("type") if isinstance(matcher, dict) else None
                    if matcher_type not in {"=", "!=", "=~", "!~"}:
                        return (
                            "matches.filters.type must be one of '=', '!=', '=~', or '!~' "
                            f"(got {matcher_type!r})"
                        )
        return None

    def _augment_mcp_error(self, name: str, result_text: str) -> str:
        try:
            payload = json.loads(result_text)
        except json.JSONDecodeError:
            payload = {"raw_result": result_text}
        payload.update(
            {
                "tool": name,
                "status": "error",
                "recovery": "Do not repeat unchanged arguments. Correct them or use another tool.",
            }
        )
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        limit = getattr(self.settings, "max_tool_result_chars", 60000)
        return encoded if len(encoded) <= limit else encoded[:limit] + "\n[tool result truncated]"

    def _log_inference_stats(self, response: dict[str, Any]) -> None:
        total_ns = response.get("total_duration")
        load_ns = response.get("load_duration")
        prompt_tokens = response.get("prompt_eval_count")
        output_tokens = response.get("eval_count")
        message = response.get("message") if isinstance(response.get("message"), dict) else {}
        content = message.get("content") or ""
        thinking = message.get("thinking") or ""
        tool_calls = message.get("tool_calls") or []
        details = []
        if isinstance(total_ns, (int, float)):
            details.append(f"total={total_ns / 1_000_000_000:.1f}s")
        if isinstance(load_ns, (int, float)):
            details.append(f"load={load_ns / 1_000_000_000:.1f}s")
        if isinstance(prompt_tokens, int):
            details.append(f"prompt_tokens={prompt_tokens}")
        if isinstance(output_tokens, int):
            details.append(f"output_tokens={output_tokens}")
        if "done" in response:
            details.append(f"done={response['done']}")
        if response.get("done_reason") is not None:
            details.append(f"done_reason={response['done_reason']}")
        details.extend(
            [
                f"content_chars={len(str(content))}",
                f"thinking_chars={len(str(thinking))}",
                f"tool_calls={len(tool_calls) if isinstance(tool_calls, list) else 0}",
            ]
        )
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
