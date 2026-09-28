"""Small local simulated application metrics exporter and scenario API."""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = 8000
LOCK = threading.Lock()
START_TIME = time.time()
REQUESTS = 0
ERRORS = 0
LATENCY_SUM = 0.0
LATENCY_COUNT = 0
LATENCY_BUCKETS = [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, float("inf")]
LATENCY_COUNTS = [0] * len(LATENCY_BUCKETS)


@dataclass(frozen=True)
class ScenarioConfig:
    error_rate: float = 0.08
    latency_min_seconds: float = 0.01
    latency_max_seconds: float = 0.45
    cpu_baseline_percent: float = 20.0
    cpu_variation_percent: float = 8.0
    memory_baseline_bytes: int = 64 * 1024 * 1024
    memory_variation_bytes: int = 4 * 1024 * 1024
    memory_growth_bytes: int = 0


DEFAULT_SCENARIO = ScenarioConfig()
PRESETS: dict[str, ScenarioConfig] = {
    "normal": DEFAULT_SCENARIO,
    "high-errors": ScenarioConfig(error_rate=0.30),
    "high-latency": ScenarioConfig(latency_min_seconds=0.30, latency_max_seconds=1.50),
    "high-cpu": ScenarioConfig(cpu_baseline_percent=78.0, cpu_variation_percent=10.0),
    "memory-pressure": ScenarioConfig(
        memory_baseline_bytes=190 * 1024 * 1024,
        memory_variation_bytes=2 * 1024 * 1024,
        memory_growth_bytes=256 * 1024,
    ),
    "bad-day": ScenarioConfig(
        error_rate=0.30,
        latency_min_seconds=0.30,
        latency_max_seconds=1.50,
        cpu_baseline_percent=82.0,
        cpu_variation_percent=8.0,
        memory_baseline_bytes=210 * 1024 * 1024,
        memory_variation_bytes=3 * 1024 * 1024,
        memory_growth_bytes=128 * 1024,
    ),
}
SCENARIO = DEFAULT_SCENARIO
SCENARIO_NAME = "normal"
CPU_USAGE = DEFAULT_SCENARIO.cpu_baseline_percent
MEMORY_USAGE = float(DEFAULT_SCENARIO.memory_baseline_bytes)


def _scenario_dict() -> dict[str, Any]:
    with LOCK:
        result = asdict(SCENARIO)
        result["preset"] = SCENARIO_NAME
    return result


