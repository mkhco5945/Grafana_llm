# Local Grafana + Prometheus + Grafana MCP playground

This repository contains a local monitoring playground, the official Grafana MCP server, and a small host-side Python agent that lets a local Ollama model operate Grafana through structured MCP tool calls. It does not use Grafana Assistant or a hosted LLM.

```text
User ── Python agent ── Ollama: http://127.0.0.1:11434
                         │ native structured tool calls
                         ▼
             MCP: http://127.0.0.1:8002/mcp
                 │ streamable HTTP + bearer caller token
                 ▼
            mcp-grafana
                 │ http://grafana:3000 (Compose network)
                 ▼
              Grafana ── Prometheus ── demo exporter
```

## Services

| Service | Local endpoint | Purpose |
| --- | --- | --- |
| Grafana | <http://localhost:3000> | Visualization and datasource API |
| Prometheus | <http://localhost:9090> | Metrics storage and PromQL |
| Demo exporter | <http://localhost:8000/metrics> | Continuously changing sample metrics |
| mcp-grafana | <http://127.0.0.1:8002/mcp> | Local MCP access to Grafana |

MCP is bound to loopback only. Host port `8001` was already occupied by another local container, so MCP uses `8002`.

Grafana's local credentials are `admin` / `admin`. The MCP server uses a separate Grafana service-account token and a separate caller bearer token; neither is committed.

## Start and stop

Create local secrets from the API if this is a fresh Grafana data volume:

```bash
curl -u admin:admin -H 'Content-Type: application/json' \
  -X POST http://localhost:3000/api/serviceaccounts \
  -d '{"name":"local-grafana-mcp","role":"Editor"}'

curl -u admin:admin -H 'Content-Type: application/json' \
  -X POST http://localhost:3000/api/serviceaccounts/<ID>/tokens \
  -d '{"name":"local-mcp-token","secondsToLive":0}'
```

Put the returned token and a random caller token in `.env`, using [.env.example](.env.example) as the template. `.env` is ignored by Git.

Start the stack:

```bash
docker compose up -d --build
docker compose ps
```

Stop or restart it without deleting persistent data:

```bash
docker compose stop
docker compose start
docker compose restart
```

`docker compose down` removes containers and the network but preserves the named Grafana and Prometheus volumes. Do not use `docker compose down -v` unless deleting those data volumes is intentional.

## MCP configuration and verification

The Compose service uses the pinned official image `grafana/mcp-grafana:1.6.0`, streamable HTTP transport, and endpoint `/mcp`. Inside the Compose network it connects to `http://grafana:3000`, never `localhost`. Caller authentication is required through `MCP_GRAFANA_SERVER_TOKEN`.

Basic checks:

```bash
docker compose ps
curl http://127.0.0.1:8002/healthz
curl -i http://127.0.0.1:8002/mcp        # must be 401 without the bearer token
```

An authenticated MCP client should initialize against `http://127.0.0.1:8002/mcp`, call `tools/list`, then use `list_datasources`, `list_prometheus_metric_names`, and `query_prometheus`. The verified server exposes 81 tools, including dashboard search/read/update tools.

Relevant tools include:

- `list_datasources`, `get_datasource`, `check_datasources_health`
- `list_prometheus_metric_names`, `list_prometheus_label_names`, `list_prometheus_label_values`
- `query_prometheus`, `query_prometheus_histogram`
- `search_dashboards`, `get_dashboard_by_uid`, `get_dashboard_summary`, `get_dashboard_panel_queries`
- `update_dashboard` (supports dashboard creation and updates)

## Local Python agent

The agent runs on the WSL host, where both Ollama and the loopback-only MCP endpoint are directly available. It intentionally avoids a large agent framework. The MCP Python SDK discovers live tool definitions, the agent converts the selected schemas to Ollama function tools, and only Ollama's native structured `tool_calls` are dispatched. Plain-text or unknown tool requests are never executed.

Install the locked Python environment:

```bash
uv sync
source .venv/bin/activate
python -m agent.main
```

One-shot mode is also available:

```bash
uv run python -m agent.main "Inspect Grafana and summarize the demo service metrics"
```

The required caller token and configurable runtime values live in `.env`. See [.env.example](.env.example). Important defaults are:

| Variable | Default |
| --- | --- |
| `OLLAMA_URL` | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | `qwen3:8b` |
| `MCP_GRAFANA_URL` | `http://127.0.0.1:8002/mcp` |
| `OLLAMA_TIMEOUT_SECONDS` | `900` |
| `OLLAMA_KEEP_ALIVE` | `30m` |
| `OLLAMA_CONTEXT_SIZE` | `12288` |
| `OLLAMA_NUM_PREDICT` | `2048` |
| `OLLAMA_THINKING` | `false` |
| `AGENT_MAX_TOOL_TURNS` | `20` |

