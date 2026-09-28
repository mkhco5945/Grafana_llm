from __future__ import annotations

import asyncio
import difflib
import json
import re
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
- For dashboard requests: discover metric names once, use only exact names from the live catalog, validate every required PromQL expression, reuse those exact validated expressions, then call update_dashboard and verify it.
- A plausible-looking metric name is invalid unless it appeared in the live metric catalog. Never invent aliases or approximate names.
- Validate important PromQL queries with query_prometheus before creating or updating a dashboard.
- Create or update dashboards only with update_dashboard. Prefer a useful, simple dashboard with clear units and legends.
- For an existing dashboard, use update_dashboard patch mode with its uid and operations; do not send a full replacement dashboard object that can carry a stale version.
- When dashboard retrieval returns a patch_path for a saved panel query, use that exact path; legacy dashboards may nest panels below another dashboard object.
- For rates derived from counters, use rate(); for histogram percentiles, use the _bucket series with rate() and aggregate by le. Reuse the exact validated expressions in panel targets.
- A panel named Request Rate must use rate() or irate() over a counter. An Error Percentage panel must divide an error rate by a request rate and scale the ratio to percent. Raw _total counters do not satisfy either request.
- For dashboard p95 validation, prefer query_prometheus with histogram_quantile over query_prometheus_histogram so the exact bucket PromQL can be reused in the panel.
- After a dashboard write, retrieve it and confirm the saved expressions, units, and legends match what you validated; then validate its important panel queries return data.
- If a tool errors, inspect the error and retry with corrected arguments.
- For simple PromQL validation, prefer an instant query with endTime "now". Never repeat an unchanged failed tool call.
- Label discovery is optional. Do not block dashboard creation on label enumeration when direct PromQL queries already work; use label tools only when a selector is genuinely needed or the user asks about labels.
- Once a discovery tool returns useful data, do not call it again unchanged; use the returned metric names and move on to targeted PromQL validation and dashboard work.
- If an identical successful call is offered again, treat the prior result as authoritative and move on; do not spend another turn re-running it.
- When building a dashboard, reuse the exact PromQL expressions that successfully returned real data; do not rewrite them from memory.
- For label matcher filters, the only legal type values are "=", "!=", "=~", and "!~". Do not add quotes or backslashes to those values.
- After a failed tool call, correct the arguments, choose another tool, or continue without that optional operation. Do not repeat the unchanged call.
- Never claim success without verification. Keep tool calls focused and avoid unnecessary tools.
- Prometheus data is observed time-series data; do not try to edit its database. If the user asks to change the simulated workload, use only the dedicated local demo scenario tools (never shell, curl, arbitrary HTTP, or files).
- After changing a demo scenario, allow Prometheus at least one scrape interval to observe new samples, then query Prometheus and verify the requested change before describing it as fact.
- Demo scenario tools control the exporter source workload; dashboard discovery, validation, creation, and updates remain Grafana MCP operations.

