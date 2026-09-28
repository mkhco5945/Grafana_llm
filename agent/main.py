from __future__ import annotations

import argparse
import asyncio
import sys

from .config import Settings
from .mcp_client import GrafanaMCPClient
from .ollama_agent import OllamaAgent, OllamaError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Ollama Grafana MCP agent")
    parser.add_argument("prompt", nargs="*", help="One-shot request; omit for interactive mode")
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    try:
        settings = Settings.from_env()
        async with GrafanaMCPClient(
            settings.mcp_grafana_url,
            settings.mcp_server_token,
            settings.mcp_timeout_seconds,
        ) as mcp:
            agent = OllamaAgent(settings, mcp)
            await agent.prepare()
            if args.prompt:
                answer = await agent.run(" ".join(args.prompt))
                print(f"\n{answer}")
                print(
                    f"\n[agent] completed with {agent.inference_turns} qwen inference "
                    f"turn(s) and {agent.mcp_calls} MCP call(s)"
                )
                return 0
            print("Grafana AI > ", end="", flush=True)
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                prompt = line.strip()
                if not prompt:
                    print("Grafana AI > ", end="", flush=True)
                    continue
                if prompt.lower() in {"/exit", "/quit"}:
                    break
                try:
                    answer = await agent.run(prompt)
                    print(f"\n{answer}")
                    print(
                        f"\n[agent] session totals: {agent.inference_turns} qwen "
                        f"inference turn(s), {agent.mcp_calls} MCP call(s)"
                    )
                except OllamaError as exc:
                    print(f"[agent] error: {exc}", file=sys.stderr)
                print("\nGrafana AI > ", end="", flush=True)
        return 0
    except (OllamaError, ValueError, OSError, RuntimeError) as exc:
        print(f"[agent] startup error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
