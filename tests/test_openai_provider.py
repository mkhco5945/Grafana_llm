from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent.api import _configured_settings, _connection_metadata, _connection_settings, _extract_dashboard_url, _selected_model, _start_job
from agent.chat_store import ChatStore
from agent.config import Settings
from agent.ollama_agent import OllamaAgent, OllamaError
from agent.openai_client import NoRedirect, OpenAIChatClient, validate_base_url
from test_agent import FakeMCP


def settings(**overrides):
    with patch.dict(os.environ, {"MCP_GRAFANA_SERVER_TOKEN": "test-mcp", "LLM_PROVIDER": "openai"}, clear=True):
        with patch("agent.config.load_dotenv"):
            value = Settings.from_env()
    return replace(value, openai_model="test-remote", openai_api_key="secret-test-key", **overrides)


def completion(message, reason="stop"):
    return {"choices": [{"message": message, "finish_reason": reason}]}


class ApiResultTests(unittest.TestCase):
    def test_markdown_dashboard_link_is_stored_without_delimiter(self):
        self.assertEqual(
            "/d/http-error-tracking/http-error-tracking",
            _extract_dashboard_url(
                "Open `http://localhost:3000/d/http-error-tracking/http-error-tracking`."
            ),
        )


class OpenAITransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_transport_tool_roundtrip_preserves_ids_and_isolates_ollama_options(self):
        replies = iter([
            completion({"content": None, "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "list_datasources", "arguments": '{"type":"prometheus"}'}},
                {"id": "call_b", "type": "function", "function": {"name": "list_datasources", "arguments": '{}'}},
            ]}, "tool_calls"),
            completion({"content": "done"}),
        ])
        requests = []

        def send(request, timeout):
            requests.append(json.loads(request.data))
            self.assertEqual(request.full_url, "https://api.openai.com/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-test-key")
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(next(replies)).encode()
            return response

        opener = MagicMock()
        opener.open.side_effect = send
        mcp = FakeMCP()
        agent = OllamaAgent(settings(), mcp, progress=lambda _: None)
        agent.tools = [{"type": "function", "function": {"name": "list_datasources", "parameters": {"type": "object"}}}]
        agent.mcp_tool_names = {"list_datasources"}
        with patch("agent.openai_client.urllib.request.build_opener", return_value=opener):
            self.assertEqual(await agent.run("Inspect sources"), "done")
        self.assertEqual(len(mcp.calls), 2)
        self.assertEqual(set(requests[0]), {"model", "messages", "stream", "tools"})
        outputs = [m for m in requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in outputs], ["call_a", "call_b"])
        self.assertNotIn("tool_name", outputs[0])
        self.assertEqual(requests[0]["model"], "test-remote")

    def test_errors_do_not_expose_provider_body_or_key(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://api.example/v1", 401, "secret-test-key", {}, io.BytesIO(b"secret-test-key"))
        with patch("agent.openai_client.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(OllamaError) as raised:
                OpenAIChatClient(settings()).request_sync({"messages": []})
        self.assertIn("401", str(raised.exception))
        self.assertNotIn("secret-test-key", str(raised.exception))

    def test_rejects_missing_tool_ids(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(completion({"tool_calls": [{"function": {"name": "list_datasources"}}]})).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch("agent.openai_client.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(OllamaError, "malformed"):
                OpenAIChatClient(settings()).request_sync({"messages": []})

    def test_no_redirect_with_credentials(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.example"))


class ConnectionTests(unittest.TestCase):
    def test_grafana_proxy_settings_supply_encrypted_connection_defaults(self):
        defaults = {"provider": "openai", "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4.1-mini", "api_key": "grafana-decrypted-key"}
        configured = _configured_settings(defaults)
        self.assertEqual(configured.llm_provider, "openai")
        self.assertEqual(configured.openai_model, "gpt-4.1-mini")
        selected = _connection_settings("inspect Grafana", {}, defaults)
        self.assertEqual(selected.openai_api_key, "grafana-decrypted-key")
        self.assertNotIn("grafana-decrypted-key", repr(selected))

    def test_saved_grafana_key_is_not_reused_for_a_different_endpoint(self):
        defaults = {"provider": "openai", "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4.1-mini", "api_key": "grafana-decrypted-key"}
        with self.assertRaisesRegex(ValueError, "API key"):
            _connection_settings("inspect", {"base_url": "https://other.example/v1"}, defaults)

    def test_environment_key_is_not_inherited_by_a_grafana_configured_endpoint(self):
        configured = _configured_settings({"provider": "openai", "base_url": "https://other.example/v1",
                                           "model": "other-model"})
        self.assertEqual(configured.openai_api_key, "")
        with self.assertRaisesRegex(ValueError, "API key"):
            _connection_settings("inspect", {}, {"provider": "openai",
                                                  "base_url": "https://other.example/v1",
                                                  "model": "other-model"})

    def test_remote_model_ignores_local_routing_defaults(self):
        with patch.dict(os.environ, {"AI_DASHBOARD_MODEL": "qwen3:8b", "AI_FAST_MODEL": "qwen3:4b"}):
            self.assertEqual(_selected_model("create dashboard", settings()), "test-remote")
            self.assertEqual(_selected_model("inspect CPU", settings()), "test-remote")

    def test_key_not_in_settings_repr(self):
        self.assertNotIn("secret-test-key", repr(settings()))

    def test_custom_endpoint_cannot_inherit_server_key(self):
        with patch("agent.api.Settings.from_env", return_value=settings()):
            with self.assertRaisesRegex(ValueError, "API key"):
                _connection_settings("inspect", {"base_url": "https://different.example/v1"})
            selected = _connection_settings("inspect", {"base_url": "https://different.example/v1", "api_key": "custom-key", "model": "custom-model"})
            self.assertEqual(selected.openai_api_key, "custom-key")
            self.assertEqual(selected.model, "custom-model")
            self.assertEqual(_connection_settings("inspect").openai_api_key, "secret-test-key")

    def test_ollama_still_routes_automatically(self):
        value = replace(settings(), llm_provider="ollama")
        with patch("agent.api.Settings.from_env", return_value=value), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_connection_settings("inspect CPU").model, "qwen3:4b")
            self.assertEqual(_connection_settings("build dashboard").model, "qwen3:8b")
            self.assertEqual(_connection_settings("inspect", {"model": "custom-local"}).model, "custom-local")

    def test_url_validation(self):
        self.assertEqual(validate_base_url("http://127.0.0.1:8080/v1/"), "http://127.0.0.1:8080/v1")
        for value in ("file:///tmp/api", "https://user:key@host/v1", "http://remote.example/v1", "https://host/v1?key=x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_base_url(value)

    def test_job_snapshot_and_retry_persist_no_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ChatStore(Path(folder) / "chat.db")
            session = store.create_session()
            value = settings()
            with patch("agent.api.STORE", store), patch("agent.api.Settings.from_env", return_value=value), patch("agent.api._launch_job") as launch:
                job = _start_job(session["id"], "inspect")
                self.assertEqual(launch.call_args.args[1].openai_api_key, "secret-test-key")
            self.assertEqual(job["connection"], _connection_metadata(value))
            self.assertNotIn("secret-test-key", json.dumps(store.get_session(session["id"])))
            store.fail_job(job["id"], "interrupted")
            retry = store.retry_job(job["id"])
            self.assertEqual(retry["connection"], job["connection"])
            self.assertNotIn(b"secret-test-key", (Path(folder) / "chat.db").read_bytes())

    def test_storage_whitelists_connection_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ChatStore(Path(folder) / "chat.db")
            job = store.create_job(store.create_session()["id"], "hello", "model", {"provider": "openai", "api_key": "never-save"})
            self.assertEqual(job["connection"], {"provider": "openai"})

    def test_existing_database_migrates_without_losing_conversations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chat.db"
            store = ChatStore(path)
            session = store.create_session("Existing history")
            job = store.create_job(session["id"], "hello", "qwen3:8b")
            with store._connect() as connection:
                connection.execute("ALTER TABLE jobs DROP COLUMN connection")
            reopened = ChatStore(path)
            self.assertEqual(reopened.get_job(job["id"])["connection"], {})
            self.assertEqual(reopened.get_session(session["id"])["messages"][0]["content"], "hello")

    def test_missing_key_rejected_before_creating_job(self):
        value = replace(settings(), openai_api_key="")
        with patch("agent.api.Settings.from_env", return_value=value), patch("agent.api._store") as store:
            with self.assertRaisesRegex(ValueError, "API key"):
                _start_job("unused", "inspect")
            store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
