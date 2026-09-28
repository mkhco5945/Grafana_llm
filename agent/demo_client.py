from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


LOCAL_DEMO_TOOL_NAMES = frozenset(
    {"get_demo_scenario", "set_demo_scenario", "set_demo_scenario_preset", "reset_demo_scenario"}
)

_SCENARIO_PROPERTIES = {
    "error_rate": {"type": "number", "description": "Error probability from 0 to 1."},
    "latency_min_seconds": {"type": "number", "description": "Minimum simulated request latency."},
    "latency_max_seconds": {"type": "number", "description": "Maximum simulated request latency."},
    "cpu_baseline_percent": {"type": "number", "description": "Target CPU-like usage percentage."},
    "cpu_variation_percent": {"type": "number", "description": "CPU variation around the target."},
    "memory_baseline_bytes": {"type": "integer", "description": "Target simulated memory in bytes."},
    "memory_variation_bytes": {"type": "integer", "description": "Memory variation in bytes."},
    "memory_growth_bytes": {"type": "integer", "description": "Memory growth per simulated request in bytes."},
}

LOCAL_DEMO_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "get_demo_scenario",
            "description": "Read the current simulated demo workload scenario.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_demo_scenario",
            "description": "Set validated numeric parameters of the local simulated demo workload.",
            "parameters": {
                "type": "object",
                "properties": _SCENARIO_PROPERTIES,
                "additionalProperties": False,
                "minProperties": 1,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_demo_scenario_preset",
            "description": "Select a named local demo workload scenario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["normal", "high-errors", "high-latency", "high-cpu", "memory-pressure", "bad-day"],
                    }
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_demo_scenario",
            "description": "Reset the local simulated demo workload to normal defaults.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
)


def local_demo_tools() -> list[dict[str, Any]]:
    """Return Ollama-compatible schemas for the explicit local tool allowlist."""
    return json.loads(json.dumps(LOCAL_DEMO_TOOLS))


def validate_local_arguments(name: str, arguments: dict[str, Any]) -> None:
    if name not in LOCAL_DEMO_TOOL_NAMES:
        raise PermissionError(f"local demo tool is not allowlisted: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("local tool arguments must be an object")
    if name in {"get_demo_scenario", "reset_demo_scenario"} and arguments:
        raise ValueError(f"{name} accepts no arguments")
    if name == "set_demo_scenario_preset" and (
        set(arguments) != {"name"} or arguments.get("name") not in LOCAL_DEMO_TOOLS[2]["function"]["parameters"]["properties"]["name"]["enum"]
    ):
        raise ValueError("set_demo_scenario_preset requires a supported name")
    if name == "set_demo_scenario":
        if not arguments or any(key not in _SCENARIO_PROPERTIES for key in arguments):
            raise ValueError("set_demo_scenario requires supported numeric fields")
        for key, value in arguments.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be numeric")
        ranges = {
            "error_rate": (0, 1),
            "latency_min_seconds": (0.001, 60),
            "latency_max_seconds": (0.001, 60),
            "cpu_baseline_percent": (0, 100),
            "cpu_variation_percent": (0, 100),
            "memory_baseline_bytes": (32, 1024 * 1024 * 1024),
            "memory_variation_bytes": (0, 256 * 1024 * 1024),
            "memory_growth_bytes": (0, 32 * 1024 * 1024),
        }
        for key, value in arguments.items():
            low, high = ranges[key]
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
        if (
            "latency_min_seconds" in arguments
            and "latency_max_seconds" in arguments
            and arguments["latency_min_seconds"] > arguments["latency_max_seconds"]
        ):
            raise ValueError("latency_min_seconds must be <= latency_max_seconds")


class DemoScenarioClient:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_local_arguments(name, arguments)
        if name == "get_demo_scenario":
            method, path, body = "GET", "/scenario", None
        elif name == "set_demo_scenario":
            method, path, body = "POST", "/scenario", arguments
        elif name == "set_demo_scenario_preset":
            method, path, body = "POST", "/scenario/preset", arguments
        else:
            method, path, body = "POST", "/scenario/reset", {}
        # This is one small localhost call; keeping it synchronous also makes
        # failures deterministic for the structured-tool dispatcher.
        return self._request(method, path, body)

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise ValueError(f"demo API HTTP {exc.code}: {detail[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"demo API request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("demo API returned a non-object response")
        return payload