def _validate_updates(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("scenario body must be a JSON object")
    allowed = set(asdict(DEFAULT_SCENARIO))
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unknown scenario field(s): {', '.join(unknown)}")
    ranges = {
        "error_rate": (0.0, 1.0),
        "latency_min_seconds": (0.001, 60.0),
        "latency_max_seconds": (0.001, 60.0),
        "cpu_baseline_percent": (0.0, 100.0),
        "cpu_variation_percent": (0.0, 100.0),
        "memory_baseline_bytes": (32.0, 1024.0 * 1024 * 1024),
        "memory_variation_bytes": (0.0, 256.0 * 1024 * 1024),
        "memory_growth_bytes": (0.0, 32.0 * 1024 * 1024),
    }
    updates = dict(payload)
    for name, value in updates.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric")
        low, high = ranges[name]
        if not low <= float(value) <= high:
            raise ValueError(f"{name} must be between {low:g} and {high:g}")
        updates[name] = int(value) if name.endswith("_bytes") else float(value)
    with LOCK:
        candidate = asdict(SCENARIO)
    candidate.update(updates)
    if candidate["latency_min_seconds"] > candidate["latency_max_seconds"]:
        raise ValueError("latency_min_seconds must be <= latency_max_seconds")
    return updates


def _set_scenario(config: ScenarioConfig, name: str) -> dict[str, Any]:
    global SCENARIO, SCENARIO_NAME, CPU_USAGE, MEMORY_USAGE
    with LOCK:
        SCENARIO = config
        SCENARIO_NAME = name
        CPU_USAGE = config.cpu_baseline_percent
        MEMORY_USAGE = float(config.memory_baseline_bytes)
    return _scenario_dict()


def update_scenario(payload: dict[str, Any]) -> dict[str, Any]:
    updates = _validate_updates(payload)
    with LOCK:
        candidate = asdict(SCENARIO)
    candidate.update(updates)
    return _set_scenario(ScenarioConfig(**candidate), "custom")


def reset_scenario() -> dict[str, Any]:
    return _set_scenario(DEFAULT_SCENARIO, "normal")


def set_preset(name: str) -> dict[str, Any]:
    if not isinstance(name, str) or name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; choose from {', '.join(PRESETS)}")
    return _set_scenario(PRESETS[name], name)


def observe_request(latency: float, error: bool = False) -> None:
    global REQUESTS, ERRORS, LATENCY_SUM, LATENCY_COUNT
    with LOCK:
        REQUESTS += 1
        ERRORS += int(error)
        LATENCY_SUM += latency
        LATENCY_COUNT += 1
        for index, boundary in enumerate(LATENCY_BUCKETS):
            if latency <= boundary:
                LATENCY_COUNTS[index] += 1


def simulate_workload() -> None:
    global CPU_USAGE, MEMORY_USAGE
    while True:
        with LOCK:
            config = SCENARIO
        latency = random.uniform(config.latency_min_seconds, config.latency_max_seconds)
        error = random.random() < config.error_rate
        time.sleep(latency)
        observe_request(latency, error)
        with LOCK:
            CPU_USAGE = max(
                1.0,
                min(
                    99.0,
                    CPU_USAGE
                    + random.uniform(-config.cpu_variation_percent, config.cpu_variation_percent)
                    + (config.cpu_baseline_percent - CPU_USAGE) * 0.12,
                ),
            )
            MEMORY_USAGE = max(
                32 * 1024 * 1024,
                min(
                    1024 * 1024 * 1024,
                    MEMORY_USAGE
                    + random.uniform(-config.memory_variation_bytes, config.memory_variation_bytes)
                    + config.memory_growth_bytes,
                ),
            )


def metric_line(name: str, value: Any, labels: dict[str, Any] | None = None) -> str:
    if labels:
        label_text = ",".join(f'{key}="{value_}"' for key, value_ in labels.items())
        return f"{name}{{{label_text}}} {value}"
    return f"{name} {value}"


def render_metrics() -> str:
    with LOCK:
        requests, errors = REQUESTS, ERRORS
        latency_sum, latency_count = LATENCY_SUM, LATENCY_COUNT
        latency_counts = list(LATENCY_COUNTS)
        cpu_usage, memory_usage = CPU_USAGE, MEMORY_USAGE
    lines = [
        "# HELP demo_http_requests_total Total number of simulated HTTP requests.",
        "# TYPE demo_http_requests_total counter",
        metric_line("demo_http_requests_total", requests),
        "# HELP demo_http_errors_total Total number of simulated HTTP errors.",
        "# TYPE demo_http_errors_total counter",
        metric_line("demo_http_errors_total", errors),
        "# HELP demo_http_request_duration_seconds HTTP request latency in seconds.",
        "# TYPE demo_http_request_duration_seconds histogram",
    ]
    for boundary, count in zip(LATENCY_BUCKETS, latency_counts):
        bucket = "+Inf" if boundary == float("inf") else boundary
        lines.append(metric_line("demo_http_request_duration_seconds_bucket", count, {"le": bucket}))
    lines.extend(
        [
            metric_line("demo_http_request_duration_seconds_sum", latency_sum),
            metric_line("demo_http_request_duration_seconds_count", latency_count),
            "# HELP demo_cpu_usage_percent Simulated CPU-like usage percentage.",
            "# TYPE demo_cpu_usage_percent gauge",
            metric_line("demo_cpu_usage_percent", round(cpu_usage, 2)),
            "# HELP demo_memory_usage_bytes Simulated memory-like usage in bytes.",
            "# TYPE demo_memory_usage_bytes gauge",
            metric_line("demo_memory_usage_bytes", round(memory_usage)),
            "# HELP demo_uptime_seconds Seconds since the demo application started.",
            "# TYPE demo_uptime_seconds gauge",
            metric_line("demo_uptime_seconds", round(time.time() - START_TIME, 2)),
        ]
    )
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, sort_keys=True).encode(), "application/json")

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("request body must contain valid JSON") from exc

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            self._send(200, render_metrics().encode(), "text/plain; version=0.0.4; charset=utf-8")
        elif path == "/health":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
        elif path == "/scenario":
            self._json(200, _scenario_dict())
        elif path == "/":
            self._send(200, b"demo metrics application\n", "text/plain; charset=utf-8")
        else:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            payload = self._read_json()
            if path == "/scenario":
                result = update_scenario(payload)
            elif path == "/scenario/preset":
                if not isinstance(payload, dict):
                    raise ValueError("preset body must be a JSON object")
                result = set_preset(payload.get("name", payload.get("preset")))
            elif path == "/scenario/reset":
                result = reset_scenario()
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return
            self._json(200, result)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, *_: Any) -> None:
        return


if __name__ == "__main__":
    threading.Thread(target=simulate_workload, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
