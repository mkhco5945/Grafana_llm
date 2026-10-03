# Local demo: quick guide

## Start

Choose the model provider first. To use an external API without Ollama, set
`LLM_PROVIDER=openai` in `.env`, then enter the base URL, API key and model in the
Grafana app's **AI connection** section. Alternatively configure `OPENAI_BASE_URL`,
`OPENAI_API_KEY` and `OPENAI_MODEL` in `.env` for both UI defaults and CLI use.
See [full provider setup](docs/MODEL_PROVIDERS.md). Local mode remains `LLM_PROVIDER=ollama`.

```bash
./start.sh
```

Windows PowerShell without WSL:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

This uses Docker Desktop with Docker VMM. See [docs/WINDOWS.md](docs/WINDOWS.md).

Docker-only startup (when the selected model provider and Python bridge are already ready):

```bash
docker compose up -d
```

## Stop

```bash
./stop.sh
```

This stops containers without deleting volumes and does not stop the host Ollama service.

## Important URLs

- Grafana: <http://localhost:3000> (development login: `admin` / `admin`)
- Prometheus: <http://localhost:9090>
- Raw exporter output: <http://localhost:8000/metrics>
- Current scenario: <http://localhost:8000/scenario>
- Scenario API: `POST http://localhost:8000/scenario`, `POST http://localhost:8000/scenario/preset`, `POST http://localhost:8000/scenario/reset`
- Grafana MCP: <http://127.0.0.1:8002/mcp>

## Ask the AI

```bash
uv run python -m agent.main
uv run python -m agent.main "What metrics are available and what is the current error percentage?"
```

The agent has a permanent system prompt that requires live discovery, PromQL validation, structured tool use, and verification. It exposes Grafana MCP tools plus four narrowly scoped local tools: `get_demo_scenario`, `set_demo_scenario`, `set_demo_scenario_preset`, and `reset_demo_scenario`.

## Existing dashboard

The real Qwen-created dashboard is **Demo Service Metrics** (UID `4e75bd1d-c556-4d43-9165-7f52442baa00`): <http://localhost:3000/d/4e75bd1d-c556-4d43-9165-7f52442baa00/demo-service-metrics>

| Panel | PromQL |
| --- | --- |
| Request Rate | `rate(demo_http_requests_total[5m])` |
| Error Rate | `100 * rate(demo_http_errors_total[5m]) / rate(demo_http_requests_total[5m])` |
| P95 Request Latency | `histogram_quantile(0.95, sum by (le) (rate(demo_http_request_duration_seconds_bucket[5m])))` |
| CPU Usage | `demo_cpu_usage_percent` |
| Memory Usage | `demo_memory_usage_bytes` |

The original request was:

```text
Inspect the Prometheus metrics available in this Grafana instance and build me a useful dashboard for the demo service.

I want to understand traffic, errors, latency, CPU-like usage, and memory-like usage.

Use the actual available metrics rather than assuming their names.

Before creating panels, test the important PromQL queries against Prometheus.

Create a clean dashboard with a sensible title and useful units and legends.

At minimum include:
- request rate
- error rate or error percentage
- p95 request latency
- CPU-like usage
- memory-like usage

Choose appropriate Grafana visualization types yourself.

After creating it, verify the dashboard exists and verify its important panel queries return data.
```

## What the data is

[`demo/app.py`](demo/app.py) generates simulated requests, errors, a latency histogram, CPU-like usage, memory-like usage, and uptime. Prometheus scrapes `demo:8000/metrics`; inspect raw current output at <http://localhost:8000/metrics> and stored/queryable time series at <http://localhost:9090>.

Existing metrics:

- `demo_http_requests_total` (counter)
- `demo_http_errors_total` (counter)
- `demo_http_request_duration_seconds` (`_bucket`, `_sum`, `_count`; histogram)
- `demo_cpu_usage_percent` (gauge)
- `demo_memory_usage_bytes` (gauge)
- `demo_uptime_seconds` (gauge)

Conceptually each sample is metric name + labels + timestamp + numeric value. The exporter source definitions and workload loop are in `demo/app.py`; rebuild after code changes with `docker compose up -d --build demo`.

## Runtime scenarios

The API changes the source workload; it does not edit Prometheus. After a change, the exporter emits new values and Prometheus scrapes them.

```bash
curl -s http://localhost:8000/scenario
curl -s -X POST http://localhost:8000/scenario/preset -H 'Content-Type: application/json' -d '{"name":"high-errors"}'
curl -s -X POST http://localhost:8000/scenario -H 'Content-Type: application/json' -d '{"error_rate":0.25,"latency_min_seconds":0.3,"latency_max_seconds":1.5}'
curl -s -X POST http://localhost:8000/scenario/reset -H 'Content-Type: application/json' -d '{}'
```

Named presets are `normal` (8% errors, normal latency), `high-errors` (30% errors), `high-latency` (0.3–1.5s), `high-cpu` (about 78%), `memory-pressure` (high and growing memory), and `bad-day` (high errors, latency, CPU, and memory). Numeric controls include error rate, latency range, CPU baseline/variation, memory baseline/variation, and memory growth per request.

The AI can discover metrics, labels, types, examples, and current/historical PromQL; create and update Grafana dashboards; and change the simulated workload through the dedicated scenario API. It must not directly modify Prometheus's time-series database, use Pushgateway, or use arbitrary shell/HTTP/filesystem operations. Useful prompts include:

```text
Explain the structure of this data and show a few current example values.
Set the demo to high-errors, wait for Prometheus to observe it, then tell me the current error percentage.
Simulate a bad day with high errors and high latency, analyze the result, and build an incident dashboard.
```
