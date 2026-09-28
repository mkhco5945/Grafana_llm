from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.chat_store import ChatStore


class ChatStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "chat.sqlite3"
        self.store = ChatStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_and_list_session(self) -> None:
        created = self.store.create_session("Service health")
        self.assertEqual("Service health", created["title"])
        self.assertEqual(created["id"], self.store.list_sessions()[0]["id"])

    def test_send_chat_persists_user_message_and_job(self) -> None:
        session = self.store.create_session()
        job = self.store.create_job(session["id"], "Inspect CPU", "qwen3:4b")
        reloaded = self.store.get_session(session["id"])
        self.assertEqual("Inspect CPU", reloaded["messages"][0]["content"])
        self.assertEqual(job["id"], reloaded["active_job"]["id"])

    def test_completed_result_and_progress_persist(self) -> None:
        session = self.store.create_session()
        job = self.store.create_job(session["id"], "Inspect CPU", "qwen3:4b")
        self.store.set_job_running(job["id"])
        self.store.append_progress(job["id"], "Queried Prometheus")
        self.store.complete_job(job["id"], "CPU is healthy", "/d/example/dashboard")
        reloaded = self.store.get_session(session["id"])
        self.assertEqual(["user", "assistant"], [message["role"] for message in reloaded["messages"]])
        self.assertEqual("CPU is healthy", reloaded["messages"][1]["content"])
        self.assertEqual(["Queried Prometheus"], reloaded["jobs"][0]["progress"])
        self.assertEqual("/d/example/dashboard", reloaded["jobs"][0]["dashboard_url"])

    def test_new_chat_does_not_delete_old_chat(self) -> None:
        first = self.store.create_session("First")
        second = self.store.create_session("Second")
        ids = {item["id"] for item in self.store.list_sessions()}
        self.assertEqual({first["id"], second["id"]}, ids)

    def test_reinitialize_store_reads_existing_conversation(self) -> None:
        session = self.store.create_session()
        job = self.store.create_job(session["id"], "Persist me", "qwen3:4b")
        self.store.set_job_running(job["id"])
        self.store.complete_job(job["id"], "Still here")
        reopened = ChatStore(self.db_path)
        self.assertEqual("Still here", reopened.get_session(session["id"])["messages"][-1]["content"])

    def test_stale_running_job_becomes_interrupted(self) -> None:
        session = self.store.create_session()
        job = self.store.create_job(session["id"], "Long request", "qwen3:8b")
        self.store.set_job_running(job["id"])
        reopened = ChatStore(self.db_path)
        self.assertEqual(1, reopened.interrupt_stale_jobs())
        interrupted = reopened.get_job(job["id"])
        self.assertEqual("interrupted", interrupted["status"])
        self.assertIn("Retry", interrupted["error"])
        self.assertEqual("Long request", reopened.get_session(session["id"])["messages"][0]["content"])

    def test_duplicate_assistant_response_is_prevented(self) -> None:
        session = self.store.create_session()
        job = self.store.create_job(session["id"], "One answer", "qwen3:4b")
        self.store.set_job_running(job["id"])
        self.store.complete_job(job["id"], "First answer")
        self.store.complete_job(job["id"], "Updated durable answer")
        messages = self.store.get_session(session["id"])["messages"]
        self.assertEqual(1, len([message for message in messages if message["role"] == "assistant"]))

    def test_retry_reuses_original_request_without_duplicate_user_message(self) -> None:
        session = self.store.create_session()
        original = self.store.create_job(session["id"], "Retry me", "qwen3:4b")
        self.store.set_job_running(original["id"])
        self.store.fail_job(original["id"], "temporary failure")
        retried = self.store.retry_job(original["id"])
        detail = self.store.get_session(session["id"])
        self.assertNotEqual(original["id"], retried["id"])
        self.assertEqual(1, len([message for message in detail["messages"] if message["role"] == "user"]))

    def test_agent_context_uses_prior_messages_but_not_current_request(self) -> None:
        session = self.store.create_session()
        first = self.store.create_job(session["id"], "First request", "qwen3:4b")
        self.store.set_job_running(first["id"])
        self.store.complete_job(first["id"], "First response")
        second = self.store.create_job(session["id"], "Second request", "qwen3:4b")
        context = self.store.get_context(session["id"], second["user_message_id"])
        self.assertEqual(["First request", "First response"], [item["content"] for item in context])

    def test_legacy_browser_chat_import_is_idempotent(self) -> None:
        payload = {
            "messages": [
                {"id": "welcome", "role": "assistant", "content": "welcome"},
                {"id": "u-1", "role": "user", "content": "Old browser request"},
                {"id": "a-1", "role": "assistant", "content": "Old browser answer"},
            ]
        }
        imported, already = self.store.import_legacy("mkhco-ai-dashboard-app.chat.v1", payload)
        imported_again, already_again = self.store.import_legacy("mkhco-ai-dashboard-app.chat.v1", payload)
        self.assertFalse(already)
        self.assertTrue(already_again)
        self.assertEqual(imported["id"], imported_again["id"])
        self.assertEqual(1, len(self.store.list_sessions()))
        self.assertEqual(2, len(imported["messages"]))

    def test_legacy_active_job_imports_as_interrupted_and_retryable(self) -> None:
        job_id = "a" * 32
        imported, _ = self.store.import_legacy(
            "legacy-active",
            {
                "messages": [{"id": "u-1", "role": "user", "content": "Build my dashboard"}],
                "activeJobId": job_id,
                "model": "qwen3:8b",
                "progress": ["Discovered metrics"],
            },
        )
        self.assertEqual("interrupted", imported["jobs"][0]["status"])
        self.assertEqual(["Discovered metrics"], imported["jobs"][0]["progress"])
        retried = self.store.retry_job(job_id)
        self.assertEqual("queued", retried["status"])


if __name__ == "__main__":
    unittest.main()
