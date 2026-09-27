# Local Grafana + Prometheus + Grafana MCP playground

This repository contains a local monitoring playground and the official Grafana MCP server. It does not install Grafana Assistant and it does not create an AI dashboard.

```text
local AI agent
  ├── Ollama: http://127.0.0.1:11434
  └── MCP:    http://127.0.0.1:8002/mcp
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

An authenticated MCP client should initialize against `http://127.0.0.1:8002/mcp`, call `tools/list`, then use `list_datasources`, `list_prometheus_metric_names`, and `query_prometheus`. The verified server exposes 81 tools, including dashboard search/read/update tools. Dashboard creation was deliberately not performed in this step.

Relevant tools include:

- `list_datasources`, `get_datasource`, `check_datasources_health`
- `list_prometheus_metric_names`, `list_prometheus_label_names`, `list_prometheus_label_values`
- `query_prometheus`, `query_prometheus_histogram`
- `search_dashboards`, `get_dashboard_by_uid`, `get_dashboard_summary`, `get_dashboard_panel_queries`
- `update_dashboard` (supports dashboard creation and updates; not called here)

## Ollama discovery

Ollama was already installed and was not installed or changed during this step:

- Binary: `/usr/local/bin/ollama`
- Version: `0.31.2`
- Service: systemd `ollama.service`, currently running
- API: <http://127.0.0.1:11434>
- Installed model: `qwen3:14b`, approximately 9.3 GB, 14.8B parameters, Q4_K_M quantization
- Model metadata: 40,960-token model context; capabilities are completion, tools, and thinking

Useful inspection commands:

```bash
command -v ollama
ollama --version
ollama list
ollama ps
ollama show qwen3:14b
curl http://127.0.0.1:11434/api/tags
```

`qwen3:14b` is the best existing candidate for the next local Grafana agent. An actual API test with `think:false`, a 1,024-token context, and 64 maximum output tokens returned a structured `add_numbers(a=2,b=3)` tool call. Cold startup is slow on this machine because the Q4 model is split between CPU and GPU; Ollama reported approximately 57% GPU / 43% CPU and about 196 seconds to start the runner. The model was already Q4_K_M, so no lower-bit model was created or downloaded.

The next agent should use:

```text
Ollama: http://127.0.0.1:11434
Model:  qwen3:14b
MCP:    http://127.0.0.1:8002/mcp
```
