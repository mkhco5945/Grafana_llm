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

DASHBOARD_MODEL="${AI_DASHBOARD_MODEL:-qwen3:8b}"
FAST_MODEL="${AI_FAST_MODEL:-qwen3:4b}"
model_installed() {
  ollama list | awk 'NR > 1 {print $1}' | grep -Fxq "$1"
}
model_installed "$DASHBOARD_MODEL" || die "Required dashboard model $DASHBOARD_MODEL is missing; install it manually (start.sh never pulls models)."
if ! model_installed "$FAST_MODEL"; then
  echo "[start] Fast model $FAST_MODEL is not installed; read-only AI requests will fall back to $DASHBOARD_MODEL."
  export AI_FAST_MODEL="$DASHBOARD_MODEL"
fi
export AI_DASHBOARD_MODEL="$DASHBOARD_MODEL"

echo "[start] Starting Grafana, Prometheus, demo exporter, MCP, and building the AI app plugin..."
if ! docker compose up -d --build; then
  echo "[start] Docker Compose startup failed. Relevant logs:" >&2
  docker compose logs --no-color --tail=160 ai-plugin-build grafana >&2 || true
  die "Local stack/plugin build failed."
fi

container_health() {
  local service="$1" id status
  id="$(docker compose ps -q "$service")"
  [[ -n "$id" ]] || return 1
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || true)"
  [[ "$status" == "healthy" ]]
}

echo "[start] Waiting for healthy Docker services..."
deadline=$((SECONDS + 180))
until container_health grafana && container_health prometheus && container_health demo && container_health mcp-grafana && ollama_ready; do
  (( SECONDS < deadline )) || {
    docker compose ps >&2 || true
    docker compose logs --no-color --tail=120 ai-plugin-build grafana >&2 || true
    die "Timed out waiting for the local stack."
  }
  sleep 2
done

if [[ ! -x .venv/bin/python ]]; then
  echo "[start] Creating the uv environment..."
  uv sync
else
  uv run --no-sync python -c 'import httpx2, mcp, dotenv' >/dev/null 2>&1 || uv sync
fi

mkdir -p .run
AI_PID_FILE=".run/ai-api.pid"
AI_LOG_FILE=".run/ai-api.log"
ai_api_ready() { curl -fsS --max-time 3 http://127.0.0.1:8010/health >/dev/null 2>&1; }

if ! ai_api_ready; then
  if [[ -f "$AI_PID_FILE" ]]; then
    old_pid="$(cat "$AI_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      kill "$old_pid" 2>/dev/null || true
      sleep 1
    fi
    rm -f "$AI_PID_FILE"
  fi
  echo "[start] Starting host-side AI bridge on port 8010..."
  : > "$AI_LOG_FILE"
  AI_API_HOST=0.0.0.0 AI_API_PORT=8010 \
    AI_FAST_MODEL="${AI_FAST_MODEL:-$FAST_MODEL}" AI_DASHBOARD_MODEL="$DASHBOARD_MODEL" \
    nohup uv run --no-sync python -m agent.api >>"$AI_LOG_FILE" 2>&1 &
  echo $! > "$AI_PID_FILE"
fi

ai_deadline=$((SECONDS + 30))
until ai_api_ready; do
  (( SECONDS < ai_deadline )) || {
    tail -n 80 "$AI_LOG_FILE" >&2 || true
    die "AI bridge did not become healthy. See $AI_LOG_FILE"
  }
  sleep 1
done

proxy_ready() {
  curl -fsS --max-time 5 -u admin:admin \
    http://127.0.0.1:3000/api/plugin-proxy/mkhco-ai-dashboard-app/ai/health >/dev/null 2>&1
}
proxy_deadline=$((SECONDS + 30))
until proxy_ready; do
  (( SECONDS < proxy_deadline )) || {
    echo "[start] Grafana is up, but the AI app proxy is not reachable yet." >&2
    docker compose logs --no-color --tail=120 grafana ai-plugin-build >&2 || true
    die "AI Dashboard Builder plugin/proxy verification failed."
  }
  sleep 1
done

cat <<'SUMMARY'

Local Grafana AI stack started successfully.
Grafana:       http://localhost:3000
Prometheus:    http://localhost:9090
Raw metrics:   http://localhost:8000/metrics
Demo API:      http://localhost:8000/scenario
MCP:           http://127.0.0.1:8002/mcp
AI bridge:     http://127.0.0.1:8010/health
AI bridge log: .run/ai-api.log

Grafana UI:
Open Grafana and choose "Create dashboard with AI" from the navigation/app page,
or open the command palette and search for "Create dashboard with AI".

CLI remains available:
uv run python -m agent.main
SUMMARY