The agent discovers all 81 MCP tools but starts by exposing twelve read-only datasource, Prometheus, and dashboard-inspection tools. After it validates a PromQL query, it unlocks the mutating `update_dashboard` tool, completing this explicit 13-tool allowlist:

- `list_datasources`, `get_datasource`, `check_datasources_health`
- `list_prometheus_metric_names`, `list_prometheus_label_names`, `list_prometheus_label_values`
- `query_prometheus`, `query_prometheus_histogram`
- `search_dashboards`, `get_dashboard_by_uid`, `get_dashboard_summary`, `get_dashboard_panel_queries`
- `update_dashboard`

Each model response may request one or more tools. The agent validates every tool name and argument object, sends valid calls to MCP, appends the MCP result as an Ollama tool message, and repeats until Qwen returns a final answer or reaches the configured turn limit. Progress logs omit both bearer tokens.

Run the focused plumbing tests with:

```bash
uv run python -m unittest discover -s tests -v
```

## Model-created dashboard

The real end-to-end run used `qwen3:8b`. The model discovered the datasource and metrics, validated PromQL through MCP, called `update_dashboard`, retrieved the result, and queried the saved expressions again. The Python application provided orchestration and safety checks; it did not contain or directly submit a prewritten dashboard.

- Title: **Demo Service Metrics**
- UID: `4e75bd1d-c556-4d43-9165-7f52442baa00`
- URL: <http://localhost:3000/d/4e75bd1d-c556-4d43-9165-7f52442baa00/demo-service-metrics>

| Panel | Unit | PromQL |
| --- | --- | --- |
| Request Rate | requests/sec | `rate(demo_http_requests_total[5m])` |
| Error Rate | percent | `100 * rate(demo_http_errors_total[5m]) / rate(demo_http_requests_total[5m])` |
| P95 Request Latency | seconds | `histogram_quantile(0.95, sum by (le) (rate(demo_http_request_duration_seconds_bucket[5m])))` |
| CPU Usage | percent | `demo_cpu_usage_percent` |
| Memory Usage | bytes | `demo_memory_usage_bytes` |

All five are `timeseries` panels, reference datasource UID `prometheus`, have descriptive legends, and returned non-empty live results during independent verification.

## Ollama discovery and runtime

The Ollama installation was already present and was not replaced:

- Binary: `/usr/local/bin/ollama`
- Version: `0.31.2`
- Service: systemd `ollama.service`, currently running
- API: <http://127.0.0.1:11434>
- `qwen3:14b`: approximately 9.3 GB, 14.8B parameters, Q4_K_M
- `qwen3:8b`: approximately 5.2 GB, 8.2B parameters, Q4_K_M
- Both report a 40,960-token model context and completion, tools, and thinking capabilities

Useful inspection commands:

```bash
command -v ollama
ollama --version
ollama list
ollama ps
ollama show qwen3:8b
ollama show qwen3:14b
curl http://127.0.0.1:11434/api/tags
```

`qwen3:14b` was the original tool-capable model, and an API test returned a structured `add_numbers(a=2,b=3)` call. Its full MCP schema prompt was too slow on this machine, so the user authorized downloading `qwen3:8b` for this workflow. The 8B model also returned a structured `add_numbers(a=17,b=25)` call before the integration run. Its measured cold request took 72.6 seconds, including 58.4 seconds of model loading; warm turns ranged from a few seconds to about three minutes for large dashboard patch payloads. `OLLAMA_KEEP_ALIVE=30m` kept the model resident throughout each run at roughly 59% GPU / 41% CPU. The existing 14B model remains installed, and no custom quantization was created.

The agent uses:

```text
Ollama: http://127.0.0.1:11434
Model:  qwen3:8b
MCP:    http://127.0.0.1:8002/mcp
```

### Troubleshooting slow starts and pulls

The first request after the model is unloaded can take over a minute; leave the 900-second timeout in place and check `ollama ps` or `journalctl -u ollama` before assuming it is stuck. Large `update_dashboard` calls also generate more slowly than short discovery calls.

If a model pull in WSL resolves `registry.ollama.ai` only to an unreachable IPv6 address, verify IPv4 reachability with `curl -4 -I https://registry.ollama.ai/v2/` and retry the pull. Ollama resumes its partial layer. The successful 8B transfer resumed from the interrupted download rather than starting over.
