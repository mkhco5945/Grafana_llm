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
from .openai_client import validate_base_url


STORE: ChatStore | None = None


def _store() -> ChatStore:
    if STORE is None:
        raise RuntimeError("chat persistence is not initialized")
    return STORE


def _is_dashboard_write_request(message: str) -> bool:
    lowered = message.lower()
    if not any(word in lowered for word in ("dashboard", "داشبورد")):
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
            "save",
            "بساز",
            "ساخت",
            "ایجاد",
            "ذخیره",
            "ویرایش",
            "تغییر",
            "اصلاح",
            "آپدیت",
            "به‌روز",
            "بروز",
        )
    )


def _selected_model(message: str, settings: Settings) -> str:
    if settings.llm_provider == "openai":
        return settings.openai_model
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
    # Return a Grafana-relative link even when the answer contains an absolute
    # URL. Markdown delimiters must not become part of the button target.
    relative = re.search(r"(/d/[A-Za-z0-9_-]+/[^\s)`'\"<>\]]+)", answer)
    if relative:
        return relative.group(1).rstrip(".,;:")
    return ""


def _configured_settings(defaults: Any = None) -> Settings:
    settings = Settings.from_env()
    if defaults is None:
        return settings
    if not isinstance(defaults, dict):
        raise ValueError("connection defaults must be an object")
    for key in ("provider", "base_url", "model", "api_key"):
        if key in defaults and not isinstance(defaults[key], str):
            raise ValueError(f"connection default {key} must be a string")
    provider = defaults.get("provider", "").strip() or settings.llm_provider
    if provider not in {"ollama", "openai"}:
        provider = settings.llm_provider
    environment_base_url = settings.openai_base_url.rstrip("/")
    base_url = defaults.get("base_url", "").strip().rstrip("/") or environment_base_url
    model = defaults.get("model", "").strip() or settings.openai_model
    api_key = defaults.get("api_key", "").strip()
    if not api_key and base_url == environment_base_url:
        api_key = settings.openai_api_key
    return replace(settings, llm_provider=provider, openai_base_url=base_url,
                   openai_model=model, openai_api_key=api_key)


def _connection_settings(message: str, options: Any = None, defaults: Any = None) -> Settings:
    settings = _configured_settings(defaults)
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("connection must be an object")
    for key in ("provider", "base_url", "model", "api_key"):
        if key in options and not isinstance(options[key], str):
            raise ValueError(f"connection.{key} must be a string")
    provider = options.get("provider", settings.llm_provider)
    if provider not in {"ollama", "openai"}:
        raise ValueError("provider must be ollama or openai")
    settings = replace(settings, llm_provider=provider)
    model = options.get("model", "").strip() or _selected_model(message, settings)
    if not model or len(model) > 200 or any(ord(c) < 32 for c in model):
        raise ValueError("Choose a valid model name")
    if provider == "ollama":
        return replace(settings, ollama_model=model)
    base = validate_base_url(options.get("base_url") or settings.openai_base_url)
    # A custom destination must never inherit the server's secret for another host/path.
    key = options.get("api_key", "").strip()
    if not key and base == settings.openai_base_url.rstrip("/"):
        key = settings.openai_api_key
    if not key:
        raise ValueError("Enter an API key for this endpoint or configure OPENAI_API_KEY on the server")
    if any(ord(c) < 32 for c in key):
        raise ValueError("API key must not contain control characters")
    return replace(settings, openai_base_url=base, openai_api_key=key, openai_model=model)


def _connection_metadata(settings: Settings) -> dict[str, str]:
    return {"provider": settings.llm_provider, "model": settings.model,
            "base_url": settings.openai_base_url if settings.llm_provider == "openai" else settings.ollama_url}


async def _run_agent(job_id: str, settings: Settings) -> None:
    store = _store()
    job = store.get_job(job_id)
    message = store.get_job_message(job_id)
    store.set_job_running(job_id)

    try:
        history = store.get_context(job["session_id"], job["user_message_id"], limit=10)
        prompt = _conversation_prompt(message, history)
        async with GrafanaMCPClient(
            settings.mcp_grafana_url,
            settings.mcp_server_token,
            settings.mcp_timeout_seconds,
        ) as mcp:
            def redact(text: str) -> str:
                return text.replace(settings.openai_api_key, "[REDACTED]") if settings.openai_api_key else text
            agent = OllamaAgent(settings, mcp, progress=lambda text: store.append_progress(job_id, redact(text)))
            await agent.prepare()
            answer = await agent.run(prompt)
        answer = redact(answer)
        store.complete_job(job_id, answer, _extract_dashboard_url(answer))
    except Exception as exc:
        error = str(exc)
        if settings.openai_api_key:
            error = error.replace(settings.openai_api_key, "[REDACTED]")
        store.fail_job(job_id, error)


def _launch_job(job_id: str, settings: Settings) -> None:
    thread = threading.Thread(
        target=lambda: asyncio.run(_run_agent(job_id, settings)),
        name=f"ai-job-{job_id[:8]}",
        daemon=True,
    )
    thread.start()


def _start_job(session_id: str, message: str, options: Any = None,
               defaults: Any = None) -> dict[str, Any]:
    settings = _connection_settings(message, options, defaults)
    job = _store().create_job(session_id, message, settings.model, _connection_metadata(settings))
    _launch_job(job["id"], settings)
    return job


class Handler(BaseHTTPRequestHandler):
    server_version = "GrafanaLocalAI/0.3"

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

    def _proxy_connection(self) -> dict[str, str]:
        result: dict[str, str] = {}
        names = {
            "provider": "X-Grafana-AI-Provider",
            "base_url": "X-Grafana-AI-Base-URL",
            "model": "X-Grafana-AI-Model",
            "api_key": "X-Grafana-AI-API-Key",
        }
        for key, header in names.items():
            value = (self.headers.get(header) or "").strip()
            if value and value not in {"<no value>", "<nil>"}:
                if len(value) > (16_384 if key == "api_key" else 2_048):
                    raise ValueError(f"configured {key} is too long")
                result[key] = value
        return result

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/health":
                self._json(200, {"ok": True, "service": "grafana-local-ai", "database": "ok"})
                return
            if path == "/connection":
                settings = _configured_settings(self._proxy_connection())
                self._json(200, {**_connection_metadata(settings),
                                 "openai_base_url": settings.openai_base_url,
                                 "openai_model": settings.openai_model,
                                 "has_api_key": bool(settings.openai_api_key)})
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
                job = _start_job(chat_match.group(1), message, payload.get("connection"),
                                 self._proxy_connection())
                self._json(202, {"job_id": job["id"], "status": job["status"], "model": job["model"]})
                return
            retry_match = re.fullmatch(r"/jobs/([A-Fa-f0-9]+)/retry", path)
            if retry_match:
                original = _store().get_job(retry_match.group(1))
                options = payload.get("connection", original["connection"] or {"provider": "ollama", "model": original["model"]})
                settings = _connection_settings(_store().get_job_message(original["id"]), options,
                                                self._proxy_connection())
                job = _store().retry_job(original["id"], settings.model, _connection_metadata(settings))
                _launch_job(job["id"], settings)
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
