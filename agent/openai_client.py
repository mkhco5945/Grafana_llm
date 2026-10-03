"""Chat Completions transport; orchestration and tool permissions stay in the agent."""
from __future__ import annotations

import asyncio
import copy
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import Settings


def validate_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("API base URL must be HTTP(S), without credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("External API endpoints require HTTPS; HTTP is allowed only on loopback")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an API key to a redirected endpoint.
        return None


class OpenAIChatClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.request_sync, payload)

    def request_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .ollama_agent import OllamaError, OllamaUnavailable

        base_url = validate_base_url(self.settings.openai_base_url)
        if not self.settings.openai_api_key:
            raise OllamaError("An API key is required for the OpenAI-compatible provider")
        if not self.settings.openai_model:
            raise OllamaError("Choose a tool-capable model or set OPENAI_MODEL")
        messages = []
        for original in payload["messages"]:
            message = {key: copy.deepcopy(original[key]) for key in
                       ("role", "content", "tool_calls", "tool_call_id") if key in original}
            for call in message.get("tool_calls") or []:
                arguments = call["function"].get("arguments", {})
                if not isinstance(arguments, str):
                    call["function"]["arguments"] = json.dumps(arguments)
            messages.append(message)
        body = {"model": self.settings.openai_model, "messages": messages, "stream": False}
        if payload.get("tools"):
            body["tools"] = payload["tools"]
        request = urllib.request.Request(
            base_url + "/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.settings.openai_api_key}, method="POST",
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=self.settings.openai_timeout_seconds
            ) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # Provider bodies may echo credentials or prompts; do not persist them.
            hints = {401: "Check the API key", 403: "Check API permissions", 404: "Check base URL and model",
                     429: "Rate limit or quota exceeded", 400: "Check model support for Chat Completions and tools"}
            raise OllamaError(f"Model API HTTP {exc.code}. {hints.get(exc.code, 'Provider request failed')}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise OllamaUnavailable("Model API connection failed or timed out; check endpoint and network") from None
        except (ValueError, UnicodeError):
            raise OllamaError("Model API returned invalid JSON") from None
        try:
            choice = result["choices"][0]
            incoming = choice["message"]
            message = {"role": "assistant", "content": incoming.get("content") or ""}
            calls = incoming.get("tool_calls") or []
            ids = set()
            if not isinstance(calls, list):
                raise ValueError()
            for call in calls:
                if (call.get("type") != "function" or not isinstance(call.get("id"), str)
                        or not call["id"] or call["id"] in ids
                        or not isinstance(call.get("function"), dict)):
                    raise ValueError()
                ids.add(call["id"])
            if calls:
                message["tool_calls"] = calls
            return {"message": message, "done_reason": choice.get("finish_reason"),
                    "eval_count": (result.get("usage") or {}).get("completion_tokens")}
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise OllamaError("Model API returned a malformed Chat Completions response") from None
