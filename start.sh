#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

die() { echo "[start] $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker is not installed or not on PATH."
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v node >/dev/null 2>&1 || die "Node.js >=22 is required to build the local Grafana app plugin."
command -v npm >/dev/null 2>&1 || die "npm is required to build the local Grafana app plugin."
node -e 'const major=Number(process.versions.node.split(".")[0]); if (major < 22) process.exit(1)' \
  || die "Node.js >=22 is required; current version is $(node --version)."

if [[ ! -f .env ]]; then
  die "Missing .env. Copy .env.example to .env and configure the existing Grafana/MCP tokens."
fi
mkdir -p .run .state

# Read only the non-secret routing values needed by this startup script. The AI
# bridge itself loads the complete .env inside its container.
dotenv_value() {
  local key="$1" value
  value="$(awk -v key="$key" '
    $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      sub("^[[:space:]]*" key "[[:space:]]*=[[:space:]]*", ""); print
    }
  ' .env | tail -n 1)"
  value="${value%$'\r'}"
  value="${value#\"}"; value="${value%\"}"
  value="${value#\'}"; value="${value%\'}"
  printf '%s' "$value"
}
LLM_PROVIDER="${LLM_PROVIDER:-$(dotenv_value LLM_PROVIDER)}"
LLM_PROVIDER="${LLM_PROVIDER:-ollama}"
export LLM_PROVIDER

ollama_ready() { curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; }
model_runtime_ready() { [[ "$LLM_PROVIDER" == "openai" ]] || ollama_ready; }
if [[ "$LLM_PROVIDER" == "ollama" ]]; then
command -v ollama >/dev/null 2>&1 || die "Ollama is required only for LLM_PROVIDER=ollama. Use LLM_PROVIDER=openai for external APIs."
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

DASHBOARD_MODEL="${AI_DASHBOARD_MODEL:-$(dotenv_value AI_DASHBOARD_MODEL)}"
DASHBOARD_MODEL="${DASHBOARD_MODEL:-qwen3:8b}"
FAST_MODEL="${AI_FAST_MODEL:-$(dotenv_value AI_FAST_MODEL)}"
FAST_MODEL="${FAST_MODEL:-qwen3:4b}"
model_installed() {
  ollama list | awk 'NR > 1 {print $1}' | grep -Fxq "$1"
}
model_installed "$DASHBOARD_MODEL" || die "Required dashboard model $DASHBOARD_MODEL is missing; install it manually (start.sh never pulls models)."
if ! model_installed "$FAST_MODEL"; then
  echo "[start] Fast model $FAST_MODEL is not installed; read-only AI requests will fall back to $DASHBOARD_MODEL."
  export AI_FAST_MODEL="$DASHBOARD_MODEL"
fi
export AI_DASHBOARD_MODEL="$DASHBOARD_MODEL"
else
  echo "[start] External model API selected; Ollama installation, service, and model checks skipped."
  echo "[start] Configure API connection in Grafana or OPENAI_* values in .env."
fi

if [[ "${AI_PLUGIN_ALREADY_BUILT:-0}" != "1" ]]; then
  echo "[start] Building Grafana AI app plugin locally with Node $(node --version)..."
  pushd grafana-ai-plugin >/dev/null
  if [[ ! -x node_modules/.bin/webpack || ! -x node_modules/.bin/tsc ]]; then
    echo "[start] Installing Grafana plugin npm dependencies using the host network/proxy settings..."
    if [[ -f package-lock.json ]]; then
      npm ci --no-audit --no-fund || die "npm ci for grafana-ai-plugin failed. Your shell HTTP(S)_PROXY settings are used automatically by npm."
    else
      npm install --no-audit --no-fund || die "npm install for grafana-ai-plugin failed. Your shell HTTP(S)_PROXY settings are used automatically by npm."
    fi
  fi
  npm run typecheck || die "Grafana AI plugin typecheck failed."
  npm run build || die "Grafana AI plugin build failed."
  popd >/dev/null
fi
[[ -f grafana-ai-plugin/dist/module.js ]] || die "Grafana AI plugin build did not produce dist/module.js."

echo "[start] Starting Grafana, Prometheus, demo exporter, and MCP..."
if ! docker compose up -d --build; then
  echo "[start] Docker Compose startup failed. Relevant Grafana logs:" >&2
  docker compose logs --no-color --tail=160 grafana >&2 || true
  die "Local stack startup failed."
