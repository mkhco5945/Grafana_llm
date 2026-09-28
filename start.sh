#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

die() { echo "[start] $*" >&2; exit 1; }

command -v ollama >/dev/null 2>&1 || die "Ollama is not installed or not on PATH."
command -v docker >/dev/null 2>&1 || die "Docker is not installed or not on PATH."
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v uv >/dev/null 2>&1 || die "uv is required for the Python agent."

if [[ ! -f .env ]]; then
  die "Missing .env. Copy .env.example to .env and configure the existing Grafana/MCP tokens."
fi

ollama_ready() { curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; }
if ! ollama_ready; then
  echo "[start] Ollama API is not responding; attempting to start the existing service..."
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files ollama.service >/dev/null 2>&1; then
    systemctl start ollama.service 2>/dev/null || sudo -n systemctl start ollama.service 2>/dev/null || true
  fi
  if ! ollama_ready && command -v systemctl >/dev/null 2>&1; then
    systemctl --user start ollama.service 2>/dev/null || true
  fi
fi
ollama_ready || die "Ollama is installed but its API is unavailable. Start the existing Ollama service and retry."

ollama list | awk 'NR > 1 {print $1}' | grep -Fxq 'qwen3:8b' || die "Required local model qwen3:8b is missing; install it manually (start.sh never pulls models)."

echo "[start] Starting Grafana, Prometheus, demo exporter, and MCP..."
docker compose up -d --build

container_health() {
  local service="$1" id status
  id="$(docker compose ps -q "$service")"
  [[ -n "$id" ]] || return 1
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || true)"
  [[ "$status" == "healthy" ]]
}

echo "[start] Waiting for healthy services..."
deadline=$((SECONDS + 120))
until container_health grafana && container_health prometheus && container_health demo && container_health mcp-grafana && ollama_ready; do
  (( SECONDS < deadline )) || die "Timed out waiting for the local stack. Run: docker compose ps"
  sleep 2
done

if [[ ! -x .venv/bin/python ]]; then
  echo "[start] Creating the uv environment..."
  uv sync
else
  uv run --no-sync python -c 'import httpx2, mcp, dotenv' >/dev/null 2>&1 || uv sync
fi

cat <<'SUMMARY'

Demo started successfully.
Grafana:     http://localhost:3000
Prometheus:  http://localhost:9090
Raw metrics: http://localhost:8000/metrics
Demo API:    http://localhost:8000/scenario
MCP:         http://127.0.0.1:8002/mcp

Ask the AI:
uv run python -m agent.main
SUMMARY
