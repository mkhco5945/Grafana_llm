import unittest
import json
from types import SimpleNamespace

from agent.mcp_client import (
    ALLOWED_TOOL_NAMES,
    mcp_tool_to_ollama_tool,
    select_allowed_tools,
)
from agent.demo_client import (
    LOCAL_DEMO_TOOL_NAMES,
    LOCAL_DEMO_TOOLS,
    DemoScenarioClient,
    validate_local_arguments,
)
from agent.ollama_agent import OllamaAgent, OllamaError, extract_promql_metric_names


class FakeTool:
    def __init__(self, name, description="", schema=None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object", "properties": {}}


class AgentPlumbingTests(unittest.TestCase):
    def test_schema_conversion_preserves_schema(self):
        tool = FakeTool(
            "query_prometheus",
            "Run PromQL",
            {"type": "object", "required": ["expr"], "properties": {"expr": {"type": "string"}}},
        )
        converted = mcp_tool_to_ollama_tool(tool)
        self.assertEqual(converted["type"], "function")
        self.assertEqual(converted["function"]["name"], "query_prometheus")
        self.assertEqual(converted["function"]["parameters"]["required"], ["expr"])

    def test_allowlist_selects_only_expected_tools(self):
        selected = select_allowed_tools(
            [FakeTool("query_prometheus"), FakeTool("not_allowed")]
        )
        self.assertEqual([tool.name for tool in selected], ["query_prometheus"])
        self.assertIn("update_dashboard", ALLOWED_TOOL_NAMES)

    def test_local_tool_schemas_are_explicitly_allowlisted(self):
        names = {tool["function"]["name"] for tool in LOCAL_DEMO_TOOLS}
        self.assertEqual(names, set(LOCAL_DEMO_TOOL_NAMES))
        validate_local_arguments("set_demo_scenario_preset", {"name": "high-errors"})
        with self.assertRaises(PermissionError):
            validate_local_arguments("curl", {})

    def test_unknown_tool_call_is_rejected(self):
        agent = OllamaAgent.__new__(OllamaAgent)
        agent.mcp_tool_names = {"query_prometheus"}
        with self.assertRaises(PermissionError):
            agent._parse_tool_call(
                {"function": {"name": "delete_everything", "arguments": {}}}
            )

    def test_malformed_arguments_are_rejected(self):
        agent = OllamaAgent.__new__(OllamaAgent)
        agent.mcp_tool_names = {"query_prometheus"}
        with self.assertRaises(OllamaError):
            agent._parse_tool_call(
                {"function": {"name": "query_prometheus", "arguments": "not-json"}}
            )


class FakeMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, structured_content={"ok": True}, content=[])


class FailingMCP(FakeMCP):
    def __init__(self, failing_names=None):
        super().__init__()
        self.failing_names = set(failing_names or ())

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.failing_names and sum(call[0] == name for call in self.calls) == 1:
            return SimpleNamespace(
                is_error=True,
                structured_content=None,
                content=[SimpleNamespace(type="text", text="bad_data: test failure")],
            )
        return SimpleNamespace(is_error=False, structured_content={"ok": True}, content=[])


class TextItem:
    def __init__(self, text):
        self.text = text

    def model_dump(self, **_kwargs):
        return {"type": "text", "text": self.text}


