from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .chat_store import ChatStore
from .config import Settings
from .mcp_client import GrafanaMCPClient
from .ollama_agent import OllamaAgent


STORE: ChatStore | None = None


def _store() -> ChatStore:
    if STORE is None:
        raise RuntimeError("chat persistence is not initialized")
    return STORE


def _is_dashboard_write_request(message: str) -> bool:
    lowered = message.lower()
    if "dashboard" not in lowered:
        return False
    return any(
        token in lowered
        for token in (
            "create",
            "build",
            "make",
            "add",
            "update",
            "modify",
            "edit",
            "repair",
            "change",
            "remove",
            "delete",
        )
    )


def _selected_model(message: str, settings: Settings) -> str:
    if _is_dashboard_write_request(message):
        return os.getenv("AI_DASHBOARD_MODEL", "qwen3:8b").strip() or settings.ollama_model
    return os.getenv("AI_FAST_MODEL", "qwen3:4b").strip() or settings.ollama_model


def _conversation_prompt(message: str, history: list[dict[str, Any]]) -> str:
    lines = [
        "You are being used from the Grafana AI Dashboard Builder app.",
        "Use the real Grafana/Prometheus tools for factual claims and perform requested dashboard writes when appropriate.",
    ]
    trimmed = history[-10:]
    if trimmed:
        lines.append("Recent conversation context:")
        for item in trimmed:
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role in {"user", "assistant"} and content:
                label = "User" if role == "user" else "Assistant"
                lines.append(f"{label}: {content[:4000]}")
    lines.append("Current user request:")
    lines.append(message)
    return "\n".join(lines)


def _extract_dashboard_url(answer: str) -> str:
    absolute = re.search(r"https?://[^\s]+(/d/[A-Za-z0-9_-]+/[^\s)]+)", answer)
    if absolute:
        return absolute.group(1).rstrip(".,")
    relative = re.search(r"(/d/[A-Za-z0-9_-]+/[^\s)]+)", answer)
    if relative:
        return relative.group(1).rstrip(".,")
    return ""


async def _run_agent(job_id: str) -> None:
    store = _store()
    job = store.get_job(job_id)
    message = store.get_job_message(job_id)
    store.set_job_running(job_id)

    try:
        settings = replace(Settings.from_env(), ollama_model=job["model"])
        history = store.get_context(job["session_id"], job["user_message_id"], limit=10)
        prompt = _conversation_prompt(message, history)
        async with GrafanaMCPClient(
            settings.mcp_grafana_url,
            settings.mcp_server_token,
            settings.mcp_timeout_seconds,
        ) as mcp:
            agent = OllamaAgent(settings, mcp, progress=lambda text: store.append_progress(job_id, text))
            await agent.prepare()
            answer = await agent.run(prompt)
        store.complete_job(job_id, answer, _extract_dashboard_url(answer))
    except Exception as exc:
        store.fail_job(job_id, str(exc))


def _launch_job(job_id: str) -> None:
    thread = threading.Thread(
        target=lambda: asyncio.run(_run_agent(job_id)),
        name=f"ai-job-{job_id[:8]}",
        daemon=True,
    )
    thread.start()


def _start_job(session_id: str, message: str) -> dict[str, Any]:
    model = _selected_model(message, Settings.from_env())
    job = _store().create_job(session_id, message, model)
    _launch_job(job["id"])
    return job


class Handler(BaseHTTPRequestHandler):
    server_version = "GrafanaLocalAI/0.2"

    def _json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 512_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/health":
                self._json(200, {"ok": True, "service": "grafana-local-ai", "database": "ok"})
                return
            if path == "/stats":
                self._json(200, _store().stats())
                return
            if path == "/sessions":
                self._json(200, {"sessions": _store().list_sessions()})
                return
            session_match = re.fullmatch(r"/sessions/([A-Fa-f0-9]+)", path)
            if session_match:
                self._json(200, _store().get_session(session_match.group(1)))
                return
            job_match = re.fullmatch(r"/jobs/([A-Fa-f0-9]+)", path)
            if job_match:
                self._json(200, _store().get_job(job_match.group(1)))
                return
            self._json(404, {"error": "not found"})
        except KeyError as exc:
            self._json(404, {"error": str(exc).strip("'")})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            if path == "/sessions":
                title = payload.get("title")
                if title is not None and not isinstance(title, str):
                    raise ValueError("title must be a string")
                self._json(201, _store().create_session(title))
                return
            if path == "/sessions/import":
                source = str(payload.get("source") or "").strip()
                session, already_imported = _store().import_legacy(source, payload)
                self._json(200 if already_imported else 201, {"session": session, "already_imported": already_imported})
                return
            chat_match = re.fullmatch(r"/sessions/([A-Fa-f0-9]+)/chat", path)
            if chat_match:
                message = str(payload.get("message") or "").strip()
                if not message:
                    raise ValueError("message is required")
                if len(message) > 20_000:
                    raise ValueError("message is too long")
                job = _start_job(chat_match.group(1), message)
                self._json(202, {"job_id": job["id"], "status": job["status"], "model": job["model"]})
                return
            retry_match = re.fullmatch(r"/jobs/([A-Fa-f0-9]+)/retry", path)
            if retry_match:
                job = _store().retry_job(retry_match.group(1))
                _launch_job(job["id"])
                self._json(202, {"job_id": job["id"], "status": job["status"], "model": job["model"]})
                return
            self._json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except KeyError as exc:
            self._json(404, {"error": str(exc).strip("'")})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def do_DELETE(self) -> None:
        path = self.path.split("?", 1)[0]
        match = re.fullmatch(r"/sessions/([A-Fa-f0-9]+)", path)
        if not match:
            self._json(404, {"error": "not found"})
            return
        try:
            deleted = _store().delete_smoke_session(match.group(1))
            self._json(200 if deleted else 404, {"deleted": deleted})
        except ValueError as exc:
            self._json(403, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[ai-api] {self.address_string()} - {format % args}", flush=True)


def main() -> None:
    global STORE
    host = os.getenv("AI_API_HOST", "0.0.0.0")
    port = int(os.getenv("AI_API_PORT", "8010"))
    Settings.from_env()
    database_path = Path(os.getenv("AI_CHAT_DB_PATH", ".state/ai-chat.sqlite3"))
    STORE = ChatStore(database_path)
    server = ThreadingHTTPServer((host, port), Handler)
    # Bind first: a mistakenly launched second bridge must not mark jobs owned by
    # the healthy process as interrupted.
    interrupted = STORE.interrupt_stale_jobs()
    print(f"[ai-api] chat database: {database_path.resolve()}", flush=True)
    if interrupted:
        print(f"[ai-api] marked {interrupted} stale job(s) interrupted", flush=True)
    print(f"[ai-api] listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
