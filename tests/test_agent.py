import unittest
from types import SimpleNamespace

from agent.mcp_client import (
    ALLOWED_TOOL_NAMES,
    mcp_tool_to_ollama_tool,
    select_allowed_tools,
)
from agent.ollama_agent import OllamaAgent, OllamaError


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


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _settings(max_turns=2):
        return SimpleNamespace(
            max_tool_turns=max_turns,
            max_tool_result_chars=60000,
            ollama_model="test-model",
            ollama_thinking=False,
            ollama_keep_alive="1m",
            ollama_context_size=2048,
            ollama_num_predict=64,
            ollama_temperature=0,
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
        self.assertEqual(len(mcp.calls), 2)


if __name__ == "__main__":
    unittest.main()