class ResultMCP(FakeMCP):
    def __init__(self, result):
        super().__init__()
        self.result = result

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _settings(max_turns=2, max_prewrite_turns=8):
        return SimpleNamespace(
            max_tool_turns=max_turns,
            max_tool_result_chars=60000,
            ollama_model="test-model",
            ollama_thinking=False,
            ollama_keep_alive="1m",
            ollama_context_size=2048,
            ollama_num_predict=64,
            ollama_temperature=0,
            demo_scrape_wait_seconds=0,
            max_prewrite_turns=max_prewrite_turns,
        )

    @staticmethod
    def _tool_call(name="list_datasources", arguments=None):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": name,
                            "arguments": arguments or {},
                        }
                    }
                ],
            }
        }

    @staticmethod
    def _valid_panel(
        title="Request Rate",
        expression="rate(demo_http_requests_total[5m])",
        panel_id=1,
        unit="reqps",
        x=0,
    ):
        return {
            "id": panel_id,
            "title": title,
            "type": "timeseries",
            "gridPos": {"x": x, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "targets": [
                {
                    "refId": "A",
                    "expr": expression,
                    "datasource": {"type": "prometheus", "uid": "prometheus"},
                }
            ],
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        }

    async def test_model_tool_call_is_dispatched_to_mcp(self):
        responses = iter(
            [
                self._tool_call(arguments={"type": "prometheus"}),
                {"message": {"role": "assistant", "content": "done"}},
            ]
        )

        async def request(_payload):
            return next(responses)

        mcp = FakeMCP()
        agent = OllamaAgent(self._settings(), mcp, ollama_request=request, progress=lambda _: None)
        agent.tools = [{"type": "function"}]
        agent.mcp_tool_names = {"list_datasources"}

        result = await agent.run("inspect datasources")

        self.assertEqual(result, "done")
        self.assertEqual(mcp.calls, [("list_datasources", {"type": "prometheus"})])
        self.assertEqual(agent.messages[-2]["role"], "tool")

    async def test_max_turn_limit_stops_repeated_tool_calls(self):
        async def request(_payload):
            return self._tool_call()

        mcp = FakeMCP()
        agent = OllamaAgent(
            self._settings(max_turns=2),
            mcp,
            ollama_request=request,
            progress=lambda _: None,
        )
        agent.tools = [{"type": "function"}]
        agent.mcp_tool_names = {"list_datasources"}

        with self.assertRaisesRegex(OllamaError, "Maximum tool-turn limit"):
            await agent.run("loop forever")
        self.assertEqual(len(mcp.calls), 1)

    async def test_local_tool_is_dispatched_without_mcp(self):
        responses = iter([
            self._tool_call(name="set_demo_scenario_preset", arguments={"name": "high-errors"}),
            {"message": {"role": "assistant", "content": "changed"}},
        ])

        async def request(_payload):
            return next(responses)

        class FakeDemo:
            def __init__(self):
                self.calls = []

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                return {"preset": "high-errors"}

        mcp = FakeMCP()
        agent = OllamaAgent(self._settings(), mcp, ollama_request=request, progress=lambda _: None)
        agent.demo = FakeDemo()
        agent.tools = list(LOCAL_DEMO_TOOLS)
        agent.mcp_tool_names = set()
        agent.tool_names = set(LOCAL_DEMO_TOOL_NAMES)
        result = await agent.run("make errors high")
        self.assertEqual(result, "changed")
        self.assertEqual(agent.demo.calls, [("set_demo_scenario_preset", {"name": "high-errors"})])
        self.assertEqual(mcp.calls, [])

    async def test_identical_failed_call_is_blocked_and_recovery_continues(self):
        bad_args = {"datasourceUid": "prometheus", "labelName": "job"}
        responses = iter(
            [
                self._tool_call(name="list_prometheus_label_values", arguments=bad_args),
                self._tool_call(name="list_prometheus_label_values", arguments=bad_args),
                self._tool_call(name="query_prometheus", arguments={"datasourceUid": "prometheus", "expr": "up", "endTime": "now"}),
                self._tool_call(name="update_dashboard", arguments={"dashboard": {"title": "recovered"}}),
                {"message": {"role": "assistant", "content": "created"}},
            ]
        )

        async def request(_payload):
            return next(responses)

        mcp = FailingMCP({"list_prometheus_label_values"})
        agent = OllamaAgent(self._settings(max_turns=6), mcp, ollama_request=request, progress=lambda _: None)
        agent.tools = [{"type": "function"}]
        agent.mcp_tool_names = {"list_prometheus_label_values", "query_prometheus", "update_dashboard"}
        agent.tool_names = set(agent.mcp_tool_names)

        self.assertEqual(await agent.run("recover"), "created")
        self.assertEqual([call[0] for call in mcp.calls], [
            "list_prometheus_label_values", "query_prometheus", "update_dashboard"
        ])
        repeated_result = json.loads(agent.messages[-6]["content"])
        self.assertEqual(repeated_result["error"], "REPEATED_FAILED_TOOL_CALL")

    async def test_failed_call_tracking_resets_between_runs(self):
        responses = iter(
            [
                self._tool_call(name="list_datasources"),
                {"message": {"role": "assistant", "content": "first"}},
                self._tool_call(name="list_datasources"),
                {"message": {"role": "assistant", "content": "second"}},
            ]
        )

        async def request(_payload):
            return next(responses)

        mcp = FailingMCP({"list_datasources"})
        agent = OllamaAgent(self._settings(max_turns=2), mcp, ollama_request=request, progress=lambda _: None)
        agent.tools = [{"type": "function"}]
        agent.mcp_tool_names = {"list_datasources"}
        agent.tool_names = set(agent.mcp_tool_names)
        self.assertEqual(await agent.run("first"), "first")
        self.assertEqual(await agent.run("second"), "second")
        self.assertEqual([call[0] for call in mcp.calls], ["list_datasources", "list_datasources"])

    def test_live_schema_validation_rejects_before_mcp(self):
        agent = OllamaAgent.__new__(OllamaAgent)
        agent.local_tool_names = set()
        agent._all_mcp_tools = {
            "query_prometheus": FakeTool(
                "query_prometheus",
                schema={
                    "type": "object",
                    "required": ["datasourceUid", "expr", "endTime"],
                    "properties": {"datasourceUid": {"type": "string"}, "expr": {"type": "string"}, "endTime": {"type": "string"}},
                    "additionalProperties": False,
                },
            )
        }
        self.assertIn("required property", agent._validate_tool_arguments("query_prometheus", {"expr": "up"}))

    def test_label_matcher_enum_is_validated(self):
        agent = OllamaAgent.__new__(OllamaAgent)
        agent.local_tool_names = set()
        agent._all_mcp_tools = {}
        error = agent._validate_tool_arguments(
            "list_prometheus_label_values",
            {"datasourceUid": "prometheus", "labelName": "job", "matches": [{"filters": [{"name": "job", "type": '=\\"', "value": "demo"}]}]},
        )
        self.assertIn("must be one of", error)

    async def test_ollama_length_diagnostic_is_specific(self):
        async def request(_payload):
            return {
                "done": True,
                "done_reason": "length",
                "eval_count": 64,
                "message": {"role": "assistant", "content": "", "thinking": "x" * 12},
            }

        agent = OllamaAgent(self._settings(max_turns=1), FakeMCP(), ollama_request=request, progress=lambda _: None)
        agent.tools = [{"type": "function"}]
        agent.mcp_tool_names = {"list_datasources"}
        with self.assertRaisesRegex(OllamaError, "length limit"):
            await agent.run("empty")

    async def test_dashboard_empty_response_gets_one_bounded_retry(self):
        responses = iter(
            [
                {"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": ""}},
                {"message": {"role": "assistant", "content": "continued"}},
            ]
        )

        async def request(_payload):
            return next(responses)

        agent = OllamaAgent(self._settings(max_turns=2), FakeMCP(), ollama_request=request, progress=lambda _: None)
        agent.tools = [{"type": "function"}]
        agent.mcp_tool_names = set()
        agent.tool_names = set()
        self.assertEqual(await agent.run("Create a dashboard"), "continued")

    async def test_prewrite_discovery_budget_blocks_early(self):
        mcp = FakeMCP()
        agent = OllamaAgent(self._settings(max_turns=20), mcp, progress=lambda _: None)
        args = {"datasourceUid": "prometheus"}
        for _ in range(2):
            fp = agent.tool_call_fingerprint("list_prometheus_metric_names", args)
            await agent._dispatch_tool_call("list_prometheus_metric_names", args, fp)
        fp = agent.tool_call_fingerprint("list_prometheus_metric_names", {**args, "page": 2})
        result = json.loads(await agent._dispatch_tool_call("list_prometheus_metric_names", {**args, "page": 2}, fp))
        self.assertIn(result["error"], {"DISCOVERY_CALL_BUDGET_EXCEEDED", "REPEATED_TOOL_PATTERN"})
        self.assertEqual(len(mcp.calls), 1)

    async def test_repetitive_tool_loop_stops_before_turn_limit(self):
        async def request(_payload):
            # Tiny argument changes should not buy the model twenty remote turns.
            page = sum(1 for message in agent.messages if message.get("role") == "tool") + 1
            return self._tool_call(
                name="list_prometheus_metric_names",
                arguments={"datasourceUid": "prometheus", "page": page},
            )

        mcp = FakeMCP()
        agent = OllamaAgent(self._settings(max_turns=20), mcp, ollama_request=request, progress=lambda _: None)
        agent.tools = [{"type": "function"}]
        agent.mcp_tool_names = {"list_prometheus_metric_names"}
        agent.tool_names = set(agent.mcp_tool_names)
        with self.assertRaisesRegex(OllamaError, "stuck in repetitive tool calls"):
            await agent.run("loop")
        self.assertLess(agent.inference_turns, 20)

    def test_metric_catalog_capture_and_promql_grounding(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        agent._capture_metric_catalog(
            json.dumps(
                {
                    "content": [["demo_http_requests_total", "demo_cpu_usage_percent"]]
                }
            )
        )
        self.assertEqual(
            agent._discovered_metric_names,
            {"demo_http_requests_total", "demo_cpu_usage_percent"},
        )
        self.assertIsNone(
            agent._promql_grounding_error("rate(demo_http_requests_total[5m])")
        )
        error = agent._promql_grounding_error("rate(demo_requests_total[5m])")
        self.assertEqual(error["error"], "UNKNOWN_PROMETHEUS_METRIC")
        self.assertIn("demo_http_requests_total", error["closest_matches"]["demo_requests_total"])

    def test_promql_extractor_ignores_functions_labels_and_durations(self):
        expression = (
            "histogram_quantile(0.95, sum by (le) (rate("
            "demo_http_request_duration_seconds_bucket[5m])))"
        )
        self.assertEqual(
            extract_promql_metric_names(expression),
            {"demo_http_request_duration_seconds_bucket"},
        )

    def test_raw_request_counter_is_rejected_for_request_rate(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        agent._current_user_prompt = "Build a dashboard with request rate"
        error = agent._query_semantic_error("demo_http_requests_total")
        self.assertEqual(error["error"], "INVALID_PROMQL_SEMANTICS")

    def test_expected_panels_are_derived_from_bulleted_prompt(self):
        count, titles = OllamaAgent._expected_panels_from_prompt(
            "It must contain five visible panels:\n- Request Rate\n- Error Percentage\n- P95 Request Latency\n- CPU Usage\n- Memory Usage"
        )
        self.assertEqual(count, 5)
        self.assertEqual(
            titles,
            {
                "Request Rate",
                "Error Percentage",
                "P95 Request Latency",
                "CPU Usage",
                "Memory Usage",
            },
        )

    def test_percentage_prompt_does_not_enable_label_discovery(self):
        self.assertFalse(
            OllamaAgent._prompt_requests_label_discovery(
                "Build an error percentage dashboard with legends"
            )
        )
        self.assertTrue(
            OllamaAgent._prompt_requests_label_discovery("What labels exist?")
        )

    def test_raw_error_counter_is_rejected_for_error_percentage(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        agent._current_user_prompt = "Correct the error percentage dashboard panel"
        error = agent._query_semantic_error("demo_http_errors_total")
        self.assertEqual(error["error"], "INVALID_PROMQL_SEMANTICS")

    def test_valid_rate_expression_is_semantically_accepted(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        agent._current_user_prompt = "Build a dashboard with request rate"
        self.assertIsNone(
            agent._query_semantic_error("rate(demo_http_requests_total[5m])")
        )

    def test_valid_error_percentage_expression_is_semantically_accepted(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        agent._current_user_prompt = "Correct request rate and error percentage"
        expression = (
            "100 * rate(demo_http_errors_total[5m]) "
            "/ rate(demo_http_requests_total[5m])"
        )
        self.assertIsNone(agent._query_semantic_error(expression))

    def test_dashboard_write_cannot_finish_without_post_write_retrieval(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        agent._prewrite_guard_enabled = True
        agent._record_dashboard_write()
        error = agent._dashboard_completion_error()
        self.assertEqual(error["error"], "DASHBOARD_WRITE_NOT_VERIFIED")
        self.assertIn("Retrieve", error["instruction"])

    def test_post_write_retrieval_and_data_verification_allows_completion(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        agent._prewrite_guard_enabled = True
        agent._record_dashboard_write()
        expression = "rate(demo_http_requests_total[5m])"
        result_text = json.dumps(
            {
                "content": [
                    {
                        "dashboard": {
                            "uid": "dashboard-uid",
                            "panels": [self._valid_panel(expression=expression)]
                        }
                    }
                ]
            }
        )
        agent._record_dashboard_retrieval(result_text)
        agent._validated_promql[expression] = {"returned_data": True}
        agent._post_write_verified_expressions.add(expression)
        self.assertIsNone(agent._dashboard_completion_error())

    def test_nested_dashboard_is_not_mistaken_for_renderable_panels(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        result_text = json.dumps(
            {
                "content": [
                    {
                        "dashboard": {
                            "uid": "dashboard-uid",
                            "dashboard": {
                                "panels": [
                                    {
                                        "title": "Request Rate",
                                        "targets": [{"expr": "demo_http_requests_total"}],
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        )
        retrieved = json.loads(agent._record_dashboard_retrieval(result_text))
        verification = retrieved["dashboard_verification"]
        self.assertEqual(verification["error"], "DASHBOARD_STRUCTURE_NOT_RENDERABLE")
        self.assertEqual(verification["panel_count"], 0)
        self.assertEqual(verification["saved_panel_queries"], [])
        self.assertIn(
            "top_level_panels_array_missing",
            {problem["problem"] for problem in verification["structure_errors"]},
        )

    def test_mcp_wrapper_unwraps_to_actual_dashboard_and_root_patch_path(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        panel = self._valid_panel()
        result_text = json.dumps(
            {"content": [{"dashboard": {"uid": "dashboard-uid", "panels": [panel]}}]}
        )
        retrieved = json.loads(agent._record_dashboard_retrieval(result_text))
        verification = retrieved["dashboard_verification"]
        self.assertEqual(verification["panel_count"], 1)
        self.assertEqual(
            verification["saved_panel_queries"][0]["patch_path"],
            "$.panels[0].targets[0].expr",
        )

    def test_empty_dashboard_fails_structural_verification(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        agent._expected_panel_count = 5
        result = json.loads(
            agent._record_dashboard_retrieval(
                json.dumps({"content": [{"dashboard": {"uid": "x", "panels": []}}]})
            )
        )
        verification = result["dashboard_verification"]
        self.assertEqual(verification["error"], "DASHBOARD_STRUCTURE_NOT_RENDERABLE")
        self.assertEqual(verification["panel_count"], 0)
        self.assertEqual(verification["expected_panel_count"], 5)

    def test_panel_with_promql_but_no_type_or_grid_fails_structure(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        panel = self._valid_panel()
        panel.pop("type")
        panel.pop("gridPos")
        problems = agent._panel_structure_problems([panel], expected_count=1)
        problem_names = {problem["problem"] for problem in problems}
        self.assertIn("missing_visualization_type", problem_names)
        self.assertIn("invalid_gridPos", problem_names)

    def test_complete_panel_passes_structural_verification(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        self.assertEqual(
            agent._panel_structure_problems(
                [self._valid_panel()], expected_count=1, expected_titles={"Request Rate"}
            ),
            [],
        )

    def test_five_panel_dashboard_passes_complete_post_write_verification(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        expressions = {
            "Request Rate": "rate(demo_http_requests_total[5m])",
            "Error Percentage": "100 * rate(demo_http_errors_total[5m]) / rate(demo_http_requests_total[5m])",
            "P95 Request Latency": "histogram_quantile(0.95, sum by (le) (rate(demo_http_request_duration_seconds_bucket[5m])))",
            "CPU Usage": "demo_cpu_usage_percent",
            "Memory Usage": "demo_memory_usage_bytes",
        }
        units = {
            "Request Rate": "reqps",
            "Error Percentage": "percent",
            "P95 Request Latency": "s",
            "CPU Usage": "percent",
            "Memory Usage": "bytes",
        }
        panels = [
            self._valid_panel(
                title=title,
                expression=expression,
                panel_id=index,
                unit=units[title],
                x=(index - 1) * 4,
            )
            for index, (title, expression) in enumerate(expressions.items(), start=1)
        ]
        agent._prewrite_guard_enabled = True
        agent._expected_panel_count = 5
        agent._expected_panel_titles = set(expressions)
        for expression in expressions.values():
            agent._validated_promql[expression] = {
                "returned_data": True,
                "datasource_uid": "prometheus",
            }
        agent._record_dashboard_write(
            {"uid": "dashboard-uid", "operations": [{"op": "add", "path": "$.panels", "value": panels}]}
        )
        agent._record_dashboard_retrieval(
            json.dumps(
                {"content": [{"dashboard": {"uid": "dashboard-uid", "panels": panels}}]}
            )
        )
        agent._post_write_verified_expressions.update(expressions.values())
        self.assertIsNone(agent._dashboard_completion_error())

    async def test_post_write_retrieval_independently_queries_saved_promql(self):
        result = SimpleNamespace(
            is_error=False,
            structured_content=None,
            content=[TextItem(json.dumps({"data": [{"value": [1, "1"]}]}))],
        )
        mcp = ResultMCP(result)
        agent = OllamaAgent(self._settings(), mcp, progress=lambda _: None)
        expression = "rate(demo_http_requests_total[5m])"
        agent._saved_dashboard_expressions = {expression}
        agent._validated_promql[expression] = {
            "returned_data": True,
            "datasource_uid": "prometheus",
        }
        response = await agent._verify_saved_dashboard_queries(
            json.dumps({"dashboard_verification": {}})
        )
        verification = json.loads(response)["dashboard_verification"]
        self.assertTrue(verification["all_saved_queries_returned_data"])
        self.assertEqual(agent._post_write_verified_expressions, {expression})
        self.assertEqual(mcp.calls[0][0], "query_prometheus")

    async def test_response_envelope_path_is_rejected_for_dashboard_patch(self):
        mcp = ResultMCP(
            SimpleNamespace(is_error=False, structured_content=None, content=[])
        )
        agent = OllamaAgent(self._settings(), mcp, progress=lambda _: None)
        expression = "rate(demo_http_requests_total[5m])"
        agent._discovered_metric_names = {"demo_http_requests_total"}
        agent._validated_promql[expression] = {
            "returned_data": True,
            "datasource_uid": "prometheus",
        }
        arguments = {
            "uid": "dashboard-uid",
            "operations": [
                {
                    "op": "replace",
                    "path": "$.dashboard.panels[0].targets[0].expr",
                    "value": expression,
                }
            ],
        }
        fingerprint = agent.tool_call_fingerprint("update_dashboard", arguments)
        result = json.loads(
            await agent._dispatch_tool_call("update_dashboard", arguments, fingerprint)
        )
        self.assertEqual(result["error"], "INVALID_DASHBOARD_PATCH_PATH")
        self.assertEqual(mcp.calls, [])

    def test_query_outside_panels_does_not_count_as_dashboard_panel(self):
        agent = OllamaAgent(self._settings(), FakeMCP(), progress=lambda _: None)
        result = json.loads(
            agent._record_dashboard_retrieval(
                json.dumps(
                    {
                        "content": [
                            {
                                "dashboard": {
                                    "uid": "x",
                                    "panels": [],
                                    "metadata": {"expr": "demo_cpu_usage_percent"},
                                }
                            }
                        ]
                    }
                )
            )
        )
        verification = result["dashboard_verification"]
        self.assertEqual(verification["panel_count"], 0)
        self.assertEqual(verification["saved_panel_queries"], [])
        self.assertEqual(verification["error"], "DASHBOARD_STRUCTURE_NOT_RENDERABLE")

    async def test_correction_flow_rejects_old_semantics_and_accepts_verified_patch(self):
        mcp = ResultMCP(
            SimpleNamespace(is_error=False, structured_content=None, content=[])
        )
        agent = OllamaAgent(self._settings(), mcp, progress=lambda _: None)
        agent._prewrite_guard_enabled = True
        old_error_panel = self._valid_panel(
            title="Error Percentage",
            expression="demo_http_errors_total",
            panel_id=1,
            unit="percent",
        )
        old_request_panel = self._valid_panel(
            title="Request Rate",
            expression="demo_http_requests_total",
            panel_id=2,
            unit="reqps",
            x=12,
        )
        old_dashboard = json.dumps(
            {
                "content": [
                    {
                        "dashboard": {
                            "panels": [old_error_panel, old_request_panel]
                        }
                    }
                ]
            }
        )
        retrieved = json.loads(agent._record_dashboard_retrieval(old_dashboard))
        self.assertEqual(
            len(retrieved["dashboard_verification"]["semantic_errors"]), 2
        )

        request_rate = "rate(demo_http_requests_total[5m])"
        error_percentage = (
            "100 * rate(demo_http_errors_total[5m]) "
            "/ rate(demo_http_requests_total[5m])"
        )
        agent._discovered_metric_names = {
            "demo_http_errors_total",
            "demo_http_requests_total",
        }
        for expression in (request_rate, error_percentage):
            agent._validated_promql[expression] = {"returned_data": True}
        patch = {
            "uid": "dashboard-uid",
            "operations": [
                {
                    "op": "replace",
                    "path": "$.panels[0].targets[0].expr",
                    "value": error_percentage,
                },
                {
                    "op": "replace",
                    "path": "$.panels[1].targets[0].expr",
                    "value": request_rate,
                },
            ],
        }
        fingerprint = agent.tool_call_fingerprint("update_dashboard", patch)
        await agent._dispatch_tool_call("update_dashboard", patch, fingerprint)
        self.assertEqual([call[0] for call in mcp.calls], ["update_dashboard"])

        corrected_error_panel = self._valid_panel(
            title="Error Percentage",
            expression=error_percentage,
            panel_id=1,
            unit="percent",
        )
        corrected_request_panel = self._valid_panel(
            title="Request Rate",
            expression=request_rate,
            panel_id=2,
            unit="reqps",
            x=12,
        )
        corrected_dashboard = json.dumps(
            {
                "content": [
                    {
                        "dashboard": {
                            "uid": "dashboard-uid",
                            "panels": [corrected_error_panel, corrected_request_panel]
                        }
                    }
                ]
            }
        )
        agent._record_dashboard_retrieval(corrected_dashboard)
        agent._post_write_verified_expressions.update(
            {request_rate, error_percentage}
        )
        self.assertIsNone(agent._dashboard_completion_error())

    async def test_no_data_query_is_not_marked_validated(self):
        result = SimpleNamespace(
            is_error=False,
            structured_content=None,
            content=[TextItem(json.dumps({"data": [], "hints": {"summary": "no data"}}))],
        )
        mcp = ResultMCP(result)
        agent = OllamaAgent(self._settings(), mcp, progress=lambda _: None)
        agent._discovered_metric_names = {"demo_http_requests_total"}
        args = {"datasourceUid": "prometheus", "expr": "demo_http_requests_total", "endTime": "now"}
        fp = agent.tool_call_fingerprint("query_prometheus", args)
        payload = json.loads(await agent._dispatch_tool_call("query_prometheus", args, fp))
        self.assertEqual(payload["status"], "no_data")
        self.assertEqual(agent._validated_promql, {})

    async def test_dashboard_gate_blocks_unvalidated_expression(self):
        mcp = ResultMCP(SimpleNamespace(is_error=False, structured_content=None, content=[]))
        agent = OllamaAgent(self._settings(), mcp, progress=lambda _: None)
        agent._discovered_metric_names = {"demo_http_requests_total"}
        panel = self._valid_panel(expression="rate(demo_requests_total[5m])")
        args = {"dashboard": {"title": "x", "panels": [panel]}}
        fp = agent.tool_call_fingerprint("update_dashboard", args)
        payload = json.loads(await agent._dispatch_tool_call("update_dashboard", args, fp))
        self.assertEqual(payload["error"], "UNKNOWN_PROMETHEUS_METRIC")
        self.assertEqual(mcp.calls, [])

    async def test_dashboard_gate_allows_only_validated_expression(self):
        mcp = ResultMCP(SimpleNamespace(is_error=False, structured_content=None, content=[]))
        agent = OllamaAgent(self._settings(), mcp, progress=lambda _: None)
        expression = "rate(demo_http_requests_total[5m])"
        agent._discovered_metric_names = {"demo_http_requests_total"}
        agent._validated_promql[expression] = {
            "expression": expression,
            "returned_data": True,
            "sample_count": 1,
        }
        panel = self._valid_panel(expression=expression)
        args = {"dashboard": {"title": "x", "panels": [panel]}}
        fp = agent.tool_call_fingerprint("update_dashboard", args)
        await agent._dispatch_tool_call("update_dashboard", args, fp)
        self.assertEqual([call[0] for call in mcp.calls], ["update_dashboard"])

    async def test_grounding_state_resets_between_requests(self):
        responses = iter([
            {"message": {"role": "assistant", "content": "first"}},
            {"message": {"role": "assistant", "content": "second"}},
        ])

        async def request(_payload):
            return next(responses)

        agent = OllamaAgent(self._settings(), FakeMCP(), ollama_request=request, progress=lambda _: None)
        agent.tools = [{"type": "function"}]
        agent.mcp_tool_names = set()
        agent.tool_names = set()
        agent._discovered_metric_names = {"stale_metric"}
        agent._validated_promql["stale_metric"] = {"returned_data": True}
        await agent.run("first")
        self.assertEqual(agent._discovered_metric_names, set())
        self.assertEqual(agent._validated_promql, {})
        agent._discovered_metric_names = {"new_metric"}
        await agent.run("second")
        self.assertEqual(agent._discovered_metric_names, set())

    async def test_demo_client_dispatches_to_specific_api_endpoint(self):
        client = DemoScenarioClient("http://demo.invalid")
        requests = []

        def fake_request(method, path, body):
            requests.append((method, path, body))
            return {"preset": "high-errors"}

        client._request = fake_request
        result = await client.call_tool("set_demo_scenario_preset", {"name": "high-errors"})
        self.assertEqual(result["preset"], "high-errors")
        self.assertEqual(requests, [("POST", "/scenario/preset", {"name": "high-errors"})])


if __name__ == "__main__":
    unittest.main()
