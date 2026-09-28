from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("completed", "failed", "interrupted")


class ChatStore:
    """Small thread-safe SQLite repository for durable chat sessions and jobs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    job_id TEXT
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    user_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'interrupted')),
                    progress TEXT NOT NULL DEFAULT '[]',
                    answer TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    dashboard_url TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS messages_assistant_job_unique
                ON messages(job_id)
                WHERE role = 'assistant' AND job_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS messages_session_created
                ON messages(session_id, created_at);

                CREATE INDEX IF NOT EXISTS jobs_session_created
                ON jobs(session_id, created_at);

                CREATE TABLE IF NOT EXISTS imports (
                    source_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def _id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _title(value: str | None) -> str:
        title = " ".join((value or "").strip().split())
        return title[:120] or "New chat"

    @staticmethod
    def _message_title(message: str) -> str:
        single_line = " ".join(message.strip().split())
        if len(single_line) > 60:
            return f"{single_line[:57]}..."
        return single_line or "New chat"

    @staticmethod
    def _decode_progress(value: str) -> list[str]:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
        return [str(item) for item in decoded if isinstance(item, str)][-80:] if isinstance(decoded, list) else []

    def create_session(self, title: str | None = None) -> dict[str, Any]:
        session_id = self._id()
        now = time.time()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, self._title(title), now, now),
            )
        return self.get_session(session_id)

    def session_exists(self, session_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is not None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*,
                       (SELECT j.status FROM jobs j WHERE j.session_id = s.id ORDER BY j.created_at DESC LIMIT 1) AS status,
                       (SELECT j.model FROM jobs j WHERE j.session_id = s.id ORDER BY j.created_at DESC LIMIT 1) AS model,
                       (SELECT j.id FROM jobs j WHERE j.session_id = s.id AND j.status IN ('queued', 'running') ORDER BY j.created_at DESC LIMIT 1) AS active_job_id
                FROM sessions s
                ORDER BY s.updated_at DESC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "status": row["status"] or "idle",
                "model": row["model"] or "",
                "active_job_id": row["active_job_id"] or "",
            }
            for row in rows
        ]

    def _job_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "user_message_id": row["user_message_id"],
            "model": row["model"],
            "status": row["status"],
            "progress": self._decode_progress(row["progress"]),
            "answer": row["answer"],
            "error": row["error"],
            "dashboard_url": row["dashboard_url"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise KeyError("session not found")
            messages = connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
            ).fetchall()
            jobs = connection.execute(
                "SELECT * FROM jobs WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
            ).fetchall()
        job_values = [self._job_dict(row) for row in jobs]
        active = next((job for job in reversed(job_values) if job["status"] in ACTIVE_STATUSES), None)
        return {
            "id": session["id"],
            "title": session["title"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "messages": [
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "job_id": row["job_id"] or "",
                }
                for row in messages
            ],
            "jobs": job_values,
            "active_job": active,
        }

    def create_job(self, session_id: str, message: str, model: str) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise ValueError("message is required")
        now = time.time()
        message_id = self._id()
        job_id = self._id()
        with self._write_lock, self._connect() as connection:
            session = connection.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise KeyError("session not found")
            active = connection.execute(
                "SELECT id FROM jobs WHERE session_id = ? AND status IN ('queued', 'running')", (session_id,)
            ).fetchone()
            if active is not None:
                raise ValueError("this session already has an active job")
            connection.execute(
                "INSERT INTO messages (id, session_id, role, content, created_at, job_id) VALUES (?, ?, 'user', ?, ?, ?)",
                (message_id, session_id, message, now, job_id),
            )
            connection.execute(
                """
                INSERT INTO jobs (id, session_id, user_message_id, model, status, progress, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', '[]', ?, ?)
                """,
                (job_id, session_id, message_id, model, now, now),
            )
            title = session["title"]
            if title == "New chat":
                title = self._message_title(message)
            connection.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?", (title, now, session_id))
        return self.get_job(job_id)

    def retry_job(self, job_id: str, model: str | None = None) -> dict[str, Any]:
        now = time.time()
        retry_id = self._id()
        with self._write_lock, self._connect() as connection:
            original = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if original is None:
                raise KeyError("job not found")
            if original["status"] not in ("failed", "interrupted"):
                raise ValueError("only failed or interrupted jobs can be retried")
            active = connection.execute(
                "SELECT id FROM jobs WHERE session_id = ? AND status IN ('queued', 'running')", (original["session_id"],)
            ).fetchone()
            if active is not None:
                raise ValueError("this session already has an active job")
            connection.execute(
                """
                INSERT INTO jobs (id, session_id, user_message_id, model, status, progress, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', '[]', ?, ?)
                """,
                (retry_id, original["session_id"], original["user_message_id"], model or original["model"], now, now),
            )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, original["session_id"]))
        return self.get_job(retry_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("job not found")
        return self._job_dict(row)

    def get_job_message(self, job_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT m.content FROM jobs j JOIN messages m ON m.id = j.user_message_id WHERE j.id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError("job not found")
        return str(row["content"])

    def get_context(self, session_id: str, user_message_id: str, limit: int = 10) -> list[dict[str, str]]:
        with self._connect() as connection:
            current = connection.execute(
                "SELECT created_at, rowid FROM messages WHERE id = ? AND session_id = ?",
                (user_message_id, session_id),
            ).fetchone()
            if current is None:
                raise KeyError("user message not found")
            rows = connection.execute(
                """
                SELECT role, content FROM messages
                WHERE session_id = ? AND (created_at < ? OR (created_at = ? AND rowid < ?))
                ORDER BY created_at DESC, rowid DESC LIMIT ?
                """,
                (session_id, current["created_at"], current["created_at"], current["rowid"], limit),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def set_job_running(self, job_id: str) -> None:
        self._set_job_status(job_id, "running", error="")

    def _set_job_status(self, job_id: str, status: str, error: str | None = None) -> None:
        now = time.time()
        with self._write_lock, self._connect() as connection:
            if error is None:
                cursor = connection.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, now, job_id))
            else:
                cursor = connection.execute(
                    "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?", (status, error, now, job_id)
                )
            if cursor.rowcount != 1:
                raise KeyError("job not found")
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = (SELECT session_id FROM jobs WHERE id = ?)", (now, job_id)
            )

    def append_progress(self, job_id: str, message: str) -> None:
        message = message.strip()
        if not message:
            return
        with self._write_lock, self._connect() as connection:
            row = connection.execute("SELECT progress FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("job not found")
            progress = self._decode_progress(row["progress"])
            if not progress or progress[-1] != message:
                progress = (progress + [message])[-80:]
            now = time.time()
            connection.execute(
                "UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ?", (json.dumps(progress), now, job_id)
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = (SELECT session_id FROM jobs WHERE id = ?)", (now, job_id)
            )

    def complete_job(self, job_id: str, answer: str, dashboard_url: str = "") -> dict[str, Any]:
        now = time.time()
        with self._write_lock, self._connect() as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError("job not found")
            connection.execute(
                """
                UPDATE jobs SET status = 'completed', answer = ?, error = '', dashboard_url = ?, updated_at = ?
                WHERE id = ?
                """,
                (answer, dashboard_url, now, job_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO messages (id, session_id, role, content, created_at, job_id)
                VALUES (?, ?, 'assistant', ?, ?, ?)
                """,
                (self._id(), job["session_id"], answer, now, job_id),
            )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, job["session_id"]))
        return self.get_job(job_id)

    def fail_job(self, job_id: str, error: str) -> None:
        self._set_job_status(job_id, "failed", error=error)

    def interrupt_stale_jobs(self) -> int:
        now = time.time()
        message = "AI bridge restarted before this job finished. Retry to run the saved request again."
        with self._write_lock, self._connect() as connection:
            sessions = connection.execute(
                "SELECT DISTINCT session_id FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'interrupted', error = ?, updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (message, now),
            )
            for row in sessions:
                connection.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, row["session_id"]))
        return cursor.rowcount

    def import_legacy(self, source_key: str, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        source_key = source_key.strip()[:200]
        if not source_key:
            raise ValueError("source key is required")
        with self._write_lock, self._connect() as connection:
            existing = connection.execute("SELECT session_id FROM imports WHERE source_key = ?", (source_key,)).fetchone()
        if existing is not None:
            return self.get_session(existing["session_id"]), True

        raw_messages = payload.get("messages")
        messages = raw_messages if isinstance(raw_messages, list) else []
        cleaned: list[dict[str, str]] = []
        for item in messages[-100:]:
            if not isinstance(item, dict) or item.get("id") == "welcome":
                continue
            role = item.get("role")
            content = item.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                cleaned.append({"role": role, "content": content.strip()[:20_000]})
        if not cleaned:
            raise ValueError("no legacy messages to import")

        session_id = self._id()
        now = time.time()
        first_user = next((item["content"] for item in cleaned if item["role"] == "user"), "Imported browser chat")
        last_user_id = ""
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, f"Imported: {self._message_title(first_user)}"[:120], now, now),
            )
            for index, item in enumerate(cleaned):
                message_id = self._id()
                if item["role"] == "user":
                    last_user_id = message_id
                connection.execute(
                    "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                    (message_id, session_id, item["role"], item["content"], now + index / 1000),
                )
            active_job_id = str(payload.get("activeJobId") or "").strip()
            if active_job_id and last_user_id:
                safe_job_id = active_job_id if re_full_hex(active_job_id) else self._id()
                progress = payload.get("progress") if isinstance(payload.get("progress"), list) else []
                progress = [str(value) for value in progress if isinstance(value, str)][-80:]
                connection.execute(
                    """
                    INSERT INTO jobs (id, session_id, user_message_id, model, status, progress, error, dashboard_url, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'interrupted', ?, ?, ?, ?, ?)
                    """,
                    (
                        safe_job_id,
                        session_id,
                        last_user_id,
                        str(payload.get("model") or ""),
                        json.dumps(progress),
                        "Imported browser job was no longer running. Retry to run the saved request again.",
                        str(payload.get("dashboardUrl") or ""),
                        now,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO imports (source_key, session_id, created_at) VALUES (?, ?, ?)",
                (source_key, session_id, now),
            )
        return self.get_session(session_id), False

    def delete_smoke_session(self, session_id: str) -> bool:
        with self._write_lock, self._connect() as connection:
            row = connection.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return False
            if not row["title"].startswith("[smoke-test]"):
                raise ValueError("only smoke-test sessions can be deleted through this endpoint")
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return True

    def stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            session_count = connection.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()["count"]
            job_rows = connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
            latest_session = connection.execute(
                "SELECT id, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            latest_job = connection.execute(
                "SELECT id, session_id, model, status, created_at, updated_at FROM jobs ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return {
            "database_exists": self.path.exists(),
            "session_count": session_count,
            "jobs_by_status": {row["status"]: row["count"] for row in job_rows},
            "latest_session": dict(latest_session) if latest_session else None,
            "latest_job": dict(latest_job) if latest_job else None,
        }


def re_full_hex(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdefABCDEF" for character in value)