For update_dashboard, use the live schema. It supports a full dashboard JSON for creation or uid plus operations for targeted updates. Use the discovered Prometheus datasource UID in panel targets.
"""


_PROMQL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_:]*")
_PROMQL_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_PROMQL_RESERVED = {
    "and",
    "bool",
    "by",
    "group_left",
    "group_right",
    "ignoring",
    "on",
    "or",
    "sum",
    "avg",
    "min",
    "max",
    "count",
    "stddev",
    "stdvar",
    "count_values",
    "bottomk",
    "topk",
    "quantile",
    "unless",
    "without",
    "offset",
}


def extract_promql_metric_names(expression: str) -> set[str]:
    """Extract metric selectors without treating PromQL syntax as metrics.

    This intentionally handles the selector/function/aggregation constructs
    used by the agent rather than attempting to evaluate PromQL. Identifiers
    inside label matchers and grouping clauses are excluded; function names
    are excluded when followed by ``(``.
    """
    tokens: list[tuple[str, int, int]] = []
    masked = list(expression)
    for match in _PROMQL_STRING_RE.finditer(expression):
        for index in range(match.start(), match.end()):
            masked[index] = " "
    masked_expression = "".join(masked)
    for match in _PROMQL_IDENTIFIER_RE.finditer(masked_expression):
        tokens.append((match.group(0), match.start(), match.end()))
    if not tokens:
        return set()

    def next_char(position: int) -> str:
        remainder = masked_expression[position:]
        match = re.search(r"\S", remainder)
        return match.group(0) if match else ""

    candidates: set[str] = set()
    brace_depth = 0
    grouping_depths: list[int] = []
    paren_depth = 0
    token_index = 0
    for value, start, end in tokens:
        # Update delimiter state for the text between identifier tokens.
        between = masked_expression[token_index:end]
        for character in between:
            if character == "{":
                brace_depth += 1
            elif character == "}":
                brace_depth = max(0, brace_depth - 1)
            elif character == "(":
                paren_depth += 1
            elif character == ")":
                paren_depth = max(0, paren_depth - 1)
                grouping_depths = [depth for depth in grouping_depths if depth < paren_depth]
        token_index = end

        if value in {"by", "without", "on", "ignoring", "group_left", "group_right"}:
            if next_char(end) == "(":
                grouping_depths.append(paren_depth + 1)
            continue
        if brace_depth or value in _PROMQL_RESERVED:
            continue
        previous_character = masked_expression[:start].rstrip()[-1:] if masked_expression[:start].rstrip() else ""
        if previous_character.isdigit() or previous_character == ".":
            # PromQL duration units (the ``m`` in ``5m``) are not metrics.
            continue
        if any(depth <= paren_depth for depth in grouping_depths):
            continue
        if next_char(end) == "(":
            continue
        candidates.add(value)
    return candidates


def _walk_json(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)
    else:
        yield value


def _result_has_data(payload: Any) -> tuple[bool, int]:
    """Return whether a Prometheus result contains one or more samples/series."""
    found = 0
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "data" and isinstance(value, list):
                found += len(value)
            else:
                has_data, count = _result_has_data(value)
                if has_data:
                    found += count
    elif isinstance(payload, list):
        for value in payload:
            has_data, count = _result_has_data(value)
            if has_data:
                found += count
    return found > 0, found


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
        self._required_dashboard_validations = 1
        self._empty_output_retries = 0
        self._dashboard_retrieved_after_write = False
        self._saved_dashboard_expressions: set[str] = set()
        self._saved_dashboard_semantic_errors: list[dict[str, str]] = []
        self._post_write_verified_expressions: set[str] = set()
        self._current_dashboard_panels: list[dict[str, Any]] = []
        self._post_write_completion_prompts = 0
        self._discovered_metric_names: set[str] = set()
        self._metric_catalog_refresh_done = False
        self._validated_promql: dict[str, dict[str, Any]] = {}
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
        if any(
            phrase in lowered
            for phrase in ("do not create", "don't create", "do not modify", "don't modify", "inspect the existing", "retrieve the existing")
        ) and not any(phrase in lowered for phrase in ("update_dashboard", "update the existing", "correct the existing", "patch the existing")):
            return False
        return "dashboard" in lowered and any(
            word in lowered
            for word in ("create", "build", "make", "update", "new", "patch", "correct", "repair")
        )

    @staticmethod
    def _prompt_is_dashboard_repair(prompt: str) -> bool:
        lowered = prompt.lower()
        return "dashboard" in lowered and any(
            word in lowered for word in ("existing", "patch", "correct", "repair")
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
        self._required_dashboard_validations = (
            2
            if self._prewrite_guard_enabled and self._prompt_is_dashboard_repair(user_prompt)
            else 5 if self._prewrite_guard_enabled else 1
        )
        self._empty_output_retries = 0
        self._dashboard_retrieved_after_write = False
        self._saved_dashboard_expressions = set()
        self._saved_dashboard_semantic_errors = []
        self._post_write_verified_expressions = set()
        self._current_dashboard_panels = []
        self._post_write_completion_prompts = 0
        self._discovered_metric_names = set()
        self._metric_catalog_refresh_done = False
        self._validated_promql = {}
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
                    if self._prewrite_guard_enabled and self._empty_output_retries < 1:
                        self._empty_output_retries += 1
                        self.progress(
                            "[agent] Ollama returned an empty dashboard-planning response; "
                            "requesting one bounded continuation"
                        )
                        self.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Continue the dashboard task. The previous response contained no tool call or final answer. "
                                    "Use the grounded metric catalog and continue validating any remaining required PromQL; do not invent metric names."
                                ),
                            }
                        )
                        continue
                    raise OllamaError("Ollama returned neither a final answer nor tool calls")
                verification_error = self._dashboard_completion_error()
                if verification_error:
                    if self._post_write_completion_prompts < 2:
                        self._post_write_completion_prompts += 1
                        self.progress(
                            f"[agent] final answer held: {verification_error['error']}"
                        )
                        self.messages.append(
                            {
                                "role": "user",
                                "content": json.dumps(verification_error, ensure_ascii=False),
                            }
                        )
                        continue
                    raise OllamaError(
                        "Dashboard completion requirements were not met: "
                        + json.dumps(verification_error, ensure_ascii=False)
                    )
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
            "query_prometheus": 10,
            "query_prometheus_histogram": 4,
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

        if name in {"query_prometheus", "query_prometheus_histogram"}:
            expression = self._query_expression(name, arguments)
            grounding_error = self._promql_grounding_error(expression)
            if grounding_error:
                grounding_error["expression"] = expression
                result_text = json.dumps(grounding_error, ensure_ascii=False)
                self._record_failure(fingerprint, name, result_text, outcome="rejected")
                self.progress(f"[grounding] blocked PromQL before MCP: {grounding_error['error']}")
                return result_text
            semantic_error = self._query_semantic_error(expression)
            if semantic_error:
                semantic_error["expression"] = expression
                result_text = json.dumps(semantic_error, ensure_ascii=False)
                self._record_failure(fingerprint, name, result_text, outcome="rejected")
                self.progress("[grounding] blocked semantically invalid PromQL before MCP")
                return result_text
        if name == "update_dashboard":
            dashboard_error = self._dashboard_promql_gate(arguments)
            if dashboard_error:
                result_text = json.dumps(dashboard_error, ensure_ascii=False)
                self._record_failure(fingerprint, name, result_text, outcome="rejected")
                self.progress(f"[grounding] blocked dashboard write: {dashboard_error['error']}")
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
                if name == "list_prometheus_metric_names":
                    self._capture_metric_catalog(result_text)
                    if (
                        self._discovered_metric_names
                        and not self._metric_catalog_refresh_done
                        and isinstance(self.mcp, GrafanaMCPClient)
                        and (
                            arguments.get("regex") is not None
                            or arguments.get("page") is not None
                            or int(arguments.get("limit", 0) or 0) < 1000
                        )
                    ):
                        self._metric_catalog_refresh_done = True
                        full_arguments = {
                            "datasourceUid": arguments.get("datasourceUid"),
                            "limit": 1000,
                        }
                        try:
                            self.mcp_calls += 1
                            full_result = await self.mcp.call_tool(
                                "list_prometheus_metric_names", full_arguments
                            )
                            if not getattr(full_result, "is_error", False):
                                full_text = serialize_mcp_result(
                                    full_result, self.settings.max_tool_result_chars
                                )
                                self._capture_metric_catalog(full_text)
                                result_text = full_text
                                self.progress(
                                    "[grounding] refreshed the complete metric catalog with an unfiltered live listing"
                                )
                        except Exception as exc:
                            self.progress(f"[grounding] complete metric refresh failed; retaining initial catalog: {exc}")
                    self._record_success(fingerprint, name)
                    self._successful_call_results[fingerprint] = result_text
                    result_text = self._compact_metric_catalog_result(result_text)
                    self.progress(f"[mcp] tool succeeded: {name}")
                elif name in {"query_prometheus", "query_prometheus_histogram"}:
                    expression = self._query_expression(name, arguments)
                    payload = json.loads(result_text)
                    returned_data, sample_count = _result_has_data(payload)
                    if not returned_data:
                        self._record_failure(fingerprint, name, expression, outcome="no_data")
                        result_text = json.dumps(
                            {
                                "status": "no_data",
                                "expression": expression,
                                "returned_data": False,
                                "instruction": "Do not use this expression in the dashboard. Choose an expression using the discovered metrics that returns real data; for p95, use query_prometheus with histogram_quantile over the discovered _bucket metric.",
                                "mcp_result": payload,
                            },
                            ensure_ascii=False,
                        )
                        self.progress(f"[grounding] PromQL returned no data: {expression}")
                    else:
                        self._record_success(fingerprint, name)
                        self._validated_promql[expression] = {
                            "expression": expression,
                            "datasource_uid": arguments.get("datasourceUid"),
                            "returned_data": True,
                            "sample_count": sample_count,
                        }
                        self._successful_call_results[fingerprint] = result_text
                        if (
                            self._dashboard_write_seen
                            and self._dashboard_retrieved_after_write
                            and expression in self._saved_dashboard_expressions
                        ):
                            self._post_write_verified_expressions.add(expression)
                        result_text = json.dumps(
                            {
                                "status": "validated",
                                "expression": expression,
                                "returned_data": True,
                                "sample_count": sample_count,
                                "validated_expressions": sorted(self._validated_promql),
                                "mcp_result": payload,
                            },
                            ensure_ascii=False,
                        )
                        self.progress(f"[grounding] validated PromQL with {sample_count} returned series/samples")
                        if (
                            not self._dashboard_tools_enabled
                            and len(self._validated_promql) >= self._required_dashboard_validations
                        ):
                            self._dashboard_tools_enabled = True
                            if self._all_mcp_tools:
                                self._set_request_tool_phase(self._current_user_prompt)
                else:
                    self._record_success(fingerprint, name)
                    if name == "get_dashboard_by_uid":
                        result_text = self._record_dashboard_retrieval(result_text)
                    if name == "update_dashboard":
                        self._record_dashboard_write()
                    self._successful_call_results[fingerprint] = result_text
                    self.progress(f"[mcp] tool succeeded: {name}")
            return result_text
        except Exception as exc:
            self._record_failure(fingerprint, name, str(exc), outcome="exception")
            self.progress(f"[{('demo' if name in self.local_tool_names else 'mcp')}] tool failed: {name}: {exc}")
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _max_same_tool_failures(self) -> int:
        return max(1, int(getattr(self.settings, "max_same_tool_failures", 2)))

    def _dashboard_completion_error(self) -> dict[str, Any] | None:
        if not self._prewrite_guard_enabled:
            return None
        if not self._dashboard_write_seen:
            return None
        if not self._dashboard_retrieved_after_write:
            return {
                "error": "DASHBOARD_WRITE_NOT_VERIFIED",
                "instruction": "Retrieve the written dashboard with get_dashboard_by_uid before finishing.",
            }
        if self._saved_dashboard_semantic_errors:
            return {
                "error": "INVALID_SAVED_DASHBOARD_PROMQL_SEMANTICS",
                "problems": self._saved_dashboard_semantic_errors,
                "instruction": "Correct the invalid saved panel expressions, write the patch, and retrieve the dashboard again.",
            }
        unvalidated = sorted(self._saved_dashboard_expressions - set(self._validated_promql))
        unverified = sorted(
            self._saved_dashboard_expressions - self._post_write_verified_expressions
        )
        if unvalidated or unverified:
            return {
                "error": "DASHBOARD_WRITE_NOT_VERIFIED",
                "unvalidated_saved_expressions": unvalidated,
                "unverified_saved_expressions": unverified,
                "instruction": (
                    "Run query_prometheus with every listed saved expression after retrieval and require real data. "
                    "Do not finish until all saved expressions are verified."
                ),
            }
        return None

    def _post_write_verification_missing(self) -> bool:
        """Compatibility helper retained for focused tests and callers."""
        return self._dashboard_completion_error() is not None

    def _record_dashboard_write(self) -> None:
        self._dashboard_write_seen = True
        self._dashboard_retrieved_after_write = False
        self._saved_dashboard_expressions = set()
        self._saved_dashboard_semantic_errors = []
        self._post_write_verified_expressions = set()
        # Exact query calls must run again after the saved dashboard is read;
        # a pre-write cached result is not post-write verification.
        self._successful_call_results = {
            fingerprint: result
            for fingerprint, result in self._successful_call_results.items()
            if not fingerprint.startswith(
                (
                    "query_prometheus:",
                    "query_prometheus_histogram:",
                    "get_dashboard_by_uid:",
                    "get_dashboard_summary:",
                    "get_dashboard_panel_queries:",
                )
            )
        }

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

    def _capture_metric_catalog(self, result_text: str) -> None:
        try:
            payload = json.loads(result_text)
        except json.JSONDecodeError:
            return
        discovered = {
            value
            for value in _walk_json(payload.get("content", []))
            if isinstance(value, str) and _PROMQL_IDENTIFIER_RE.fullmatch(value)
        }
        if not discovered:
            return
        self._discovered_metric_names.update(discovered)
        relevant = self._relevant_metric_names()
        suffix = ", ".join(sorted(relevant)[:12])
        self.progress(
            f"[grounding] discovered {len(self._discovered_metric_names)} Prometheus metric names"
            + (f"; relevant metrics: {suffix}" if suffix else "")
        )

    def _relevant_metric_names(self) -> list[str]:
        terms = ("request", "error", "duration", "latency", "cpu", "memory", "uptime", "demo")

        def score(name: str) -> tuple[int, str]:
            lowered = name.lower()
            return (sum(lowered.count(term) for term in terms), name)

        ordered = sorted(self._discovered_metric_names, key=score, reverse=True)
        return ordered[:40]

    def _compact_metric_catalog_result(self, result_text: str) -> str:
        try:
            original = json.loads(result_text)
        except json.JSONDecodeError:
            original = {"raw_result": result_text}
        relevant = self._relevant_metric_names()
        payload = {
            "status": "metric_catalog",
            "authoritative_metric_count": len(self._discovered_metric_names),
            "relevant_metric_names": relevant,
            "instruction": (
                "AUTHORITATIVE METRIC NAMES: these names came from the live Prometheus datasource. "
                "Use only these exact metric names in PromQL. Do not invent aliases or approximate names. "
                "The host retains the complete catalog and will reject any other metric identifier."
            ),
            "source_result": (
                original.get("content", original)
                if len(self._discovered_metric_names) <= 50
                else "Complete catalog retained host-side; relevant names are listed above."
            ),
        }
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        limit = getattr(self.settings, "max_tool_result_chars", 60000)
        return encoded if len(encoded) <= limit else encoded[:limit] + "\n[tool result truncated]"

    def _closest_metric_matches(self, names: set[str]) -> dict[str, list[str]]:
        return {
            name: difflib.get_close_matches(name, sorted(self._discovered_metric_names), n=3, cutoff=0.35)
            for name in sorted(names)
        }

    def _promql_grounding_error(self, expression: str) -> dict[str, Any] | None:
        if self._prewrite_guard_enabled and not self._discovered_metric_names:
            return {
                "error": "METRICS_NOT_DISCOVERED",
                "instruction": "Call list_prometheus_metric_names first, then build PromQL using exact discovered names.",
            }
        if not self._discovered_metric_names:
            return None
        referenced = extract_promql_metric_names(expression)
        unknown = referenced - self._discovered_metric_names
        if not unknown:
            return None
        return {
            "error": "UNKNOWN_PROMETHEUS_METRIC",
            "unknown_metrics": sorted(unknown),
            "instruction": "Use only exact metric names from the live discovered catalog.",
            "closest_matches": self._closest_metric_matches(unknown),
        }

    @staticmethod
    def _query_expression(name: str, arguments: dict[str, Any]) -> str:
        if name == "query_prometheus":
            return str(arguments.get("expr", ""))
        metric = str(arguments.get("metric", ""))
        labels = str(arguments.get("labels", "")).strip()
        rate_interval = str(arguments.get("rateInterval", "5m"))
        selector = f"{{{labels}}}" if labels else ""
        return (
            f"histogram_quantile({float(arguments.get('percentile', 0)) / 100:g}, "
            f"sum by (le) (rate({metric}_bucket{selector}[{rate_interval}]))"
            f")"
        )

    @staticmethod
    def _semantic_promql_problem(title: str, expression: str) -> str | None:
        """Reject a few high-confidence dashboard query semantic mistakes."""
        title_lower = title.lower()
        expression_lower = expression.lower()
        metrics = extract_promql_metric_names(expression)
        has_counter = any(metric.endswith("_total") for metric in metrics)
        rate_calls = len(re.findall(r"\b(?:i?rate)\s*\(", expression_lower))

        if "request" in title_lower and "rate" in title_lower:
            if has_counter and rate_calls < 1:
                return "Request-rate panels must apply rate() or irate() to the _total counter; a raw counter is cumulative, not a rate."

        is_error_percentage = "error" in title_lower and (
            "percent" in title_lower or "percentage" in title_lower or "%" in title
        )
        if is_error_percentage:
            has_ratio = "/" in expression
            has_percent_scale = bool(
                re.search(r"(?:^|[^\d.])100(?:\.0+)?\s*\*", expression)
                or re.search(r"\*\s*100(?:\.0+)?(?:[^\d.]|$)", expression)
            )
            if rate_calls < 2 or not has_ratio or not has_percent_scale:
                return "Error-percentage panels must divide an error rate by a request rate and multiply the ratio by 100; a raw error counter is not a percentage."
        elif "error" in title_lower and "rate" in title_lower:
            if has_counter and rate_calls < 1:
                return "Error-rate panels must apply rate() or irate() to the _total counter; a raw counter is cumulative, not a rate."

        is_percentile_latency = (
            ("latency" in title_lower or "duration" in title_lower)
            and any(marker in title_lower for marker in ("p95", "95th", "percentile"))
        )
        if is_percentile_latency:
            has_bucket = any(metric.endswith("_bucket") for metric in metrics)
            if "histogram_quantile" not in expression_lower or not has_bucket or rate_calls < 1:
                return "Percentile-latency panels must use histogram_quantile() over rate() of a discovered _bucket metric."
        return None

    def _query_semantic_error(self, expression: str) -> dict[str, Any] | None:
        """Apply request-intent semantics before a query can become validated."""
        prompt = self._current_user_prompt.lower()
        metrics = extract_promql_metric_names(expression)
        synthetic_titles: list[str] = []
        if "request rate" in prompt and any(
            metric.endswith("_total") and "request" in metric.lower() for metric in metrics
        ):
            synthetic_titles.append("Request Rate")
        if any(phrase in prompt for phrase in ("error percentage", "error percent")) and any(
            metric.endswith("_total") and "error" in metric.lower() for metric in metrics
        ):
            synthetic_titles.append("Error Percentage")
        if any(term in prompt for term in ("p95", "95th percentile")) and any(
            metric.endswith("_bucket") for metric in metrics
        ):
            synthetic_titles.append("P95 Latency")
        problems = [
            problem
            for title in synthetic_titles
            if (problem := self._semantic_promql_problem(title, expression))
        ]
        if not problems:
            return None
        return {
            "error": "INVALID_PROMQL_SEMANTICS",
            "problems": list(dict.fromkeys(problems)),
            "instruction": "Correct the PromQL explicitly; the host will not silently rewrite it.",
        }

    @staticmethod
    def _extract_dashboard_panels(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            panels = payload.get("panels")
            if isinstance(panels, list) and all(isinstance(panel, dict) for panel in panels):
                return panels
            for child in payload.values():
                found = OllamaAgent._extract_dashboard_panels(child)
                if found:
                    return found
        elif isinstance(payload, list):
            for child in payload:
                found = OllamaAgent._extract_dashboard_panels(child)
                if found:
                    return found
        return []

    @staticmethod
    def _dashboard_document(payload: Any) -> dict[str, Any] | None:
        """Return the dashboard document from mcp-grafana's response wrapper."""
        if isinstance(payload, dict):
            content = payload.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("dashboard"), dict):
                        return item["dashboard"]
            if isinstance(payload.get("dashboard"), dict):
                return payload["dashboard"]
        return None

    @staticmethod
    def _find_panels_with_path(
        value: Any, path: str = "$"
    ) -> tuple[list[dict[str, Any]], str]:
        if isinstance(value, dict):
            panels = value.get("panels")
            if isinstance(panels, list) and all(
                isinstance(panel, dict) for panel in panels
            ):
                return panels, f"{path}.panels"
            for key, child in value.items():
                if isinstance(child, dict):
                    found, found_path = OllamaAgent._find_panels_with_path(
                        child, f"{path}.{key}"
                    )
                    if found:
                        return found, found_path
        return [], "$.panels"

    @classmethod
    def _panel_query_contexts(cls, panels: list[dict[str, Any]]) -> list[dict[str, str]]:
        contexts: list[dict[str, str]] = []
        for panel in panels:
            title = str(panel.get("title") or "Untitled panel")
            for target in panel.get("targets", []) or []:
                if isinstance(target, dict) and isinstance(target.get("expr"), str):
                    contexts.append({"panel": title, "expression": target["expr"]})
        return contexts

    def _record_dashboard_retrieval(self, result_text: str) -> str:
        try:
            payload = json.loads(result_text)
        except json.JSONDecodeError:
            return result_text
        document = self._dashboard_document(payload)
        panels, panel_path = self._find_panels_with_path(document or payload)
        if not panels:
            return result_text
        self._current_dashboard_panels = panels
        contexts = self._panel_query_contexts(panels)
        for panel_index, panel in enumerate(panels):
            title = str(panel.get("title") or "Untitled panel")
            for target_index, target in enumerate(panel.get("targets", []) or []):
                if not isinstance(target, dict) or not isinstance(target.get("expr"), str):
                    continue
                for context in contexts:
                    if context["panel"] == title and context["expression"] == target["expr"] and "patch_path" not in context:
                        context["patch_path"] = (
                            f"{panel_path}[{panel_index}].targets[{target_index}].expr"
                        )
                        break
        semantic_errors = [
            {"panel": context["panel"], "expression": context["expression"], "problem": problem}
            for context in contexts
            if (problem := self._semantic_promql_problem(context["panel"], context["expression"]))
        ]
        if self._dashboard_write_seen:
            self._dashboard_retrieved_after_write = True
            self._saved_dashboard_expressions = {
                context["expression"] for context in contexts
            }
            self._saved_dashboard_semantic_errors = semantic_errors
            self._post_write_verified_expressions = set()
            # Force every saved expression to reach Prometheus after this
            # retrieval, even if it was validated before the write.
            self._successful_call_results = {
                fingerprint: result
                for fingerprint, result in self._successful_call_results.items()
                if not fingerprint.startswith(("query_prometheus:", "query_prometheus_histogram:"))
            }
        payload["dashboard_verification"] = {
            "status": "semantic_errors" if semantic_errors else "retrieved",
            "saved_panel_queries": contexts,
            "semantic_errors": semantic_errors,
            "instruction": (
                "Correct the listed semantic errors with validated PromQL before updating."
                if semantic_errors
                else "After a write, run every saved expression with query_prometheus and require real data before finishing."
            ),
        }
        return json.dumps(payload, ensure_ascii=False)

    def _dashboard_expression_contexts(
        self, arguments: dict[str, Any]
    ) -> list[dict[str, str]]:
        dashboard = arguments.get("dashboard")
        contexts = (
            self._panel_query_contexts(self._extract_dashboard_panels(dashboard))
            if isinstance(dashboard, dict)
            else []
        )
        for operation in arguments.get("operations", []) or []:
            if not isinstance(operation, dict):
                continue
            path = str(operation.get("path", ""))
            value = operation.get("value")
            if path.endswith(".expr") and isinstance(value, str):
                panel_title = ""
                panel_match = re.search(r"\$\.(?:[A-Za-z_][A-Za-z0-9_]*\.)*panels\[(\d+)\]", path)
                if panel_match:
                    panel_index = int(panel_match.group(1))
                    if panel_index < len(self._current_dashboard_panels):
                        panel_title = str(
                            self._current_dashboard_panels[panel_index].get("title") or ""
                        )
                contexts.append({"panel": panel_title, "expression": value})
            elif isinstance(value, dict) and "targets" in value:
                contexts.extend(self._panel_query_contexts([value]))
        return contexts

    @staticmethod
    def _dashboard_expressions(arguments: dict[str, Any]) -> list[str]:
        expressions: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "expr" and isinstance(child, str):
                        expressions.append(child)
                    elif key != "datasource":
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        if isinstance(arguments.get("dashboard"), dict):
            collect(arguments["dashboard"])
        for operation in arguments.get("operations", []) or []:
            if not isinstance(operation, dict):
                continue
            path = str(operation.get("path", ""))
            value = operation.get("value")
            if path.endswith("expr") and isinstance(value, str):
                expressions.append(value)
            elif isinstance(value, (dict, list)):
                collect(value)
        return list(dict.fromkeys(expressions))

    def _dashboard_promql_gate(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        expressions = self._dashboard_expressions(arguments)
        if not expressions:
            return None
        if not self._discovered_metric_names:
            return {
                "error": "METRICS_NOT_DISCOVERED",
                "instruction": "Call list_prometheus_metric_names first, then validate every dashboard PromQL expression.",
            }
        unknown: set[str] = set()
        unvalidated: list[str] = []
        for expression in expressions:
            referenced = extract_promql_metric_names(expression)
            unknown.update(referenced - self._discovered_metric_names)
            if expression not in self._validated_promql:
                unvalidated.append(expression)
        if unknown:
            return {
                "error": "UNKNOWN_PROMETHEUS_METRIC",
                "unknown_metrics": sorted(unknown),
                "instruction": "Use only exact metric names from the live discovered catalog.",
                "closest_matches": self._closest_metric_matches(unknown),
            }
        semantic_errors = [
            {"panel": context["panel"], "expression": context["expression"], "problem": problem}
            for context in self._dashboard_expression_contexts(arguments)
            if context["panel"]
            if (problem := self._semantic_promql_problem(context["panel"], context["expression"]))
        ]
        if semantic_errors:
            return {
                "error": "INVALID_DASHBOARD_PROMQL_SEMANTICS",
                "problems": semantic_errors,
                "instruction": "Correct and validate the panel PromQL explicitly; raw counters cannot satisfy rate or percentage panels.",
            }
        if unvalidated:
            return {
                "error": "UNVALIDATED_DASHBOARD_PROMQL",
                "expressions": unvalidated,
                "instruction": "Validate every panel PromQL expression with query_prometheus before creating or updating the dashboard.",
            }
        return None

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