fi

# Grafana reads plugin.json (including proxy routes) only during startup. The
# bind-mounted bundle can change without Compose recreating the container, so a
# restart is required after every local metadata build.
echo "[start] Reloading Grafana plugin metadata..."
docker compose restart grafana >/dev/null || die "Could not restart Grafana after the plugin build."

container_health() {
  local service="$1" id status
  id="$(docker compose ps -q "$service")"
  [[ -n "$id" ]] || return 1
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || true)"
  [[ "$status" == "healthy" ]]
}

echo "[start] Waiting for healthy Docker services..."
deadline=$((SECONDS + 180))
until container_health grafana && container_health prometheus && container_health demo && container_health mcp-grafana && container_health ai-bridge && model_runtime_ready; do
  (( SECONDS < deadline )) || {
    docker compose ps >&2 || true
    docker compose logs --no-color --tail=120 grafana >&2 || true
    die "Timed out waiting for the local stack."
  }
  sleep 2
done

# Keep the existing jsonData/secureJsonData intact. Re-running this script must
# never erase the provider, model, base URL, or encrypted API key saved in Grafana.
echo "[start] Ensuring the AI app is enabled for Grafana org 1..."
plugin_settings="$(curl -fsS --max-time 10 -u admin:admin \
  http://127.0.0.1:3000/api/plugins/mkhco-ai-dashboard-app/settings)" || plugin_settings=''
if [[ -z "$plugin_settings" ]]; then
  echo "[start] Failed to create/update Grafana app settings. Current plugin settings response:" >&2
  curl -sS --max-time 10 -u admin:admin \
    http://127.0.0.1:3000/api/plugins/mkhco-ai-dashboard-app/settings >&2 || true
  printf '\n' >&2
  die "Could not enable AI Dashboard Builder app."
fi
plugin_enabled="$(printf '%s' "$plugin_settings" | node -e 'let b=""; process.stdin.on("data",c=>b+=c).on("end",()=>console.log(JSON.parse(b).enabled ? "1" : "0"))')"
if [[ "$plugin_enabled" != "1" ]]; then
  enable_payload="$(printf '%s' "$plugin_settings" | node -e 'let b=""; process.stdin.on("data",c=>b+=c).on("end",()=>{const v=JSON.parse(b); console.log(JSON.stringify({enabled:true,pinned:Boolean(v.pinned),jsonData:v.jsonData||{}}))})')"
  curl -fsS --max-time 10 -u admin:admin -H 'Content-Type: application/json' \
    -X POST http://127.0.0.1:3000/api/plugins/mkhco-ai-dashboard-app/settings \
    -d "$enable_payload" >/dev/null || die "Could not enable AI Dashboard Builder app."
fi

echo "[start] AI bridge is running as a restartable Docker service."
curl -fsS --max-time 5 http://127.0.0.1:8010/health >/dev/null \
  || die "Containerized AI bridge is not reachable. Run: docker compose logs ai-bridge"

proxy_ready() {
  curl -fsS --max-time 5 -u admin:admin \
    http://127.0.0.1:3000/api/plugin-proxy/mkhco-ai-dashboard-app/ai/health >/dev/null 2>&1
}
if [[ "${AI_PLUGIN_SKIP_PROXY_CHECK:-0}" != "1" ]]; then
  proxy_deadline=$((SECONDS + 30))
  until proxy_ready; do
    (( SECONDS < proxy_deadline )) || {
      echo "[start] Grafana is up, but the AI app proxy is not reachable yet." >&2
      docker compose logs --no-color --tail=120 grafana >&2 || true
      die "AI Dashboard Builder plugin/proxy verification failed."
    }
    sleep 1
  done
fi

cat <<'SUMMARY'

Local Grafana AI stack started successfully.
Grafana:       http://localhost:3000
Prometheus:    http://localhost:9090
Raw metrics:   http://localhost:8000/metrics
Demo API:      http://localhost:8000/scenario
MCP:           http://127.0.0.1:8002/mcp
AI bridge:     http://127.0.0.1:8010/health
AI bridge log: docker compose logs ai-bridge
Chat database: .state/ai-chat.sqlite3

Grafana UI:
Open Grafana and choose "Create dashboard with AI" from the navigation/app page,
or open the command palette and search for "Create dashboard with AI".

CLI remains available:
uv run python -m agent.main
SUMMARY
