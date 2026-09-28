from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Settings
from .mcp_client import GrafanaMCPClient
from .ollama_agent import OllamaAgent


@dataclass
class Job:
    id: str
    message: str
    model: str
    status: str = "queued"
    progress: list[str] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    dashboard_url: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


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


def _job_progress(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        if not job.progress or job.progress[-1] != message:
            job.progress.append(message)
            job.progress = job.progress[-80:]
        job.updated_at = time.time()


async def _run_agent(job_id: str, message: str, history: list[dict[str, Any]]) -> None:
    settings = Settings.from_env()
    model = _selected_model(message, settings)
    settings = replace(settings, ollama_model=model)
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.model = model
        job.status = "running"
        job.updated_at = time.time()

    prompt = _conversation_prompt(message, history)
    try:
        async with GrafanaMCPClient(
            settings.mcp_grafana_url,
            settings.mcp_server_token,
            settings.mcp_timeout_seconds,
        ) as mcp:
            agent = OllamaAgent(
                settings,
                mcp,
                progress=lambda text: _job_progress(job_id, text),
            )
            await agent.prepare()
            answer = await agent.run(prompt)
        with JOBS_LOCK:
            job = JOBS[job_id]
            job.answer = answer
            job.dashboard_url = _extract_dashboard_url(answer)
            job.status = "completed"
            job.updated_at = time.time()
    except Exception as exc:
        with JOBS_LOCK:
            job = JOBS[job_id]
            job.error = str(exc)
            job.status = "failed"
            job.updated_at = time.time()


def _start_job(message: str, history: list[dict[str, Any]]) -> Job:
    settings = Settings.from_env()
    job_id = uuid.uuid4().hex
    job = Job(id=job_id, message=message, model=_selected_model(message, settings))
    with JOBS_LOCK:
        JOBS[job_id] = job
    thread = threading.Thread(
        target=lambda: asyncio.run(_run_agent(job_id, message, history)),
        name=f"ai-job-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return job


def _job_dict(job: Job) -> dict[str, Any]:
    payload = asdict(job)
    payload["progress"] = list(job.progress)
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "GrafanaLocalAI/0.1"

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
        if path == "/health":
            self._json(200, {"ok": True, "service": "grafana-local-ai"})
            return
        match = re.fullmatch(r"/jobs/([A-Fa-f0-9]+)", path)
        if match:
            with JOBS_LOCK:
                job = JOBS.get(match.group(1))
                payload = _job_dict(job) if job else None
            if payload is None:
                self._json(404, {"error": "job not found"})
            else:
                self._json(200, payload)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/chat":
            self._json(404, {"error": "not found"})
            return
        try:
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ValueError("body must be an object")
            message = str(payload.get("message") or "").strip()
            if not message:
                raise ValueError("message is required")
            if len(message) > 20_000:
                raise ValueError("message is too long")
            history = payload.get("history") or []
            if not isinstance(history, list):
                raise ValueError("history must be an array")
            history = [item for item in history if isinstance(item, dict)][-10:]
            job = _start_job(message, history)
            self._json(
                202,
                {
                    "job_id": job.id,
                    "status": job.status,
                    "model": job.model,
                },
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[ai-api] {self.address_string()} - {format % args}", flush=True)


def main() -> None:
    host = os.getenv("AI_API_HOST", "0.0.0.0")
    port = int(os.getenv("AI_API_PORT", "8010"))
    Settings.from_env()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[ai-api] listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
