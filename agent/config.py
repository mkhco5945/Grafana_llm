from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    ollama_url: str
    ollama_model: str
    mcp_grafana_url: str
    demo_api_url: str
    mcp_server_token: str
    ollama_timeout_seconds: float
    mcp_timeout_seconds: float
    ollama_keep_alive: str
    ollama_context_size: int
    ollama_num_predict: int
    ollama_temperature: float
    ollama_thinking: bool
    max_tool_turns: int
    max_same_tool_failures: int
    max_prewrite_recovery_blocks: int
    max_prewrite_turns: int
    max_tool_result_chars: int
    demo_api_timeout_seconds: float
    demo_scrape_wait_seconds: float

    @classmethod
    def from_env(cls, env_file: str | None = None) -> "Settings":
        load_dotenv(env_file, override=False)
        token = os.getenv("MCP_GRAFANA_SERVER_TOKEN", "").strip()
        if not token:
            raise ValueError(
                "MCP_GRAFANA_SERVER_TOKEN is required; set it in .env or the environment"
            )
        return cls(
            ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
            mcp_grafana_url=os.getenv(
                "MCP_GRAFANA_URL", "http://127.0.0.1:8002/mcp"
            ),
            demo_api_url=os.getenv("DEMO_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            mcp_server_token=token,
            ollama_timeout_seconds=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "900")),
            mcp_timeout_seconds=float(os.getenv("MCP_TIMEOUT_SECONDS", "120")),
            ollama_keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
            ollama_context_size=int(os.getenv("OLLAMA_CONTEXT_SIZE", "12288")),
            ollama_num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "2048")),
            ollama_temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0.2")),
            ollama_thinking=_env_bool("OLLAMA_THINKING", False),
            max_tool_turns=int(os.getenv("AGENT_MAX_TOOL_TURNS", "20")),
            max_same_tool_failures=max(
                1, int(os.getenv("AGENT_MAX_SAME_TOOL_FAILURES", "2"))
            ),
            max_prewrite_recovery_blocks=max(
                1, int(os.getenv("AGENT_MAX_PREWRITE_RECOVERY_BLOCKS", "3"))
            ),
            max_prewrite_turns=max(
                1, int(os.getenv("AGENT_MAX_PREWRITE_TURNS", "8"))
            ),
            max_tool_result_chars=int(
                os.getenv("AGENT_MAX_TOOL_RESULT_CHARS", "60000")
            ),
            demo_api_timeout_seconds=float(os.getenv("DEMO_API_TIMEOUT_SECONDS", "10")),
            demo_scrape_wait_seconds=float(os.getenv("DEMO_SCRAPE_WAIT_SECONDS", "6")),
        )
