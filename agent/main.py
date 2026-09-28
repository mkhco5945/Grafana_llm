from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from .config import Settings
from .mcp_client import GrafanaMCPClient, serialize_mcp_result
from .ollama_agent import OllamaAgent, OllamaError


LIVE_DATA_GUIDANCE = """

Additional live-data rules:
- When the user asks for raw data, a sample, a row, a current value, or asks what unit an observed metric uses, answer from this live Prometheus datasource rather than from generic Prometheus/node_exporter knowledge.
- Never substitute a standard metric such as node_cpu_seconds_total unless that exact metric was actually discovered in this datasource.
- If a narrow metric-name search returns no matches, broaden discovery instead of concluding the metric is unavailable. Use a broader concept search or the unfiltered live metric catalog, then choose the exact relevant discovered metric.
- Prefer an exact local/demo metric when it directly represents what the user asked for. A direct gauge is already raw observed data and does not need to be converted into a rate.
- If the user asks to see a raw row/sample/value, you MUST call query_prometheus on the exact discovered metric and report the returned labels/timestamp/value. Do not answer that request from scenario configuration alone and do not merely describe what a standard exporter would normally expose.
- Infer units only from the actual discovered metric and local context. In particular, do not claim CPU is measured in seconds unless a seconds-valued CPU metric was actually discovered and queried.
- If the live datasource cannot support the requested fact, say that explicitly after discovery; do not fill the gap with generic examples presented as local data.
"""


def _requests_live_sample(prompt: str) -> bool:
    lowered = prompt.lower()
    sample_words = (
        "raw",
        "sample",
        "row",
        "current value",
        "current data",
        "show me",
        "give me",
    )
    metric_words = (
        "cpu",
        "memory",
        "latency",
        "request",
        "error",
        "metric",
        "prometheus",
        "data",
    )
    return any(word in lowered for word in sample_words) and any(
        word in lowered for word in metric_words
    )


class GroundedOllamaAgent(OllamaAgent):
    """Runtime hardening for small local models on live-data questions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.messages[0]["content"] += LIVE_DATA_GUIDANCE

    async def _dispatch_tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        fingerprint: str,
    ) -> str:
        result_text = await super()._dispatch_tool_call(name, arguments, fingerprint)

        # The core agent already expands a restricted metric search when it found
        # at least one metric. Small models can guess a foreign prefix such as
        # node_cpu, though, which may return zero names. In that case broaden to
        # the complete catalog automatically so the model sees the real local
        # demo metrics instead of falling back to generic knowledge.
        if name != "list_prometheus_metric_names":
            return result_text

        restricted = (
            arguments.get("regex") is not None
            or arguments.get("page") is not None
            or int(arguments.get("limit", 0) or 0) < 1000
        )
        if (
            restricted
            and not self._discovered_metric_names
            and not self._metric_catalog_refresh_done
            and isinstance(self.mcp, GrafanaMCPClient)
        ):
            self._metric_catalog_refresh_done = True
            full_arguments = {
                "datasourceUid": arguments.get("datasourceUid"),
                "limit": 1000,
            }
            try:
                self.mcp_calls += 1
                full_result = await self.mcp.call_tool(
                    "list_prometheus_metric_names", full_arguments
                )
                if not getattr(full_result, "is_error", False):
                    full_text = serialize_mcp_result(
                        full_result, self.settings.max_tool_result_chars
                    )
                    self._capture_metric_catalog(full_text)
                    self.progress(
                        "[grounding] narrow metric search returned no names; refreshed the complete live metric catalog"
                    )
                    return self._compact_metric_catalog_result(full_text)
            except Exception as exc:
                self.progress(
                    f"[grounding] fallback complete metric refresh failed: {exc}"
                )
        return result_text

    async def run(self, user_prompt: str) -> str:
        answer = await super().run(user_prompt)
        if not _requests_live_sample(user_prompt) or self._validated_promql:
            return answer

        # Do not let a small model answer a request for a concrete live sample
        # without actually querying Prometheus. Give it one bounded correction;
        # if it still refuses to ground the answer, fail rather than hallucinate.
        self.progress(
            "[grounding] live-data answer had no Prometheus sample; forcing one grounded retry"
        )
        correction = (
            "Your previous answer did not satisfy the live-data requirement. "
            "The user asked for a concrete raw/current metric sample. Discover the exact live metric if needed, "
            "then call query_prometheus with an instant query at now and answer using the returned sample. "
            "Do not use generic node_exporter examples or scenario configuration as the sample. "
            f"Original request: {user_prompt}"
        )
        retry_answer = await super().run(correction)
        if not self._validated_promql:
            raise OllamaError(
                "Live-data request could not be grounded in an actual Prometheus query; refusing an unverified answer."
            )
        return retry_answer


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
            agent = GroundedOllamaAgent(settings, mcp)
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
